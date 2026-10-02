"""WS 연결 상태 알람 — 장 닫힌 시간엔 없음, 장중엔 30초 유예, 복구되면 자동 해제.

운영로그: LS가 매일 06:10 WS 3개를 끊고 수 초 내 재연결되는데, 알람이 해제되지 않아
사용자가 끌 때까지 1분마다 울렸다(09-17 약 2시간, 10-01 1시간).
"""

import threading
from datetime import datetime as real_datetime

import pytest

import trdbot.app as app_mod
from trdbot.app import TradingApp


class FakeScreen:
    def __init__(self):
        self.shown: list = []

    def post(self, fn):
        fn()

    def show_alarm(self, active):
        self.shown.append(list(active))


@pytest.fixture
def env(monkeypatch):
    """시각(HHMM·날짜)과 monotonic을 조종할 수 있는 최소 TradingApp."""
    state = {"hhmm": 1000, "date": real_datetime(2026, 10, 1, 10, 0), "mono": 1000.0}

    class FakeDT(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return state["date"]

    monkeypatch.setattr(app_mod, "_now_hhmm", lambda: state["hhmm"])
    monkeypatch.setattr(app_mod, "datetime", FakeDT)
    monkeypatch.setattr(app_mod.time, "monotonic", lambda: state["mono"])
    beeps: list = []
    a = TradingApp.__new__(TradingApp)
    a.screen = FakeScreen()
    a._alarms, a._alarm_lock = {}, threading.Lock()
    a._conn_down, a._conn_acked, a._holidays = {}, set(), set()
    a._play_alarm = lambda: beeps.append(state["mono"])
    return a, state, beeps


def test_장중_30초안에_복구되면_알람없음(env):
    a, st, beeps = env                 # 10:00 수요일 → 주간장
    a._on_ws_disconnect("체결")
    st["mono"] += 7
    a._check_conn_alarms()
    a._on_ws_reconnect("체결")
    st["mono"] += 60
    a._check_conn_alarms()
    assert a._alarms == {} and beeps == []


def test_장중_30초넘게_끊기면_알람_복구되면_자동해제(env):
    a, st, beeps = env
    a._on_ws_disconnect("시세")
    st["mono"] += 29
    a._check_conn_alarms()
    assert a._alarms == {}
    st["mono"] += 2
    a._check_conn_alarms()
    assert "ws:시세" in a._alarms and len(beeps) == 1
    a._check_conn_alarms()             # 이미 울린 알람은 다시 등록 안 함(2초마다 벨 X)
    assert len(beeps) == 1
    a._on_ws_reconnect("시세")
    assert a._alarms == {}


@pytest.mark.parametrize("hhmm", [610, 1600])   # 야간 마감 후 / 주간 마감 후
def test_장_닫힌_시간엔_알람없음(env, hhmm):
    a, st, beeps = env
    st["hhmm"] = hhmm
    a._on_ws_disconnect("장운영")
    st["mono"] += 3600
    a._check_conn_alarms()
    assert a._alarms == {} and beeps == []


def test_닫힌시간에_끊겨_개장까지_미복구면_개장때_알람(env):
    a, st, beeps = env
    st["hhmm"] = 610                   # 06:10 서버 끊김
    a._on_ws_disconnect("체결")
    st["mono"] += 9000
    a._check_conn_alarms()
    assert a._alarms == {}
    st["hhmm"] = 845                   # 주간 개장
    a._check_conn_alarms()
    assert "ws:체결" in a._alarms


def test_주말_휴일은_장_닫힘(env):
    a, st, beeps = env
    st["date"] = real_datetime(2026, 10, 3, 10, 0)   # 토요일 10:00
    a._on_ws_disconnect("시세")
    st["mono"] += 600
    a._check_conn_alarms()
    assert a._alarms == {}
    # 금요일 밤 세션의 자정 이후(토 01:00)는 장중 — 시작일(금)이 영업일
    st["date"], st["hhmm"] = real_datetime(2026, 10, 3, 1, 0), 100
    a._check_conn_alarms()
    assert "ws:시세" in a._alarms


def test_사용자가_해제하면_복구전까지_다시_안울림(env):
    a, st, beeps = env
    a._on_ws_disconnect("시세")
    st["mono"] += 31
    a._check_conn_alarms()
    a._dismiss_alarms()
    st["mono"] += 120
    a._check_conn_alarms()
    assert a._alarms == {} and len(beeps) == 1

"""잔고 기준 세션 판단(TradingApp._balance_session) — 시계가 아니라 장운영(JIF)으로.

개시 시각이 바뀌는 날(수능일 등) 시계로 18:00에 야간잔고를 부르면 야간장이 아직 안 열린 상태다.
JIF를 받았으면 실제로 열린 장(단일가 호가접수 포함) 기준으로만 바꾼다.
"""

from types import SimpleNamespace

import trdbot.app as app_mod
from lsapi.market_state import JANGUBUN_DAY_FUTURES as DAY
from lsapi.market_state import JANGUBUN_NIGHT_FUTURES as NIGHT
from lsapi.market_state import MarketStateTracker
from trdbot.app import TradingApp


def _app(events=None):
    if events is None:
        return SimpleNamespace(rt=None)
    t = MarketStateTracker()
    for jangubun, jstatus in events:
        t.on_jif(jangubun, jstatus)
    return SimpleNamespace(rt=SimpleNamespace(seen=t.seen, phase=t.phase))


def _sess(app, hhmm, monkeypatch):
    monkeypatch.setattr(app_mod, "_now_hhmm", lambda: hhmm)
    return TradingApp._balance_session(app)


def test_야간개시_늦어진날_18시엔_야간잔고_안부름(monkeypatch):
    # 주간 마감까지만 JIF 수신, 야간 아직 안 열림 → 시계는 18:30=야간이지만 'closed'(전환 안 함)
    app = _app([(DAY, "21"), (DAY, "31"), (DAY, "41")])
    assert _sess(app, 1830, monkeypatch) == "closed"


def test_야간_시가단일가_시작되면_night(monkeypatch):
    app = _app([(DAY, "41"), (NIGHT, "11")])
    assert _sess(app, 1850, monkeypatch) == "night"


def test_야간_접속매매면_night(monkeypatch):
    app = _app([(DAY, "41"), (NIGHT, "21")])
    assert _sess(app, 1700, monkeypatch) == "night"   # 시계와 무관


def test_야간마감후_주간_열리면_day(monkeypatch):
    app = _app([(NIGHT, "21"), (NIGHT, "41"), (DAY, "11")])
    assert _sess(app, 830, monkeypatch) == "day"


def test_주간_늦게끝나는날_15시45분_넘어도_day(monkeypatch):
    app = _app([(DAY, "21")])
    assert _sess(app, 1630, monkeypatch) == "day"


def test_JIF_못받았으면_시계_폴백(monkeypatch):
    assert _sess(_app([]), 1830, monkeypatch) == "night"
    assert _sess(_app(), 1000, monkeypatch) == "day"      # 실시간 피드 없음(시동 직후)

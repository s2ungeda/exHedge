"""특별일 거래시간(수능일 등) — 시각 폴백(session_now·current_time_phase)이 시간표를 따르는지."""

from datetime import date

import pytest

from lsapi.futures import session_now
from lsapi.market_state import Phase, current_time_phase
from lsapi.session_hours import Hours, parse_special_hours, set_special_hours

CSAT = date(2026, 11, 19)      # 수능일(목)
NEXT = date(2026, 11, 20)
NORMAL = date(2026, 11, 18)


@pytest.fixture(autouse=True)
def _csat_table():
    set_special_hours({CSAT: Hours(945, 1545, 1900, 600)})
    yield
    set_special_hours({})


def test_파일_형식_파싱():
    t = parse_special_hours(
        "# 주석\n"
        "2026-11-19  09:45-15:45  19:00-06:00   # 수능일\n"
        "\n"
        "2026-12-01  0900-1545 1800-0600\n"      # 콜론 없어도 됨
    )
    assert t[date(2026, 11, 19)] == Hours(945, 1545, 1900, 600)
    assert t[date(2026, 12, 1)] == Hours(900, 1545, 1800, 600)


@pytest.mark.parametrize("bad", [
    "2026-11-19  09:45-15:45",              # 야간 없음
    "11/19  09:45-15:45  19:00-06:00",      # 날짜 형식
    "2026-11-19  25:00-15:45  19:00-06:00", # 시각 범위
    "2026-11-19  16:00-15:45  19:00-06:00", # 개장 > 마감
])
def test_잘못된_줄은_무시(bad):
    assert parse_special_hours(bad) == {}


def test_수능일_주간은_09시45분부터():
    assert session_now(900, CSAT) == "closed"
    assert session_now(945, CSAT) == "day"
    assert session_now(1545, CSAT) == "day"
    assert session_now(1600, CSAT) == "closed"


def test_수능일_야간은_19시부터():
    assert session_now(1830, CSAT) == "closed"   # 평소면 야간 — 이날은 아직 안 열림
    assert session_now(1900, CSAT) == "night"


def test_다음날_새벽은_전날_야간장_기준():
    assert session_now(300, NEXT) == "night"
    assert session_now(600, NEXT) == "closed"


def test_평소날은_기존_시간():
    assert session_now(845, NORMAL) == "day"
    assert session_now(1800, NORMAL) == "night"
    assert session_now(559, NORMAL) == "night"


def test_수능일_시각폴백_국면():
    assert current_time_phase(944, "day", CSAT) == Phase.AUCTION       # 아직 개장 전
    assert current_time_phase(945, "day", CSAT) == Phase.CONTINUOUS
    assert current_time_phase(1535, "day", CSAT) == Phase.AUCTION      # 마감 동시호가(마감 10분 전)
    assert current_time_phase(550, "night", NEXT) == Phase.AUCTION     # 야간 종가단일가
    assert current_time_phase(549, "night", NEXT) == Phase.CONTINUOUS

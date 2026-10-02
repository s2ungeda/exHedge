"""미국달러선물 만기·롤오버일 계산 테스트."""

from datetime import date

from trdbot.expiry import (
    expiry_day,
    expiry_prev_day,
    is_trading_day,
    load_holidays,
    rollover_window,
    third_monday,
)


def test_셋째월요일():
    assert third_monday(2026, 1) == date(2026, 1, 19)
    assert third_monday(2026, 2) == date(2026, 2, 16)


def test_만기_직전_휴일없음():
    assert expiry_day(2026, 1, set()) == date(2026, 1, 19)       # 셋째 월요일
    assert expiry_prev_day(2026, 1, set()) == date(2026, 1, 16)  # 직전 영업일(금)


def test_만기일이_휴일이면_직전영업일로():
    hol = {date(2026, 1, 19)}                          # 셋째 월요일이 휴일
    assert expiry_day(2026, 1, hol) == date(2026, 1, 16)        # 금요일로 당김
    assert expiry_prev_day(2026, 1, hol) == date(2026, 1, 15)   # 그 전 영업일(목)


def test_롤오버구간_전주_화요일부터_금요일():
    start, end = rollover_window(2026, 1)
    assert start == date(2026, 1, 13)   # 만기 전주 화요일
    assert end == date(2026, 1, 16)     # 만기 전주 금요일
    # 구간 내 영업일만 알림 대상(주말·휴일 제외)
    assert is_trading_day(date(2026, 1, 14), set()) is True          # 수
    assert is_trading_day(date(2026, 1, 15), {date(2026, 1, 15)}) is False  # 휴일


def test_휴일파일_로딩(tmp_path):
    p = tmp_path / "h.txt"
    p.write_text("# 주석\n2026-05-05  # 어린이날\n20260601\n\n", encoding="utf-8")
    hs = load_holidays(p)
    assert hs == {date(2026, 5, 5), date(2026, 6, 1)}


def test_휴일파일_없으면_빈집합(tmp_path):
    assert load_holidays(tmp_path / "없음.txt") == set()

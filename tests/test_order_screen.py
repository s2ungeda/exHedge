"""실행 중 변경 항목 검증(order_screen.parse_live_params) 테스트 — GUI 없이 순수 검증."""

import pytest

from trdbot.order_screen import LIVE_KEYS, parse_live_params


def _raw(**kw):
    base = {
        "account_dollar_ratio": "10", "hedge_ratio": "10", "manual_balance_adjust": "0",
        "qty_per_order": "3", "order_interval_sec": "10", "max_position": "100",
    }
    base.update(kw)
    return base


def test_정상_파싱():
    p = parse_live_params(_raw())
    assert p["qty_per_order"] == 3
    assert p["max_position"] == 100
    assert p["hedge_ratio"] == 10.0
    assert p["order_interval_sec"] == 10.0
    assert "account_dollar_ratio" not in p   # A는 실행 중 변경 대상에서 제외


def test_수동조정_빈값은_0():
    assert parse_live_params(_raw(manual_balance_adjust=""))["manual_balance_adjust"] == 0.0


def test_수동조정_음수_허용():
    p = parse_live_params(_raw(manual_balance_adjust="-50000000"))
    assert p["manual_balance_adjust"] == -50000000.0


def test_천단위콤마_허용():
    p = parse_live_params(_raw(manual_balance_adjust="1,000,000,000"))
    assert p["manual_balance_adjust"] == 1_000_000_000.0


def test_qty가_한도초과면_오류():
    with pytest.raises(ValueError):
        parse_live_params(_raw(qty_per_order="200", max_position="100"))


def test_인터벌_0이면_오류():
    with pytest.raises(ValueError):
        parse_live_params(_raw(order_interval_sec="0"))


def test_qty_0이면_오류():
    with pytest.raises(ValueError):
        parse_live_params(_raw(qty_per_order="0"))


def test_LIVE_KEYS_7개():
    assert len(LIVE_KEYS) == 7
    assert "manual_balance_adjust" in LIVE_KEYS
    assert "fill_limit" in LIVE_KEYS and "fill_window_min" in LIVE_KEYS
    assert "code" not in LIVE_KEYS                    # 종목코드는 실행 중 잠금
    assert "account_dollar_ratio" not in LIVE_KEYS    # 계좌달러비율도 실행 중 잠금(Ready 후 고정)


def test_체결량한도_기본_off_빈값():
    p = parse_live_params(_raw())                     # fill_* 미지정
    assert p["fill_limit"] == 0                       # 0=기능 끔
    assert p["fill_window_min"] == 10.0               # 기본 10분


def test_체결량한도_파싱():
    p = parse_live_params(_raw(fill_window_min="5", fill_limit="20"))
    assert p["fill_window_min"] == 5.0
    assert p["fill_limit"] == 20

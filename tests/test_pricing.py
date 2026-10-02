"""주문가격 계산(pricing.py) 테스트.

호가공백(gap) → offset(틱) → maker 주문가격 규칙을 검증한다.
규칙(pricing.py 헤더):
    공백  0  1  2  3  4  5  ≥6
    offset 1  1  2  2  3  3  None
"""

import pytest

from trdbot.pricing import (
    gap_from,
    infer_tick_from_levels,
    levels_from_orderbook,
    offset_for_gap,
    order_price,
    quote_from_orderbook,
)

TICK = 0.1  # 미국달러선물 호가단위


# ── gap_from: 호가공백 = (매도1−매수1)/틱 − 1 ──────────
def test_gap_스프레드_1틱이면_공백0():
    assert gap_from(1380.0, 1380.1, TICK) == 0


def test_gap_스프레드_5틱이면_공백4():
    assert gap_from(1380.0, 1380.5, TICK) == 4


def test_gap_역전호가_음수():
    # 매수1 > 매도1 (잠김/역전) → 음수 → 이후 주문 안 함 처리
    assert gap_from(1380.2, 1380.0, TICK) < 0


def test_gap_틱0이면_ValueError():
    with pytest.raises(ValueError):
        gap_from(1380.0, 1380.1, 0)


# ── offset_for_gap: 공백 → 더할 틱 수 ──────────────────
@pytest.mark.parametrize(
    "gap,expected",
    [(0, 1), (1, 1), (2, 2), (3, 2), (4, 3), (5, 3)],
)
def test_offset_표대로(gap, expected):
    assert offset_for_gap(gap) == expected


@pytest.mark.parametrize("gap", [6, 7, 20])
def test_offset_공백6이상이면_None(gap):
    assert offset_for_gap(gap) is None


def test_offset_음수공백이면_None():
    assert offset_for_gap(-1) is None


# ── order_price: maker 주문가 ──────────────────────────
def test_order_price_매수_자기호가에서_위로():
    # 공백0 → offset 1틱 → 매수1(1380.0) + 0.1 = 1380.1
    assert order_price("buy", 1380.0, 1380.1, TICK) == pytest.approx(1380.1)


def test_order_price_매도_자기호가에서_아래로():
    # 공백0 → offset 1틱 → 매도1(1380.1) − 0.1 = 1380.0
    assert order_price("sell", 1380.0, 1380.1, TICK) == pytest.approx(1380.0)


def test_order_price_공백4_매수():
    # 공백4 → offset 3틱 → 1380.0 + 0.3 = 1380.3
    assert order_price("buy", 1380.0, 1380.5, TICK) == pytest.approx(1380.3)


def test_order_price_공백5_경계_주문함():
    # 공백5(스프레드 6틱)까지는 주문 → offset 3 → 1380.0 + 0.3
    assert order_price("buy", 1380.0, 1380.6, TICK) == pytest.approx(1380.3)


def test_order_price_공백6_주문안함_None():
    # 공백6(스프레드 7틱) → 슬리피지 가드 → None
    assert order_price("buy", 1380.0, 1380.7, TICK) is None


def test_order_price_역전호가_None():
    assert order_price("buy", 1380.2, 1380.0, TICK) is None


def test_order_price_잘못된_side():
    with pytest.raises(ValueError):
        order_price("hold", 1380.0, 1380.1, TICK)  # type: ignore[arg-type]


# ── infer_tick_from_levels: 인접 최소간격 = 틱 ─────────
def test_infer_tick_최소간격():
    assert infer_tick_from_levels([1380.0, 1380.05, 1380.15]) == pytest.approx(0.05)


def test_infer_tick_레벨부족시_폴백():
    assert infer_tick_from_levels([1380.0], fallback=0.1) == pytest.approx(0.1)


def test_infer_tick_레벨부족_폴백없으면_ValueError():
    with pytest.raises(ValueError):
        infer_tick_from_levels([])


# ── levels_from_orderbook: dict 파싱 ───────────────────
def _orderbook() -> dict:
    # offerhoN(매도)/bidhoN(매수) — LS OutBlock 형태(문자열 값)
    return {
        "offerho1": "1380.5", "offerho2": "1380.6", "offerho3": "1380.7",
        "bidho1": "1380.0", "bidho2": "1379.9", "bidho3": "1379.8",
        "기타필드": "무시됨", "volume": "0",
    }


def test_levels_정렬_및_무관키_무시():
    offers, bids = levels_from_orderbook(_orderbook())
    assert offers == [1380.5, 1380.6, 1380.7]        # 매도 오름차순
    assert bids == [1380.0, 1379.9, 1379.8]          # 매수 내림차순
    assert offers[0] == 1380.5                        # best_ask
    assert bids[0] == 1380.0                          # best_bid


def test_levels_0이하값_제외():
    ob = {"offerho1": "0", "offerho2": "1380.5", "bidho1": "1380.0"}
    offers, bids = levels_from_orderbook(ob)
    assert offers == [1380.5]                          # "0"은 빠짐


# ── quote_from_orderbook: 종합 ─────────────────────────
def test_quote_매수_가격산출():
    q = quote_from_orderbook(_orderbook(), "buy", fallback_tick=TICK)
    assert q is not None
    assert q.best_ask == 1380.5
    assert q.best_bid == 1380.0
    assert q.tick == pytest.approx(0.1)
    assert q.gap == 4                                  # (1380.5−1380.0)/0.1 − 1
    assert q.price == pytest.approx(1380.3)            # 1380.0 + 3틱
    assert q.should_order is True


def test_quote_빈호가면_None():
    assert quote_from_orderbook({}, "buy", fallback_tick=TICK) is None


def test_quote_한쪽만_있으면_None():
    ob = {"offerho1": "1380.5"}  # 매수호가 없음
    assert quote_from_orderbook(ob, "buy", fallback_tick=TICK) is None

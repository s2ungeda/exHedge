"""주문가격 계산 (호가공백 기반 maker 가격 규칙).

설계: docs/execution-engine.md 4장.

규칙:
- 자기호가 = 내 방향 1호가 (매수→매수1호가, 매도→매도1호가)
- 호가공백 = (매도1호가 − 매수1호가)/틱 − 1   (매수1·매도1 사이 빈 호가 칸 수)
- offset(틱) = floor(공백/2) + 1   (공백 0~5),   공백 ≥6 → 주문 안 함
    공백  0  1  2  3  4  5  ≥6
    offset 1  1  2  2  3  3  None
- 매수 가격 = 매수1호가 + offset×틱 (↑),  매도 가격 = 매도1호가 − offset×틱 (↓)
- 공백 6 이상(스프레드 과다)은 주문 안 함 → 슬리피지 가드 역할

틱은 호가 인접 레벨 최소 간격으로 역산(주식 차등/ETF 예외 자동). 호가가 성기면 fallback 사용.
핵심 계산은 명시값(best_bid/ask/tick)을 받는 순수 함수 — GUI/네트워크 비의존, 테스트 용이.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

Side = Literal["buy", "sell"]

# 호가공백 6 이상이면 주문하지 않는다.
MAX_GAP = 5


# ──────────────────────────────────────────────────────────
# 코어 (순수, 명시값 입력)
# ──────────────────────────────────────────────────────────
def gap_from(best_bid: float, best_ask: float, tick: float) -> int:
    """호가공백 = (매도1 − 매수1)/틱 − 1. 빈 호가 칸 수.

    정상: 스프레드 ≥ 1틱 → 공백 ≥ 0. 잠김/역전(스프레드 ≤0)이면 음수 반환(→ 주문 안 함 처리).
    """
    if tick <= 0:
        raise ValueError("tick은 0보다 커야 합니다.")
    spread_ticks = int(
        ((Decimal(str(best_ask)) - Decimal(str(best_bid))) / Decimal(str(tick)))
        .to_integral_value(rounding=ROUND_HALF_UP)
    )
    return spread_ticks - 1


def offset_for_gap(gap: int) -> int | None:
    """호가공백 → 자기호가에서 더할 틱 수. 공백<0(비정상) 또는 >5 → None(주문 안 함)."""
    if gap < 0 or gap > MAX_GAP:
        return None
    return gap // 2 + 1


def order_price(side: Side, best_bid: float, best_ask: float, tick: float) -> float | None:
    """maker 주문가격. 주문 안 하는 경우(공백 ≥6, 비정상 호가) None.

    매수 = 매수1 + offset×틱, 매도 = 매도1 − offset×틱.
    """
    gap = gap_from(best_bid, best_ask, tick)
    offset = offset_for_gap(gap)
    if offset is None:
        return None
    t = Decimal(str(tick))
    if side == "buy":
        price = Decimal(str(best_bid)) + offset * t
    elif side == "sell":
        price = Decimal(str(best_ask)) - offset * t
    else:
        raise ValueError(f"side는 'buy'/'sell' (got {side!r})")
    return float(price)


def infer_tick_from_levels(levels: list[float], fallback: float | None = None) -> float:
    """호가 레벨들의 인접 최소 간격 = 틱. 레벨 부족 시 fallback(없으면 ValueError)."""
    pts = sorted({Decimal(str(x)) for x in levels if x and float(x) > 0})
    diffs = [pts[i + 1] - pts[i] for i in range(len(pts) - 1)]
    diffs = [d for d in diffs if d > 0]
    if not diffs:
        if fallback is not None:
            return float(fallback)
        raise ValueError("틱 역산 불가: 유효 호가 레벨이 부족합니다.")
    return float(min(diffs))


# ──────────────────────────────────────────────────────────
# 호가 dict 파싱 (보조) + 종합
# ──────────────────────────────────────────────────────────
_OFFER_RE = re.compile(r"offerho\d+$")
_BID_RE = re.compile(r"bidho\d+$")


def levels_from_orderbook(ob: dict) -> tuple[list[float], list[float]]:
    """호가 OutBlock(dict)에서 (매도호가들, 매수호가들) 추출. offerhoN/bidhoN 키 스캔.

    TR마다 키명이 다를 수 있다(주식 t1101 검증됨). 다르면 호출측에서 직접 best/levels 전달 권장.
    """
    offers = [float(v) for k, v in ob.items() if _OFFER_RE.match(k) and _is_num(v) and float(v) > 0]
    bids = [float(v) for k, v in ob.items() if _BID_RE.match(k) and _is_num(v) and float(v) > 0]
    return sorted(offers), sorted(bids, reverse=True)


@dataclass
class PriceQuote:
    side: Side
    best_bid: float
    best_ask: float
    tick: float
    gap: int
    offset: int | None
    price: float | None  # None이면 주문 안 함

    @property
    def should_order(self) -> bool:
        return self.price is not None


def quote_from_orderbook(
    ob: dict, side: Side, *, fallback_tick: float | None = None
) -> PriceQuote | None:
    """호가 dict → PriceQuote. best/levels 추출 불가면 None.

    tick은 전체 레벨로 역산(fallback_tick 보조). gap/offset/price 계산.
    """
    offers, bids = levels_from_orderbook(ob)
    if not offers or not bids:
        return None
    best_ask, best_bid = offers[0], bids[0]
    tick = infer_tick_from_levels(offers + bids, fallback=fallback_tick)
    # 진짜 틱은 가장 좁은 간격. 호가가 성겨 역산이 과대평가되면(예: 안쪽 스프레드를 틱으로 오판)
    # 설정 폴백이 더 정확하므로 둘 중 작은 값. (선물=flat tick 폴백 신뢰 / 주식=폴백 None이라 역산만)
    if fallback_tick is not None:
        tick = min(tick, float(fallback_tick))
    gap = gap_from(best_bid, best_ask, tick)
    offset = offset_for_gap(gap)
    price = order_price(side, best_bid, best_ask, tick)
    return PriceQuote(side, best_bid, best_ask, tick, gap, offset, price)


def _is_num(v: object) -> bool:
    try:
        float(v)  # type: ignoreㅋ[arg-type]
        return True
    except (TypeError, ValueError):
        return False

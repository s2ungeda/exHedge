"""목표계약수 산출 (원달러선물 헤지).

외부 시그널이 환율·코인포지션합을 실어 보내고, 화면이 보유 원달러(계약수)·헤지비율(B%)을
제공한다. 이 둘로 목표 계약수를 계산한다.

공식 (사용자 정의):
    달러노출선물계약수 (C) = 보유 원달러   ← 화면 입력 계약수(±) 그대로 (환율·1만달러 환산 없음)
    포지션헤지비율    (D) = -floor( 코인포지션합 × 헤지비율 / 환율 / 10000 )   ← 코인 롱→숏, 숏→롱
      └ 토큰이 Meme이면 D = D × −1. 이유: 같은 차익거래(국내코인매수+해외코인매도)를
        Dalin은 국내코인'매수'(원화) 다리로, Meme(kp-arb)은 해외코인'매도'(달러) 다리로 받는다.
        반대 다리라 부호가 뒤집혀 있어 Meme만 D 부호를 되돌린다. (단위 원↔달러는 환율이 별도 처리)
    목표계약수 = C + D
    └ 보유 원달러(usd_futures_held, ±정수): 유지할 원달러선물 계약수. 화면 입력(기본 0).
      (2026-10 변경: 예전엔 '계좌달러노출액' 금액을 받아 trunc(금액/환율/10000)으로 환산했다.)
    └ 코인포지션합(total_coin): 시그널이 주는 코인 포지션 합.
    └ meme는 원환산을 달러로 입력 → 환율 1로 사이징. 코인 매수=원달러 매도헤지, 매도=매수헤지.
    └ D가 음수라 |D|>C면 목표가 음수 → 숏. 방향은 (목표−포지션) 부호로 결정.

- 10000 = 원달러선물 1계약 = US$10,000 (KRX 미국달러선물 계약크기). D 환산에 쓴다:
  (KRW금액) → /환율 = USD환산 → /10000 = 계약수.
- ⚠️ 헤지비율 단위: 화면 입력이 '%'(예: 50)이므로 내부에서 /100 하여 소수비율(0.5)로 쓴다.
"""

import math

# 원달러선물(미국달러선물) 1계약 = US$10,000
USD_FUTURES_CONTRACT_USD = 10000


def _contracts(balance_krw: float, ratio_pct: float, fx: float) -> int:
    if fx <= 0:
        raise ValueError("환율(fx)은 0보다 커야 합니다.")
    usd_notional = balance_krw * (ratio_pct / 100.0) / fx
    return math.floor(usd_notional / USD_FUTURES_CONTRACT_USD)


def hedge_ratio_contracts(total_coin_krw: float, hedge_ratio_pct: float, fx: float) -> int:
    """(D) 포지션헤지비율 = −floor(코인포지션합 × B/100 / 환율 / 10000).

    부호 있는 값에 floor 후 음수화 → 코인 롱(+)이면 원달러 숏(−), 코인 숏(−)이면 원달러 롱(+)으로
    방향이 자동 반전된다. (절대값 안 씀 — 음수 코인쪽 크기가 1 클 수 있음)
    """
    return -_contracts(total_coin_krw, hedge_ratio_pct, fx)


def target_contracts(
    usd_futures_held: int,
    total_coin_krw: float,
    hedge_ratio_pct: float,
    fx: float,
    flip_hedge: bool = False,
) -> tuple[int, int, int]:
    """(C, D, 목표계약수=C+D) 반환. C = 보유 원달러(화면 입력 계약수) 그대로.

    flip_hedge=True(예: Meme 토큰)면 D 부호를 반대로 뒤집는다.
    """
    c = int(usd_futures_held)
    d = hedge_ratio_contracts(total_coin_krw, hedge_ratio_pct, fx)
    if flip_hedge:
        d = -d
    return c, d, c + d

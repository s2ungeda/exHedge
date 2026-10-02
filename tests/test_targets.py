"""목표계약수 산출(targets.py) 테스트 — 순수 함수, IO 없이 입력→출력만 검증.

산식:
    C(달러노출선물계약수) = 보유 원달러(화면 입력 계약수, ±) 그대로
    D(포지션헤지비율)     = −floor(코인포지션합 × B/100 / 환율 / 10000)
    목표 = C + D
"""

import pytest

from trdbot.targets import hedge_ratio_contracts, target_contracts


# ── C: 보유 원달러(계약수)를 그대로 사용 — 환율·1만달러 환산 없음 ──
@pytest.mark.parametrize("held", [0, 3, 25, -7])
def test_C는_보유원달러_그대로(held):
    c, _, _ = target_contracts(held, 0, 0, 1380)
    assert c == held


def test_C는_환율과_무관():
    assert target_contracts(20, 0, 0, 1)[0] == target_contracts(20, 0, 0, 1380)[0] == 20


# ── D: 포지션헤지비율 = −floor(코인포지션합 × B/100 / 환율 / 10000) ──
def test_hedge_기준예시_음수():
    # −floor(200_000_000 × 0.3 / 1380 / 10000) = −floor(4.34..) = −4
    assert hedge_ratio_contracts(200_000_000, 30, 1380) == -4


def test_hedge_항상_0이하():
    assert hedge_ratio_contracts(1_000_000_000, 99, 1380) <= 0


def test_hedge_코인0이면_0():
    assert hedge_ratio_contracts(0, 30, 1380) == 0


def test_hedge_meme_환율1():
    # 코인 1,000,000 × 100% / 1 / 10000 = 100 → D = −100
    assert hedge_ratio_contracts(1_000_000, 100, 1) == -100


def test_hedge_방향반전_절대값없음():
    # 절대값·부호곱 없이 -floor → 코인 롱(+)이면 숏(-), 코인 숏(-)이면 롱(+).
    # 비대칭: 음수 코인쪽 크기가 1 큼(floor 특성).
    assert hedge_ratio_contracts(1_287_085, 100, 1) == -128    # -floor(128.70)
    assert hedge_ratio_contracts(-1_287_085, 100, 1) == 129    # -floor(-128.70) = -(-129)


def test_target_meme면_D부호반전():
    c0, d0, t0 = target_contracts(0, 1_287_085, 100, 1)                    # Meme 아님
    c1, d1, t1 = target_contracts(0, 1_287_085, 100, 1, flip_hedge=True)   # Meme
    assert (c0, d0) == (0, -128)
    assert (c1, d1) == (0, 128)      # D 부호만 반대
    assert t1 == c1 + d1


# ── 목표 = C + D ──
def test_target_meme_기준예시():
    # 보유 원달러 100 / 코인 1,272,962 / B=100% / 환율 1 → C=100, D=−127, 목표=−27
    c, d, target = target_contracts(100, 1_272_962, 100, 1)
    assert (c, d, target) == (100, -127, -27)


def test_target_원화_기준예시():
    # 보유 원달러 18 / 코인 2억 / B=30% / 환율 1380 → C=18, D=−4, 목표=14
    assert target_contracts(18, 200_000_000, 30, 1380) == (18, -4, 14)


def test_target_숏_가능():
    # 헤지(D)가 달러(C)보다 크면 목표가 음수 → 숏
    c, d, target = target_contracts(10, 900_000_000, 80, 1380)
    assert target < 0
    assert target == c + d


def test_target_합산_일관성():
    c, d, target = target_contracts(-5, 150_000_000, 17, 1355)
    assert target == c + d


# ── 방어: 환율은 0보다 커야 함(D 환산) ──
@pytest.mark.parametrize("fx", [0, -1, -1380.5])
def test_D_환율_0이하면_ValueError(fx):
    with pytest.raises(ValueError):
        hedge_ratio_contracts(200_000_000, 30, fx)

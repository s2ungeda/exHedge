"""reconcile(맞춰가기) 계획(executor.reconcile_plan) 테스트.

들고 온 미체결 주문을 고려해 '취소 목록 + 새 주문'을 정한다. 순수 함수.
규칙: 같은방향+제가격이면 유지(목표 한도 내), 그 외(반대방향/낡은가격/초과분)는 취소.
"""

from trdbot.executor import reconcile_plan

BUY_PX = 1534.3   # 현재 매수 maker 가격
SELL_PX = 1534.2  # 현재 매도 maker 가격


def _o(ordno, side, remaining, price):
    return {"ordno": ordno, "side": side, "remaining": remaining, "price": price}


def _plan(target, position, open_orders, *, buy=BUY_PX, sell=SELL_PX, qty=3, cap=100, win=True):
    return reconcile_plan(target, position, open_orders, buy_price=buy, sell_price=sell,
                          qty_per_order=qty, max_position=cap, in_window=win)


# ── 기본 ───────────────────────────────────────────────
def test_시간외면_아무것도_안함():
    p = _plan(20, 0, [_o("1", "buy", 3, 1500.0)], win=False)
    assert p.decision.action == "skip"
    assert p.cancels == []          # 장 외엔 취소도 안 함


def test_미체결없고_부족하면_새주문():
    p = _plan(20, 0, [])
    assert p.cancels == []
    assert p.decision.action == "order"
    assert (p.decision.side, p.decision.qty, p.decision.price) == ("buy", 3, BUY_PX)


def test_수렴이면_stop():
    p = _plan(2, 0, [])              # 차이 2 < qty 3
    assert p.decision.action == "stop"


def test_목표아래면_매도():
    p = _plan(-20, 0, [])
    assert p.decision.action == "order"
    assert p.decision.side == "sell"
    assert p.decision.price == SELL_PX


# ── 들고 온 주문 관리 ──────────────────────────────────
def test_같은방향_제가격이면_유지하고_새주문은안함():
    # 동시 1개(기본): 목표 6, 들고 온 매수 3@제가격 → 유지하고 새 주문은 안 냄(체결 기다림)
    p = _plan(6, 0, [_o("111", "buy", 3, BUY_PX)])
    assert p.cancels == []                       # 유지(취소 안 함)
    assert p.decision.action == "skip"           # 동시 1개 관리 중 → 추가 주문 안 함


def test_동시한도_늘리면_부족분_추가():
    # max_orders=2면 유지(3) + 부족분(3) 하나 더 → 새 주문
    p = reconcile_plan(6, 0, [_o("111", "buy", 3, BUY_PX)], buy_price=BUY_PX, sell_price=SELL_PX,
                       qty_per_order=3, max_position=100, in_window=True, max_orders=2)
    assert p.cancels == []
    assert p.decision.action == "order" and p.decision.side == "buy"


def test_인터벌안_fresh주문은_낡은가격이어도_유지():
    # 우리가 낸 주문이 인터벌 안(fresh)이면 호가가 움직여 가격이 낡아도 취소 안 함
    p = reconcile_plan(6, 0, [_o("111", "buy", 3, 1533.0)], buy_price=BUY_PX, sell_price=SELL_PX,
                       qty_per_order=3, max_position=100, in_window=True, fresh_ordnos={"111"})
    assert p.cancels == []                       # fresh → 유지(취소는 인터벌 뒤)
    assert p.decision.action == "skip"           # 동시 1개 관리 중


def test_인터벌지난_주문은_낡은가격_취소():
    # fresh 아님(인터벌 지남) → 낡은 가격이면 취소·재호가
    p = reconcile_plan(6, 0, [_o("111", "buy", 3, 1533.0)], buy_price=BUY_PX, sell_price=SELL_PX,
                       qty_per_order=3, max_position=100, in_window=True)
    assert p.cancels == ["111"]
    assert p.decision.action == "order"


def test_반대방향은_fresh여도_즉시취소():
    # 반대 방향은 인터벌과 무관하게 즉시 취소(잘못된 노출 방지)
    p = reconcile_plan(6, 0, [_o("222", "sell", 2, SELL_PX)], buy_price=BUY_PX, sell_price=SELL_PX,
                       qty_per_order=3, max_position=100, in_window=True, fresh_ordnos={"222"})
    assert p.cancels == ["222"]
    assert p.decision.action == "order" and p.decision.side == "buy"


def test_반대방향은_취소():
    # 목표 6(매수 필요)인데 들고 온 매도 → 취소
    p = _plan(6, 0, [_o("222", "sell", 2, SELL_PX)])
    assert p.cancels == ["222"]
    assert p.decision.action == "order" and p.decision.side == "buy"


def test_낡은가격은_취소후_재호가():
    # 들고 온 매수가 옛 가격(1533.0) → 현재 maker(1534.3)와 다름 → 취소 후 새로
    p = _plan(6, 0, [_o("333", "buy", 3, 1533.0)])
    assert p.cancels == ["333"]
    assert p.decision.action == "order"
    assert p.decision.price == BUY_PX            # 현재 maker로 재호가


def test_목표초과분은_취소():
    # 목표 3인데 들고 온 매수가 3+3=6 → 한도(3) 넘는 두 번째는 취소
    p = _plan(3, 0, [_o("111", "buy", 3, BUY_PX), _o("222", "buy", 3, BUY_PX)])
    assert p.cancels == ["222"]                  # 초과분 취소
    assert p.decision.action == "stop"           # 유지분 3이 목표 채움 → 새 주문 없음


def test_제가격_유지분이_목표_채우면_새주문없음():
    p = _plan(3, 0, [_o("111", "buy", 3, BUY_PX)])
    assert p.cancels == []
    assert p.decision.action == "stop"


# ── 한도/호가 ──────────────────────────────────────────
def test_최대잔고한도_초과면_skip():
    p = _plan(200, 99, [], cap=100)
    assert p.decision.action == "skip"
    assert "한도" in p.decision.reason


def test_maker가격_없으면_새주문안하고_기존유지():
    # 호가공백 과다 등으로 가격 None → 낡음 판정 불가 → 기존 유지, 새 주문도 안 함
    p = _plan(6, 0, [_o("333", "buy", 3, 1533.0)], buy=None, sell=None)
    assert p.cancels == []                       # 판정 불가 → 취소 안 함
    assert p.decision.action == "skip"


# ── 청산 방향(반대 미체결 정리) ────────────────────────
def test_포지션초과시_반대주문_및_같은방향취소():
    # 포지션 20, 목표 14 → 6 줄여야 함(매도). 들고 온 매수는 반대 → 취소, 매도 주문.
    p = _plan(14, 20, [_o("111", "buy", 3, BUY_PX)])
    assert p.cancels == ["111"]
    assert p.decision.action == "order" and p.decision.side == "sell"


def test_반대방향취소후_차이작으면_수렴():
    # 포지션 16, 목표 14 → 반대 매수만 취소하고, 차이 2(<qty)라 새 주문은 없음(수렴)
    p = _plan(14, 16, [_o("111", "buy", 3, BUY_PX)])
    assert p.cancels == ["111"]
    assert p.decision.action == "stop"

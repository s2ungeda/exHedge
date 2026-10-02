"""발주 장부 — 운영로그 2026-09 중복주문(목표 사이 ±2 진동) 회귀 테스트.

원인 ①: 포지션을 먼저 읽고 미체결(t0434 ~1초)을 나중에 읽어, 그 사이 도착한 체결이
         미체결에선 빠지고 포지션엔 없음 → 같은 주문이 또 나감.
원인 ②: t0434가 C01보다 먼저 체결을 반영하면 주문이 미체결에서 사라지는데 포지션은 그대로.
"""

from trdbot.datastore import MarketDataStore
from trdbot.executor import Executor

OB = {"bidho1": "1370.5", "offerho1": "1370.7", "bidho2": "1370.4", "offerho2": "1370.8"}


class RaceBroker:
    """get_open_orders(t0434 HTTP) 도중 직전 주문 체결 통보가 도착하는 브로커.

    체결 처리 순서는 실제와 같다: 포지션(C01 net) 먼저 → 미체결 제거.
    """

    def __init__(self, pos: int):
        self.pos, self.pending, self.inflight, self.placed = pos, {}, None, []

    def get_position(self):
        return self.pos

    def get_open_orders(self):
        if self.inflight:   # t0434 조회 중 C01 도착
            oid, side, q = self.inflight
            self.pos += q if side == "buy" else -q
            self.pending.pop(oid, None)
            self.inflight = None
        return [{"ordno": k, **v} for k, v in self.pending.items()]

    def get_orderbook(self):
        return OB

    def place_order(self, side, qty, price):
        oid = str(len(self.placed) + 1)
        self.placed.append((side, qty))
        self.pending[oid] = {"side": side, "remaining": qty, "price": price}
        self.inflight = (oid, side, qty)   # 즉시 체결될 주문(통보는 다음 사이클 조회 중 도착)
        return oid

    def cancel(self, ordno):
        pass


def test_체결이_미체결조회중_도착해도_중복주문_안냄():
    b = RaceBroker(pos=-31)
    ex = Executor(b, qty_per_order=2, order_interval_sec=10, max_position=100,
                  compute_target=lambda s: -29, fallback_tick=0.1)
    ex.on_signal(object())
    for _ in range(6):
        ex.reconcile()
    assert b.placed == [("buy", 2)]   # 예전엔 buy,buy,sell,sell…로 진동
    assert b.pos == -29


# ── store 장부: t0434에서 사라져도 통보 전엔 미체결 유지 ──
class FakeApi:
    def __init__(self, orders=None, bal=None):
        self.orders = orders or []
        self.bal = bal or []
        self.balance_calls = 0

    def get_unfilled_orders(self, code="", chegb="2"):
        return {"t0434OutBlock1": list(self.orders)}

    def get_balance(self):
        self.balance_calls += 1
        return {"t0441OutBlock1": self.bal}


class FakeFeed:
    def __init__(self):
        self.net, self._fills = 0, []

    def c01_state(self):
        return (True, self.net)

    def fills(self):
        return list(self._fills)


def _row(ordno="5001", rem=2):
    return {"ordno": ordno, "expcode": "A7569000", "medosu": "매수", "ordrem": rem,
            "price": "1370.7"}


def _store(api, feed=None, clk=None):
    clk = clk if clk is not None else {"t": 0.0}
    return MarketDataStore(api, code_prefix="A756", feed=feed, clock=lambda: clk["t"])


def test_t0434가_C01보다_먼저_사라져도_통보전엔_미체결로_셈():
    api, clk = FakeApi(orders=[_row()]), {"t": 0.0}
    s = _store(api, clk=clk)
    s.note_placed("5001", "buy", 2, 1370.7)
    assert [o["ordno"] for o in s.open_orders_exec()] == ["5001"]   # t0434에 보임(seen)
    api.orders = []                          # t0434는 체결 반영, C01은 아직
    s.invalidate_open_orders()
    clk["t"] = 2.0
    assert [o["ordno"] for o in s.open_orders_exec()] == ["5001"]   # 장부가 유지 → 새 주문 막음
    s.apply_fill("5001", 2)                  # C01 도착
    assert s.open_orders_exec() == []


def test_통보누락이면_30초뒤_정리하고_포지션_재앵커():
    api, clk, feed = FakeApi(orders=[_row()], bal=[]), {"t": 0.0}, FakeFeed()
    s = _store(api, feed=feed, clk=clk)
    s.net_position()                         # 최초 앵커
    s.open_orders_exec()                     # t0434에서 봄 → 장부 편입(seen)
    api.orders = []
    for t in (2.0, 20.0):                    # 통보 없이 사라진 채 30초 미만 → 유지
        clk["t"] = t
        s.invalidate_open_orders()
        assert len(s.open_orders_exec()) == 1
    clk["t"] = 33.0
    s.invalidate_open_orders()
    assert s.open_orders_exec() == []        # 30초 넘어 정리
    calls = api.balance_calls
    s.net_position()
    assert api.balance_calls == calls + 1    # 포지션은 t0441로 다시 앵커


def test_취소요청만으론_장부유지_H01로_제거():
    s = _store(FakeApi())
    s.note_placed("5001", "sell", 2, 1370.6)
    s.mark_cancel_requested("5001")
    assert len(s.open_orders_exec()) == 1
    s.apply_cancel("5001")
    assert s.open_orders_exec() == []


def test_snapshot은_미체결먼저_포지션나중():
    """체결 처리가 net 먼저 → 미체결 제거 순이므로, 미체결 조회 중 체결이 와도 누락 없음."""
    feed = FakeFeed()
    api = FakeApi(orders=[])
    s = _store(api, feed=feed)
    s.net_position()                         # 앵커(0)
    s.note_placed("5001", "buy", 2, 1370.7)
    real_fetch = s._fetch_open_raw

    def fetch_during_fill():                 # t0434 HTTP 도중 C01 도착
        feed.net += 2
        s.apply_fill("5001", 2)
        return real_fetch()

    s._fetch_open_raw = fetch_during_fill
    s.invalidate_open_orders()
    pos, orders = s.snapshot()
    assert (pos, orders) == (2, [])          # 체결이 포지션에 반영됨(빠진 곳 없음)

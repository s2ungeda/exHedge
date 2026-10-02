"""Executor.reconcile() 통합 테스트 — 가짜 브로커로 취소·재호가 배선 확인.

순수 판단(reconcile_plan)은 test_reconcile.py에서, 여기선 그 계획이 실제 브로커 호출로
이어지는지(취소 목록 → cancel, 새 주문 → place_order)를 본다.
"""

from trdbot.executor import Executor, recent_fill_qty


def _ob():
    # 매수1=1380.0 매도1=1380.5 (틱 0.1, 공백 4) → 매수 maker 1380.3 / 매도 maker 1380.2
    return {"offerho1": "1380.5", "offerho2": "1380.6",
            "bidho1": "1380.0", "bidho2": "1379.9"}


class FakeExecBroker:
    def __init__(self, position=0, open_orders=None, ob=None):
        self._position = position
        self._open = open_orders or []
        self._ob = ob or _ob()
        self.placed = []
        self.cancelled = []
        self._seq = 9000

    def get_position(self):
        return self._position

    def get_open_orders(self):
        return list(self._open)

    def get_orderbook(self):
        return self._ob

    def place_order(self, side, qty, price):
        self._seq += 1
        self.placed.append((side, qty, price))
        return str(self._seq)

    def cancel(self, ordno):
        self.cancelled.append(ordno)


def _exec(broker):
    return Executor(
        broker, qty_per_order=3, order_interval_sec=1.0, max_position=100,
        compute_target=lambda s: s,      # 시그널이 곧 목표(int)
        in_window=lambda: True, fallback_tick=0.1,
    )


class PendingBroker(FakeExecBroker):
    """낸 주문이 미체결로 남는다 — 체결/취소 통보 두절 시뮬(확인이 안 옴).

    base cancel()은 미체결에서 안 뺀다(취소 요청만 기록) → 취소 '확인'도 안 오는 상태.
    """

    def place_order(self, side, qty, price):
        ordno = super().place_order(side, qty, price)
        self._open.append({"ordno": ordno, "side": side, "remaining": qty, "price": price})
        return ordno


def _exec_clk(broker, clk, interval=1.0):
    return Executor(
        broker, qty_per_order=3, order_interval_sec=interval, max_position=100,
        compute_target=lambda s: s, in_window=lambda: True, fallback_tick=0.1,
        clock=lambda: clk["t"],
    )


def test_인터벌경과_미체결_취소요청만_새주문은안냄():
    # 인터벌 지난 미체결 → 취소 요청만. 같은 사이클에 새 주문 절대 안 냄(취소 확인 후에나).
    clk = {"t": 0.0}
    broker = FakeExecBroker(
        position=0,
        open_orders=[{"ordno": "111", "side": "buy", "remaining": 3, "price": 1380.3}],
    )
    ex = _exec_clk(broker, clk)
    ex.on_signal(6)
    ex._placed_at["111"] = 0.0
    clk["t"] = 2.0                        # 인터벌(1초) 지남
    r = ex.reconcile()
    assert broker.cancelled == ["111"]    # 취소 요청
    assert broker.placed == []            # 새 주문 안 냄(취소 확인 대기)
    assert r.decision.action == "skip"


def test_반대방향_미체결은_인터벌전에도_즉시취소():
    clk = {"t": 0.0}
    broker = FakeExecBroker(
        position=0,
        open_orders=[{"ordno": "111", "side": "sell", "remaining": 3, "price": 1380.2}],
    )
    ex = _exec_clk(broker, clk, interval=10.0)
    ex.on_signal(6)                       # 목표 6 → 매수 필요인데 든 건 매도(반대)
    ex._placed_at["111"] = 0.0            # 인터벌 안
    ex.reconcile()
    assert broker.cancelled == ["111"]    # 반대 → 인터벌 전이라도 즉시 취소
    assert broker.placed == []            # 취소 확인 대기 → 새 주문 없음


def test_취소확인_사라지면_다음주문():
    # 취소 요청한 주문이 미체결에서 사라지면(확인) → 그때 새 주문.
    clk = {"t": 0.0}
    broker = FakeExecBroker(
        position=0,
        open_orders=[{"ordno": "111", "side": "buy", "remaining": 3, "price": 1380.3}],
    )
    ex = _exec_clk(broker, clk)
    ex.on_signal(6)
    ex._placed_at["111"] = 0.0
    clk["t"] = 2.0
    ex.reconcile()                        # 111 취소 요청(취소 확인 대기)
    assert broker.placed == []
    broker._open = []                     # 취소 확인(미체결에서 사라짐)
    r = ex.reconcile()
    assert broker.placed == [("buy", 3, 1380.3)]   # 미체결 0 → 재호가
    assert r.decision.action == "order"


def test_통보두절이면_첫주문만_나가고_멈춤():
    # 핵심 불변식: 체결/취소 통보가 안 오면 미체결이 안 사라져 → 다음 주문이 안 나간다(폭주 방지).
    clk = {"t": 0.0}
    broker = PendingBroker(position=0, open_orders=[])
    ex = _exec_clk(broker, clk)
    ex.on_signal(6)
    ex.reconcile()                        # 미체결 0 → 첫 주문 1건
    assert len(broker.placed) == 1
    for _ in range(5):
        clk["t"] += 2.0                   # 인터벌 계속 지나도
        ex.reconcile()                    # 통보 없어 미체결 그대로 → 새 주문 안 나감
    assert len(broker.placed) == 1        # 첫 주문 1건뿐 — 폭주 없음


def test_같은방향_제가격이면_유지하고_새주문은안함():
    # 동시 1개(기본): 들고 온 매수 유지하고 새 주문은 안 냄(체결 기다림)
    broker = FakeExecBroker(
        position=0,
        open_orders=[{"ordno": "111", "side": "buy", "remaining": 3, "price": 1380.3}],  # 제 가격
    )
    ex = _exec(broker)
    ex.on_signal(6)
    r = ex.reconcile()
    assert broker.cancelled == []         # 유지(취소 안 함)
    assert broker.placed == []            # 동시 1개 관리 중 → 추가 주문 없음
    assert r.kept_open == 1


def test_수렴이면_주문도_취소도_없음():
    broker = FakeExecBroker(position=0, open_orders=[])
    ex = _exec(broker)
    ex.on_signal(2)                       # 차이 2 < qty 3
    r = ex.reconcile()
    assert broker.placed == []
    assert broker.cancelled == []
    assert r.decision.action == "stop"


def test_시그널없으면_skip():
    broker = FakeExecBroker()
    ex = _exec(broker)
    r = ex.reconcile()                    # on_signal 호출 안 함
    assert r.decision.action == "skip"
    assert broker.placed == []


def test_주문가격_검증로그_콜백():
    captured = {}
    broker = FakeExecBroker(position=0, open_orders=[])
    ex = Executor(
        broker, qty_per_order=3, order_interval_sec=1.0, max_position=100,
        compute_target=lambda s: s, in_window=lambda: True, fallback_tick=0.1,
        on_quote=lambda info: captured.update(info),
    )
    ex.on_signal(6)
    ex.reconcile()
    # _ob: 매수1 1380.0 / 매도1 1380.5 (틱 0.1) → 공백 4, 매수 maker 1380.3
    assert captured["best_bid"] == 1380.0
    assert captured["best_ask"] == 1380.5
    assert captured["gap"] == 4
    assert captured["buy_price"] == 1380.3
    assert captured["action"] == "order"


def test_테스트_비체결방향_틱이동():
    captured = {}
    broker = FakeExecBroker(position=0, open_orders=[])
    ex = Executor(
        broker, qty_per_order=3, order_interval_sec=1.0, max_position=100,
        compute_target=lambda s: s, in_window=lambda: True, fallback_tick=0.1,
        test_nofill_ticks=3, on_quote=lambda i: captured.update(i),
    )
    ex.on_signal(6)                       # 목표 6, 포지션 0 → 매수
    ex.reconcile()
    # 매수 maker 1380.3 → 3틱(0.3) 낮게 → 1380.0 (체결 안 되는 방향)
    assert captured["buy_price"] == 1380.0
    assert ("buy", 3, 1380.0) in broker.placed


def test_최근체결량_윈도우_안만_합산():
    fills = [
        {"qty": 2, "_ts": 100.0},   # now-5초 → 포함
        {"qty": 3, "_ts": 50.0},    # now-55초 → 윈도우(30초) 밖
        {"qty": 1, "_ts": 95.0},    # now-10초 → 포함
    ]
    assert recent_fill_qty(fills, now=105.0, window_sec=30) == 3   # 2+1
    assert recent_fill_qty(fills, now=105.0, window_sec=0) == 0    # 0=끔
    # since(재개 리셋): since 이후 체결만 → ts=100만 카운트(95는 since 이하)
    assert recent_fill_qty(fills, now=105.0, window_sec=30, since=97.0) == 2


def test_체결량_한도초과_미체결전부취소_주문중지():
    broker = FakeExecBroker(
        position=0,
        open_orders=[
            {"ordno": "111", "side": "buy", "remaining": 3, "price": 1380.3},
            {"ordno": "222", "side": "buy", "remaining": 3, "price": 1380.3},
        ],
    )
    tripped = {"v": False}
    ex = Executor(
        broker, qty_per_order=3, order_interval_sec=1.0, max_position=100,
        compute_target=lambda s: s, in_window=lambda: True, fallback_tick=0.1,
        trade_enabled=lambda: True,
        fill_breaker=lambda: True,                       # 최근 체결량 한도 초과 상태
        on_breaker_trip=lambda: tripped.__setitem__("v", True),
    )
    ex.on_signal(6)
    r = ex.reconcile()
    assert set(broker.cancelled) == {"111", "222"}       # 미체결 전부 취소
    assert broker.placed == []                            # 새 주문 안 냄
    assert tripped["v"] is True                           # 트립 콜백('주문' 해제)
    assert r.decision.action == "stop"


def test_체결량_브레이커_관찰모드면_트립안함():
    # '주문' 꺼진 관찰모드에선 브레이커 트립 안 함(어차피 주문 안 나감)
    broker = FakeExecBroker(
        position=0, open_orders=[{"ordno": "111", "side": "buy", "remaining": 3, "price": 1380.3}],
    )
    tripped = {"v": False}
    ex = Executor(
        broker, qty_per_order=3, order_interval_sec=1.0, max_position=100,
        compute_target=lambda s: s, in_window=lambda: True, fallback_tick=0.1,
        trade_enabled=lambda: False,                     # 관찰모드
        fill_breaker=lambda: True,
        on_breaker_trip=lambda: tripped.__setitem__("v", True),
    )
    ex.on_signal(6)
    ex.reconcile()
    assert broker.cancelled == []                         # 관찰모드 → 트립 안 함
    assert tripped["v"] is False


def test_최대잔고한도_초과시_사유_상단표시_중복방지():
    msgs = []
    broker = FakeExecBroker(position=100, open_orders=[])   # 이미 한도(100)
    ex = Executor(
        broker, qty_per_order=3, order_interval_sec=1.0, max_position=100,
        compute_target=lambda s: s, in_window=lambda: True, fallback_tick=0.1,
        trade_enabled=lambda: True, on_status=lambda m: msgs.append(m),
    )
    ex.on_signal(150)                     # 목표 150 > 한도 100 → 한도 초과 skip
    r = ex.reconcile()
    assert r.decision.action == "skip"
    assert any("최대잔고한도" in m for m in msgs)   # 사유가 상단 로그로 나감
    n1 = len(msgs)
    ex.reconcile()                        # 같은 사유 반복
    assert len(msgs) == n1                # 중복은 표시 안 함(덮어쓰기 방지)


def test_브레이커_트립후_관찰보류_노이즈_억제():
    # 체결량 한도로 멈춘(=관찰모드 + fill_breaker still True) 상태에선 '[관찰] 보류' 안 냄
    msgs = []
    broker = FakeExecBroker(position=0, open_orders=[])
    ex = Executor(
        broker, qty_per_order=3, order_interval_sec=1.0, max_position=100,
        compute_target=lambda s: s, in_window=lambda: True, fallback_tick=0.1,
        trade_enabled=lambda: False,      # 트립으로 '주문' 해제된 상태
        fill_breaker=lambda: True,        # 여전히 체결량 한도 초과
        on_status=lambda m: msgs.append(m),
    )
    ex.on_signal(6)
    ex.reconcile()
    assert not any("[관찰]" in m for m in msgs)   # 중지 메시지가 안 덮이게 → 관찰 보류 억제


def test_주문비활성_관찰모드면_주문도_취소도_안함():
    broker = FakeExecBroker(
        position=0,
        open_orders=[{"ordno": "111", "side": "buy", "remaining": 3, "price": 1300.0}],  # 낡은 주문
    )
    ex = Executor(
        broker, qty_per_order=3, order_interval_sec=1.0, max_position=100,
        compute_target=lambda s: s, in_window=lambda: True, fallback_tick=0.1,
        trade_enabled=lambda: False,      # 관찰모드(주문 게이트 OFF)
    )
    ex.on_signal(6)                       # 목표 6 → 평소면 취소+매수
    r = ex.reconcile()
    assert broker.placed == []            # 주문 안 나감
    assert broker.cancelled == []         # 취소도 안 함(관찰)
    assert r.decision.action == "stop"    # 루프는 다음 시그널까지 idle


class RejectBroker(FakeExecBroker):
    """place_order가 항상 거부(예외) — 매매불가·증거금부족 등 지속 거부 시뮬."""

    def place_order(self, side, qty, price):
        raise RuntimeError("매매불가(01407)")


def test_주문거부_rejected반환_화면표시_주문없음():
    msgs = []
    broker = RejectBroker(position=0, open_orders=[])
    ex = Executor(
        broker, qty_per_order=3, order_interval_sec=1.0, max_position=100,
        compute_target=lambda s: s, in_window=lambda: True, fallback_tick=0.1,
        trade_enabled=lambda: True, on_status=lambda m: msgs.append(m),
    )
    ex.on_signal(6)                       # 목표 6 → 매수 시도 → 거부
    r = ex.reconcile()
    assert r.rejected is True             # run이 백오프 판단하도록 rejected 플래그
    assert "주문거부" in r.decision.reason
    assert broker.placed == []            # 거부라 접수 안 됨


def test_주문거부_연속시_백오프_지수증가():
    # run()의 백오프 계산 — 연속 거부마다 대기 1→2→4초(base=1, cap=30)
    broker = RejectBroker(position=0, open_orders=[])
    ex = Executor(
        broker, qty_per_order=3, order_interval_sec=1.0, max_position=100,
        compute_target=lambda s: s, in_window=lambda: True, fallback_tick=0.1,
        reject_backoff_base=1.0, reject_backoff_cap=30.0,
    )
    waits = []
    for streak in (1, 2, 3, 6):
        ex._reject_streak = streak
        w = min(ex._reject_backoff_base * (2 ** (ex._reject_streak - 1)),
                ex._reject_backoff_cap)
        waits.append(w)
    assert waits == [1.0, 2.0, 4.0, 30.0]   # 6회째는 상한(30) 적용

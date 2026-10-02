"""세션 라우팅 + 경계 고정(SessionRoutingBroker) 테스트.

Layer A: 한 집행 사이클(get_position으로 시작)은 시작 시점 세션으로 고정된다.
경계(예: 15:45)에서 시각이 넘어가도 place_order 와 cancel 이 같은 세션으로 라우팅.
"""

from trdbot.broker_ls import LSDayFuturesBroker, SessionRoutingBroker
from trdbot.datastore import MarketDataStore


class FakeBroker:
    """LSDay/NightFuturesBroker 자리에 끼우는 호출 기록용 가짜."""

    def __init__(self, name: str):
        self.name = name
        self.calls: list = []

    def get_orderbook(self) -> dict:
        self.calls.append("orderbook")
        return {"offerho1": "1", "bidho1": "1"}

    def get_position(self) -> int:
        self.calls.append("position")
        return 0

    def place_order(self, side, qty, price) -> str:
        self.calls.append(("place", side, qty, price))
        return "ORD1"

    def cancel(self, order_no) -> None:
        self.calls.append(("cancel", order_no))

    def cancel_all_unfilled(self) -> int:
        self.calls.append("cancel_all")
        return 3


class FakeFuturesAPI:
    """LSDayFuturesBroker가 부르는 메서드만 가진 가짜 FuturesAPI."""

    def __init__(self, rows: list, balance_rows: list | None = None):
        self._rows = rows
        self._balance_rows = balance_rows or []
        self.cancelled: list = []
        self.balance_calls = 0
        self.unfilled_calls = 0

    def get_unfilled_orders(self, code: str = "", chegb: str = "2") -> dict:
        self.unfilled_calls += 1
        return {"t0434OutBlock1": self._rows}

    def cancel_order(self, code, ordno, qty) -> dict:
        self.cancelled.append((code, ordno, qty))
        return {}

    def get_orderbook(self, code) -> dict:
        return {"t2112OutBlock": {"offerho1": "1536.50", "bidho1": "1536.40", "_src": "REST"}}

    def get_balance(self) -> dict:
        self.balance_calls += 1
        return {"t0441OutBlock1": self._balance_rows}


def _broker(now_ref: dict):
    day, night = FakeBroker("day"), FakeBroker("night")
    b = SessionRoutingBroker(day, night, now_hhmm=lambda: now_ref["t"])
    return b, day, night


def test_사이클_시작세션으로_고정_경계넘어도_유지():
    now = {"t": 1000}  # 주간(08:45~15:45)
    b, day, night = _broker(now)

    b.get_position()                       # 사이클 시작 → 주간으로 고정
    no = b.place_order("buy", 1, 1380.0)   # 주간으로 주문
    now["t"] = 1546                        # 시각이 마감(closed)으로 넘어감
    b.cancel(no)                           # 고정된 주간으로 취소 (예외 없음)

    assert ("place", "buy", 1, 1380.0) in day.calls
    assert ("cancel", "ORD1") in day.calls
    assert night.calls == []               # 야간으로 새지 않음


def test_get_position이_세션을_재고정():
    now = {"t": 1000}  # 주간
    b, day, night = _broker(now)
    b.get_position()                       # 주간 고정
    now["t"] = 2000                        # 야간(18:00~)
    b.get_position()                       # 새 사이클 → 야간 재고정
    b.place_order("sell", 1, 1380.0)
    assert ("place", "sell", 1, 1380.0) in night.calls
    assert not any(c[0] == "place" for c in day.calls if isinstance(c, tuple))


def test_마감시각엔_get_position이_주간브로커로_조회():
    # 잔고는 장 마감에도 조회 가능해야 함(화면 표시) → 주간 브로커(t0441), 예외 안 남.
    now = {"t": 1700}  # 15:45~18:00 사이 = closed
    b, day, night = _broker(now)
    b.get_position()
    assert "position" in day.calls
    assert night.calls == []


def test_session은_고정과_무관하게_라이브():
    now = {"t": 1000}
    b, _, _ = _broker(now)
    assert b.session() == "day"
    now["t"] = 2000
    assert b.session() == "night"          # 표시용 세션은 항상 실시간


# ── 4번: 기동 시 미체결 정리 ───────────────────────────
def test_세션라우팅_미체결정리_위임():
    now = {"t": 1000}
    b, day, night = _broker(now)
    assert b.cancel_all_unfilled() == 3
    assert "cancel_all" in day.calls       # 활성 세션(주간)으로 위임
    assert night.calls == []


def _day_broker(api, code="A7567000", orderbook_source=None):
    # 2단계: 브로커는 저장소에 위임. cancel_all/orderbook은 api로(저장소 미사용).
    store = MarketDataStore(api, code_prefix=code[:4])
    return LSDayFuturesBroker(store, api, code, orderbook_source=orderbook_source)


def test_주간_미체결_우리종목만_취소():
    rows = [
        {"expcode": "A7567000", "ordno": 111, "ordrem": 2},   # 우리(최근월) → 취소
        {"expcode": "A7568000", "ordno": 112, "ordrem": 3},   # 우리(차근월 A756) → 취소
        {"expcode": "A7567000", "ordno": 113, "ordrem": 0},   # 잔량 0 → 건너뜀
        {"expcode": "B1234000", "ordno": 114, "ordrem": 5},   # 다른 종목 → 건드리지 않음
    ]
    api = FakeFuturesAPI(rows)
    broker = _day_broker(api)

    n = broker.cancel_all_unfilled()

    assert n == 2
    assert ("A7567000", 111, 2) in api.cancelled
    assert ("A7568000", 112, 3) in api.cancelled
    cancelled_ordnos = [c[1] for c in api.cancelled]
    assert 113 not in cancelled_ordnos      # 잔량 0
    assert 114 not in cancelled_ordnos      # 다른 종목


def test_미체결_없으면_0건():
    api = FakeFuturesAPI([])
    broker = _day_broker(api)
    assert broker.cancel_all_unfilled() == 0
    assert api.cancelled == []


# ── 취소: t0434 미반영 막 낸 주문(pending)도 취소돼야 (주문 쌓임 방지) ──
def test_cancel_막낸주문_pending도_취소():
    api = FakeFuturesAPI([])                       # t0434 아직 비어있음
    store = MarketDataStore(api, code_prefix="A756")
    broker = LSDayFuturesBroker(store, api, "A7567000")
    store.note_placed("5001", "buy", 3, 1380.3)    # 막 낸 주문(t0434 미반영)
    broker.cancel("5001")
    assert ("A7567000", 5001, 3) in api.cancelled  # 우리 종목코드로 취소됨
    assert store.find_open("5001") is None         # pending에서 제거(더는 in-flight 아님)


def test_cancel_t0434주문은_캐시값으로():
    rows = [{"ordno": "111", "expcode": "A7568000", "ordrem": 2, "medosu": "매수", "price": "1531.0"}]
    api = FakeFuturesAPI(rows)
    store = MarketDataStore(api, code_prefix="A756")
    broker = LSDayFuturesBroker(store, api, "A7567000")
    store.open_orders()                            # 캐시 채움
    broker.cancel("111")
    assert ("A7568000", 111, 2) in api.cancelled   # t0434의 expcode·잔량으로 취소


def test_cancel_없는주문은_noop():
    api = FakeFuturesAPI([])
    store = MarketDataStore(api, code_prefix="A756")
    broker = LSDayFuturesBroker(store, api, "A7567000")
    broker.cancel("9999")                          # 캐시·pending 둘 다 없음
    assert api.cancelled == []                     # 아무것도 안 함


# ── 실시간 호가 → get_orderbook 우선 / REST 폴백 (브로커 책임) ──
def test_실시간호가_있으면_캐시사용():
    live = {"offerho1": "1536.50", "bidho1": "1536.40", "_src": "WS"}
    broker = _day_broker(FakeFuturesAPI([]), orderbook_source=lambda: live)
    assert broker.get_orderbook()["_src"] == "WS"   # REST 안 거치고 실시간 캐시


def test_실시간호가_비었으면_REST폴백():
    broker = _day_broker(FakeFuturesAPI([]), orderbook_source=lambda: {})
    assert broker.get_orderbook()["_src"] == "REST"  # 캐시 비면 t2112 REST


def test_실시간호가_소스없으면_REST():
    broker = _day_broker(FakeFuturesAPI([]))         # orderbook_source 미지정
    assert broker.get_orderbook()["_src"] == "REST"


# ── 야간 브로커: 주간과 동일하게 중앙저장소에 위임 (미체결 pending / 포지션 CCENQ90200) ──
class FakeNightAPI:
    def __init__(self, balance_rows=None):
        self._balance_rows = balance_rows or []
        self.cancelled: list = []
        self.placed: list = []

    def place_order(self, code, qty, *, side, kind, price) -> dict:
        self.placed.append((code, qty, side, price))
        return {"CCENT00100OutBlock2": {"OrdNo": "7001"}}

    def cancel_order(self, code, ordno, qty) -> dict:
        self.cancelled.append((code, ordno, qty))
        return {}

    def get_orderbook(self, code) -> dict:
        return {"t8457OutBlock": {"offerho1": "1", "bidho1": "1"}}

    def get_balance(self) -> dict:
        return {"CCENQ90200OutBlock3": self._balance_rows}


def _night_broker(night_api):
    from trdbot.broker_ls import LSNightFuturesBroker
    store = MarketDataStore(FakeFuturesAPI([]), code_prefix="A756",
                            night_api=night_api, session=lambda: "night")
    return LSNightFuturesBroker(store, night_api, "A7567000"), store


def test_야간_주문하면_pending에_뜨고_미체결조회로_보인다():
    # 야간 브로커도 store.note_placed → get_open_orders에 즉시 반영(상태기계가 다음 주문 안 냄).
    api = FakeNightAPI()
    broker, _store = _night_broker(api)
    ordno = broker.place_order("buy", 1, 1528.6)
    assert ordno == "7001"
    assert ("A7567000", 1, "buy", 1528.6) in api.placed
    assert any(o["ordno"] == "7001" for o in broker.get_open_orders())   # pending으로 보임


def test_야간_취소하면_야간TR로_취소하고_pending제거():
    api = FakeNightAPI()
    broker, store = _night_broker(api)
    broker.place_order("buy", 1, 1528.6)
    broker.cancel("7001")
    assert ("A7567000", 7001, 1) in api.cancelled   # 야간 취소 TR(CCENT00300) 호출
    assert store.find_open("7001") is None           # pending에서 제거


def test_야간_포지션은_CCENQ90200을_정규화해_계산():
    # 야간 세션이면 저장소가 CCENQ90200(FnoIsuNo/UnsttQty/BnsTpNm)을 net으로 환산.
    api = FakeNightAPI(balance_rows=[
        {"FnoIsuNo": "A7567000", "UnsttQty": 3, "BnsTpNm": "매도"},   # -3
        {"FnoIsuNo": "A7568000", "UnsttQty": 1, "BnsTpNm": "매수"},   # +1
        {"FnoIsuNo": "B1234000", "UnsttQty": 9, "BnsTpNm": "매수"},   # 다른종목 제외
    ])
    broker, _store = _night_broker(api)
    assert broker.get_position() == -2   # -3 + 1

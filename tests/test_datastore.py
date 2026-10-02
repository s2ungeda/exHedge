"""중앙저장소(MarketDataStore) 테스트 — 포지션(baseline+C01+재동기화)·미체결 캐시·체결 누적.

2단계: broker에 있던 포지션/미체결 로직이 저장소로 이전됨 → 여기서 검증한다.
"""

from trdbot.datastore import MarketDataStore
from trdbot.realtime_feed import RealtimeFeed


class FakeApi:
    def __init__(self, bal=None, orders=None):
        self._bal = bal or []
        self._orders = orders or []
        self.chegb_used: list[str] = []
        self.balance_calls = 0

    def get_balance(self):
        self.balance_calls += 1
        return {"t0441OutBlock1": self._bal}

    def get_unfilled_orders(self, code="", chegb="2"):
        self.chegb_used.append(chegb)
        return {"t0434OutBlock1": self._orders}


class FakeFeed:
    def __init__(self, fills=None, net=0, seen=True):
        self._f = fills or []
        self._net = net
        self._seen = seen

    def fills(self):
        return list(self._f)

    def c01_state(self):
        return (self._seen, self._net)


def _bal(qty, medosu="매수"):
    return [{"expcode": "A7567000", "jqty": str(qty), "medosu": medosu}]


def _oo(ordno=1, rem=2):
    return [{"ordno": ordno, "expcode": "A7567000", "medosu": "매수", "ordrem": rem, "price": "1531.0"}]


# ── 포지션: feed 없으면 t0441, 있으면 baseline+C01, 주기 재동기화 ──
def test_포지션_feed없으면_매번_t0441():
    api = FakeApi(bal=_bal(3))
    s = MarketDataStore(api, code_prefix="A756")   # feed=None
    assert s.net_position() == 3
    assert s.net_position() == 3
    assert api.balance_calls == 2          # 매번 t0441(기존 동작)


def test_포지션_시작값_더하기_C01증감_폴링없음():
    api = FakeApi(bal=_bal(2))             # 시작 +2
    feed = FakeFeed(net=0)
    s = MarketDataStore(api, code_prefix="A756", feed=feed, clock=lambda: 0.0, resync_sec=30)
    assert s.net_position() == 2           # baseline=2, net=0
    feed._net = 3
    assert s.net_position() == 5           # 2 + (3−0)
    feed._net = 1
    assert s.net_position() == 3           # 2 + (1−0)
    assert api.balance_calls == 1          # 그동안 t0441 폴링 안 함


def test_포지션_주기경과해도_자동재동기화안함():
    # 자동 t0441 재조회는 끔(체결 직후 stale 조회가 실포지션 덮어써 오버슛 유발) → C01만으로 추적.
    api = FakeApi(bal=_bal(5))
    feed = FakeFeed(net=0)
    clk = {"t": 0.0}
    s = MarketDataStore(api, code_prefix="A756", feed=feed, clock=lambda: clk["t"], resync_sec=30)
    assert s.net_position() == 5           # 최초 1회만 앵커(baseline=5)
    assert api.balance_calls == 1
    clk["t"] = 31                          # 주기 경과해도
    feed._net = 1
    assert s.net_position() == 6           # 재동기화 안 함: 5 + (1−0) = 6 (실시간 C01)
    assert api.balance_calls == 1          # t0441 다시 안 읽음


def test_포지션_수동조회로만_재동기화():
    # 사용자 [Idle조회] → resync_position → 다음 net_position에서만 t0441 재동기화.
    api = FakeApi(bal=_bal(5))
    feed = FakeFeed(net=0)
    s = MarketDataStore(api, code_prefix="A756", feed=feed, clock=lambda: 0.0, resync_sec=30)
    assert s.net_position() == 5
    feed._net = 1
    assert s.net_position() == 6           # 자동 재동기화 없음
    api._bal = _bal(6)                     # 이제 t0441도 6을 반영
    s.resync_position()                    # 사용자 조회 요청
    assert s.net_position() == 6           # 재동기화: baseline=6, baseline_net=1 → 6+(1−1)
    assert api.balance_calls == 2          # 조회 때만 t0441 읽음


# ── 미체결: 캐시(변동/주기/무효화 때만 재조회), exec 형식, 정정 방향 ──
def test_미체결_캐시_변동없으면_재조회안함():
    api = FakeApi(orders=_oo())
    s = MarketDataStore(api, code_prefix="A756", clock=lambda: 0.0, resync_sec=30)
    s.open_orders_exec()
    s.open_orders_exec()
    s.open_orders_exec()
    assert api.chegb_used == ["2"]         # 첫 조회만, 이후 캐시(미체결 chegb=2)


def test_미체결_캐시_무효화하면_재조회():
    api = FakeApi(orders=_oo())
    clk = {"t": 0.0}
    s = MarketDataStore(api, code_prefix="A756", clock=lambda: clk["t"], resync_sec=30)
    s.open_orders_exec()
    s.invalidate_open_orders()
    clk["t"] = 1.5                      # 재조회 최소간격(1초) 지난 뒤
    s.open_orders_exec()
    assert api.chegb_used == ["2", "2"]


def test_force_open_refetch_쓰로틀무시_즉시재조회():
    # 체결 직후 — 최소간격 안이어도 force면 즉시 t0434 재조회(다음 주문 바로 나가게)
    api = FakeApi(orders=_oo())
    s = MarketDataStore(api, code_prefix="A756", clock=lambda: 0.0, resync_sec=30)
    s.open_orders_exec()               # 1회
    s.force_open_refetch()             # 체결 직후
    s.open_orders_exec()               # 같은 시각(0초)이지만 force → 재조회
    assert api.chegb_used == ["2", "2"]


def test_미체결_캐시_무효화해도_최소간격안엔_재조회안함():
    # 체결 연타 보호 — 더러워도 최소간격(1초) 안에선 t0434 재조회 안 함
    api = FakeApi(orders=_oo())
    s = MarketDataStore(api, code_prefix="A756", clock=lambda: 0.0, resync_sec=30)
    s.open_orders_exec()               # 1회
    s.invalidate_open_orders()
    s.open_orders_exec()               # 같은 시각(0초) → 최소간격 안 → 재조회 안 함
    assert api.chegb_used == ["2"]      # 여전히 1회


def test_미체결_캐시_30초경과_재동기화():
    api = FakeApi(orders=_oo())
    clk = {"t": 0.0}
    s = MarketDataStore(api, code_prefix="A756", clock=lambda: clk["t"], resync_sec=30)
    s.open_orders_exec()
    clk["t"] = 31
    s.open_orders_exec()
    assert len(api.chegb_used) == 2


def test_미체결_exec형식_종목·잔량·정정방향():
    rows = [
        {"ordno": 1, "expcode": "A7567000", "medosu": "매수", "ordrem": 2, "price": "1531.0"},
        {"ordno": 2, "expcode": "A7567000", "medosu": "매수정정", "ordrem": 1, "price": "1532.0"},
        {"ordno": 3, "expcode": "A7567000", "medosu": "매도정정", "ordrem": 3, "price": "1538.0"},
        {"ordno": 4, "expcode": "B1234000", "medosu": "매수", "ordrem": 5, "price": "1000.0"},
        {"ordno": 5, "expcode": "A7567000", "medosu": "매수", "ordrem": 0, "price": "1530.0"},
    ]  # 4=다른종목, 5=잔량0 → 제외
    s = MarketDataStore(FakeApi(orders=rows), code_prefix="A756")
    oo = s.open_orders_exec()
    assert {o["ordno"] for o in oo} == {"1", "2", "3"}   # 우리종목·잔량>0만
    by = {o["ordno"]: o["side"] for o in oo}
    assert by["2"] == "buy"      # 매수정정 → buy
    assert by["3"] == "sell"     # 매도정정 → sell


# ── 이중주문 방지: 막 낸 주문 in-flight(t0434 반영 전) ──
def test_막낸주문_t0434미반영이어도_inflight로_보임():
    api = FakeApi(orders=[])   # t0434 아직 비어있음(반영 지연)
    s = MarketDataStore(api, code_prefix="A756", clock=lambda: 0.0)
    s.note_placed("5001", "buy", 3, 1380.3)
    oo = s.open_orders_exec()
    assert any(o["ordno"] == "5001" and o["side"] == "buy" and o["remaining"] == 3 for o in oo)


def test_pending_t0434반영되면_중복없음():
    rows = [{"ordno": "5001", "expcode": "A7567000", "medosu": "매수", "ordrem": 3, "price": "1380.3"}]
    api = FakeApi(orders=rows)
    s = MarketDataStore(api, code_prefix="A756", clock=lambda: 0.0)
    s.note_placed("5001", "buy", 3, 1380.3)
    oo = s.open_orders_exec()
    assert [o["ordno"] for o in oo].count("5001") == 1   # t0434 것만, 중복 카운트 없음


def test_pending_C01체결되면_제거():
    # t0434에 뜨기 전 체결되면(C01) pending 제거 — 무한 in-flight(쌓임) 방지
    api = FakeApi(orders=[])
    feed = FakeFeed(fills=[{"ordno": "00005001", "qty": 3}])   # zero-padded도 매칭
    s = MarketDataStore(api, code_prefix="A756", clock=lambda: 0.0, feed=feed)
    s.note_placed("5001", "buy", 3, 1380.3)
    s.invalidate_open_orders()
    assert not any(o["ordno"] == "5001" for o in s.open_orders_exec())  # 체결완료 → 제거


def test_pending_TTL로는_안지움_통보로만():
    # 시간이 아무리 지나도 pending은 TTL로 안 빠진다 — 체결/취소 통보나 t0434 확인으로만 제거.
    # (통보 두절 시 "미해소"로 남겨 다음 주문을 막기 위함)
    api = FakeApi(orders=[])
    clk = {"t": 0.0}
    s = MarketDataStore(api, code_prefix="A756", clock=lambda: clk["t"], resync_sec=30)
    s.note_placed("5001", "buy", 3, 1380.3)
    assert any(o["ordno"] == "5001" for o in s.open_orders_exec())
    clk["t"] = 1000.0                                                  # 한참 지나도
    s.invalidate_open_orders()
    assert any(o["ordno"] == "5001" for o in s.open_orders_exec())     # 여전히 in-flight(안 빠짐)
    s.apply_fill("5001", 3)                                            # 전량체결 통보 → 제거
    assert not any(o["ordno"] == "5001" for o in s.open_orders_exec())


# ── 실시간 체결 반영: apply_fill ──
def _oo_row(ordno="5001", rem=2):
    return [{"ordno": ordno, "expcode": "A7567000", "medosu": "매수", "ordrem": rem, "price": "1380.3"}]


def test_apply_fill_전량체결_캐시에서_제거():
    s = MarketDataStore(FakeApi(orders=_oo_row(rem=2)), code_prefix="A756", clock=lambda: 0.0)
    s.open_orders()                        # 캐시 채움(5001, 잔량 2)
    s.apply_fill("0000005001", 2)          # 전량체결(zero-padded도 매칭)
    assert s.find_open("5001") is None
    assert not any(o["ordno"] == "5001" for o in s.open_orders_exec())


def test_apply_fill_부분체결_잔량차감():
    s = MarketDataStore(FakeApi(orders=_oo_row(rem=3)), code_prefix="A756", clock=lambda: 0.0)
    s.open_orders()
    s.apply_fill("5001", 1)                # 1 체결 → 잔량 2
    o = next(o for o in s.open_orders_exec() if o["ordno"] == "5001")
    assert o["remaining"] == 2


def test_apply_fill_억제_t0434지연에도_재등장안함():
    clk = {"t": 0.0}
    api = FakeApi(orders=_oo_row(rem=2))   # t0434는 계속 5001 보여줌(반영 지연 가정)
    s = MarketDataStore(api, code_prefix="A756", clock=lambda: clk["t"], resync_sec=30)
    s.open_orders()                        # 캐시 5001
    s.apply_fill("5001", 2)                # 전량체결 → 제거 + 억제(t=0)
    clk["t"] = 1.5                          # 최소간격 지나 재조회됨
    s.invalidate_open_orders()
    assert not any(o["ordno"] == "5001" for o in s.open_orders())   # 억제로 재등장 안 함


# ── 실시간 접수/취소: apply_accept / apply_cancel + O01 파싱 ──
def test_apply_accept_미체결_즉시추가():
    s = MarketDataStore(FakeApi(orders=[]), code_prefix="A756", clock=lambda: 0.0)
    s.apply_accept({"ordno": "5001", "expcode": "A7567000", "medosu": "매수",
                    "ordrem": 3, "price": "1380.3"})
    oo = s.open_orders_exec()
    assert any(o["ordno"] == "5001" and o["side"] == "buy" and o["remaining"] == 3 for o in oo)


def test_apply_cancel_미체결_즉시제거():
    s = MarketDataStore(FakeApi(orders=_oo_row(rem=2)), code_prefix="A756", clock=lambda: 0.0)
    s.open_orders()                        # 캐시 5001
    s.apply_cancel("0000005001")           # H01 ordordno(zero-padded)
    assert s.find_open("5001") is None


def test_parse_accept_신규_정정_취소_구분():
    rt = RealtimeFeed(code="A7567000", account=None, position_prefix="A756")
    # 신규(orgordno=0) → 추가, orgordno=""
    rec = rt._parse_accept({"ordno": "26757", "fnoIsuno": "A7567000", "bnstp": "2",
                            "unercqty": "20", "ordprc": "1544.50", "orgordno": "0"})
    assert rec == {"ordno": "26757", "orgordno": "", "expcode": "A7567000",
                   "medosu": "매수", "ordrem": 20, "price": "1544.50"}
    # 정정(orgordno 있음 + 잔량>0) → 원주문 함께 실어 교체 처리(제외 아님)
    rec2 = rt._parse_accept({"ordno": "26760", "fnoIsuno": "A7567000", "bnstp": "2",
                             "unercqty": "20", "ordprc": "1545.00", "orgordno": "26759"})
    assert rec2["ordno"] == "26760" and rec2["orgordno"] == "26759"
    # 순수 취소(잔량=0) → 제외
    assert rt._parse_accept({"ordno": "26761", "fnoIsuno": "A7567000", "bnstp": "2",
                             "unercqty": "0", "ordprc": "1544.50", "orgordno": "26759"}) is None
    # 다른 종목 → 제외
    assert rt._parse_accept({"ordno": "1", "fnoIsuno": "B1234000", "bnstp": "2",
                             "unercqty": "5", "ordprc": "100", "orgordno": "0"}) is None


def test_cancel_ordno_주간_야간_필드():
    # 정정취소 취소원주문번호: 주간 H01=ordordno, 야간 H02=orgordno (필드명 다름).
    rt = RealtimeFeed(code="A7567000", account=None, position_prefix="A756")
    assert rt._cancel_ordno({"ordordno": "26759"}) == "26759"        # H01(주간)
    assert rt._cancel_ordno({"orgordno": "0000026759"}) == "0000026759"  # H02(야간)
    assert rt._cancel_ordno({}) == ""                                 # 둘 다 없음


def test_apply_accept_정정_원주문_대체():
    # 정정: 원주문(5001) 미체결 캐시에 있음 → 정정 새 주문(5002) 추가하며 원주문 사라짐
    s = MarketDataStore(FakeApi(orders=_oo_row(rem=3)), code_prefix="A756", clock=lambda: 0.0)
    s.open_orders()                        # 캐시에 5001
    s.apply_accept({"ordno": "5002", "orgordno": "5001", "expcode": "A7567000",
                    "medosu": "매수", "ordrem": 3, "price": "1381.0"})
    ords = {o["ordno"] for o in s.open_orders_exec()}
    assert "5002" in ords                  # 정정 새 주문 보임
    assert "5001" not in ords              # 원주문은 대체되어 사라짐


# ── 표시: refresh(미체결만 chegb=2), 종목군 필터 ──
def test_refresh_미체결만_chegb2():
    api = FakeApi(_bal(3), _oo())
    s = MarketDataStore(api, code_prefix="A756")
    s.refresh()
    assert api.chegb_used == ["2"]         # 체결(chegb=0) 안 가져옴 — 미체결만
    assert len(s.positions()) == 1
    assert len(s.open_orders()) == 1


def test_refresh_orders_미체결만_t0441안읽음():
    # 체결 통보마다 호출되는 경로 — 포지션(t0441)은 안 읽고 미체결(t0434)만 (accno 절감)
    api = FakeApi(_bal(3), _oo())
    s = MarketDataStore(api, code_prefix="A756")
    s.refresh_orders()
    assert api.balance_calls == 0         # t0441 안 읽음
    assert api.chegb_used == ["2"]        # 미체결(chegb=2)만
    assert len(s.open_orders()) == 1


def test_refresh_종목군_필터():
    api = FakeApi(
        [{"expcode": "A7567000", "jqty": "1", "medosu": "매수"}, {"expcode": "B1234000"}],
        [{"expcode": "A7567000", "ordrem": 2}, {"expcode": "C9999000", "ordrem": 2}],
    )
    s = MarketDataStore(api, code_prefix="A756")
    s.refresh()
    assert [r["expcode"] for r in s.positions()] == ["A7567000"]
    assert [r["expcode"] for r in s.open_orders()] == ["A7567000"]


# ── 체결: feed에서 읽음, C01 본문 → 체결 행 ──
def test_fills_feed에서_읽음():
    s = MarketDataStore(None, code_prefix="A756", feed=FakeFeed(fills=[{"ordno": "1"}]))
    assert s.fills() == [{"ordno": "1"}]


def test_fills_feed없으면_빈목록():
    assert MarketDataStore(None).fills() == []


def test_체결누적_매수매도_부호():
    rt = RealtimeFeed(code="A7567000", account=None)
    rt._record_fill({"ordno": "5", "expcode": "KR4A75670005",
                     "dosugb": "2", "cheprice": "1380.0"}, 3)    # 매수 +3
    rt._record_fill({"ordno": "6", "expcode": "KR4A75670005",
                     "dosugb": "1", "cheprice": "1381.0"}, -2)   # 매도 -2
    f = rt.fills()
    assert [r["medosu"] for r in f] == ["매수", "매도"]
    assert [r["cheqty"] for r in f] == [3, 2]                    # 절대값(수량)
    assert f[0]["status"] == "체결"

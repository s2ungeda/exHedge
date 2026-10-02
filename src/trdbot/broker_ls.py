"""LS 실거래 BrokerPort 구현 (executor의 가짜 브로커 자리에 끼우는 진짜 브로커).

executor는 BrokerPort(추상)만 알고, 여기서 lsapi로 실제 주문/조회를 한다.
필드명은 모의계좌 실측으로 확정(2026-06-18, 미국달러선물 A7567000):

  호가  t2112 → 'offerho1'/'bidho1' (문자열). USD선물 틱 0.10.
  포지션 t0441 → OutBlock1 행: expcode·medosu('매수'/'매도')·jqty → 부호 합산
  주문   CFOAT00100 → OutBlock2.OrdNo
  체결   t0434(chegb='0' 전체) → 행: ordno·cheqty(체결)·ordrem(잔량)

주간(FuturesAPI) 전용. 야간(NightFuturesAPI: t8457 호가 / CCENQ90200 잔고)은 필드가 달라
별도 어댑터가 필요 — 모의 야간장에서 필드 확인 후 추가 예정.
"""

from __future__ import annotations

import logging
from typing import Callable, Literal

from lsapi.exceptions import LSOrderError
from lsapi.futures import FuturesAPI, NightFuturesAPI, session_now
from lsapi.util import redact
from trdbot.datastore import MarketDataStore

logger = logging.getLogger(__name__)

Side = Literal["buy", "sell"]

# 미국달러선물 1계약 호가단위(실측). 호가 성길 때 폴백.
USD_FUTURES_TICK = 0.10


def _order_no(tr: str, resp: dict, side: str, qty: int, price: float, code: str) -> str:
    """주문 응답 → 주문번호. 응답 원문을 남기고, 주문번호가 없거나 0이면 거부로 본다.

    거부가 OutBlock을 동봉해 오면(야간 CCENT00100 02752 증거금부족 실측) client가 예외를
    내지 않는다. 그 응답의 무의미한 주문번호를 성공으로 받으면 존재하지 않는 주문을 미체결로
    들고 있다가 취소(02259 주문내용 없음)→재주문을 반복한다 → 여기서 LSOrderError로 끊어
    executor의 거부 처리(백오프·화면 표시)를 타게 한다.
    """
    out2 = resp.get(f"{tr}OutBlock2", {})
    ordno = out2.get("OrdNo")
    logger.info("주문응답 #%s %s %d@%s %s [%s %s] OutBlock2=%s",
                ordno, side, qty, price, code,
                resp.get("rsp_cd"), resp.get("rsp_msg"), redact(out2))
    try:
        valid = int(str(ordno).strip()) > 0
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise LSOrderError(f"[{resp.get('rsp_cd')}] {resp.get('rsp_msg') or '주문번호 없음'}",
                           code=resp.get("rsp_cd"), payload=redact(resp))
    return str(ordno).strip()


def _cancel(store: MarketDataStore, api, code: str, order_no: str) -> None:
    """미체결 잔량 취소(주간·야간 공통). 요청·응답·생략을 모두 로그로 남긴다."""
    found = store.find_open(order_no)
    if found is None:
        logger.info("취소 생략 #%s — 미체결·pending에 없음(이미 체결/취소/소멸)", order_no)
        return
    target = found["expcode"] or code
    logger.info("취소요청 #%s 잔%d %s", order_no, found["remaining"], target)
    resp = api.cancel_order(target, int(order_no), found["remaining"])
    logger.info("취소응답 #%s [%s %s]", order_no, resp.get("rsp_cd"), resp.get("rsp_msg"))
    store.drop_pending(order_no)          # pending이었으면 in-flight에서 제거
    store.invalidate_open_orders()        # 취소 → 미체결 변동


class LSDayFuturesBroker:
    """주간 선물 BrokerPort 구현 (FuturesAPI 래핑).

    code: 매매 대상(최근월) 단축코드. position_prefix: 포지션 합산 대상 종목군 접두(최근월+차근월).
    """

    def __init__(
        self,
        store: MarketDataStore,
        api: FuturesAPI,
        code: str,
        *,
        position_prefix: str | None = None,
        orderbook_source: Callable[[], dict] | None = None,
    ):
        # store: 중앙저장소(포지션·미체결의 단일 출처). api: 주문/호가(t2112)/취소.
        self._store = store
        self.api = api
        self.code = code
        self.position_prefix = position_prefix or code[:4]  # 'A756' = 미국달러선물
        # 실시간 호가(FH9) 캐시 getter. 비어있지 않으면 REST(t2112) 대신 사용.
        self._orderbook_source = orderbook_source

    # ── BrokerPort (포지션·미체결은 중앙저장소에 위임) ──
    def get_orderbook(self) -> dict:
        if self._orderbook_source is not None:
            ob = self._orderbook_source()
            if ob:  # 실시간 캐시가 차 있으면 그걸 사용 (REST 폴링 제거)
                return ob
        return self.api.get_orderbook(self.code).get("t2112OutBlock", {})

    def get_position(self) -> int:
        """순포지션 — 중앙저장소(t0441 시작값 + C01 증감 + 주기 재동기화)."""
        return self._store.net_position()

    def get_open_orders(self) -> list[dict]:
        """미체결 [{ordno, side, remaining, price}] — 중앙저장소(t0434 캐시)."""
        return self._store.open_orders_exec()

    def invalidate_open_orders(self) -> None:
        self._store.invalidate_open_orders()

    def resync_position(self) -> None:
        self._store.resync_position()

    def place_order(self, side: Side, qty: int, price: float) -> str:
        resp = self.api.place_order(self.code, qty, side=side, kind="limit", price=price)
        ordno = _order_no("CFOAT00100", resp, side, qty, price, self.code)
        # in-flight 등록(이중주문 방지) — expcode로 모니터 미체결 표시까지 채움
        self._store.note_placed(str(ordno), side, qty, price, expcode=self.code)
        self._store.invalidate_open_orders()                   # 새 주문 → 미체결 변동
        return str(ordno)

    def cancel(self, order_no: str) -> None:
        """미체결 잔량 취소. 캐시(t0434)뿐 아니라 **막 낸 주문(pending)**도 찾아 취소한다.

        막 낸 주문이 t0434에 아직 안 떠도 취소가 되어야 함 — 안 그러면 취소 누락으로 주문이 쌓인다.
        """
        _cancel(self._store, self.api, self.code, str(order_no))

    def cancel_all_unfilled(self) -> int:
        """이 종목군(position_prefix)의 미체결 주문을 전부 취소. 취소 건수 반환.

        기동 시 정리용 — 직전 실행이 미체결을 남긴 채 죽었을 때 유령 주문을 제거한다.
        """
        n = 0
        for r in self.api.get_unfilled_orders(chegb="2").get("t0434OutBlock1", []):
            if not str(r.get("expcode", "")).startswith(self.position_prefix):
                continue
            rem = int(r.get("ordrem", 0) or 0)
            if rem <= 0:
                continue
            self.api.cancel_order(r.get("expcode", self.code), int(r.get("ordno")), rem)
            n += 1
        return n


class LSNightFuturesBroker:
    """야간 선물 BrokerPort — **주간과 동일하게 중앙저장소에 위임**. 다른 건 야간 TR뿐:
    호가 t8457 / 주문 CCENT00100 / 취소 CCENT00300 / 통보 C02·O02·H02(store가 반영).

    포지션·미체결은 store(pending + C02/O02/H02 통보 + 야간잔고 재동기화)로 실시간 추적 →
    주간과 같은 상태기계로 안전(미체결 조회가 항상 정확해 다음 주문 판단이 옳다).
    """

    def __init__(
        self,
        store: MarketDataStore,
        api: NightFuturesAPI,
        code: str,
        *,
        position_prefix: str | None = None,
    ):
        self._store = store
        self.api = api
        self.code = code
        self.position_prefix = position_prefix or code[:4]

    def get_orderbook(self) -> dict:
        return self.api.get_orderbook(self.code).get("t8457OutBlock", {})

    def get_position(self) -> int:
        """순포지션 — 중앙저장소(야간잔고 CCENQ90200 시작값 + C02 증감 + 주기 재동기화)."""
        return self._store.net_position()

    def get_open_orders(self) -> list[dict]:
        """미체결 [{ordno, side, remaining, price}] — 중앙저장소(pending + C02/O02/H02)."""
        return self._store.open_orders_exec()

    def invalidate_open_orders(self) -> None:
        self._store.invalidate_open_orders()

    def resync_position(self) -> None:
        self._store.resync_position()

    def place_order(self, side: Side, qty: int, price: float) -> str:
        resp = self.api.place_order(self.code, qty, side=side, kind="limit", price=price)
        ordno = _order_no("CCENT00100", resp, side, qty, price, self.code)
        self._store.note_placed(str(ordno), side, qty, price, expcode=self.code)  # in-flight 등록
        self._store.invalidate_open_orders()
        return str(ordno)

    def cancel(self, order_no: str) -> None:
        """미체결 잔량 취소. 캐시·pending에서 찾아 취소(막 낸 야간주문도 취소 가능)."""
        _cancel(self._store, self.api, self.code, str(order_no))

    def cancel_all_unfilled(self) -> int:
        """이 종목군의 야간 미체결을 전부 취소(저장소 기준). 취소 건수 반환."""
        n = 0
        for o in self._store.open_orders_exec():
            self.cancel(str(o["ordno"]))
            n += 1
        return n


class SessionRoutingBroker:
    """현재 세션(주간/야간)에 맞는 어댑터로 위임. executor는 세션을 모른다.

    now_hhmm(): 현재 한국시각 HHMM 정수를 돌려주는 콜백(런처가 주입).
    세션 'closed'면 executor가 in_window 검사로 먼저 걸러야 하지만, 안전망으로 에러를 낸다.
    """

    def __init__(
        self,
        day: LSDayFuturesBroker,
        night: LSNightFuturesBroker,
        now_hhmm: Callable[[], int],
    ):
        self.day = day
        self.night = night
        self.now_hhmm = now_hhmm
        # 한 집행 사이클 동안 고정되는 활성 브로커. get_position()(사이클 시작)에서 재고정.
        self._pinned: LSDayFuturesBroker | LSNightFuturesBroker | None = None

    def session(self) -> str:
        """현재 시각 기준 라이브 세션(표시·조회용). 고정값이 아니라 항상 실시간 판정."""
        return session_now(self.now_hhmm())

    def _resolve(self):
        s = self.session()
        if s == "day":
            return self.day
        if s == "night":
            return self.night
        raise RuntimeError(f"주문가능시간 외(session={s}) — 활성 브로커 없음")

    def _active(self):
        """사이클 중 호출(주문/취소). 고정된 세션 브로커를 쓴다.

        경계(예: 15:45)에서 시각이 넘어가도, place_order 와 그 뒤 cancel 이
        서로 다른 세션으로 갈라져 주문이 방치되는 일을 막는다.
        """
        if self._pinned is None:
            self._pinned = self._resolve()
        return self._pinned

    def get_orderbook(self) -> dict:
        return self._active().get_orderbook()

    def get_position(self) -> int:
        # 집행 사이클(step)의 첫 호출 — 세션을 다시 고정한다.
        # 단 잔고 조회는 **장 마감에도 가능해야** 함(화면 표시). 마감/주간은 주간 브로커(t0441),
        # 야간만 야간 브로커로 라우팅(예외 안 던짐).
        self._pinned = self.night if self.session() == "night" else self.day
        return self._pinned.get_position()

    def place_order(self, side: Side, qty: int, price: float) -> str:
        return self._active().place_order(side, qty, price)

    def cancel(self, order_no: str) -> None:
        self._active().cancel(order_no)

    def get_open_orders(self) -> list[dict]:
        """현재 세션 브로커의 미체결 목록(reconcile용)."""
        active = self._active()
        getter = getattr(active, "get_open_orders", None)
        return getter() if getter else []

    def cancel_all_unfilled(self) -> int:
        """현재 세션 브로커의 미체결 전량 취소(기능 보유 — 기동 시 자동 호출은 안 함)."""
        return self._active().cancel_all_unfilled()

    def invalidate_open_orders(self) -> None:
        """미체결 캐시 무효화(C01 체결·수동조회 → 다음 조회 때 t0434 갱신). 양 세션 모두."""
        for b in (self.day, self.night):
            fn = getattr(b, "invalidate_open_orders", None)
            if fn:
                fn()

    def resync_position(self) -> None:
        """포지션 시작값 재동기화 표시(수동조회 → 다음 조회 때 t0441 갱신). 양 세션 모두."""
        for b in (self.day, self.night):
            fn = getattr(b, "resync_position", None)
            if fn:
                fn()

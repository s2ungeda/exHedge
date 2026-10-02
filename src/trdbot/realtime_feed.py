"""LS 실시간(WebSocket) 구독 — 호가(FH9) + 체결통보(C01) + 장운영(JIF).

⚠️ TR 주의 (LS 공식 답변 2026-06-23): **FH0/FC0는 폐지 → FH9/FC9로 대체.**
   우리는 FH9(주간 선물 호가)로 구독한다. (구 FH0를 쓰면 10002 "허용되지 않은 trcd" 거부)
   USD선물 호가가 FH9로 실제 전달되는지는 **장중 검증 필요**(장마감엔 데이터 없음).
   야간(18:00~06:00)은 DH0(호가)/DC0(체결) — 야간 검증 때 추가 예정.

연결(그룹별 별도 WebSocket):
  - 호가  : FH9 (tr_key=종목코드)        → 최신 호가 dict 캐시
  - 체결  : C01 (tr_key=계좌, tr_type=1) → 누적 부호체결수량 → 포지션(baseline+증감)
  - 장운영: JIF (tr_key="0")           → MarketStateTracker

별도 스레드 하나의 asyncio 루프에서 연결들을 asyncio.gather로 함께 돌린다.
executor/broker(다른 스레드)는 조회 메서드로 최신 상태를 읽는다.

페일세이프: 캐시가 비면(장마감·미수신) broker가 REST(t2112)로 폴백 → 매매 지장 없음.
스레드 안전: 상태는 WS 스레드만 갱신, 다른 스레드는 읽기만(참조 대입/단일 증감은 원자적).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable

from lsapi.auth import TokenManager
from lsapi.config import Settings, get_settings
from lsapi.market_state import (
    DOSUGB_BUY,
    JIF_TR_CD,
    JIF_TR_KEY,
    MarketStateTracker,
    parse_c01,
    parse_jif,
)
from lsapi.ws_client import LSWebSocket

logger = logging.getLogger(__name__)

# 주간 선물 호가 TR. 구 FH0는 폐지 → FH9 (LS 공식 답변 2026-06-23). KOSPI200선물호가 TR을
# USD선물 코드로 구독(공용). USD선물 실전달 여부는 장중 검증 필요.
FH9_DAY_ORDERBOOK = "FH9"
# 선물 체결통보 TR. 시세(tr_type=3)가 아니라 계좌등록(tr_type=1)으로 등록해야 한다(실측 확인).
# 계좌등록 1회로 C01(체결)·O01(접수)·H01(정정취소)가 모두 흘러온다(실측). C01만 체결 파싱,
# O01/H01은 변동 알림(미체결 목록 갱신)용으로만 받는다.
C01_EXEC_NOTICE = "C01"
O01_ACCEPT_NOTICE = "O01"
H01_MODIFY_NOTICE = "H01"
# KRX 야간파생(글로벌 거래) 통보 TR — 주간(C0/O0/H0 → 1)의 야간판(→ 2). LS 개발가이드 확인.
#   C02 야간체결 / O02 야간접수 / H02 야간정정취소.
# 필드가 주간과 같은지는 실야간에서 raw 캡처로 검증 후 확정한다(그때 야간 주문 잠금 해제).
C02_NIGHT_EXEC = "C02"
O02_NIGHT_ACCEPT = "O02"
H02_NIGHT_MODIFY = "H02"
_FILLS_CAP = 1000   # 체결 누적 상한(메모리 보호)


class RealtimeFeed:
    """호가(FH9) + 체결(C01) + 장운영(JIF) 실시간 구독을 백그라운드 스레드에서 운영한다."""

    def __init__(
        self,
        *,
        code: str,
        market: str = "futures",
        account: str | None = None,
        position_prefix: str | None = None,
        settings: Settings | None = None,
        on_event: Callable[[str], None] | None = None,
        on_fill: Callable[[str, int], None] | None = None,
        on_notice: Callable[[], None] | None = None,
        on_accept: Callable[[dict], None] | None = None,   # O01 접수 → 미체결 추가
        on_cancel: Callable[[str], None] | None = None,     # H01 정정취소 → 미체결 제거
        on_disconnect: Callable[[str], None] | None = None,  # 예기치 않은 WS 끊김(소켓명) → 알람
        token_manager: Any = None,   # REST와 공유하면 토큰 재발급이 WS에도 즉시 반영됨
    ):
        # code: 호가를 구독할 종목 단축코드(예: 'A7567000' 미국달러선물 근월).
        # account: 체결통보(C01) 계좌등록 키. 없으면 C01 미구독(포지션은 REST 유지).
        # position_prefix: 우리 종목군 접두('A756'). C01 expcode(표준코드)에 부분문자열로 포함됨.
        self.code = code
        self._settings = settings or get_settings()
        self._token_manager = token_manager
        self._market = market
        self._account = account
        self._prefix = position_prefix or code[:4]
        self._on_event = on_event
        self._on_fill = on_fill       # 체결(C01) 시 호출 → 미체결 캐시 무효화
        self._on_notice = on_notice   # 계좌 변동(C01/O01/H01) 시 → 중앙저장소 갱신
        self._on_accept = on_accept   # O01 접수 → 미체결 추가
        self._on_cancel = on_cancel   # H01 정정취소 → 미체결 제거
        self._on_ws_disconnect = on_disconnect   # 예기치 않은 끊김 → 상위(app) 알람
        self._fills: list[dict] = []  # 체결 누적(실행 이후) — 중앙저장소/모니터용
        self._fills_lock = threading.Lock()
        self.tracker = MarketStateTracker()
        self._day_ob: dict = {}  # FH9 최신 호가(통째로 교체)
        self._c01_net = 0        # C01 누적 부호체결수량(우리 종목)
        self._c01_seen = False   # C01 한 건이라도 받았는가
        self._raw_seen: set[str] = set()  # 통보 TR별 원본 1회 로그(야간 필드 검증용)
        self._wss: list[LSWebSocket] = []  # 그룹별 연결들
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ── 조회 (집행/메인 스레드에서 호출) ──────────────
    def tradeable(self, jangubun: str) -> bool:
        return self.tracker.tradeable(jangubun)

    def seen(self, jangubun: str) -> bool:
        return self.tracker.seen(jangubun)

    def phase(self, jangubun: str):
        """해당 시장의 현재 국면(Phase). 화면 장운영 상태 표시용."""
        return self.tracker.phase(jangubun)

    def day_orderbook(self) -> dict:
        """최신 주간 호가(FH9). 아직 못 받았으면 빈 dict → 호출측이 REST 폴백."""
        return self._day_ob

    def c01_state(self) -> tuple[bool, int]:
        """(C01 활성여부, 누적 부호체결수량). 브로커가 baseline+delta로 포지션 계산."""
        return self._c01_seen, self._c01_net

    def fills(self) -> list[dict]:
        """실행 이후 누적된 체결 목록(C01). 모니터/중앙저장소가 읽기만 한다."""
        with self._fills_lock:
            return list(self._fills)

    # ── 수신 핸들러 (WS 스레드) ──────────────────────
    def _notify_ws_disconnect(self, name: str) -> None:
        """예기치 않은 WS 끊김 → 상위(app) 알람 콜백. (의도된 재연결은 ws_client에서 제외됨)"""
        if self._on_ws_disconnect:
            try:
                self._on_ws_disconnect(name)
            except Exception:
                logger.exception("on_ws_disconnect 예외")

    def _on_jif_disconnect(self) -> None:
        """장운영(JIF) WS 끊김 → 저장된 국면 리셋. 다음 JIF 이벤트 전까지 시각 폴백에 맡긴다.

        (WS 스레드에서 호출 — 트래커는 이 스레드만 갱신하므로 안전.)
        """
        self.tracker.reset()
        logger.warning("장운영(JIF) 끊김 — 국면 리셋(시각 폴백으로 전환, 다음 JIF까지)")
        if self._on_event:
            try:
                self._on_event("장운영 연결 끊김 — 시각 기준으로 전환")
            except Exception:
                logger.exception("JIF disconnect on_event 예외")
        self._notify_ws_disconnect("장운영")   # 알람도

    async def _on_jif(self, msg: dict) -> None:
        # 수신 자체를 항상 남긴다(원본, DEBUG) — "받았는지" 여부를 로그로 확실히 구분하기 위함.
        body = msg.get("body")
        logger.debug("JIF 수신(원본): %s", body if body is not None else msg)
        parsed = parse_jif(msg)
        if parsed is None:
            # 수신은 됐으나 처리 못함(필드 누락 등) → 과거엔 조용히 버려 사각지대였다. 이제 경고로 남긴다.
            # (body 없는 구독응답은 정상 노이즈라 DEBUG만)
            if body:
                logger.warning("JIF 수신했으나 처리 못함(파싱 실패): %s", body)
            return
        jangubun, jstatus = parsed
        phase = self.tracker.on_jif(jangubun, jstatus)
        logger.info("JIF 장상태: jangubun=%s jstatus=%s → %s", jangubun, jstatus, phase.value)
        if self._on_event:
            try:
                self._on_event(f"장운영 {jangubun}/{jstatus} → {phase.value}")
            except Exception:
                logger.exception("JIF on_event 예외")

    async def _on_quote(self, msg: dict) -> None:
        body = msg.get("body")
        if body:  # 구독 응답(body=None)은 무시, 실제 호가만 캐시
            self._day_ob = body

    def _capture_raw(self, msg: dict) -> None:
        """통보 TR별 첫 원본 body를 1회 INFO 로그 — 야간 통보(C02/O02/H02) 필드 검증용."""
        tr = str(msg.get("header", {}).get("tr_cd") or "")
        body = msg.get("body")
        if tr and body and tr not in self._raw_seen:
            self._raw_seen.add(tr)
            logger.info("[통보원본 %s] %s", tr, body)

    async def _on_c01(self, msg: dict) -> None:
        self._capture_raw(msg)   # C01/C02 첫 건 원본 캡처(야간 필드 확정용)
        parsed = parse_c01(msg)
        if parsed is None:
            return  # 구독 응답(body=None)/접수통보 등, 또는 야간(C02) 필드 불일치 → raw로 확인
        expcode, signed = parsed
        if self._prefix not in expcode:  # 우리 종목군만(표준코드에 단축코드 접두 포함)
            return
        body = msg.get("body") or {}
        self._c01_net += signed
        self._c01_seen = True
        self._record_fill(body, signed)  # 체결 누적(중앙저장소용)
        logger.info("C01 체결: #%s %s %+d @%s → 누적 net=%d", body.get("ordno"), expcode, signed,
                    body.get("cheprice"), self._c01_net)
        if self._on_fill:
            try:
                self._on_fill(str(body.get("ordno", "")), abs(signed))  # 미체결 즉시 차감
            except Exception:
                logger.exception("C01 on_fill 예외")
        self._notify()            # 중앙저장소 갱신(미체결/체결 변동)
        if self._on_event:
            try:
                self._on_event(f"체결 {signed:+d} (net {self._c01_net})")
            except Exception:
                logger.exception("C01 on_event 예외")

    async def _on_account_event(self, msg: dict) -> None:
        """접수(O01/O02)→미체결 추가, 정정취소(H01/H02)→미체결 제거. 둘 다 변동 알림(_notify).

        야간(O02/H02)은 주간(O01/H01)과 필드 동일 가정 — raw 캡처로 검증 후 확정.
        """
        self._capture_raw(msg)   # O01/O02/H01/H02 첫 건 원본 캡처(야간 필드 확정용)
        body = msg.get("body")
        if not body:
            return
        tr = str(msg.get("header", {}).get("tr_cd") or "")
        if tr in (O01_ACCEPT_NOTICE, O02_NIGHT_ACCEPT):
            rec = self._parse_accept(body)
            if rec is not None:
                logger.info("%s 접수: #%s %s 잔%d @%s%s", tr, rec["ordno"], rec["medosu"],
                            rec["ordrem"], rec["price"],
                            f" (정정 원주문 #{rec['orgordno']})" if rec["orgordno"] else "")
            if rec is not None and self._on_accept:
                try:
                    self._on_accept(rec)
                except Exception:
                    logger.exception("O01 on_accept 예외")
        elif tr in (H01_MODIFY_NOTICE, H02_NIGHT_MODIFY):
            org = self._cancel_ordno(body)
            logger.info("%s 정정취소: 원주문 #%s", tr, org)
            if org.strip().lstrip("0") and self._on_cancel:
                try:
                    self._on_cancel(org)
                except Exception:
                    logger.exception("H01/H02 on_cancel 예외")
        self._notify()

    @staticmethod
    def _cancel_ordno(body: dict) -> str:
        """정정취소 통보에서 '취소된 원주문번호'. 주간(H01)=ordordno, 야간(H02)=orgordno."""
        return str(body.get("ordordno") or body.get("orgordno") or "")

    def _parse_accept(self, body: dict) -> dict | None:
        """O01 접수 본문 → 미체결 행(t0434 형식). 우리 종목·잔량(unercqty)>0만.

        신규(orgordno=0): 그 주문 추가. 정정(orgordno=원주문, 잔량>0): 원주문을 대체
        → orgordno를 함께 실어 보내 교체 처리(원주문 제거+새 주문 추가). 순수 취소(잔량=0)는
        잔량 필터로 제외. 실측 필드: fnoIsuno(shcode)·bnstp(2=매수)·unercqty·ordprc·ordno·orgordno.
        """
        code = str(body.get("fnoIsuno", "")).strip()
        if not code.startswith(self._prefix):   # 우리 종목군만(shcode 접두)
            return None
        try:
            rem = int(body.get("unercqty", 0) or 0)
        except (TypeError, ValueError):
            return None
        if rem <= 0:   # 잔량 없음(취소 등) → 살아있는 주문 아님 → 추가 안 함
            return None
        return {
            "ordno": str(body.get("ordno", "")).strip(),
            "orgordno": str(body.get("orgordno", "")).strip().lstrip("0"),  # 정정 원주문(없으면 "")
            "expcode": code,
            "medosu": "매수" if str(body.get("bnstp")) == DOSUGB_BUY else "매도",
            "ordrem": rem,
            "price": body.get("ordprc", ""),
        }

    def _record_fill(self, body: dict, signed: int) -> None:
        """C01 본문 → 체결 행 한 건 누적. 표시 컬럼에 맞춰 구성."""
        rec = {
            "ordno": str(body.get("ordno", "")),
            "expcode": str(body.get("expcode", "")),
            "medosu": "매수" if str(body.get("dosugb")) == DOSUGB_BUY else "매도",
            "qty": abs(signed), "cheqty": abs(signed), "ordrem": 0,
            "price": body.get("cheprice", ""), "status": "체결",
            "ordtime": str(body.get("chetime") or body.get("ordtime") or ""),
            "_ts": time.time(),   # 수신 시각(epoch) — 최근 N분 체결량 계산용
        }
        with self._fills_lock:
            self._fills.append(rec)
            if len(self._fills) > _FILLS_CAP:
                del self._fills[:-_FILLS_CAP]

    def _notify(self) -> None:
        if self._on_notice:
            try:
                self._on_notice()
            except Exception:
                logger.exception("on_notice 예외")

    # ── 수명주기 ─────────────────────────────────────
    def _build_connections(self) -> list[LSWebSocket]:
        """그룹별 별도 연결 구성 — 섞으면 거부되므로 시세/체결/장운영을 각각 분리."""
        # REST와 같은 TokenManager를 공유(주면) — 토큰 재발급이 WS 재구독에도 즉시 반영.
        token_mgr = self._token_manager or TokenManager(self._settings, market=self._market)
        wss: list[LSWebSocket] = []

        ws_quote = LSWebSocket(self._settings, token_mgr, name="시세",   # 호가(FH9)
                               on_disconnect=lambda: self._notify_ws_disconnect("시세"))
        ws_quote.on(FH9_DAY_ORDERBOOK, self._on_quote)
        ws_quote.subscribe(FH9_DAY_ORDERBOOK, self.code)
        wss.append(ws_quote)

        # JIF는 엣지 트리거라, 끊기면 직전 국면이 stale로 남는다 → 끊김 시 트래커 리셋(시각 폴백 전환).
        ws_jif = LSWebSocket(self._settings, token_mgr, name="장운영",
                             on_disconnect=self._on_jif_disconnect)  # JIF — 다른 그룹
        ws_jif.on(JIF_TR_CD, self._on_jif)
        ws_jif.subscribe(JIF_TR_CD, JIF_TR_KEY)
        wss.append(ws_jif)

        if self._account:
            ws_exec = LSWebSocket(self._settings, token_mgr, name="체결",   # 체결통보 — 계좌등록(1)
                                  on_disconnect=lambda: self._notify_ws_disconnect("체결"))
            ws_exec.on(C01_EXEC_NOTICE, self._on_c01)
            ws_exec.on(O01_ACCEPT_NOTICE, self._on_account_event)   # 접수 → 변동 알림
            ws_exec.on(H01_MODIFY_NOTICE, self._on_account_event)   # 정정취소 → 변동 알림
            ws_exec.subscribe(C01_EXEC_NOTICE, self._account, tr_type="1")
            # 야간파생 통보(C02 체결/O02 접수/H02 정정취소) — 주간과 별도 TR.
            # 핸들러 등록 + C02 계좌등록. 필드는 주간 동일 가정 + 첫 건 raw 로그로 검증.
            ws_exec.on(C02_NIGHT_EXEC, self._on_c01)
            ws_exec.on(O02_NIGHT_ACCEPT, self._on_account_event)
            ws_exec.on(H02_NIGHT_MODIFY, self._on_account_event)
            ws_exec.subscribe(C02_NIGHT_EXEC, self._account, tr_type="1")
            wss.append(ws_exec)
        return wss

    def start(self) -> None:
        if self._thread is not None:
            return
        self._wss = self._build_connections()

        def runner() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            try:
                # 세 연결을 한 루프에서 함께 — 하나가 끝나도 나머지는 stop까지 유지.
                loop.run_until_complete(
                    asyncio.gather(*(w.run() for w in self._wss), return_exceptions=True)
                )
            except Exception:
                logger.exception("실시간 WS 루프 종료(예외)")
            finally:
                # 남은 정리 태스크(소켓 close 등)를 끝까지 실행해 서버 구독을 확실히 해제.
                try:
                    pending = asyncio.all_tasks(loop)
                    for tsk in pending:
                        tsk.cancel()
                    if pending:
                        loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True)
                        )
                except Exception:
                    logger.debug("WS 정리 태스크 드레인 중 예외(무시)")
                loop.close()

        self._thread = threading.Thread(target=runner, name="realtime-feed", daemon=True)
        self._thread.start()
        logger.info(
            "실시간 구독 시작 (연결 분리: FH9 + JIF%s, code=%s, market=%s)",
            " + C01/C02(야간)" if self._account else "",
            self.code,
            self._market,
        )

    def reconnect(self) -> None:
        """모든 WS 연결을 끊고 즉시 재연결·재구독 — 토큰 갱신 후 새 토큰으로 재등록시킬 때.

        TokenManager를 REST와 공유하고 그쪽에서 force_refresh 해두면, 재구독이 새 토큰을 쓴다.
        WS 스레드 루프에 안전하게 위임(각 ws의 recv 루프가 빠져나와 재연결).
        """
        loop, wss = self._loop, list(self._wss)
        if loop is None:
            return
        for ws in wss:
            try:
                loop.call_soon_threadsafe(ws.request_reconnect)
            except Exception:
                logger.debug("WS 재연결 위임 실패(이미 종료된 루프 가능)")

    def stop(self, *, timeout: float = 5.0) -> None:
        """모든 연결 종료. WS 스레드 루프에 안전하게 위임(각 ws가 구독해제 후 graceful close)."""
        loop, wss, t = self._loop, list(self._wss), self._thread
        self._wss = []
        self._thread = self._loop = None
        if loop is not None:
            for ws in wss:
                try:
                    loop.call_soon_threadsafe(ws.stop)
                except Exception:
                    logger.debug("실시간 stop 위임 실패(이미 종료된 루프 가능)")
        if t is not None and t.is_alive():
            t.join(timeout)
        logger.info("실시간 구독 중지")

"""LS OpenAPI 실시간(WebSocket) 시세 클라이언트.

LS WebSocket은 access_token으로 인증한 뒤 tr_type=3(등록)/4(해제)로 구독한다.
실시간 TR 예시:
    - S3_ : KOSPI 체결 (예: tr_cd="S3_", body={"shcode":"005930"})
    - H1_ : KOSPI 호가
    - K3_ : KOSDAQ 체결
    - FC0 : 선물 체결
정확한 TR 코드는 공식 문서 기준으로 확인.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

from lsapi.auth import TokenManager
from lsapi.config import Settings, get_settings

logger = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[None]]


class LSWebSocket:
    def __init__(
        self,
        settings: Settings | None = None,
        token_manager: TokenManager | None = None,
        *,
        name: str = "WS",
        on_disconnect: Callable[[], None] | None = None,
    ):
        self.settings = settings or get_settings()
        self.tokens = token_manager or TokenManager(self.settings)
        self._name = name  # 로그 식별용(시세/장운영/체결) — 어느 소켓이 끊겼는지 구분
        # 연결이 끊길 때(끊김·강제재연결) 1회 호출 — 예: 장운영 소켓이 끊기면 stale 국면 리셋.
        self._on_disconnect = on_disconnect

        self._subscriptions: list[tuple[str, str, str]] = []  # (tr_type, tr_cd, key)
        self._handlers: dict[str, Handler] = {}
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._running = False
        self._stop_evt: asyncio.Event | None = None  # run()에서 생성, stop()이 set
        self._reconnect_evt: asyncio.Event | None = None  # 재연결(재구독) 요청 — 토큰 갱신 시

    def on(self, tr_cd: str, handler: Handler) -> None:
        """tr_cd 별 메시지 핸들러 등록."""
        self._handlers[tr_cd] = handler

    def subscribe(self, tr_cd: str, key: str, *, tr_type: str = "3") -> None:
        """구독 추가. key는 종목코드 등 식별자.

        tr_type: "3" 실시간 시세 등록(기본). "1" 계좌등록 — 체결통보(C01 등)는 1을 써야 한다.
        """
        self._subscriptions.append((tr_type, tr_cd, key))

    async def _send(self, tr_type: str, tr_cd: str, key: str) -> None:
        assert self._ws is not None
        msg = {
            "header": {
                "token": self.tokens.get_token(),
                "tr_type": tr_type,  # "1" 시스템, "3" 등록, "4" 해제
            },
            "body": {"tr_cd": tr_cd, "tr_key": key},
        }
        await self._ws.send(json.dumps(msg))

    async def _register_all(self) -> None:
        for tr_type, tr_cd, key in self._subscriptions:
            await self._send(tr_type, tr_cd, key)
            logger.info("실시간 구독: tr_type=%s tr_cd=%s key=%s", tr_type, tr_cd, key)

    async def _unregister_all(self) -> None:
        """정상 종료 시 구독 해제를 서버에 통지(시세 3→4, 계좌 1→2) → 유령 구독 방지."""
        off_map = {"3": "4", "1": "2"}
        for tr_type, tr_cd, key in self._subscriptions:
            off = off_map.get(tr_type)
            if off and self._ws is not None:
                try:
                    await self._send(off, tr_cd, key)
                    logger.info("실시간 해제: tr_type=%s tr_cd=%s key=%s", off, tr_cd, key)
                except Exception:
                    logger.debug("구독 해제 전송 실패(무시) tr_cd=%s", tr_cd)

    async def run(self) -> None:
        """이벤트 루프 진입. stop() 호출 시 구독 해제 후 graceful close."""
        self._running = True
        self._stop_evt = asyncio.Event()
        self._reconnect_evt = asyncio.Event()
        attempt = 0  # 0=최초 연결, >0=재연결 — 재연결 성공을 명확히 로그로 남기기 위함
        while self._running:
            forced = False
            connected = False
            try:
                # async with: 정상 이탈 시 close 프레임을 보내 서버가 구독을 즉시 해제하게 함.
                async with websockets.connect(
                    self.settings.ws_base_url, ping_interval=20
                ) as ws:
                    self._ws = ws
                    connected = True
                    await self._register_all()
                    if attempt > 0:
                        logger.info("[%s] WS 재연결됨 — 구독 재등록 완료(%d회째 시도)",
                                    self._name, attempt)
                    attempt = 0
                    await self._recv_until_stop(ws)
                    if self._reconnect_evt.is_set():
                        forced = True                 # 의도된 재연결(토큰 갱신 등) → 즉시 재구독
                        self._reconnect_evt.clear()
                        logger.info("[%s] WS 재연결(토큰 갱신) — 재구독", self._name)
                    elif not self._running:
                        await self._unregister_all()  # 정상 종료 → 명시적 해제
            except websockets.ConnectionClosed as e:
                logger.warning("[%s] WS 연결 종료, 재연결 시도: %s", self._name, e)
            except Exception:
                logger.exception("[%s] WS 루프 오류, 재연결 시도", self._name)
            finally:
                self._ws = None
                # 예기치 않은 끊김일 때만 콜백(예: stale 국면 리셋). **의도된 재연결(forced,
                # 토큰갱신·주간장 시작)은 제외** — 시장상태는 그대로라 방금 받은 JIF 국면을 버리면 안 됨.
                # (실측 버그: 08:45 JIF continuous 수신 직후 주간장-시작 재연결이 그 국면을 리셋 → 폴백)
                if connected and self._running and not forced and self._on_disconnect is not None:
                    try:
                        self._on_disconnect()
                    except Exception:
                        logger.exception("[%s] on_disconnect 콜백 예외", self._name)
            if not self._running:
                break
            if forced:
                continue                              # 백오프 없이 즉시 재연결(재구독)
            attempt += 1
            # 점진 백오프(2,4,…최대 30초) — 장기 단절 시 재시도·로그 폭주 방지.
            await asyncio.sleep(min(attempt * 2, 30))

    async def _recv_until_stop(self, ws) -> None:
        """stop·재연결 신호와 recv를 레이스 — recv를 강제 취소하지 않고 깔끔히 빠져나온다."""
        assert self._stop_evt is not None and self._reconnect_evt is not None
        stop_wait = asyncio.ensure_future(self._stop_evt.wait())
        recon_wait = asyncio.ensure_future(self._reconnect_evt.wait())
        try:
            while self._running:
                recv = asyncio.ensure_future(ws.recv())
                done, _ = await asyncio.wait(
                    {recv, stop_wait, recon_wait}, return_when=asyncio.FIRST_COMPLETED)
                if recv in done:
                    try:
                        raw = recv.result()
                    except websockets.ConnectionClosed:
                        return
                    await self._dispatch(raw)
                else:  # stop 또는 재연결 신호 → 진행 중 recv 정리 후 종료(호출측이 판단)
                    recv.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await recv
                    return
        finally:
            for w in (stop_wait, recon_wait):
                if not w.done():
                    w.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await w

    async def _dispatch(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("JSON 파싱 실패: %r", raw[:200])
            return

        tr_cd = msg.get("header", {}).get("tr_cd") or msg.get("trcode")
        handler = self._handlers.get(tr_cd) if tr_cd else None
        if handler:
            try:
                await handler(msg)
            except Exception:
                logger.exception("핸들러 예외 (tr_cd=%s)", tr_cd)
        else:
            logger.debug("핸들러 없음 (tr_cd=%s): %s", tr_cd, msg)

    def stop(self) -> None:
        """종료 요청. WS 이벤트 루프 스레드에서 호출해야 한다
        (RealtimeFeed가 loop.call_soon_threadsafe(ws.stop)로 위임).
        recv 루프가 즉시 빠져나와 구독 해제 → graceful close 순으로 닫힌다.
        """
        self._running = False
        if self._stop_evt is not None:
            self._stop_evt.set()

    def request_reconnect(self) -> None:
        """현재 연결을 끊고 즉시 재연결·재구독. 토큰 갱신 후 새 토큰으로 재등록시킬 때 쓴다.
        WS 이벤트 루프 스레드에서 호출(RealtimeFeed가 loop.call_soon_threadsafe로 위임).
        """
        if self._reconnect_evt is not None:
            self._reconnect_evt.set()

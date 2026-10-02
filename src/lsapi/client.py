import logging
import time
from typing import Any, Literal

import httpx

from lsapi.auth import TokenManager
from lsapi.config import Settings, get_settings
from lsapi.exceptions import LSAPIError, LSAuthError, LSRateLimitError

logger = logging.getLogger(__name__)

# LS 게이트웨이 레이트리밋 회피 — 호출 간 최소 간격(초).
# IGW00201 자주 보이면 늘리고, 여유 있으면 줄여도 됨.
_MIN_CALL_INTERVAL = 0.3

# 레이트리밋(IGW00201 / HTTP 429) 시 재시도 횟수와 백오프(초).
_RATE_LIMIT_RETRIES = 3
_RATE_LIMIT_BACKOFF = 0.7  # attempt마다 *배수
_RATE_LIMIT_CODE = "IGW00201"


class LSClient:
    """LS OpenAPI REST 호출 공통 클라이언트.

    LS OpenAPI는 endpoint(path)와 tr_cd(헤더) 조합으로 요청을 식별한다.
    각 도메인 모듈(stock, futures)이 이 클라이언트의 `call()`을 호출한다.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        token_manager: TokenManager | None = None,
        market: Literal["stock", "futures"] = "stock",
        timeout: float = 10.0,
    ):
        # market: 어느 계좌(appkey)로 인증할지. 주식=stock, 선물=futures.
        self.settings = settings or get_settings()
        self.market = market
        self.tokens = token_manager or TokenManager(self.settings, market=market)
        self._http = httpx.Client(base_url=self.settings.rest_base_url, timeout=timeout)
        self._last_call_at = 0.0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "LSClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def call(
        self,
        path: str,
        tr_cd: str,
        body: dict[str, Any],
        *,
        tr_cont: str = "N",
        tr_cont_key: str = "",
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """LS OpenAPI 요청.

        - path: 엔드포인트 경로 (예: "/stock/market-data")
        - tr_cd: TR 코드 (예: "t1102")
        - body: TR별 InBlock 페이로드 (LS 표준: {f"{tr_cd}InBlock": {...}})
        - tr_cont: 연속조회 여부 ("N" 또는 "Y")
        - tr_cont_key: 연속조회 키
        """
        headers = {
            **self.tokens.auth_header(),
            "content-type": "application/json; charset=utf-8",
            "tr_cd": tr_cd,
            "tr_cont": tr_cont,
            "tr_cont_key": tr_cont_key,
        }
        if extra_headers:
            headers.update(extra_headers)

        refreshed_auth = False   # 토큰 강제 재발급은 요청당 1회만(무한루프 방지)
        for attempt in range(_RATE_LIMIT_RETRIES + 1):
            self._throttle()
            try:
                resp = self._http.post(path, headers=headers, json=body)
            except httpx.HTTPError as e:
                raise LSAPIError(f"HTTP 오류 ({tr_cd}): {e}") from e

            if resp.status_code == 401 and not refreshed_auth:
                # 토큰 만료 가능성 — 1회 강제 재발급 후 재시도
                logger.info("401 수신, 토큰 강제 재발급 후 재시도 (tr_cd=%s)", tr_cd)
                refreshed_auth = True
                headers["authorization"] = f"Bearer {self.tokens.get_token(force_refresh=True)}"
                continue

            payload = _safe_json(resp)
            if _is_rate_limited(resp, payload):
                if attempt < _RATE_LIMIT_RETRIES:
                    wait = _RATE_LIMIT_BACKOFF * (attempt + 1)
                    logger.warning("레이트리밋(%s) — %.1f초 후 재시도 %d/%d",
                                   tr_cd, wait, attempt + 1, _RATE_LIMIT_RETRIES)
                    time.sleep(wait)
                    continue
                raise LSRateLimitError(f"요청 한도 초과 재시도 실패 ({tr_cd})", payload=payload)

            # 무효 토큰(IGW00121 등)은 HTTP 500 body에 실려 오기도 한다 → 강제 재발급 후 1회 재시도.
            if str(payload.get("rsp_cd", "")).startswith("IGW") and not refreshed_auth:
                logger.info("인증오류 %s — 토큰 강제 재발급 후 재시도 (tr_cd=%s)",
                            payload.get("rsp_cd"), tr_cd)
                refreshed_auth = True
                headers["authorization"] = f"Bearer {self.tokens.get_token(force_refresh=True)}"
                continue

            if resp.status_code >= 400:
                raise LSAPIError(
                    f"{tr_cd} 호출 실패 (HTTP {resp.status_code}) body={payload}",
                    code=str(resp.status_code),
                    payload=payload,
                )
            break

        data = resp.json()
        # LS 응답코드 규약(모의 실측):
        #  - "00000" = 조회류 정상
        #  - 주문류는 비-00000 성공코드를 쓴다. 예: 00040(매수접수)/00156(취소완료).
        #    이때 응답에 *OutBlock* 데이터가 동봉된다.
        #  - 실패는 rsp_cd/rsp_msg만 오고 OutBlock이 없다. 예: 01407(매매불가).
        # 따라서 "비-00000 && OutBlock 없음"만 에러로 raise한다.
        rsp_cd = data.get("rsp_cd")
        if rsp_cd and rsp_cd != "00000":
            msg = data.get("rsp_msg", "응답 코드 비정상")
            if rsp_cd.startswith("IGW"):
                raise LSAuthError(f"인증 오류 [{rsp_cd}] {msg}", code=rsp_cd, payload=data)
            has_block = any("OutBlock" in k for k in data)
            if not has_block:
                raise LSAPIError(f"[{rsp_cd}] {msg}", code=rsp_cd, payload=data)
            logger.info("응답코드 %s: %s (tr_cd=%s)", rsp_cd, msg, tr_cd)

        return data

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call_at
        if elapsed < _MIN_CALL_INTERVAL:
            time.sleep(_MIN_CALL_INTERVAL - elapsed)
        self._last_call_at = time.monotonic()


def _is_rate_limited(resp: httpx.Response, payload: dict) -> bool:
    """레이트리밋 여부. HTTP 429 또는 응답코드 IGW00201(호출 거래건수 초과)."""
    if resp.status_code == 429:
        return True
    return payload.get("rsp_cd") == _RATE_LIMIT_CODE


def _safe_json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except Exception:
        return {"raw": resp.text[:500]}

import json
import logging
import time
from dataclasses import dataclass
from typing import Literal

import httpx

from lsapi.config import Settings, get_settings
from lsapi.exceptions import LSAuthError

Market = Literal["stock", "futures"]

logger = logging.getLogger(__name__)

# 토큰 만료 30초 전에 미리 재발급 — 경계 케이스 회피.
_REFRESH_MARGIN_SEC = 30


@dataclass
class Token:
    access_token: str
    token_type: str
    issued_at: float
    expires_in: int  # seconds

    @property
    def expires_at(self) -> float:
        return self.issued_at + self.expires_in

    def is_expired(self) -> bool:
        return time.time() >= (self.expires_at - _REFRESH_MARGIN_SEC)

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "token_type": self.token_type,
            "issued_at": self.issued_at,
            "expires_in": self.expires_in,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Token":
        return cls(**data)


class TokenManager:
    """OAuth2 토큰 발급 + 디스크 캐시 + 자동 갱신.

    market별로 appkey가 다르므로(주식/선물 계좌 분리) 토큰도 market별로 관리한다.
    """

    def __init__(self, settings: Settings | None = None, market: Market = "stock"):
        self.settings = settings or get_settings()
        self.market = market
        self._token: Token | None = self._load_cached()

    def _load_cached(self) -> Token | None:
        path = self.settings.token_cache_path(self.market)
        if not path.exists():
            return None
        try:
            return Token.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            logger.warning("토큰 캐시 손상, 새로 발급합니다: %s", e)
            return None

    def _save_cached(self, token: Token) -> None:
        self.settings.token_cache_path(self.market).write_text(
            json.dumps(token.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _issue(self) -> Token:
        url = f"{self.settings.rest_base_url}/oauth2/token"
        app_key, app_secret = self.settings.credentials(self.market)
        data = {
            "grant_type": "client_credentials",
            "appkey": app_key,
            "appsecretkey": app_secret,
            "scope": "oob",
        }
        try:
            resp = httpx.post(
                url,
                data=data,
                headers={"content-type": "application/x-www-form-urlencoded"},
                timeout=10.0,
            )
        except httpx.HTTPError as e:
            raise LSAuthError(f"토큰 발급 네트워크 오류: {e}") from e

        if resp.status_code != 200:
            payload = _safe_json(resp)
            raise LSAuthError(
                f"토큰 발급 실패 (HTTP {resp.status_code}) body={payload}",
                payload=payload,
            )

        body = resp.json()
        token = Token(
            access_token=body["access_token"],
            token_type=body.get("token_type", "Bearer"),
            issued_at=time.time(),
            expires_in=int(body.get("expires_in", 86400)),
        )
        self._save_cached(token)
        logger.info(
            "토큰 발급 성공 (만료까지 %d초, env=%s, market=%s)",
            token.expires_in,
            self.settings.ls_env,
            self.market,
        )
        return token

    def get_token(self, *, force_refresh: bool = False) -> str:
        if force_refresh or self._token is None or self._token.is_expired():
            self._token = self._issue()
        return self._token.access_token

    def auth_header(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.get_token()}"}


def _safe_json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except Exception:
        return {"raw": resp.text[:500]}

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from lsapi import credentials as creds
from lsapi.exceptions import LSAuthError
from lsapi.paths import app_base_dir

# 소스 실행이면 프로젝트 루트, 배포(.exe)면 실행파일 폴더 — .env/tokens 를 그 아래서 찾는다.
PROJECT_ROOT = app_base_dir()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 비밀키는 더 이상 .env 필수 항목이 아님. keyring(Windows 자격 증명 관리자) 우선.
    # 호환을 위해 환경변수로 넣으면 fallback으로 동작은 한다.
    ls_app_key: str = Field("", description="(폴백) LS OpenAPI appkey — 기본은 keyring에서 조회")
    ls_app_secret: str = Field("", description="(폴백) appsecret — 기본은 keyring에서 조회")

    ls_env: Literal["prod", "mock"] = Field("mock", description="실거래 / 모의투자")
    # 계좌 — 주식과 파생이 분리되는 경우 각각 설정. 단일 계좌라면 ls_account_no만 채워도 됨.
    ls_account_no: str = Field("", description="(폴백) 단일 계좌번호")
    ls_stock_account_no: str = Field("", description="주식/ETF 계좌번호")
    ls_futures_account_no: str = Field("", description="선물옵션(파생) 계좌번호")
    ls_account_product_code: str = Field("01", description="상품코드 (주식 01)")
    # 시그널 발신처 구분 토큰. 설정 시 이 토큰과 일치하는 시그널만 처리(다른 발신처 거부).
    signal_token: str = Field("", description="시그널 처리 토큰 (SIGNAL_TOKEN). 예: Dalin / Meme")
    log_level: str = Field("INFO")

    @property
    def rest_base_url(self) -> str:
        return "https://openapi.ls-sec.co.kr:8080"

    @property
    def ws_base_url(self) -> str:
        # 실시간(WebSocket) 포트는 환경별로 다르다: 실거래 9443 / 모의투자 29443.
        # (REST :8080은 모의/실거래 공용이지만 WS는 포트가 갈린다.)
        port = 29443 if self.ls_env == "mock" else 9443
        return f"wss://openapi.ls-sec.co.kr:{port}/websocket"

    def token_cache_path(self, market: Literal["stock", "futures"] = "stock") -> Path:
        # 시장별 appkey가 다르면 토큰도 다르므로 캐시 파일을 시장별로 분리한다.
        cache_dir = PROJECT_ROOT / "tokens"
        cache_dir.mkdir(exist_ok=True)
        return cache_dir / f"access_token_{self.ls_env}_{market}.json"

    def account_for(self, market: Literal["stock", "futures"]) -> str:
        """시장에 맞는 계좌번호 반환. 하이픈 제거 + 폴백 처리."""
        specific = self.ls_stock_account_no if market == "stock" else self.ls_futures_account_no
        acct = (specific or self.ls_account_no).replace("-", "").replace(" ", "")
        if not acct:
            raise ValueError(
                f"{market} 계좌번호가 설정되지 않았습니다. "
                f".env의 LS_{market.upper()}_ACCOUNT_NO 또는 LS_ACCOUNT_NO를 채워주세요."
            )
        return acct

    def account_password(self, market: Literal["stock", "futures"]) -> str:
        """keyring에서 계좌 비밀번호 조회. 없으면 빈 문자열."""
        return creds.get_account_password(self.ls_env, market) or ""

    def credentials(self, market: Literal["stock", "futures"] = "stock") -> tuple[str, str]:
        """시장별 (app_key, app_secret) 해결. keyring(시장별→레거시) → .env 순서로 조회.

        LS는 appkey를 계좌(시장) 단위로 발급하므로 주식/선물 키가 다르다.
        """
        kr_key, kr_secret = creds.get_credentials(self.ls_env, market)
        app_key = kr_key or self.ls_app_key
        app_secret = kr_secret or self.ls_app_secret
        if not app_key or not app_secret:
            raise LSAuthError(
                f"{market} 자격 증명을 찾을 수 없습니다 (env={self.ls_env}). "
                f"먼저 'python examples\\00_setup_credentials.py' 를 실행하세요."
            )
        return app_key, app_secret


@lru_cache
def get_settings() -> Settings:
    return Settings()

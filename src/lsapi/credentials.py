"""Windows 자격 증명 관리자(DPAPI 기반) 연동 — 키 평문 파일 저장 회피.

LS는 appkey를 '계좌 단위'로 발급한다. 주식계좌와 선물계좌의 appkey가 다르므로
시장별로 따로 저장한다. 서비스명은 환경별로 분리(mock/prod 섞임 방지).
  service  = "trdbot-ls-mock"
  username = "stock_app_key" | "stock_app_secret"     (주식계좌 appkey)
             "futures_app_key" | "futures_app_secret" (선물계좌 appkey)
             "app_key" | "app_secret"                 (레거시 공용 — 폴백용)
             "stock_account_pwd" | "futures_account_pwd"

GUI 확인: Windows 검색 → "자격 증명 관리자" → "Windows 자격 증명" 탭.
"""

from typing import Literal

import keyring
from keyring.errors import PasswordDeleteError

_SERVICE_PREFIX = "trdbot-ls"
Market = Literal["stock", "futures"]


def _service(env: str) -> str:
    return f"{_SERVICE_PREFIX}-{env}"


def get_credentials(env: str, market: Market | None = None) -> tuple[str | None, str | None]:
    """저장된 (app_key, app_secret) 반환. 없으면 None.

    market 지정 시 시장별 키({market}_app_key)를 우선하고, 없으면
    레거시 공용 키(app_key)로 폴백한다(단일 appkey 환경 호환).
    """
    svc = _service(env)
    if market:
        key = keyring.get_password(svc, f"{market}_app_key") or keyring.get_password(svc, "app_key")
        secret = (
            keyring.get_password(svc, f"{market}_app_secret")
            or keyring.get_password(svc, "app_secret")
        )
        return key, secret
    return keyring.get_password(svc, "app_key"), keyring.get_password(svc, "app_secret")


def set_credentials(
    env: str, app_key: str, app_secret: str, market: Market | None = None
) -> None:
    """자격 증명 저장(기존 값 덮어쓰기). market 지정 시 시장별로 저장."""
    svc = _service(env)
    key_user = f"{market}_app_key" if market else "app_key"
    secret_user = f"{market}_app_secret" if market else "app_secret"
    keyring.set_password(svc, key_user, app_key)
    keyring.set_password(svc, secret_user, app_secret)


def get_account_password(env: str, market: Market) -> str | None:
    """계좌 비밀번호(4자리 PIN) 조회."""
    return keyring.get_password(_service(env), f"{market}_account_pwd")


def set_account_password(env: str, market: Market, pwd: str) -> None:
    """계좌 비밀번호 저장."""
    keyring.set_password(_service(env), f"{market}_account_pwd", pwd)


def delete_credentials(env: str) -> None:
    """해당 환경의 자격 증명/비밀번호 전부 삭제."""
    svc = _service(env)
    usernames = (
        "app_key", "app_secret",
        "stock_app_key", "stock_app_secret",
        "futures_app_key", "futures_app_secret",
        "stock_account_pwd", "futures_account_pwd",
    )
    for username in usernames:
        try:
            keyring.delete_password(svc, username)
        except PasswordDeleteError:
            pass


def backend_name() -> str:
    """현재 활성 keyring 백엔드 이름(진단용)."""
    return keyring.get_keyring().__class__.__name__

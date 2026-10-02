"""토큰 발급 / 캐시 동작 확인."""

from lsapi import TokenManager
from lsapi.logger import setup_logging


def main() -> None:
    setup_logging()
    tm = TokenManager()
    token = tm.get_token()
    print(f"access_token: {token[:20]}... (len={len(token)})")
    print(f"cache: {tm.settings.token_cache_path}")


if __name__ == "__main__":
    main()

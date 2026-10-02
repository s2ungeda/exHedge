"""LS OpenAPI 자격 증명 + 계좌 비밀번호를 Windows 자격 증명 관리자에 저장.

한 번 실행하면 DPAPI로 암호화되어 같은 Windows 사용자만 복호화 가능.

LS는 appkey를 계좌(시장) 단위로 발급 — 주식계좌·선물계좌 appkey가 다르므로 각각 저장한다.

사용법:
    python examples\\00_setup_credentials.py            # 주식/선물 appkey + 계좌비번 각각 입력
    python examples\\00_setup_credentials.py --env prod # 환경 명시
    python examples\\00_setup_credentials.py --check    # 현재 저장 상태 확인
    python examples\\00_setup_credentials.py --delete   # 전부 삭제

각 프롬프트는 비워두고 엔터치면 기존 값 유지.
"""

import argparse
import getpass
import sys

from lsapi import credentials as creds
from lsapi.config import get_settings


def _mask(s: str | None) -> str:
    if not s:
        return "(없음)"
    if len(s) <= 8:
        return "*" * len(s)
    return f"{s[:4]}...{s[-4:]} (len={len(s)})"


def cmd_check(env: str) -> None:
    print(f"backend : {creds.backend_name()}")
    print(f"env     : {env}")
    for market in ("stock", "futures"):
        key, secret = creds.get_credentials(env, market)
        pwd = creds.get_account_password(env, market)
        print(f"[{market:7}] app_key={_mask(key)}  app_secret={_mask(secret)}  account_pwd={_mask(pwd)}")


def cmd_setup(env: str) -> None:
    print(f"=== {env} 환경 설정 ===")
    print("(입력값은 화면에 표시되지 않음. 빈 채로 엔터 = 기존 값 유지)")
    print("LS는 appkey를 계좌(시장) 단위로 발급 — 주식/선물 각각 입력.\n")

    for market in ("stock", "futures"):
        label = "주식" if market == "stock" else "선물(파생)"
        print(f"[{label} API 키]")
        app_key = getpass.getpass(f"  {market} app_key   : ").strip()
        app_secret = getpass.getpass(f"  {market} app_secret: ").strip()
        if app_key and app_secret:
            creds.set_credentials(env, app_key, app_secret, market=market)
            print("  → 저장됨")
        elif app_key or app_secret:
            print("  [주의] key/secret 둘 다 입력하거나 둘 다 비워야 합니다. 키는 저장 안 함.")
        else:
            print("  (변경 없음)")

        pwd = getpass.getpass(f"  {label} 계좌 비밀번호(4자리): ").strip()
        if pwd:
            creds.set_account_password(env, market, pwd)
            print("  → 비번 저장됨")
        else:
            print("  (비번 변경 없음)")
        print()

    print(f"=== 결과 (backend={creds.backend_name()}) ===")
    cmd_check(env)


def cmd_delete(env: str) -> None:
    confirm = input(f"[주의] {env} 환경 자격 증명/비밀번호 전부 삭제. 진행? [yes/N]: ")
    if confirm.strip().lower() != "yes":
        print("취소했습니다.")
        return
    creds.delete_credentials(env)
    print("삭제 완료.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", choices=["mock", "prod"], default=None)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--delete", action="store_true")
    args = parser.parse_args()

    env = args.env or get_settings().ls_env

    if args.delete:
        cmd_delete(env)
    elif args.check:
        cmd_check(env)
    else:
        cmd_setup(env)


if __name__ == "__main__":
    main()

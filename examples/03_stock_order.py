"""주식 주문 → 잔고 조회 예시.

⚠️ .env 의 LS_ENV=mock 으로 모의투자 환경에서 먼저 확인하자.
실거래 계좌로 바로 돌리지 말 것.
"""

import json

from lsapi import LSClient, get_settings
from lsapi.logger import setup_logging
from lsapi.stock import StockAPI


def main() -> None:
    setup_logging()
    settings = get_settings()
    if settings.ls_env != "mock":
        confirm = input(f"⚠️ 환경이 '{settings.ls_env}' 입니다. 실거래로 진행할까요? [yes/N]: ")
        if confirm.strip().lower() != "yes":
            print("취소했습니다.")
            return

    with LSClient(settings) as client:
        stock = StockAPI(client)

        # 1주 지정가 매수 (예: 삼성전자 50,000원). 모의투자 기준.
        resp = stock.place_order(
            "005930",
            qty=1,
            side="buy",
            kind="limit",
            price=50000,
        )
        print("=== 주문 결과 ===")
        print(json.dumps(resp, ensure_ascii=False, indent=2))

        bal = stock.get_balance()
        print("\n=== 잔고 ===")
        print(json.dumps(bal, ensure_ascii=False, indent=2)[:1200])


if __name__ == "__main__":
    main()

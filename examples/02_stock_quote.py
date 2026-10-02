"""주식 현재가 / 호가 / 분봉 조회 예시."""

import json

from lsapi import LSClient
from lsapi.logger import setup_logging
from lsapi.stock import StockAPI

CODE = "005930"  # 삼성전자


def main() -> None:
    setup_logging()
    with LSClient() as client:
        stock = StockAPI(client)

        price = stock.get_current_price(CODE)
        print("=== 현재가 ===")
        print(json.dumps(price, ensure_ascii=False, indent=2)[:600])

        ob = stock.get_orderbook(CODE)
        print("\n=== 호가 ===")
        print(json.dumps(ob, ensure_ascii=False, indent=2)[:600])

        chart = stock.get_minute_chart(CODE, ncnt=1)
        rows = chart.get("t8412OutBlock1", [])
        print(f"\n=== 1분봉 ({len(rows)}건, 최근 3개만 표시) ===")
        for row in rows[-3:]:
            print(row)


if __name__ == "__main__":
    main()

"""선물옵션 현재가 / 호가 조회 예시."""

import json
import sys

from lsapi import LSClient
from lsapi.futures import FuturesAPI
from lsapi.logger import setup_logging


def main() -> None:
    setup_logging()
    # 종목코드는 만기에 따라 바뀌므로 인자로 받는다.
    if len(sys.argv) < 2:
        print("사용법: python 05_futures_quote.py <FNO_종목코드>")
        print("예시: python 05_futures_quote.py 101W9000  (KOSPI200 선물)")
        return
    code = sys.argv[1]

    with LSClient(market="futures") as client:
        f = FuturesAPI(client)
        print("=== 현재가 ===")
        print(json.dumps(f.get_current_price(code), ensure_ascii=False, indent=2)[:600])
        print("\n=== 호가 ===")
        print(json.dumps(f.get_orderbook(code), ensure_ascii=False, indent=2)[:600])


if __name__ == "__main__":
    main()

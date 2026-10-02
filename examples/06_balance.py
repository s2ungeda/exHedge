"""주식/파생 잔고 조회 — 자동매매 핵심 호출."""

import json

from lsapi import LSClient
from lsapi.futures import FuturesAPI
from lsapi.logger import setup_logging
from lsapi.stock import StockAPI


def main() -> None:
    setup_logging()
    # 주식/선물은 appkey(계좌)가 다르므로 클라이언트도 시장별로 따로 만든다.
    with LSClient(market="stock") as sc, LSClient(market="futures") as fc:
        s = StockAPI(sc).get_balance()
        f = FuturesAPI(fc).get_balance()

        sb = s.get("t0424OutBlock", {})
        print("=== 주식 잔고 ===")
        print(f"  예수금          : {sb.get('sunamt'):>15,}")
        print(f"  출금가능금액    : {sb.get('sunamt1'):>15,}")
        print(f"  당일평가손익    : {sb.get('dtsunik'):>15,}")
        print(f"  보유종목 수     : {len(s.get('t0424OutBlock1', []))}")
        for h in s.get("t0424OutBlock1", []):
            print(f"    - {h}")

        fb = f.get("t0441OutBlock", {})
        print("\n=== 파생 잔고 ===")
        print(f"  총평가금액      : {fb.get('tappamt'):>15,}")
        print(f"  총평가손익      : {fb.get('tsunik'):>15,}")
        print(f"  보유포지션 수   : {len(f.get('t0441OutBlock1', []))}")
        for h in f.get("t0441OutBlock1", []):
            print(f"    - {h}")

        # 디버그용 원본도 마지막에
        print("\n--- raw (debug) ---")
        print(json.dumps({"stock": s, "futures": f}, ensure_ascii=False, indent=2)[:800])


if __name__ == "__main__":
    main()

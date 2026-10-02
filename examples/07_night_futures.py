"""KRX 야간파생(CME 연계, 18:00~익일 05:00) 시세 조회 예시.

종목코드를 모르면 인자 없이 실행 → 야간선물 마스터(t8455)에서 목록을 뽑아준다.
주문 예시는 사고 방지를 위해 일부러 넣지 않았다 — NightFuturesAPI.place_order 참고.
"""

import json
import sys

from lsapi import LSClient
from lsapi.futures import NightFuturesAPI, session_now
from lsapi.logger import setup_logging


def main() -> None:
    setup_logging()
    with LSClient(market="futures") as client:
        nf = NightFuturesAPI(client)

        if len(sys.argv) < 2:
            print("종목코드 미지정 → 야간선물 마스터(t8455)에서 목록 조회\n")
            master = nf.get_master("NFU")  # NFU=야간선물, NOP=야간옵션
            rows = master.get("t8455OutBlock", [])
            for r in rows[:20]:
                print(f"  {r.get('shcode'):10s} {r.get('expcode'):14s} {r.get('hname')}")
            print("\n사용법: python 07_night_futures.py <단축코드>  (예: 101W9000)")
            return

        code = sys.argv[1]
        print(f"세션 판정(현재 입력 무관, 예시): {session_now(1900)} 가정\n")
        print("=== 야간 현재가(t8456) ===")
        print(json.dumps(nf.get_current_price(code), ensure_ascii=False, indent=2)[:600])
        print("\n=== 야간 호가(t8457) ===")
        print(json.dumps(nf.get_orderbook(code), ensure_ascii=False, indent=2)[:600])


if __name__ == "__main__":
    main()

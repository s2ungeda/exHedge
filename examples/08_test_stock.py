"""주식 주문 직접 테스트 (안전판).

사용법:
    python examples\\08_test_stock.py                 # 읽기전용 (현재가/잔고/미체결) — 무위험
    python examples\\08_test_stock.py --order         # 하한가 비체결 매수 → 즉시 취소 1회전
    python examples\\08_test_stock.py 005930 --order  # 종목 지정

안전장치:
- 매수가는 '하한가'라 절대 체결되지 않음(가격밴드 안). 접수 직후 자동 취소.
- 응답의 비밀번호 필드(InptPwd/Pwd)는 마스킹해서 출력.
- 서버가 echo한 '실제 라우팅 계좌'를 표시 → 어느 계좌로 주문이 갔는지 확인 가능.
"""

import json
import sys

from lsapi import LSClient, get_settings
from lsapi.logger import setup_logging
from lsapi.stock import StockAPI
from lsapi.util import redact


def _find(d: dict, key: str):
    """응답 어디든 key를 찾아 첫 값 반환."""
    if isinstance(d, dict):
        for k, v in d.items():
            if k == key:
                return v
            r = _find(v, key)
            if r is not None:
                return r
    elif isinstance(d, list):
        for v in d:
            r = _find(v, key)
            if r is not None:
                return r
    return None


def main() -> None:
    setup_logging()
    args = sys.argv[1:]
    do_order = "--order" in args
    codes = [a for a in args if not a.startswith("--")]
    code = codes[0] if codes else "005930"  # 기본: 삼성전자

    s = get_settings()
    print(f"환경: LS_ENV={s.ls_env}  종목={code}  모드={'주문+취소' if do_order else '읽기전용'}")
    if s.ls_env != "mock":
        print("[주의] 모의(mock) 환경이 아닙니다. 실계좌 위험! 중단합니다.")
        return

    with LSClient() as c:
        api = StockAPI(c)

        cp = api.get_current_price(code)
        ob = cp.get("t1102OutBlock", {})
        price = ob.get("price"); dn = ob.get("dnlmtprice"); up = ob.get("uplmtprice")
        print(f"\n[현재가 t1102] rsp_cd={cp.get('rsp_cd')}  현재가={price} 하한가={dn} 상한가={up}")

        bal = api.get_balance()
        print(f"[잔고 t0424]   rsp_cd={bal.get('rsp_cd')}")

        uf = api.get_unfilled_orders(code)
        rows = uf.get("t0425OutBlock1", []) or []
        print(f"[미체결 t0425] rsp_cd={uf.get('rsp_cd')}  미체결={len(rows)}건")

        if not do_order:
            print("\n읽기전용 완료. 주문까지 검증하려면 --order 를 붙여 다시 실행하세요.")
            return

        if not dn or float(dn) <= 0:
            print("\n하한가를 못 구해 주문 테스트를 건너뜁니다."); return

        dn = int(float(dn))
        print(f"\n>> 매수 1주 @하한가 {dn} (체결 불가 가격) 접수 …")
        r = api.place_order(code, 1, side="buy", kind="limit", price=dn, exchange="KRX")
        print(f"   place_order rsp_cd={r.get('rsp_cd')} msg={r.get('rsp_msg')}")
        print(f"   실제 라우팅 계좌(AcntNo)={_find(r, 'AcntNo')}")
        ono = _find(r, "OrdNo")
        print(f"   OrdNo={ono}")
        # 마스킹된 응답 일부
        print("   [응답(마스킹)]", json.dumps(redact(r.get("CSPAT00601OutBlock1", {})), ensure_ascii=False)[:200])

        if ono:
            cc = api.cancel_order(code, int(ono), 1)
            print(f">> 취소 rsp_cd={cc.get('rsp_cd')} msg={cc.get('rsp_msg')}")

        uf2 = api.get_unfilled_orders(code)
        rows2 = uf2.get("t0425OutBlock1", []) or []
        print(f">> 최종 미체결={len(rows2)}건 {'-> 깨끗(OK)' if not rows2 else '-> 잔존! 확인 필요'}")


if __name__ == "__main__":
    main()

"""선물 주문 직접 테스트 — 삼성 주식선물 / 원달러(미국달러)선물 (안전판).

사용법:
    python examples\\09_test_futures.py            # 읽기전용 (종목탐색/현재가/잔고) — 무위험
    python examples\\09_test_futures.py --order    # 하한가 비체결 매수 → 즉시 취소 1회전

특징:
- 만기에 따라 종목코드가 바뀌므로 마스터(t8401/t8426)에서 현재 활성 종목을 자동 탐색.
- 주식선물 시세는 전용 TR t8402 사용(범용 t2111은 주식선물에 0을 줌).
- 매수가는 '하한가'라 체결 불가. 접수 직후 자동 취소.
- [주의] 선물계좌는 보통 별도 appkey가 필요. 주문이 '01425 주문가능금액 부족'으로
  거부되면, 지금 토큰이 주식계좌로 바인딩된 것 -> 선물계좌 appkey로 .env/keyring 전환 필요.
"""

import sys

from lsapi import LSClient, get_settings
from lsapi.exceptions import LSAPIError
from lsapi.futures import FuturesAPI
from lsapi.logger import setup_logging


def _find(d, key):
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


def _price_limits(ob):
    """OutBlock에서 (현재가, 하한가, 상한가) 추출."""
    return ob.get("price"), ob.get("dnlmtprice"), ob.get("uplmtprice")


def discover_samsung(api):
    """삼성전자 주식선물 활성 종목 1개 탐색 → (code, hname, dnlmt)."""
    rows = api.get_stock_future_master().get("t8401OutBlock", []) or []
    cands = [r for r in rows if r.get("basecode") == "A005930"][:4]
    for r in cands:
        code = r.get("shcode")
        ob = api.get_stock_future_price(code).get("t8402OutBlock", {})
        price, dn, up = _price_limits(ob)
        if price and float(price) > 0 and dn and float(dn) > 0:
            return code, r.get("hname"), int(float(dn)), price
    return None


def discover_usd(api):
    """미국달러(원달러)선물 활성 종목 1개 탐색 → (code, hname, dnlmt)."""
    rows = api.get_commodity_future_master().get("t8426OutBlock", []) or []
    cands = [r for r in rows
             if "달러" in r.get("hname", "")
             and r.get("shcode", "").startswith("A75")][:4]
    for r in cands:
        code = r.get("shcode")
        ob = api.get_current_price(code).get("t2111OutBlock", {})  # 통화선물은 t2111 OK
        price, dn, up = _price_limits(ob)
        if price and float(str(price)) > 0 and dn and float(str(dn)) > 0:
            return code, r.get("hname"), float(str(dn)), price
    return None


def test_one(api, label, code, hname, dn, price, do_order):
    print(f"\n===== {label}: {hname} ({code}) =====")
    print(f"  현재가={price}  하한가={dn}")
    if not do_order:
        return
    print(f"  >> 매수 1계약 @하한가 {dn} 접수 …")
    try:
        r = api.place_order(code, 1, side="buy", kind="limit", price=dn)
        print(f"     rsp_cd={r.get('rsp_cd')} msg={r.get('rsp_msg')}")
        print(f"     실제 라우팅 계좌(AcntNo)={_find(r, 'AcntNo')}")
        ono = _find(r, "OrdNo")
        print(f"     OrdNo={ono}")
        if ono:
            cc = api.cancel_order(code, int(ono), 1)
            print(f"  >> 취소 rsp_cd={cc.get('rsp_cd')} msg={cc.get('rsp_msg')}")
    except LSAPIError as e:
        msg = e.payload.get("rsp_msg") if e.payload else str(e)
        print(f"     거부 [{e.code}] {msg}")
        if e.code == "01425":
            print("     -> 주문가능금액 0. 지금 토큰이 '선물계좌'에 접근 못 하는 상태입니다.")
            print("       선물계좌로 발급한 appkey/secret을 keyring/.env에 넣고 다시 실행하세요.")


def main() -> None:
    setup_logging()
    do_order = "--order" in sys.argv[1:]

    s = get_settings()
    print(f"환경: LS_ENV={s.ls_env}  모드={'주문+취소' if do_order else '읽기전용'}")
    if s.ls_env != "mock":
        print("[주의] 모의(mock) 환경이 아닙니다. 중단합니다."); return

    with LSClient(market="futures") as c:
        api = FuturesAPI(c)
        # [주의] 비밀번호 평문 출력 — 로컬 모의 디버깅 전용. 공유/커밋/로그 전에 제거할 것.
        print(f"선물 계좌번호 = {s.account_for('futures')}")
        print(f"선물 비밀번호 = {s.account_password('futures')}")
        bal = api.get_balance()
        print(f"[선물잔고 t0441] rsp_cd={bal.get('rsp_cd')}")

        sam = discover_samsung(api)
        if sam:
            test_one(api, "삼성 주식선물", *sam, do_order=do_order)
        else:
            print("\n삼성 주식선물 활성 종목을 못 찾음(장 시작 전/마스터 비어있음).")

        usd = discover_usd(api)
        if usd:
            test_one(api, "원달러(미국달러)선물", *usd, do_order=do_order)
        else:
            print("\n미국달러선물 활성 종목을 못 찾음.")

        if not do_order:
            print("\n읽기전용 완료. 주문까지 검증하려면 --order 를 붙여 다시 실행하세요.")


if __name__ == "__main__":
    main()

"""선물옵션 시세·주문 모듈.

주간/야간 세션 분리:
- 주간(09:00~15:45): `FuturesAPI` — t2101/t2105, CFOAT00100~00300
- 야간(18:00~익일 05:00, KRX 야간파생/CME 연계): `NightFuturesAPI`
  — t8455/t8456/t8457(시세), CCENT00100~00300(주문), CCENQ90200(잔고)

TR 코드/필드는 LS OpenAPI 공식 명세(openapi.ls-sec.co.kr) 확정값.
실거래 전 반드시 모의에서 응답 필드를 1회 검증할 것.
"""

import logging
from typing import Any, Literal

from lsapi import endpoints as ep
from lsapi.client import LSClient
from lsapi.exceptions import LSAPIError

logger = logging.getLogger(__name__)

OrderSide = Literal["buy", "sell"]
_SIDE_CODE = {"sell": "1", "buy": "2"}

OrderKind = Literal["limit", "market"]
_ORD_KIND = {"limit": "00", "market": "03"}


def _require(endpoint: ep.Endpoint | None, name: str) -> ep.Endpoint:
    if endpoint is None:
        raise NotImplementedError(
            f"{name} TR 코드 미설정. LS 공식 문서 확인 후 endpoints.py 의 "
            f"FUTURES_NIGHT_* 항목에 채워주세요."
        )
    return endpoint


class FuturesAPI:
    def __init__(self, client: LSClient):
        self.c = client

    # ── 시세 ─────────────────────────────────────────
    def get_current_price(self, code: str) -> dict[str, Any]:
        """선물옵션 현재가(t2111). code: 종목코드(예: 'A0166000' / '101T9000')."""
        body = {f"{ep.FUTURES_CURRENT_PRICE.tr_cd}InBlock": {"focode": code}}
        return self.c.call(ep.FUTURES_CURRENT_PRICE.path, ep.FUTURES_CURRENT_PRICE.tr_cd, body)

    def get_orderbook(self, code: str) -> dict[str, Any]:
        """선물옵션 호가(t2112). InBlock 키가 `shcode`임에 주의(현재가와 다름)."""
        body = {f"{ep.FUTURES_ORDERBOOK.tr_cd}InBlock": {"shcode": code}}
        return self.c.call(ep.FUTURES_ORDERBOOK.path, ep.FUTURES_ORDERBOOK.tr_cd, body)

    # ── 주식선물 전용 시세 ───────────────────────────
    # 범용 t2111/t2112는 주식선물 시세를 0으로 반환 → 전용 TR 사용.
    def get_stock_future_master(self) -> dict[str, Any]:
        """주식선물 종목 마스터(t8401). OutBlock에 basecode(기초자산)/shcode(선물코드)."""
        e = ep.FUTURES_STOCK_MASTER
        return self.c.call(e.path, e.tr_cd, {f"{e.tr_cd}InBlock": {"dummy": ""}})

    def get_stock_future_price(self, code: str) -> dict[str, Any]:
        """주식선물 현재가(t8402). code=선물 단축코드(예: 삼성전자선물 'A1169000')."""
        e = ep.FUTURES_STOCK_PRICE
        return self.c.call(e.path, e.tr_cd, {f"{e.tr_cd}InBlock": {"focode": code}})

    def get_stock_future_orderbook(self, code: str) -> dict[str, Any]:
        """주식선물 호가(t8403). InBlock 키가 `shcode`."""
        e = ep.FUTURES_STOCK_ORDERBOOK
        return self.c.call(e.path, e.tr_cd, {f"{e.tr_cd}InBlock": {"shcode": code}})

    def get_commodity_future_master(self) -> dict[str, Any]:
        """상품선물 마스터(t8426). 미국달러(원달러)선물·국채선물 등."""
        e = ep.FUTURES_COMMODITY_MASTER
        return self.c.call(e.path, e.tr_cd, {f"{e.tr_cd}InBlock": {"dummy": ""}})

    # ── 주문 ─────────────────────────────────────────
    def place_order(
        self,
        code: str,
        qty: int,
        *,
        side: OrderSide,
        kind: OrderKind = "limit",
        price: float = 0.0,
    ) -> dict[str, Any]:
        """선물옵션 신규 주문(CFOAT00100).

        공식 InBlock1: {FnoIsuNo, BnsTpCode, FnoOrdprcPtnCode, FnoOrdPrc, OrdQty}.
        계좌는 토큰에 묶이므로 AcntNo/Pwd 불요. KRX 파생은 자동 상계라 신규/청산 구분 없음.
        """
        if kind == "limit" and price <= 0:
            raise ValueError("지정가 주문에는 price > 0 가 필요합니다.")

        body = {
            f"{ep.FUTURES_ORDER_NEW.tr_cd}InBlock1": {
                "FnoIsuNo": code,
                "BnsTpCode": _SIDE_CODE[side],
                "FnoOrdprcPtnCode": _ORD_KIND[kind],
                "FnoOrdPrc": price,
                "OrdQty": qty,
            }
        }
        logger.info("선물옵션 주문: %s %s %d @ %s", code, side, qty, price or "시장가")
        return self.c.call(ep.FUTURES_ORDER_NEW.path, ep.FUTURES_ORDER_NEW.tr_cd, body)

    def modify_order(
        self,
        code: str,
        original_order_no: int,
        qty: int,
        price: float,
        *,
        kind: OrderKind = "limit",
    ) -> dict[str, Any]:
        """선물옵션 정정 주문(CFOAT00200). qty=정정 수량(MdfyQty)."""
        body = {
            f"{ep.FUTURES_ORDER_MODIFY.tr_cd}InBlock1": {
                "FnoIsuNo": code,
                "OrgOrdNo": original_order_no,
                "FnoOrdprcPtnCode": _ORD_KIND[kind],
                "FnoOrdPrc": price,
                "MdfyQty": qty,
            }
        }
        return self.c.call(ep.FUTURES_ORDER_MODIFY.path, ep.FUTURES_ORDER_MODIFY.tr_cd, body)

    def cancel_order(self, code: str, original_order_no: int, qty: int) -> dict[str, Any]:
        """선물옵션 취소 주문(CFOAT00300). qty=취소 수량(CancQty)."""
        body = {
            f"{ep.FUTURES_ORDER_CANCEL.tr_cd}InBlock1": {
                "FnoIsuNo": code,
                "OrgOrdNo": original_order_no,
                "CancQty": qty,
            }
        }
        return self.c.call(ep.FUTURES_ORDER_CANCEL.path, ep.FUTURES_ORDER_CANCEL.tr_cd, body)

    def get_unfilled_orders(self, code: str = "", chegb: str = "2") -> dict[str, Any]:
        """선물옵션 체결/미체결 조회 (t0434). code='' → 전체.

        chegb: 0=전체(체결+미체결), 1=체결, 2=미체결. 체결 추적엔 '0'(전체) 사용 —
        전량체결 주문은 미체결('2') 목록에서 빠지므로, cheqty를 보려면 전체로 조회.
        """
        body = {
            f"{ep.FUTURES_ORDER_HISTORY.tr_cd}InBlock": {
                "expcode": code,
                "chegb": chegb,
                "sortgb": "1",  # 1=주문번호 역순
                "cts_ordno": "",
            }
        }
        return self.c.call(ep.FUTURES_ORDER_HISTORY.path, ep.FUTURES_ORDER_HISTORY.tr_cd, body)

    # ── 계좌 ─────────────────────────────────────────
    def get_balance(self) -> dict[str, Any]:
        """선물옵션 잔고현황(미결제) 조회(t0441).

        공식 InBlock: {cts_expcode, cts_medocd} — 연속조회 키. 첫 조회는 공백.
        """
        body = {
            f"{ep.FUTURES_BALANCE.tr_cd}InBlock": {
                "cts_expcode": "",
                "cts_medocd": "",
            }
        }
        return self.c.call(ep.FUTURES_BALANCE.path, ep.FUTURES_BALANCE.tr_cd, body)


class NightFuturesAPI:
    """KRX 야간파생(CME 연계, 18:00~익일 05:00) 시세/주문.

    주간(FuturesAPI)과 path는 같고 tr_cd만 다르다(endpoints.FUTURES_NIGHT_*).
    공식 명세 기준 차이점:
    - 시세 InBlock 필드명: 시세(t8456)=`focode`, 호가(t8457)=`shcode`.
    - 주문 InBlock1에는 AcntNo/Pwd/신규청산 구분이 없다. 계좌는 토큰에 묶이고,
      KRX 파생은 자동 상계(netting)라 신규/청산 플래그가 없다.
    - 종목코드는 야간 마스터(t8455, gubun=NFU)의 `shcode`(예: '101W9000')를 쓴다.
    """

    def __init__(self, client: LSClient):
        self.c = client

    # ── 시세 ─────────────────────────────────────────
    def get_master(self, gubun: str = "NFU") -> dict[str, Any]:
        """야간파생 종목 마스터. gubun: 'NFU' 야간선물 / 'NOP' 야간옵션.

        OutBlock 각 항목의 `shcode`(단축코드)/`expcode`(표준코드)를 시세·주문에 사용.
        """
        e = _require(ep.FUTURES_NIGHT_MASTER, "야간파생 마스터")
        return self.c.call(e.path, e.tr_cd, {f"{e.tr_cd}InBlock": {"gubun": gubun}})

    def get_current_price(self, code: str) -> dict[str, Any]:
        """야간파생 현재가(시세). code: 단축코드(예: '101W9000')."""
        e = _require(ep.FUTURES_NIGHT_CURRENT_PRICE, "야간파생 현재가")
        return self.c.call(e.path, e.tr_cd, {f"{e.tr_cd}InBlock": {"focode": code}})

    def get_orderbook(self, code: str) -> dict[str, Any]:
        """야간파생 호가. 호가 TR(t8457)은 InBlock 키가 `shcode`임에 주의."""
        e = _require(ep.FUTURES_NIGHT_ORDERBOOK, "야간파생 호가")
        return self.c.call(e.path, e.tr_cd, {f"{e.tr_cd}InBlock": {"shcode": code}})

    # ── 주문 ─────────────────────────────────────────
    def place_order(
        self,
        code: str,
        qty: int,
        *,
        side: OrderSide,
        kind: OrderKind = "limit",
        price: float = 0.0,
    ) -> dict[str, Any]:
        """야간파생 신규 주문(CCENT00100).

        KRX 파생은 자동 상계라 신규/청산 구분이 없다(주간 FuturesAPI와 동일).
        """
        e = _require(ep.FUTURES_NIGHT_ORDER_NEW, "야간파생 신규주문")
        if kind == "limit" and price <= 0:
            raise ValueError("지정가 주문에는 price > 0 가 필요합니다.")
        body = {
            f"{e.tr_cd}InBlock1": {
                "FnoIsuNo": code,
                "BnsTpCode": _SIDE_CODE[side],
                "FnoOrdprcPtnCode": _ORD_KIND[kind],
                "FnoOrdPrc": price,
                "OrdQty": qty,
            }
        }
        logger.info("야간파생 주문: %s %s %d @ %s", code, side, qty, price or "시장가")
        return self.c.call(e.path, e.tr_cd, body)

    def modify_order(
        self,
        code: str,
        original_order_no: int,
        qty: int,
        price: float,
        *,
        kind: OrderKind = "limit",
    ) -> dict[str, Any]:
        """야간파생 정정 주문(CCENT00200). qty=정정 수량(MdfyQty)."""
        e = _require(ep.FUTURES_NIGHT_ORDER_MODIFY, "야간파생 정정주문")
        body = {
            f"{e.tr_cd}InBlock1": {
                "FnoIsuNo": code,
                "OrgOrdNo": original_order_no,
                "FnoOrdprcPtnCode": _ORD_KIND[kind],
                "FnoOrdPrc": price,
                "MdfyQty": qty,
            }
        }
        return self.c.call(e.path, e.tr_cd, body)

    def cancel_order(self, code: str, original_order_no: int, qty: int) -> dict[str, Any]:
        """야간파생 취소 주문(CCENT00300). qty=취소 수량(CancQty)."""
        e = _require(ep.FUTURES_NIGHT_ORDER_CANCEL, "야간파생 취소주문")
        body = {
            f"{e.tr_cd}InBlock1": {
                "FnoIsuNo": code,
                "OrgOrdNo": original_order_no,
                "CancQty": qty,
            }
        }
        return self.c.call(e.path, e.tr_cd, body)

    # ── 계좌 ─────────────────────────────────────────
    def get_balance(self) -> dict[str, Any]:
        """야간파생 잔고조회(CCENQ90200)."""
        e = _require(ep.FUTURES_NIGHT_BALANCE, "야간파생 잔고")
        body = {
            f"{e.tr_cd}InBlock1": {
                "RecCnt": 1,
                "BalEvalTp": "0",
                "FutsPrcEvalTp": "0",
            }
        }
        return self.c.call(e.path, e.tr_cd, body)

    def get_order_history(self, code: str = "") -> dict[str, Any]:
        """야간파생 주문/체결내역 조회(CCENQ30100). code=''(전체)."""
        e = _require(ep.FUTURES_NIGHT_ORDER_HISTORY, "야간파생 주문/체결내역")
        body = {
            f"{e.tr_cd}InBlock1": {
                "RecCnt": 1,
                "QrySrtDt": "00000000",
                "QryEndDt": "00000000",
                "FnoClssCode": "00",
                "PrdgrpCode": "00",
                "PrdtExecTpCode": "0",
                "StnlnSeqTp": "4",
                "MktTpCode": "0",
                "CommdaCode": "99",
                "FnoIsuNo": code,
                "FnoTrdPtnCode": "00",
                "SrtOrdNo2": 0,
            }
        }
        return self.c.call(e.path, e.tr_cd, body)


def session_now(now_hhmm: int) -> Literal["day", "night", "closed"]:
    """현재 한국시각 HHMM 정수로 미국달러선물 세션 판정(KRX 공식 거래시간).

    주간 정규 08:45~15:45 → day
    야간(글로벌)  18:00~익일 06:00 → night
    그 외 → closed
    (단일가 시간 포함 대략치. 정밀 운영시간/휴장은 장운영 TR로 보강 예정.)
    """
    if 845 <= now_hhmm <= 1545:
        return "day"
    if now_hhmm >= 1800 or now_hhmm < 600:
        return "night"
    return "closed"

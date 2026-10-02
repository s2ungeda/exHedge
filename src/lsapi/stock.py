"""주식/ETF 시세·주문 모듈.

ETF도 6자리 종목코드 기반이므로 주식과 동일한 TR을 사용한다.
"""

import logging
from typing import Any, Literal

from lsapi import endpoints as ep
from lsapi.client import LSClient

logger = logging.getLogger(__name__)

OrderSide = Literal["buy", "sell"]
# LS 표준: 매도=1, 매수=2
_SIDE_CODE = {"sell": "1", "buy": "2"}

OrderKind = Literal["limit", "market"]
# 호가유형코드 — limit=00(지정가), market=03(시장가)
_ORD_KIND = {"limit": "00", "market": "03"}

# 거래소 라우팅(MbrNo) — 넥스트레이드(NXT) 출범 후 주문 시 필수.
Exchange = Literal["KRX", "NXT", "SOR"]


class StockAPI:
    def __init__(self, client: LSClient):
        self.c = client

    # ── 시세 ─────────────────────────────────────────
    def get_current_price(self, code: str) -> dict[str, Any]:
        """현재가 단건 조회.

        Returns: TR 응답 그대로. {tr_cd}OutBlock 안에 시가/고가/저가/현재가/거래량 등.
        """
        body = {f"{ep.STOCK_CURRENT_PRICE.tr_cd}InBlock": {"shcode": code}}
        return self.c.call(ep.STOCK_CURRENT_PRICE.path, ep.STOCK_CURRENT_PRICE.tr_cd, body)

    def get_orderbook(self, code: str) -> dict[str, Any]:
        body = {f"{ep.STOCK_ORDERBOOK.tr_cd}InBlock": {"shcode": code}}
        return self.c.call(ep.STOCK_ORDERBOOK.path, ep.STOCK_ORDERBOOK.tr_cd, body)

    def get_minute_chart(
        self,
        code: str,
        *,
        ncnt: int = 1,
        sdate: str | None = None,
        edate: str | None = None,
        cts_date: str = "",
        cts_time: str = "",
    ) -> dict[str, Any]:
        """주식 N분봉 (t8412).

        - ncnt: 분 단위. 1=1분, 5=5분, 10=10분 등
        - sdate/edate: YYYYMMDD. None이면 최근 5거래일 자동 설정
        - cts_date/cts_time: 연속조회용. 응답 OutBlock.cts_date/cts_time을 그대로 전달.

        주의:
        - LS는 1회 응답 최대 500건 (qrycnt는 사실상 무시). 더 필요하면 cts_*로 연속조회.
        - 호출 간 텀이 짧으면 IGW00201 레이트리밋. 0.5초 이상 간격 권장.
        """
        from datetime import date, timedelta
        today = date.today()
        body = {
            f"{ep.STOCK_MINUTE_CHART.tr_cd}InBlock": {
                "shcode": code,
                "ncnt": ncnt,
                "qrycnt": 500,
                "nday": "0",
                "sdate": sdate or (today - timedelta(days=7)).strftime("%Y%m%d"),
                "edate": edate or today.strftime("%Y%m%d"),
                "cts_date": cts_date,
                "cts_time": cts_time,
                "comp_yn": "N",
            }
        }
        return self.c.call(ep.STOCK_MINUTE_CHART.path, ep.STOCK_MINUTE_CHART.tr_cd, body)

    # ── 주문 ─────────────────────────────────────────
    def place_order(
        self,
        code: str,
        qty: int,
        *,
        side: OrderSide,
        kind: OrderKind = "limit",
        price: int = 0,
        exchange: Exchange = "KRX",
    ) -> dict[str, Any]:
        """주식 현금주문(CSPAT00601, 매수/매도).

        - kind="limit"  : price 필수
        - kind="market" : price=0
        - exchange: 거래소 라우팅(MbrNo). 넥스트레이드(NXT) 출범 후 필수 필드.
          "KRX"(정규시장) / "NXT"(대체거래소) / "SOR"(최선집행 자동라우팅).

        공식 InBlock1에는 AcntNo/InptPwd가 없다(계좌는 토큰에 묶임).
        종목번호 IsuNo는 공식 예시 기준 'A005930' 형식(앞 'A' 포함).
        """
        if kind == "limit" and price <= 0:
            raise ValueError("지정가 주문에는 price > 0 가 필요합니다.")

        body = {
            f"{ep.STOCK_ORDER_CASH.tr_cd}InBlock1": {
                "IsuNo": code,
                "OrdQty": qty,
                "OrdPrc": price,
                "BnsTpCode": _SIDE_CODE[side],
                "OrdprcPtnCode": _ORD_KIND[kind],
                "MgntrnCode": "000",  # 신용거래코드: 000=보통
                "LoanDt": "",
                "OrdCndiTpCode": "0",  # 주문조건: 0=없음, 1=IOC, 2=FOK
                "MbrNo": exchange,    # 거래소 라우팅: KRX/NXT/SOR
            }
        }
        logger.info("주문 요청: %s %s %d주 @ %s [%s]", code, side, qty, price or "시장가", exchange)
        return self.c.call(ep.STOCK_ORDER_CASH.path, ep.STOCK_ORDER_CASH.tr_cd, body)

    def cancel_order(self, code: str, original_order_no: int, qty: int) -> dict[str, Any]:
        """주식 취소주문(CSPAT00801). 공식 InBlock1: {OrgOrdNo, IsuNo, OrdQty}."""
        body = {
            f"{ep.STOCK_ORDER_CANCEL.tr_cd}InBlock1": {
                "OrgOrdNo": original_order_no,
                "IsuNo": code,
                "OrdQty": qty,
            }
        }
        return self.c.call(ep.STOCK_ORDER_CANCEL.path, ep.STOCK_ORDER_CANCEL.tr_cd, body)

    def modify_order(
        self,
        code: str,
        original_order_no: int,
        qty: int,
        new_price: int,
    ) -> dict[str, Any]:
        """주식 정정주문(CSPAT00701). 공식 InBlock1에 AcntNo/InptPwd 없음."""
        body = {
            f"{ep.STOCK_ORDER_MODIFY.tr_cd}InBlock1": {
                "OrgOrdNo": original_order_no,
                "IsuNo": code,
                "OrdQty": qty,
                "OrdPrc": new_price,
                "OrdprcPtnCode": "00",
                "OrdCndiTpCode": "0",
            }
        }
        return self.c.call(ep.STOCK_ORDER_MODIFY.path, ep.STOCK_ORDER_MODIFY.tr_cd, body)

    def get_unfilled_orders(self, code: str = "") -> dict[str, Any]:
        """체결/미체결 내역 조회 (t0425). code='' → 전체. 공식 InBlock에 계좌/비번 없음."""
        body = {
            f"{ep.STOCK_ORDER_HISTORY.tr_cd}InBlock": {
                "expcode": code,
                "chegb": "2",   # 0=전체, 1=체결, 2=미체결
                "medosu": "0",  # 0=전체, 1=매도, 2=매수
                "sortgb": "1",  # 정렬: 1=주문번호역순
                "cts_ordno": "",
            }
        }
        return self.c.call(ep.STOCK_ORDER_HISTORY.path, ep.STOCK_ORDER_HISTORY.tr_cd, body)

    # ── 계좌 ─────────────────────────────────────────
    def get_balance(self) -> dict[str, Any]:
        """주식잔고2 조회(t0424). 공식 InBlock: {prcgb, chegb, dangb, charge, cts_expcode}."""
        body = {
            f"{ep.STOCK_BALANCE.tr_cd}InBlock": {
                "prcgb": "1",  # 단가구분: 1=평균단가
                "chegb": "0",  # 체결구분: 0=결제일기준
                "dangb": "0",
                "charge": "1",  # 제비용포함여부
                "cts_expcode": "",
            }
        }
        return self.c.call(ep.STOCK_BALANCE.path, ep.STOCK_BALANCE.tr_cd, body)

"""TR 코드와 endpoint 경로 모음.

LS OpenAPI 문서(openapi.ls-sec.co.kr)를 기준으로 한 번에 관리.
새 TR을 쓸 때마다 여기에 추가하고, 도메인 모듈에서 import 한다.

NOTE: 아래 값들은 일반적으로 알려진 LS(구 이베스트) TR 코드 기준이다.
포털의 최신 명세와 차이가 있을 수 있으므로, 처음 적용 전 반드시
공식 문서/시뮬레이터로 응답 필드를 한 번 확인하자.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Endpoint:
    path: str
    tr_cd: str
    description: str


# ── 주식 시세 ────────────────────────────────────────
STOCK_CURRENT_PRICE = Endpoint(
    path="/stock/market-data",
    tr_cd="t1102",
    description="주식 현재가(시세) 조회",
)
STOCK_ORDERBOOK = Endpoint(
    path="/stock/market-data",
    tr_cd="t1101",
    description="주식 호가 조회",
)
STOCK_DAILY_CHART = Endpoint(
    path="/stock/chart",
    tr_cd="t1305",
    description="주식 기간별(일/주/월) 시세",
)
STOCK_MINUTE_CHART = Endpoint(
    path="/stock/chart",
    tr_cd="t8412",
    description="주식 N분봉 차트",
)

# ── 주식 주문/계좌 ────────────────────────────────────
STOCK_ORDER_CASH = Endpoint(
    path="/stock/order",
    tr_cd="CSPAT00601",
    description="주식 현금 주문(매수/매도)",
)
STOCK_ORDER_MODIFY = Endpoint(
    path="/stock/order",
    tr_cd="CSPAT00701",
    description="주식 주문 정정",
)
STOCK_ORDER_CANCEL = Endpoint(
    path="/stock/order",
    tr_cd="CSPAT00801",
    description="주식 주문 취소",
)
STOCK_BALANCE = Endpoint(
    path="/stock/accno",
    tr_cd="t0424",
    description="주식 잔고 조회",
)
STOCK_ORDER_HISTORY = Endpoint(
    path="/stock/accno",
    tr_cd="t0425",
    description="주식 체결/미체결 조회",
)

# ── 선물옵션 시세 (주간) ──────────────────────────────
# 공식 문서화 TR은 t2111(현재가)/t2112(호가). 구버전 t2101/t2105는 명세 미게재.
# InBlock 필드명 주의: 현재가(t2111)=focode, 호가(t2112)=shcode.
FUTURES_CURRENT_PRICE = Endpoint(
    path="/futureoption/market-data",
    tr_cd="t2111",
    description="선물/옵션 현재가(시세) 조회",
)
FUTURES_ORDERBOOK = Endpoint(
    path="/futureoption/market-data",
    tr_cd="t2112",
    description="선물/옵션 현재가 호가 조회",
)

# ── 주식선물 / 상품선물 마스터·시세 ───────────────────
# ⚠️ 주식선물은 범용 t2111이 시세를 0으로 주므로 전용 TR(t8402/t8403)을 써야 한다.
#    원달러(미국달러)선물 등 상품선물은 t2111로도 시세 조회가 된다.
FUTURES_STOCK_MASTER = Endpoint(
    path="/futureoption/market-data",
    tr_cd="t8401",
    description="주식선물 마스터 (basecode=기초자산, shcode=선물코드)",
)
FUTURES_STOCK_PRICE = Endpoint(
    path="/futureoption/market-data",
    tr_cd="t8402",
    description="주식선물 현재가 (focode)",
)
FUTURES_STOCK_ORDERBOOK = Endpoint(
    path="/futureoption/market-data",
    tr_cd="t8403",
    description="주식선물 호가 (shcode)",
)
FUTURES_COMMODITY_MASTER = Endpoint(
    path="/futureoption/market-data",
    tr_cd="t8426",
    description="상품선물 마스터 (통화/금리 등 — 미국달러선물 포함)",
)

# ── 선물옵션 주문/계좌 ────────────────────────────────
FUTURES_ORDER_NEW = Endpoint(
    path="/futureoption/order",
    tr_cd="CFOAT00100",
    description="선물옵션 신규 주문",
)
FUTURES_ORDER_MODIFY = Endpoint(
    path="/futureoption/order",
    tr_cd="CFOAT00200",
    description="선물옵션 주문 정정",
)
FUTURES_ORDER_CANCEL = Endpoint(
    path="/futureoption/order",
    tr_cd="CFOAT00300",
    description="선물옵션 주문 취소",
)
FUTURES_BALANCE = Endpoint(
    path="/futureoption/accno",
    tr_cd="t0441",
    description="선물옵션 잔고",
)
FUTURES_ORDER_HISTORY = Endpoint(
    path="/futureoption/accno",
    tr_cd="t0434",
    description="선물옵션 체결/미체결 조회",
)

# ── KRX 야간파생 (CME 연계, 18:00 ~ 익일 05:00) ────────
# 공식 명세 확정(openapi.ls-sec.co.kr, 2026-06 기준).
# 주간과 path는 동일하고 tr_cd만 다르다. 야간 시세 InBlock 필드명 주의:
#   t8456(시세)=focode, t8457(호가)=shcode.
# 주문 InBlock에는 AcntNo/Pwd/신규청산구분이 없다(공식 예제 기준) — 주간과 다름.
FUTURES_NIGHT_MASTER: Endpoint | None = Endpoint(
    path="/futureoption/market-data",
    tr_cd="t8455",
    description="KRX 야간파생 마스터조회 (gubun=NFU 야간선물 / NOP 야간옵션)",
)
FUTURES_NIGHT_CURRENT_PRICE: Endpoint | None = Endpoint(
    path="/futureoption/market-data",
    tr_cd="t8456",
    description="KRX 야간파생 시세조회 (현재가)",
)
FUTURES_NIGHT_ORDERBOOK: Endpoint | None = Endpoint(
    path="/futureoption/market-data",
    tr_cd="t8457",
    description="KRX 야간파생 호가조회",
)
FUTURES_NIGHT_ORDER_NEW: Endpoint | None = Endpoint(
    path="/futureoption/order",
    tr_cd="CCENT00100",
    description="KRX 야간파생 위탁 신규주문",
)
FUTURES_NIGHT_ORDER_MODIFY: Endpoint | None = Endpoint(
    path="/futureoption/order",
    tr_cd="CCENT00200",
    description="KRX 야간파생 위탁 정정주문",
)
FUTURES_NIGHT_ORDER_CANCEL: Endpoint | None = Endpoint(
    path="/futureoption/order",
    tr_cd="CCENT00300",
    description="KRX 야간파생 위탁 취소주문",
)
FUTURES_NIGHT_ORDER_HISTORY: Endpoint | None = Endpoint(
    path="/futureoption/accno",
    tr_cd="CCENQ30100",
    description="KRX 야간파생 주문/체결내역 조회",
)
FUTURES_NIGHT_BALANCE: Endpoint | None = Endpoint(
    path="/futureoption/accno",
    tr_cd="CCENQ90200",
    description="KRX 야간파생 잔고조회",
)

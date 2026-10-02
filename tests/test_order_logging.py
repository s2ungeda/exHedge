"""주문 생애주기 로그 — 주문번호로 주문→취소를 추적할 수 있어야 한다.

운영로그(2026-10-01 야간)에서 거부된 주문이 정상처럼 지나가고, 존재하지 않는 주문번호로
취소가 나갔는데 주문번호가 로그에 없어 확인할 수 없었다. 그 재발 방지용.
"""

import logging

from trdbot.broker_ls import LSDayFuturesBroker
from trdbot.datastore import MarketDataStore


class FakeAPI:
    def __init__(self, order_resp: dict):
        self._order_resp = order_resp
        self.cancelled: list = []

    def place_order(self, code, qty, *, side, kind, price) -> dict:
        return self._order_resp

    def cancel_order(self, code, ordno, qty) -> dict:
        self.cancelled.append((code, ordno, qty))
        return {"rsp_cd": "00156", "rsp_msg": "취소주문이 완료되었습니다."}

    def get_unfilled_orders(self, code: str = "", chegb: str = "2") -> dict:
        return {"t0434OutBlock1": []}


def _broker(resp: dict) -> tuple[LSDayFuturesBroker, FakeAPI]:
    api = FakeAPI(resp)
    store = MarketDataStore(api, code_prefix="A756")
    return LSDayFuturesBroker(store, api, "A7569000"), api


def test_주문응답_주문번호_응답코드_원문_기록(caplog):
    resp = {"rsp_cd": "00040", "rsp_msg": "매수 주문이 완료되었습니다.",
            "CFOAT00100OutBlock2": {"OrdNo": 12345, "Pwd": "1234"}}
    b, _ = _broker(resp)
    with caplog.at_level(logging.INFO, logger="trdbot.broker_ls"):
        b.place_order("buy", 2, 1370.7)
    line = next(r.getMessage() for r in caplog.records if "주문응답" in r.getMessage())
    assert "#12345" in line and "00040" in line
    assert "1234'" not in line and "****" in line   # 비밀번호 필드는 마스킹


def test_취소_요청과_응답을_주문번호로_기록(caplog):
    resp = {"rsp_cd": "00040", "CFOAT00100OutBlock2": {"OrdNo": 777}}
    b, api = _broker(resp)
    b.place_order("sell", 2, 1370.6)   # pending 등록 → 취소 대상이 됨
    with caplog.at_level(logging.INFO, logger="trdbot.broker_ls"):
        b.cancel("777")
    msgs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("취소요청 #777 잔2") for m in msgs)
    assert any(m.startswith("취소응답 #777 [00156") for m in msgs)
    assert api.cancelled == [("A7569000", 777, 2)]


def test_취소대상_없으면_생략도_기록(caplog):
    b, api = _broker({})
    with caplog.at_level(logging.INFO, logger="trdbot.broker_ls"):
        b.cancel("999")
    assert any("취소 생략 #999" in r.getMessage() for r in caplog.records)
    assert api.cancelled == []


def test_거부응답_주문번호0이면_예외_미체결로_안남김():
    """야간 02752(증거금부족)는 OutBlock을 동봉해 예외 없이 온다 → 주문번호 0이면 거부 처리."""
    import pytest

    from lsapi.exceptions import LSOrderError

    resp = {"rsp_cd": "02752", "rsp_msg": "증거금부족으로 주문이 불가합니다.",
            "CFOAT00100OutBlock2": {"OrdNo": 0}}
    b, _ = _broker(resp)
    with pytest.raises(LSOrderError, match="02752"):
        b.place_order("buy", 2, 1363.0)
    assert b.get_open_orders() == []   # 존재하지 않는 주문을 미체결로 들고 있지 않음


def test_주문번호_없거나_빈값도_거부():
    import pytest

    from lsapi.exceptions import LSOrderError

    for out2 in ({}, {"OrdNo": ""}, {"OrdNo": "  "}):
        b, _ = _broker({"rsp_cd": "99999", "CFOAT00100OutBlock2": out2})
        with pytest.raises(LSOrderError):
            b.place_order("buy", 1, 1363.0)

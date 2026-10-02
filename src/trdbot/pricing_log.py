"""주문가격 계산 검증용 CSV 로그.

매 집행 사이클의 호가·공백·주문가격을 파일로 남겨, 가격이 제대로 계산되는지 확인한다.
파일: logs/order_pricing_YYYYMMDD.csv (날짜별, append). 엑셀에서 바로 열림(utf-8-sig).

컬럼:
  time        기록 시각(HH:MM:SS.mmm)
  code        종목코드
  best_bid    매수1호가
  best_ask    매도1호가
  tick        호가단위(역산/폴백)
  gap         호가공백((매도1−매수1)/틱 − 1)
  offset      자기호가에서 더한 틱 수(floor(gap/2)+1, 주문안함이면 빈칸)
  buy_price   매수 주문가(매수1 + offset×틱)
  sell_price  매도 주문가(매도1 − offset×틱)
  target      목표계약수
  position    현재 포지션
  action      집행 판단(order/stop/skip)
  detail      주문 내용(side qty@price) 또는 사유
"""

from __future__ import annotations

import csv
import logging
import threading
from datetime import datetime

from lsapi.paths import app_base_dir

logger = logging.getLogger(__name__)

_LOG_DIR = app_base_dir() / "logs"
_HEADER = ["time", "code", "best_bid", "best_ask", "tick", "gap", "offset",
           "buy_price", "sell_price", "target", "position", "action", "detail"]
_lock = threading.Lock()


def log_pricing(code: str, info: dict) -> None:
    """주문가격 계산 1행을 날짜별 CSV에 append(스레드 안전). 실패해도 매매엔 지장 없음."""
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = _LOG_DIR / f"order_pricing_{datetime.now():%Y%m%d}.csv"
        is_new = not path.exists()
        row = {
            "time": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "code": code,
            "best_bid": info.get("best_bid"),
            "best_ask": info.get("best_ask"),
            "tick": info.get("tick"),
            "gap": info.get("gap"),
            "offset": info.get("offset"),
            "buy_price": info.get("buy_price"),
            "sell_price": info.get("sell_price"),
            "target": info.get("target"),
            "position": info.get("position"),
            "action": info.get("action"),
            "detail": info.get("detail"),
        }
        with _lock, path.open("a", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=_HEADER)
            if is_new:
                w.writeheader()
            w.writerow(row)
    except Exception:
        logger.exception("주문가격 로그 기록 실패")

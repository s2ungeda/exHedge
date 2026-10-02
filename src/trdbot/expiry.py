"""미국달러선물 만기(최종거래일)·롤오버일 계산 + KRX 휴일 로딩.

- 최종거래일(만기일) = 결제월 **셋째 월요일**. 그날이 휴일이면 **직전 영업일**로 당김.
- 롤오버일 = **만기 전주 금요일**. 그날이 휴일이면 전날, 그전날도 휴일이면 또 전날.
- 휴일: config/holidays.txt (YYYY-MM-DD 한 줄씩, '#' 주석). 파일 없으면 주말만 휴일로 본다.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from lsapi.paths import app_base_dir


def holidays_path() -> Path:
    return app_base_dir() / "config" / "holidays.txt"


def load_holidays(path: Path | None = None) -> set[date]:
    """휴일 파일(YYYY-MM-DD 줄단위) → date 집합. 없거나 오류면 빈 집합(주말만)."""
    p = Path(path) if path else holidays_path()
    out: set[date] = set()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        digits = "".join(ch for ch in s if ch.isdigit())[:8]
        if len(digits) == 8:
            try:
                out.add(date(int(digits[:4]), int(digits[4:6]), int(digits[6:8])))
            except ValueError:
                pass
    return out


def third_monday(year: int, month: int) -> date:
    """그 달의 셋째 월요일."""
    first = date(year, month, 1)
    return first + timedelta(days=(0 - first.weekday()) % 7 + 14)


def _on_or_before_biz(d: date, holidays: set[date]) -> date:
    """d가 주말/휴일이면 직전 영업일로 당긴다."""
    while d.weekday() >= 5 or d in holidays:   # 5=토, 6=일
        d -= timedelta(days=1)
    return d


def expiry_day(year: int, month: int, holidays: set[date]) -> date:
    """최종거래일 — 셋째 월요일(휴일이면 직전 영업일)."""
    return _on_or_before_biz(third_monday(year, month), holidays)


def expiry_prev_day(year: int, month: int, holidays: set[date]) -> date:
    """만기 직전 거래일 — 최종거래일의 직전 영업일."""
    return _on_or_before_biz(expiry_day(year, month, holidays) - timedelta(days=1), holidays)


def rollover_window(year: int, month: int) -> tuple[date, date]:
    """롤오버 알림 구간 [만기 전주 화요일, 만기 전주 금요일].

    (셋째월요일 -6일=전주 화, -3일=전주 금). 구간 내 주말·휴일은 호출측이 건너뛴다.
    """
    mon = third_monday(year, month)
    return mon - timedelta(days=6), mon - timedelta(days=3)


def is_trading_day(d: date, holidays: set[date]) -> bool:
    """주말·휴일이 아니면 True(영업일)."""
    return d.weekday() < 5 and d not in holidays

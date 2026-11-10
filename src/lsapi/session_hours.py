"""미국달러선물 거래시간 — 평소 시간 + 특별일(수능일 등) 시간표.

장운영(JIF)은 상태가 바뀔 때만 오고 조회가 안 된다. 장중 재시작·재접속 직후엔 다음 전환
전까지 현재 국면을 모르므로 **시각으로 폴백**하는데, 거래소가 시간을 바꾸는 날(예: 수능일
주간 09:45 개장 / 야간 19:00 개장)엔 고정 시각이 틀린다. 그래서 그런 날의 시간을
config/special_hours.txt 에 미리 적어 두고, 시각 폴백이 그걸 따른다.

파일 형식 (한 줄에 하루, '#' 뒤는 주석):
    2026-11-19  09:45-15:45  19:00-06:00   # 수능일
    날짜        주간 개장-마감  야간 개장-익일마감
야간 마감은 **다음 날 새벽** 시각이다(그 날짜에 시작한 야간장 기준).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Hours:
    """하루 거래시간(HHMM 정수). 동시호가 포함 세션 경계."""

    day_open: int = 845      # 주간 접속매매 시작
    day_close: int = 1545    # 주간 마감(15:35~15:45 장마감 동시호가 포함)
    night_open: int = 1800   # 야간 접속매매 시작
    night_close: int = 600   # 야간 마감(익일 새벽, 05:50~06:00 종가단일가 포함)


DEFAULT_HOURS = Hours()
CLOSE_AUCTION_MIN = 10   # 마감 전 단일가(동시호가) 길이 — 주간 15:35~15:45, 야간 05:50~06:00

_special: dict[date, Hours] = {}


def set_special_hours(table: dict[date, Hours]) -> None:
    """특별일 시간표 등록(앱 시동 시 1회). 이후 session_now·시각 폴백이 이걸 따른다."""
    global _special
    _special = dict(table)


def hours_for(d: date) -> Hours:
    """그 날짜의 거래시간 — 특별일이면 그 값, 아니면 평소 시간."""
    return _special.get(d, DEFAULT_HOURS)


def minus_minutes(hhmm: int, minutes: int) -> int:
    """HHMM에서 분을 뺀 HHMM(같은 날 안에서만 쓰는 용도)."""
    total = (hhmm // 100) * 60 + hhmm % 100 - minutes
    total %= 24 * 60
    return (total // 60) * 100 + total % 60


def night_close_today(today: date) -> int:
    """오늘 새벽에 끝나는 야간장의 마감 — **어제 시작한** 야간장 기준."""
    return hours_for(today - timedelta(days=1)).night_close


_RANGE = re.compile(r"(\d{1,2}):?(\d{2})\s*-\s*(\d{1,2}):?(\d{2})")


def _hhmm(h: str, m: str) -> int:
    hh, mm = int(h), int(m)
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"시각 범위 오류: {h}:{m}")
    return hh * 100 + mm


def parse_special_hours(text: str) -> dict[date, Hours]:
    """시간표 텍스트 → {날짜: Hours}. 형식이 틀린 줄은 경고 로그 남기고 건너뛴다."""
    out: dict[date, Hours] = {}
    for no, line in enumerate(text.splitlines(), 1):
        s = line.split("#", 1)[0].strip()
        if not s:
            continue
        try:
            m = re.match(r"(\d{4})-(\d{2})-(\d{2})\s+(.*)$", s)
            if not m:
                raise ValueError("날짜(YYYY-MM-DD)로 시작해야 함")
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            ranges = _RANGE.findall(m.group(4))
            if len(ranges) != 2:
                raise ValueError("주간·야간 시간 두 개(HH:MM-HH:MM)가 필요")
            (a, b, c, e), (f, g, h, i) = ranges
            hours = Hours(_hhmm(a, b), _hhmm(c, e), _hhmm(f, g), _hhmm(h, i))
            if hours.day_open >= hours.day_close:
                raise ValueError("주간 개장이 마감보다 늦음")
        except ValueError as ex:
            logger.warning("특별일 시간표 %d행 무시(%s): %s", no, ex, line.strip())
            continue
        out[d] = hours
    return out


def load_special_hours(path: Path) -> dict[date, Hours]:
    """시간표 파일 읽기. 없으면 빈 표(평소 시간만)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    table = parse_special_hours(text)
    if table:
        logger.info("특별일 시간표 %d건 적용: %s", len(table),
                    ", ".join(f"{d} 주간 {h.day_open:04d}-{h.day_close:04d} "
                              f"야간 {h.night_open:04d}-{h.night_close:04d}"
                              for d, h in sorted(table.items())))
    return table

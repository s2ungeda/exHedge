"""자동주문화면 (Tkinter).

목업 "자동주문화면"을 모던하게 구현한 입력 폼.
- 노란색 칸 = 사용자 입력
- 흰색 칸   = 조회값(잔고 등) — API 배선 전까지 placeholder
- 강조 칸   = 산출값(목표계약수 등)

설계 단일 소스: docs/execution-engine.md

검증/설정수집은 순수 함수(parse_*, build_config)로 분리 — GUI 없이 테스트 가능.
실제 집행(executor)·잔고배선은 별도. 실행버튼은 on_execute 콜백으로 위임한다.
"""

from __future__ import annotations

import json
import logging
import re
import tkinter as tk
from dataclasses import dataclass, field
from datetime import time
from tkinter import messagebox, ttk
from typing import Callable

from lsapi.paths import app_base_dir

logger = logging.getLogger(__name__)

# 화면 입력값 저장 위치 (비밀값 아님 — 시간/종목/비율/수량). 비밀은 keyring 사용.
# 소스 실행이면 프로젝트 루트, 배포(.exe)면 실행파일 옆의 config/.
_STATE_PATH = app_base_dir() / "config" / "order_screen.json"


# ──────────────────────────────────────────────────────────
# 순수 로직 (GUI 비의존, 테스트 대상)
# ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class TimeWindow:
    start: time
    end: time

    def crosses_midnight(self) -> bool:
        return self.end <= self.start

    def __str__(self) -> str:
        return f"{self.start:%H:%M:%S}~{self.end:%H:%M:%S}"


@dataclass
class OrderConfig:
    """자동주문화면 입력값 모음. executor의 입력이 된다."""

    code: str
    qty_per_order: int          # 1회주문계약수
    order_interval_sec: float   # 주문 인터벌(s) — N초마다 재평가, 호가 바뀌면 취소·재호가
    max_position: int           # 최대잔고한도(계약)
    hedge_ratio: float           # 헤지비율 B(%)
    day_window: TimeWindow      # 주간 주문가능시간
    night_window: TimeWindow    # 야간 주문가능시간
    fill_window_min: float = 10.0  # 체결량 감시 시간(분)
    fill_limit: int = 0            # 이 시간 내 누적 체결량 한도(개). 0=기능 끔
    usd_futures_held: int = 0      # 보유 원달러(계약수 ±) → C(달러노출선물계약수) 그대로
    extra: dict = field(default_factory=dict)


def parse_time(s: str) -> time:
    """'H:M:S' / 'HH:MM:SS' / 'HH:MM' → datetime.time."""
    s = s.strip()
    if not s:
        raise ValueError("시간이 비어 있습니다.")
    parts = s.split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"시간 형식 오류: {s!r} (HH:MM:SS 형태로 입력)")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        raise ValueError(f"시간에 숫자가 아닌 값: {s!r}")
    h, m = nums[0], nums[1]
    sec = nums[2] if len(nums) == 3 else 0
    if not (0 <= h <= 23 and 0 <= m <= 59 and 0 <= sec <= 59):
        raise ValueError(f"시간 범위 오류: {s!r}")
    return time(h, m, sec)


def _num(label: str, s: str, *, cast, minv=None, allow_zero=True):
    s = s.strip().replace(",", "")  # 천단위 콤마 허용 (표시값 "1,380.50" 등도 파싱)
    if s == "":
        raise ValueError(f"{label}: 비어 있습니다.")
    try:
        v = cast(s)
    except ValueError:
        raise ValueError(f"{label}: 숫자 형식 오류 ({s!r})")
    if not allow_zero and v == 0:
        raise ValueError(f"{label}: 0이 될 수 없습니다.")
    if minv is not None and v < minv:
        raise ValueError(f"{label}: {minv} 이상이어야 합니다 (입력 {v}).")
    return v


def _is_signed_int_input(text: str) -> bool:
    """입력 중인 칸 값 검사 — 빈칸, '-'(입력 중), 부호 있는 정수만 허용."""
    return re.fullmatch(r"-?\d*", text) is not None


def _held(raw: dict[str, str]) -> int:
    """보유 원달러(±정수 계약수). 빈칸·숫자 아님·소수면 ValueError.

    C에 그대로 들어가는 값이라 잘못된 입력이나 실수로 지운 칸을 0으로 삼키면 안 된다
    (실행·적용 전에 막는다). 0이면 0을 직접 입력. 칸 기본값은 0.
    """
    return _num("보유 원달러(계약수)", raw.get("usd_futures_held", ""), cast=int)


def build_config(raw: dict[str, str]) -> OrderConfig:
    """화면 입력(str dict) → 검증된 OrderConfig. 오류 시 ValueError.

    raw 키: code, qty_per_order, order_interval_sec, max_position,
            hedge_ratio, day_start, day_end, night_start, night_end
    (환율은 입력이 아니라 시그널이 주는 조회값 → 여기서 다루지 않음)
    """
    code = raw.get("code", "").strip()
    if not code:
        raise ValueError("종목코드: 비어 있습니다.")

    qty = _num("1회주문계약수", raw.get("qty_per_order", ""), cast=int, minv=2)
    interval = _num("주문 인터벌(s)", raw.get("order_interval_sec", ""), cast=float, minv=0, allow_zero=False)
    maxpos = _num("최대잔고한도", raw.get("max_position", ""), cast=int, minv=1)
    b = _num("헤지비율(B)", raw.get("hedge_ratio", ""), cast=float, minv=0)
    held = _held(raw)
    fwin, flim = _fill_params(raw)

    day = TimeWindow(parse_time(raw.get("day_start", "")), parse_time(raw.get("day_end", "")))
    night = TimeWindow(parse_time(raw.get("night_start", "")), parse_time(raw.get("night_end", "")))

    if qty > maxpos:
        raise ValueError(f"1회주문계약수({qty})가 최대잔고한도({maxpos})보다 큽니다.")

    return OrderConfig(
        code=code, qty_per_order=qty, order_interval_sec=interval, max_position=maxpos,
        hedge_ratio=b,
        day_window=day, night_window=night,
        fill_window_min=fwin, fill_limit=flim, usd_futures_held=held,
    )


def _fill_params(raw: dict[str, str]) -> tuple[float, int]:
    """체결량 한도 입력 → (감시시간 분, 한도 개). 빈값이면 (10분, 0=기능 끔)."""
    win_raw = raw.get("fill_window_min", "").strip()
    win = _num("체결감시시간(분)", win_raw, cast=float, minv=0) if win_raw else 10.0
    lim_raw = raw.get("fill_limit", "").strip()
    lim = _num("체결량한도", lim_raw, cast=int, minv=0) if lim_raw else 0
    return win, lim


# 실행 중에도 변경 가능한(=적용 버튼으로 반영) 입력 키.
# 종목코드·시간창은 실행(Ready) 중 잠금.
LIVE_KEYS = (
    "hedge_ratio", "usd_futures_held",
    "qty_per_order", "order_interval_sec", "max_position",
    "fill_window_min", "fill_limit",
)


def parse_live_params(raw: dict[str, str]) -> dict:
    """실행 중 변경 가능한 7개 항목을 검증해 dict로 반환. 오류 시 ValueError.

    B(헤지비율)·보유 원달러(±계약수) + 1회주문계약수·주문인터벌·최대잔고한도
    + 체결감시시간(분)·체결량한도(개).
    """
    b = _num("헤지비율(B)", raw.get("hedge_ratio", ""), cast=float, minv=0)
    held = _held(raw)
    qty = _num("1회주문계약수", raw.get("qty_per_order", ""), cast=int, minv=2)
    interval = _num("주문 인터벌(s)", raw.get("order_interval_sec", ""), cast=float,
                    minv=0, allow_zero=False)
    maxpos = _num("최대잔고한도", raw.get("max_position", ""), cast=int, minv=1)
    fwin, flim = _fill_params(raw)
    if qty > maxpos:
        raise ValueError(f"1회주문계약수({qty})가 최대잔고한도({maxpos})보다 큽니다.")
    return {
        "hedge_ratio": b, "usd_futures_held": held,
        "qty_per_order": qty, "order_interval_sec": interval, "max_position": maxpos,
        "fill_window_min": fwin, "fill_limit": flim,
    }


# ──────────────────────────────────────────────────────────
# 디자인 토큰
# ──────────────────────────────────────────────────────────
C = {
    "bg": "#EEF1F5",         # 윈도우 배경
    "card": "#FFFFFF",       # 카드
    "border": "#DCE1E8",     # 카드/표시칸 테두리
    "header": "#1E293B",     # 상단 타이틀 바
    "text": "#1F2933",       # 본문
    "text_sub": "#6B7280",   # 보조
    "accent": "#2563EB",     # 강조(실행 버튼)
    "accent_dark": "#1D4ED8",
    "danger": "#DC2626",     # 중지 버튼/실행중 표시
    "danger_dark": "#B91C1C",
    "input_bg": "#FFF6BF",   # 입력칸(부드러운 노랑)
    "input_bd": "#E6C200",
    "input_off": "#ECECE4",  # 입력칸 비활성(실행중 잠금)
    "chip": "#EEF2F7",       # 산출 표시칸
    "chip_white": "#FFFFFF",  # 조회 표시칸
    "running_bg": "#FB923C",  # 실행 중 표시 — 주문가능시간 표 배경(선명한 주황)
    "ok": "#1565C0",
    "err": "#C62828",
}
F = {
    "title": ("Malgun Gothic", 12, "bold"),
    "section": ("Malgun Gothic", 9, "bold"),
    "label": ("Malgun Gothic", 9),
    "small": ("Malgun Gothic", 8),
    "input": ("Malgun Gothic", 9, "bold"),    # 숫자 입력 → 볼드
    "value": ("Malgun Gothic", 9, "bold"),    # 숫자 표시 → 볼드
    "hero": ("Malgun Gothic", 11, "bold"),
    "btn": ("Malgun Gothic", 9, "bold"),
    "status": ("Malgun Gothic", 8),
    "run": ("Malgun Gothic", 15, "bold"),     # '자동주문 실행중' 표시
}


# ──────────────────────────────────────────────────────────
# 입력값 저장/로드
# ──────────────────────────────────────────────────────────
def _load_state() -> dict[str, str]:
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(values: dict[str, str]) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("화면 설정 저장 실패: %s", e)


# ──────────────────────────────────────────────────────────
# Tkinter View
# ──────────────────────────────────────────────────────────
class OrderScreen:
    """자동주문화면. on_execute(config)로 실행을 위임한다(미지정 시 설정 확인 팝업)."""

    DISPLAY_KEYS = (
        "exchange_rate",
        "total_domestic", "total_coin", "usd_futures_balance",
        "dollar_contracts", "hedge_contracts", "target_contracts",
    )

    def __init__(
        self,
        on_execute: Callable[[OrderConfig], None] | None = None,
        on_stop: Callable[[], None] | None = None,
        on_apply: Callable[[dict], None] | None = None,
        on_trade_toggle: Callable[[bool], None] | None = None,
        on_query: Callable[[], None] | None = None,
        on_alarm_dismiss: Callable[[], None] | None = None,
        env: str = "mock",
        signal_token: str = "",
    ):
        # on_execute: 실행 시작. on_stop: 중지. on_apply: 실행 중 6개 항목 변경.
        # on_trade_toggle: '주문' 체크박스 토글(True=실주문 허용 / False=관찰모드).
        # on_query: [조회] — 봇의 미체결 캐시·포지션 강제 갱신. env: 'prod'/'mock'.
        # signal_token: 시그널 발신처 구분(Dalin/Meme 등) — 캡션·로그줄 색으로 구분.
        self.on_execute = on_execute
        self.on_stop = on_stop
        self.on_apply = on_apply
        self.on_trade_toggle = on_trade_toggle
        self.on_query = on_query
        self.on_alarm_dismiss = on_alarm_dismiss
        self._env = env
        self._signal_token = signal_token
        self._is_meme = signal_token == "Meme"   # Meme 발신처 전용 표시 분기(단위·숨김)
        self._running = False
        self._btn_base = C["accent"]
        self._btn_hover = C["accent_dark"]
        self.root = tk.Tk()
        self.root.title(f"자동주문화면 [{'운영' if env == 'prod' else '모의'}]")
        self.root.configure(bg=C["bg"])
        self.root.resizable(False, False)
        # 안전 게이트: 체크돼 있을 때만 실제 주문이 나간다. 기본 꺼짐.
        self.trade_enabled = tk.BooleanVar(value=False)
        self.sound_enabled = tk.BooleanVar(value=True)   # 체결 시 윈도우 경고음(기본 켜짐)
        self._saved = _load_state()
        self._inputs: dict[str, tk.Entry] = {}
        self._displays: dict[str, tk.Label] = {}
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _initial(self, key: str, default: str = "") -> str:
        """저장된 입력값이 있으면 그걸로, 없으면 기본값으로 프리필."""
        return str(self._saved.get(key, default))

    # ---- 위젯 헬퍼 ----
    def _card(self, parent, title: str = "") -> tk.Frame:
        # title 없으면 제목 헤더 생략(밀착). 카드 테두리만 유지해 시각 구분.
        outer = tk.Frame(parent, bg=C["card"], highlightthickness=1,
                         highlightbackground=C["border"], highlightcolor=C["border"])
        outer.pack(fill="x", pady=(0, 2), padx=1)
        if title:
            head = tk.Frame(outer, bg=C["card"])
            head.pack(fill="x", padx=6, pady=(2, 0))
            tk.Frame(head, bg=C["accent"], width=3, height=10).pack(side="left", padx=(0, 6))
            tk.Label(head, text=title, font=F["section"], bg=C["card"],
                     fg=C["text"]).pack(side="left")
        inner = tk.Frame(outer, bg=C["card"], padx=6, pady=2)
        inner.pack(fill="x")
        return inner

    def _lab(self, parent, text, sub=False, **kw):
        return tk.Label(parent, text=text, font=F["label"], bg=C["card"],
                        fg=C["text_sub"] if sub else C["text"], anchor="w", **kw)

    def _entry(self, parent, key, default="", width=12):
        e = tk.Entry(parent, font=F["input"], bg=C["input_bg"], fg=C["text"], width=width,
                     justify="right", relief="flat", insertbackground=C["text"],
                     disabledbackground=C["input_off"], disabledforeground="#9AA0A6",
                     highlightthickness=1, highlightbackground=C["input_bd"], highlightcolor=C["accent"])
        val = self._initial(key, default)  # 저장된 값 우선, 없으면 기본값
        if val:
            e.insert(0, val)
        self._inputs[key] = e
        return e

    def _display(self, parent, key, *, computed=False, hero=False, width=12, font=None):
        if hero:
            bg, fg, dfont = C["accent"], "white", F["hero"]
        elif computed:
            bg, fg, dfont = C["chip"], C["text"], F["value"]
        else:
            bg, fg, dfont = C["chip_white"], C["text_sub"], F["value"]
        font = font or dfont
        lbl = tk.Label(parent, text="—", font=font, width=width, anchor="e", bg=bg, fg=fg,
                       relief="flat", padx=6, pady=1,
                       highlightthickness=1, highlightbackground=C["border"])
        self._displays[key] = lbl
        return lbl

    def _build(self):
        # 발신처(SIGNAL_TOKEN) 구분 색 — Meme이면 보라 배경·앰버 글씨, 그 외(Dalin 등)는 기본.
        is_meme = self._signal_token == "Meme"
        log_bg = "#6D28D9" if is_meme else C["header"]
        log_fg = "#FDE68A" if is_meme else "#E5E7EB"

        title_fg = log_fg if is_meme else "white"   # 캡션 폰트색도 로그와 동일 테마
        # 상단 타이틀 바 — 배경·폰트색을 로그줄과 같이(Meme이면 보라·앰버).
        bar = tk.Frame(self.root, bg=log_bg)
        bar.pack(fill="x")
        tk.Label(bar, text="자동주문화면", font=F["title"], bg=log_bg, fg=title_fg).pack(
            side="left", padx=(16, 0), pady=3)
        # 모의/운영 배지 — 운영(실매매)은 빨강으로 확실히 구분(실수로 실매매 방지).
        is_prod = self._env == "prod"
        tk.Label(bar, text=" 운영 " if is_prod else " 모의 ", font=(F["title"][0], 10, "bold"),
                 bg=(C["danger"] if is_prod else "#475569"), fg="white",
                 padx=6, pady=2).pack(side="left", padx=(10, 0), pady=3)
        # 발신처 토큰 배지 — 구분용. Meme이면 보라, 그 외는 회색.
        if self._signal_token:
            tk.Label(bar, text=f" {self._signal_token} ", font=(F["title"][0], 10, "bold"),
                     bg=("#6D28D9" if is_meme else "#475569"),
                     fg=("#FDE68A" if is_meme else "white"),
                     padx=6, pady=2).pack(side="left", padx=(6, 0), pady=3)
        # 장운영 상태 칩(우측) — 접속매매/동시호가/장마감 등. 앱이 주기적으로 갱신.
        self.market_label = tk.Label(bar, text="● 장운영 확인중", font=F["status"],
                                     bg=log_bg, fg="#9FB3C8", anchor="e")
        self.market_label.pack(side="right", padx=(8, 16), pady=3)
        # 빌드 날짜/버전 — 어느 빌드가 실행 중인지 구분(다른 PC 배포본 식별용).
        from trdbot.version import build_label
        tk.Label(bar, text=f"build {build_label()}", font=F["small"], bg=log_bg,
                 fg="#7C8AA0").pack(side="right", padx=(0, 4), pady=3)

        # 리스크 알람 배너 — 평소엔 숨김. 알람 발생 시 빨간 띠 + 사유 + [알람 해제] 버튼.
        # 사용자가 해제할 때까지 앱이 1분마다 경고음. (체결량한도·최대잔고한도·연결끊김 등)
        self.alarm_bar = tk.Frame(self.root, bg=C["danger"])
        self.alarm_label = tk.Label(self.alarm_bar, text="", font=(F["title"][0], 11, "bold"),
                                    bg=C["danger"], fg="white", anchor="w")
        self.alarm_label.pack(side="left", padx=(14, 8), pady=5, fill="x", expand=True)
        tk.Button(self.alarm_bar, text="알람 해제", font=F["btn"], bg="white", fg=C["danger"],
                  relief="flat", bd=0, cursor="hand2", padx=12, pady=3,
                  command=lambda: self.on_alarm_dismiss and self.on_alarm_dismiss()
                  ).pack(side="right", padx=(0, 12), pady=5)
        # 초기엔 pack 안 함(숨김) — show_alarm에서 토글.

        # 로그 줄 — 제목 바로 아래 별도 칸. 시그널·집행 메시지를 볼드체로 표시.
        # 발신처(Meme)면 배경·글씨색을 바꿔 한눈에 구분. width=1+fill: 창 폭 안 늘림.
        logbar = tk.Frame(self.root, bg=log_bg)
        logbar.pack(fill="x")
        self._logbar = logbar   # 알람 배너를 이 위(캡션 아래)에 끼워 넣기 위한 기준
        self.log_label = tk.Label(logbar, text="", font=(F["title"][0], 10, "bold"),
                                  bg=log_bg, fg=log_fg, anchor="w", width=1)
        self.log_label.pack(side="left", padx=(16, 16), pady=(0, 4), fill="x", expand=True)

        body = tk.Frame(self.root, bg=C["bg"], padx=6, pady=4)
        body.pack(fill="both", expand=True)

        self._build_time(body)
        self._build_symbol(body)
        self._build_order(body)     # 주문설정을 잔고/목표 위로(위치 변경)
        self._build_balance(body)
        self._build_footer(body)

    def _build_time(self, body):
        c = self._card(body)
        self._time_bg = [c]   # Ready 시 주황 배경으로 바꿀 위젯들(표 프레임 + 라벨들)
        # 주간(위)·야간(아래) 2행.
        for r, (tag, ks, ke, ds, de) in enumerate([
            ("주간", "day_start", "day_end", "8:00:00", "15:45:00"),
            ("야간", "night_start", "night_end", "18:00:00", "4:00:00"),
        ]):
            tag_lbl = tk.Label(c, text=tag, font=F["small"], bg=C["chip"], fg=C["text"],
                               width=5, pady=1)
            tag_lbl.grid(row=r, column=0, sticky="w", pady=1, padx=(0, 6))
            self._entry(c, ks, ds, width=10).grid(row=r, column=1, pady=1)
            sep = tk.Label(c, text="~", font=F["label"], bg=C["card"], fg=C["text_sub"])
            sep.grid(row=r, column=2, padx=4)
            self._entry(c, ke, de, width=10).grid(row=r, column=3, pady=1)
            self._time_bg += [tag_lbl, sep]
        # '자동주문중' — 주문가능시간 오른쪽 공간. Ready+주문 체크 시 빨간 볼드 15로 표시.
        # trade_enabled 변화(사용자 토글·브레이커 자동해제 모두)에 자동 반응.
        self.auto_run_label = tk.Label(c, text="", font=F["run"], bg=C["card"], fg=C["danger"])
        self.auto_run_label.grid(row=0, column=4, rowspan=2, padx=(24, 0), sticky="w")
        self._time_bg.append(self.auto_run_label)
        self.trade_enabled.trace_add("write", lambda *_: self.auto_run_label.configure(
            text="자동주문중" if self.trade_enabled.get() else ""))

    def _set_time_bg(self, running: bool) -> None:
        """Ready(실행) 중이면 주문가능시간 표 전체를 주황으로, 정지면 원래 색으로 복귀."""
        bg = C["running_bg"] if running else C["card"]
        for w in self._time_bg:      # 프레임·태그·구분자·자동주문 라벨
            w.configure(bg=bg)
        # 시간 입력칸(실행 중엔 잠금=비활성)도 주황으로 → 표 전체가 오렌지로 확실히 보이게.
        ebg = C["running_bg"] if running else C["input_off"]
        efg = C["text"] if running else "#9AA0A6"
        for key in ("day_start", "day_end", "night_start", "night_end"):
            self._inputs[key].configure(disabledbackground=ebg, disabledforeground=efg)

    def _build_symbol(self, body):
        c = self._card(body)
        self._lab(c, "종목코드").grid(row=0, column=0, sticky="w", pady=1, padx=(0, 6))
        # 종목코드: 콤보박스(근월/차근월). 앱 시작 시 t8426으로 채움. 표시는 라벨, 값은 코드.
        self._code_map: dict[str, str] = {}   # 표시라벨 → 종목코드
        self.code_var = tk.StringVar(value="불러오는 중…")
        self.code_combo = ttk.Combobox(c, textvariable=self.code_var, state="readonly",
                                       width=12, font=F["input"], values=[])
        self.code_combo.grid(row=0, column=1, sticky="w", pady=1)
        self._lab(c, "환율").grid(row=0, column=2, sticky="e", pady=1, padx=(14, 6))
        self._display(c, "exchange_rate", width=9).grid(row=0, column=3, sticky="w", pady=1)

        # 보유 원달러(왼쪽) + 헤지비율(오른쪽)을 한 행에 — 세로 한 칸 절약.
        # 보유 원달러: 유지할 원달러선물 계약수(±정수) → C = 입력값 그대로.
        # (키를 예전 manual_balance_adjust(금액)에서 바꿈 → 저장된 옛 금액이 계약수로 읽히지 않게)
        self._lab(c, "보유 원달러").grid(
            row=1, column=0, sticky="w", pady=1, padx=(0, 6))
        wrap = tk.Frame(c, bg=C["card"])
        wrap.grid(row=1, column=1, sticky="w", pady=1)
        held = self._entry(wrap, "usd_futures_held", "0", width=8)
        if not held.get().strip():
            held.insert(0, "0")   # 기본값 0 — 저장값이 빈칸이어도(비운 채 닫음) 0으로 시작
        # 숫자와 맨 앞 '-'만 입력되게(키 입력·붙여넣기 모두). 실행/적용 때 build_config도 재검증.
        held.configure(validate="key",
                       validatecommand=(held.register(_is_signed_int_input), "%P"))
        held.pack(side="left")
        self._lab(c, "헤지비율").grid(row=1, column=2, sticky="e", pady=1, padx=(14, 6))
        self._pct(c, "hedge_ratio", 1, 3)

    def _pct(self, parent, key, row, col):
        wrap = tk.Frame(parent, bg=C["card"])
        wrap.grid(row=row, column=col, sticky="w", pady=1)
        self._entry(wrap, key, width=8).pack(side="left")
        tk.Label(wrap, text="%", font=F["label"], bg=C["card"], fg=C["text_sub"]).pack(
            side="left", padx=(4, 0))

    def _build_balance(self, body):
        c = self._card(body)
        left = tk.Frame(c, bg=C["card"])
        left.grid(row=0, column=0, sticky="nw", padx=(0, 20))
        right = tk.Frame(c, bg=C["card"])
        right.grid(row=0, column=1, sticky="nw")

        big = ("Malgun Gothic", 16, "bold")   # 핵심 수치(선물잔고·목표) 크게 — +3

        # 왼쪽: 코인포지션합 + C + D (중간 계산값)
        left_rows = [("코인포지션합(밈/$,달/원)", "total_coin", 13, False),
                     ("달러노출선물계약수 (C)", "dollar_contracts", 6, True),
                     ("포지션헤지비율 (D)", "hedge_contracts", 6, True)]
        for r, (lab, key, w, computed) in enumerate(left_rows):
            self._lab(left, lab, sub=True).grid(row=r, column=0, sticky="w", pady=1, padx=(0, 6))
            self._display(left, key, computed=computed, width=w).grid(
                row=r, column=1, sticky="w", pady=1)

        # 오른쪽: 원달러 선물잔고 + 목표계약수를 나란히 — 큰 글씨로 잘 보이게.
        pairs = [("원달러 선물잔고", "usd_futures_balance", True),
                 ("목표계약수 (C+D)", "target_contracts", False)]
        for col, (lab, key, sub) in enumerate(pairs):
            padx = (0 if col == 0 else 20, 6)
            self._lab(right, lab, sub=sub).grid(
                row=0, column=col, sticky="w", pady=(0, 1), padx=padx)
            self._display(right, key, computed=True, width=6, font=big).grid(
                row=1, column=col, sticky="w", padx=padx)

    def _build_order(self, body):
        c = self._card(body)
        w = 5   # 1회주문계약수~체결량한도: 3자리면 충분 → 입력칸 좁게
        self._lab(c, "1회주문계약수").grid(row=0, column=0, sticky="w", pady=1, padx=(0, 6))
        self._entry(c, "qty_per_order", "3", width=w).grid(row=0, column=1, sticky="w", pady=1)
        self._lab(c, "주문 인터벌(s)").grid(row=0, column=2, sticky="e", pady=1, padx=(14, 6))
        self._entry(c, "order_interval_sec", width=w).grid(row=0, column=3, sticky="w", pady=1)

        # 최대잔고한도 + 체결감시(분) + 체결량한도를 한 행에 — 세로 한 칸 절약.
        # 체결량 서킷 브레이커: 최근 N분 누적 체결이 한도 이상이면 미체결 취소+주문 중지(0=끔).
        self._lab(c, "최대잔고한도").grid(row=1, column=0, sticky="w", pady=1, padx=(0, 6))
        wrap = tk.Frame(c, bg=C["card"])
        wrap.grid(row=1, column=1, sticky="w", pady=1)
        self._entry(wrap, "max_position", "100", width=w).pack(side="left")
        tk.Label(wrap, text="계약", font=F["label"], bg=C["card"], fg=C["text_sub"]).pack(
            side="left", padx=(4, 0))
        self._lab(c, "체결감시(분)").grid(row=1, column=2, sticky="e", pady=1, padx=(14, 6))
        self._entry(c, "fill_window_min", "10", width=w).grid(row=1, column=3, sticky="w", pady=1)
        self._lab(c, "체결량한도").grid(row=1, column=4, sticky="e", pady=1, padx=(14, 6))
        wrap2 = tk.Frame(c, bg=C["card"])
        wrap2.grid(row=1, column=5, sticky="w", pady=1)
        self._entry(wrap2, "fill_limit", "0", width=w).pack(side="left")
        tk.Label(wrap2, text="개(0=끔)", font=F["label"], bg=C["card"], fg=C["text_sub"]).pack(
            side="left", padx=(4, 0))

    def _build_footer(self, body):
        foot = tk.Frame(body, bg=C["bg"])
        foot.pack(fill="x", pady=(4, 0))
        # width=1 + fill/expand: 카드 너비가 창 폭을 정하고 상태줄은 남는 공간만 채운다.
        # (고정 너비가 없으면 적용 시 글자 길이에 따라 창이 늘었다 줄어든다 — 그 흔들림 방지)
        self.status = tk.Label(foot, text="Ready를 누르면 시그널 수신을 시작합니다.", font=F["status"],
                               bg=C["bg"], fg=C["text_sub"], anchor="w", width=1)
        self.status.pack(side="left", fill="x", expand=True)

        self.exec_btn = tk.Button(foot, text="Ready", font=F["btn"], bg=self._btn_base, fg="white",
                                  activebackground=self._btn_hover, activeforeground="white",
                                  relief="flat", bd=0, cursor="hand2", padx=18, pady=5,
                                  command=self._on_toggle)
        self.exec_btn.pack(side="right")
        self.exec_btn.bind("<Enter>", lambda _e: self.exec_btn.config(bg=self._btn_hover))
        self.exec_btn.bind("<Leave>", lambda _e: self.exec_btn.config(bg=self._btn_base))

        # 적용: 실행 중 6개 항목 변경 반영(정지 상태에선 비활성 — 실행이 전체 적용).
        self.apply_btn = tk.Button(foot, text="적용", font=F["btn"], bg="#475569", fg="white",
                                   activebackground="#334155", activeforeground="white",
                                   relief="flat", bd=0, cursor="hand2", padx=14, pady=5,
                                   state="disabled", command=self._on_apply)
        self.apply_btn.pack(side="right", padx=(0, 8))

        # 주문 안전 체크박스: 체크돼 있을 때만 실제 주문이 나간다(기본 꺼짐 = 관찰모드).
        self.trade_chk = tk.Checkbutton(
            foot, text="주문", variable=self.trade_enabled, command=self._on_trade_toggle,
            font=F["btn"], bg=C["bg"], fg=C["danger"], selectcolor="#FFFFFF",
            activebackground=C["bg"], activeforeground=C["danger"], cursor="hand2",
            state="disabled")
        self.trade_chk.pack(side="right", padx=(0, 12))

        # 조회: 봇의 미체결 캐시·포지션을 강제 갱신(실행 중에만 의미 있음).
        self.query_btn = tk.Button(foot, text="Idle조회", font=F["btn"], bg="#475569", fg="white",
                                   activebackground="#334155", activeforeground="white",
                                   relief="flat", bd=0, cursor="hand2", padx=14, pady=5,
                                   state="disabled", command=self._on_query)
        self.query_btn.pack(side="right", padx=(0, 8))

        # 사운드: 체크 시 체결마다 윈도우 경고음. 앱이 self.sound_enabled를 읽어 재생.
        self.sound_chk = tk.Checkbutton(
            foot, text="사운드", variable=self.sound_enabled, font=F["btn"],
            bg=C["bg"], fg=C["text_sub"], selectcolor="#FFFFFF",
            activebackground=C["bg"], activeforeground=C["text_sub"], cursor="hand2")
        self.sound_chk.pack(side="right", padx=(0, 8))

    # ---- 동작 ----
    def _collect(self) -> dict[str, str]:
        raw = {k: e.get() for k, e in self._inputs.items()}
        # 종목코드는 콤보(표시라벨) → 실제 코드로 변환
        label = self.code_var.get()
        raw["code"] = self._code_map.get(label, "")
        return raw

    def set_symbol_options(self, codes: list[str], default_code: str) -> None:
        """종목 콤보 채우기. codes=[종목코드…](근월→차근월), default_code=기본 선택(근월).

        앱이 시작 시 t8426 조회 결과로 호출(메인스레드). 콤보에는 코드만 표시한다.
        """
        self._code_map = {c: c for c in codes}   # 유효 코드 집합(미채움 placeholder 거르기용)
        self.code_combo["values"] = list(codes)
        self.code_var.set(default_code if default_code in self._code_map
                          else (codes[0] if codes else ""))

    def _on_toggle(self):
        """실행버튼: 정지 상태면 시작, 실행 중이면 중지(토글)."""
        if self._running:
            self._stop()
        else:
            self._start()

    def _start(self):
        # 환율은 입력이 아니라 시그널이 주는 조회값(표시 전용) → config 검증 대상 아님.
        try:
            cfg = build_config(self._collect())
        except ValueError as e:
            self._error(str(e))
            return
        _save_state(self._collect())  # 실행 시 입력값 저장
        self._set_running(True)
        logger.info("전략 실행 시작: %s", cfg)
        if self.on_execute:
            self.on_execute(cfg)

    def _stop(self):
        self._set_running(False)
        logger.info("전략 실행 중지")
        if self.on_stop:
            self.on_stop()

    def _on_query(self):
        """[조회] → 봇의 미체결 캐시·포지션 강제 갱신(앱에 위임)."""
        if self.on_query:
            self.on_query()

    def _on_trade_toggle(self):
        """'주문' 체크박스 토글 → 앱에 전달(실주문 허용/관찰모드)."""
        on = self.trade_enabled.get()
        msg = "● 주문 활성 — 실주문 나감" if on else "○ 관찰모드 — 주문 안 나감"
        self.status.configure(text=msg, fg=C["danger"])
        # '자동주문 실행중' 표시는 trade_enabled trace가 자동 처리(사용자·브레이커 공통).
        logger.info("주문 %s", "활성" if on else "비활성(관찰)")
        if self.on_trade_toggle:
            self.on_trade_toggle(on)

    def _on_apply(self):
        """실행 중 6개 항목 변경 적용. 검증 통과하면 on_apply 콜백으로 전달."""
        if not self._running:
            return
        try:
            live = parse_live_params(self._collect())
        except ValueError as e:
            self._error(str(e))
            return
        _save_state(self._collect())
        self.status.configure(text="● 설정 적용됨 — 실행 중", fg=C["danger"])
        logger.info("실행 중 설정 적용: %s", live)
        if self.on_apply:
            self.on_apply(live)

    def _set_running(self, running: bool):
        """실행/중지 상태에 맞춰 버튼·상태줄·입력잠금 갱신."""
        self._running = running
        self._set_time_bg(running)   # Ready → 주문가능시간 표 주황 / 정지 → 흰색
        if running:
            self._btn_base, self._btn_hover = C["danger"], C["danger_dark"]
            self.exec_btn.configure(text="중지", bg=self._btn_base,
                                    activebackground=self._btn_hover)
            self.status.configure(text="● 실행 중(관찰) — '주문' 체크 시 실주문", fg=C["danger"])
            self._set_inputs_enabled(False)             # 종목·계좌달러비율 등 고정 항목 잠금
            self.code_combo.configure(state="disabled")  # 종목코드 콤보 잠금
            self.apply_btn.configure(state="normal")    # 실행 중에만 적용 가능
            self.trade_enabled.set(False)               # 안전: 매 실행은 관찰모드로 시작
            self.trade_chk.configure(state="normal")    # 실행 중에만 주문 체크 가능
            self.query_btn.configure(state="normal")    # 실행 중에만 조회 의미 있음
        else:
            self._btn_base, self._btn_hover = C["accent"], C["accent_dark"]
            self.exec_btn.configure(text="Ready", bg=self._btn_base,
                                    activebackground=self._btn_hover)
            self.status.configure(text="Ready를 누르면 시그널 수신을 시작합니다.", fg=C["text_sub"])
            self._set_inputs_enabled(True)
            self.code_combo.configure(state="readonly")  # 종목코드 콤보 다시 선택 가능
            self.apply_btn.configure(state="disabled")
            self.trade_enabled.set(False)               # 중지 시 주문 끔
            self.trade_chk.configure(state="disabled")
            self.query_btn.configure(state="disabled")

    def _set_inputs_enabled(self, enabled: bool):
        """enabled=True(정지): 전체 입력. False(실행): 고정 항목만 잠그고 LIVE_KEYS는 열어둠."""
        for k, e in self._inputs.items():
            on = enabled or (k in LIVE_KEYS)
            e.configure(state="normal" if on else "disabled")

    def _on_close(self):
        _save_state(self._collect())  # 창 닫을 때도 입력값 저장
        self.root.destroy()

    def _error(self, msg: str):
        self.status.configure(text=f"⚠ {msg}", fg=C["err"])
        messagebox.showerror("입력 오류", msg)

    # 부호별 글씨색을 입힐 표시칸(+빨강 / −파랑, 한국식). 0·미표시는 기본색.
    _SIGNED = ("usd_futures_balance", "target_contracts")

    # ---- 외부에서 표시칸 갱신 (잔고 배선 시 사용) ----
    def set_display(self, key: str, value: str):
        lbl = self._displays.get(key)
        if lbl is None:
            return
        lbl.configure(text=value)
        if key in self._SIGNED:
            try:
                n = float(str(value).replace(",", "").strip())
            except ValueError:
                n = 0.0
            lbl.configure(fg=(C["danger"] if n > 0 else C["accent"] if n < 0 else C["text"]))

    def set_market_status(self, text: str, color: str) -> None:
        """장운영 상태 칩 갱신(상단 우측). 예: '주간 · 접속매매 (JIF)'. 메인스레드에서 호출."""
        self.market_label.configure(text=f"● {text}", fg=color)

    def show_alarm(self, messages: list[str]) -> None:
        """리스크 알람 배너 갱신(메인스레드). messages 있으면 빨간 띠 표시, 없으면 숨김."""
        try:
            if messages:
                self.alarm_label.configure(text="⚠ " + "  |  ".join(messages))
                if not self.alarm_bar.winfo_ismapped():
                    self.alarm_bar.pack(fill="x", before=self._logbar)
            else:
                self.alarm_bar.pack_forget()
        except tk.TclError:
            pass

    def alert(self, title: str, message: str) -> None:
        """확인창(모달) — 만기/롤오버 안내. 메인스레드에서 호출."""
        messagebox.showwarning(title, message, parent=self.root)

    def set_status(self, msg: str) -> None:
        """로그성 상태(시그널·집행 메시지)를 제목 옆 상단 라벨에 표시.

        백그라운드 스레드에서 호출 시 post()로 메인스레드에 위임할 것.
        (Ready/실행중 등 화면 상태는 하단 self.status가 따로 유지)
        """
        self.log_label.configure(text=msg)

    def post(self, fn) -> None:
        """백그라운드 스레드 → Tk 메인스레드로 작업 위임(스레드 안전)."""
        self.root.after(0, fn)

    def apply_signal(self, *, fx: float, total_coin: float,
                     hedge_ratio: float, usd_futures_held: int) -> tuple[int, int, int]:
        """외부 시그널(환율·코인포지션합) + **적용된 입력**으로 표시칸 + 목표계약수 갱신.

        헤지비율·보유 원달러는 화면 입력칸이 아니라 **적용된 값**(app이 Ready/적용 시점에
        캡처)을 인자로 받는다 — 입력 도중 값이 화면 C/D에 반영되지 않도록(적용 버튼 눌러야 갱신).
        C = 보유 원달러(계약수 그대로), D = −(코인포지션합 × B%) / 환율 / 10000.
        Returns (C, D, 목표).
        """
        from trdbot.targets import target_contracts
        self.set_display("exchange_rate", f"{fx:,.2f}")
        self.set_display("total_coin", f"{total_coin:,.0f}")
        # Meme(달러·해외코인매도 다리)은 Dalin(원화·국내코인매수 다리)과 반대 다리라 D 부호 반전.
        c, d, t = target_contracts(usd_futures_held, total_coin, hedge_ratio, fx,
                                   flip_hedge=self._is_meme)
        self.set_display("dollar_contracts", str(c))
        self.set_display("hedge_contracts", str(d))
        self.set_display("target_contracts", str(t))
        return c, d, t

    def run(self):
        self.root.mainloop()


def main() -> None:
    from lsapi.logger import setup_logging
    setup_logging()
    OrderScreen().run()


if __name__ == "__main__":
    main()

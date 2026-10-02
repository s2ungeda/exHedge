"""실시간 주문/잔고 모니터 (Tkinter).

    python -m trdbot.monitor_screen   (또는 examples/14_monitor.py)

포지션(t0441)과 주문/체결(t0434)을 표로 보여준다.

데이터 갱신 정책 (주기 폴링 안 함 — 변동 있을 때만):
- 시작 시: REST 1회 (초기 스냅샷)
- 이후: **실시간 통보(C01 체결/O01 접수/H01 정정취소) 수신 → 변동 시 자동 REST 재조회**
  (디바운스 0.4초로 연속 체결 합침. HTS·봇 어느 쪽 주문이든 변동이 반영됨)
- 조회 버튼: 언제든 수동 재조회
- 통보 WS가 실패해도 무해 — 초기 조회 + 수동 버튼은 그대로 동작(페일세이프)

데이터 소스와 화면(Tk) 분리: REST/WS는 백그라운드, 화면 갱신은 post()로 메인스레드 위임.
WS 연결 시 update_positions()/update_orders()를 그대로 호출하면 됨(주입 지점 동일).
"""

from __future__ import annotations

import logging
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Any

from trdbot.datastore import MarketDataStore

logger = logging.getLogger(__name__)

# 디자인 토큰 — 자동주문화면과 통일(맑은 고딕·볼드 숫자·카드·강조).
C = {
    "bg": "#EEF1F5", "card": "#FFFFFF", "border": "#DCE1E8", "header": "#1E293B",
    "text": "#1F2933", "text_sub": "#6B7280", "accent": "#2563EB", "accent_dark": "#1D4ED8",
    "buy": "#1D4ED8", "sell": "#DC2626",          # 매수 파랑 / 매도 빨강(글자)
    "buy_bg": "#EFF6FF", "sell_bg": "#FEF2F2",     # 매수/매도 행 배경(옅게)
    "fill_bg": "#ECFDF5",                          # 체결 행 배경(옅은 녹)
    "head_bg": "#F1F5F9", "sel": "#DBEAFE",        # 표 헤더 / 선택 행
}
F = {
    "title": ("Malgun Gothic", 13, "bold"),
    "section": ("Malgun Gothic", 10, "bold"),
    "data": ("Malgun Gothic", 10, "bold"),   # 표 데이터 — 볼드 숫자
    "head": ("Malgun Gothic", 9, "bold"),    # 표 헤더
    "btn": ("Malgun Gothic", 9, "bold"),
    "status": ("Malgun Gothic", 9),
}

POS_COLS = [("종목", "expcode", 96), ("구분", "medosu", 52), ("수량", "jqty", 60),
            ("청산가능", "cqty", 70), ("평단", "pamt", 90), ("현재가", "price", 90),
            ("평가손익", "dtsunik1", 100)]
ORD_COLS = [("주문번호", "ordno", 84), ("종목", "expcode", 90), ("구분", "medosu", 60),
            ("주문", "qty", 52), ("체결", "cheqty", 52), ("미체결", "ordrem", 60),
            ("가격", "price", 82), ("상태", "status", 64), ("시간", "ordtime", 84)]


class MonitorScreen:
    def __init__(self, *, code_prefix: str = "", store: MarketDataStore | None = None,
                 master: tk.Misc | None = None, account: str = "", signal_token: str = ""):
        # code_prefix: 종목군 필터(예 'A756'). store: 중앙저장소(임베디드 모드 — 앱이 소유).
        # master: 주어지면 Toplevel(봇 프로세스 안의 창), 없으면 단독 Tk(). account: 사용 계좌번호.
        # signal_token: 상단바 색을 발신처별로 구분(Meme=보라/앰버, 그 외=기본 남색).
        self.code_prefix = code_prefix
        self._account = account
        self._signal_token = signal_token
        self._store = store
        self._owns = store is None      # 단독 모드면 자체 client+feed 소유
        self._client = None
        self._feed = None
        self._pending_refresh = None    # 디바운스용 after 핸들
        self.root = tk.Toplevel(master) if master is not None else tk.Tk()
        self.root.title("실시간 주문/잔고 모니터")
        self.root.configure(bg=C["header"])
        # 주문/체결 필터 (다중선택). 접수=미체결(t0434), 체결=C01 누적(실행 이후). 전체=둘 다.
        self.f_all = tk.BooleanVar(value=True)
        self.f_pending = tk.BooleanVar(value=True)   # 접수(미체결)
        self.f_filled = tk.BooleanVar(value=True)    # 체결
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build(self):
        # Treeview 스타일 — 맑은 고딕 볼드 데이터 + 넉넉한 행높이 + 옅은 헤더(전용 스타일명).
        style = ttk.Style()
        style.configure("Mon.Treeview", font=F["data"], rowheight=23,
                        background=C["card"], fieldbackground=C["card"], foreground=C["text"],
                        borderwidth=0)
        style.configure("Mon.Treeview.Heading", font=F["head"], foreground=C["text_sub"],
                        background=C["head_bg"], relief="flat", padding=(3, 4))
        style.map("Mon.Treeview.Heading", background=[("active", C["head_bg"])])
        style.map("Mon.Treeview", background=[("selected", C["sel"])],
                  foreground=[("selected", C["text"])])

        # 상단 타이틀 바 — SIGNAL_TOKEN 에 따라 색 구분(자동주문화면과 통일).
        is_meme = self._signal_token == "Meme"
        bar_bg = "#6D28D9" if is_meme else C["header"]       # Meme=보라, 그 외=기본 남색
        title_fg = "#FDE68A" if is_meme else "white"         # 제목 글자
        sub_fg = "#DDD6FE" if is_meme else "#9FB3C8"         # 계좌·상태 보조 글자
        self.root.configure(bg=bar_bg)
        bar = tk.Frame(self.root, bg=bar_bg)
        bar.pack(fill="x")
        tk.Label(bar, text="실시간 주문 / 잔고", font=F["title"], bg=bar_bg,
                 fg=title_fg).pack(side="left", padx=(14, 0), pady=6)
        # 사용 계좌번호 표시 — 어느 계좌를 보고 있는지 구분.
        if self._account:
            tk.Label(bar, text=f"계좌 {self._account}", font=F["section"], bg=bar_bg,
                     fg=sub_fg).pack(side="left", padx=(10, 0), pady=6)
        self.refresh_btn = tk.Button(bar, text="Idle조회", font=F["btn"], bg=C["accent"], fg="white",
                                     activebackground=C["accent_dark"], activeforeground="white",
                                     relief="flat", bd=0, cursor="hand2", padx=14, pady=4,
                                     command=lambda: self._do_refresh(full=True))
        self.refresh_btn.pack(side="right", padx=(8, 12), pady=5)
        self.status = tk.Label(bar, text="대기", font=F["status"], bg=bar_bg, fg=sub_fg)
        self.status.pack(side="right", padx=4)

        body = tk.Frame(self.root, bg=C["bg"], padx=6, pady=6)
        body.pack(fill="both", expand=True)

        pos_inner = self._card(body, "포지션 · 잔고", expand=False)
        self.pos_tree = self._table(pos_inner, POS_COLS, height=3, expand=False)

        # 주문/체결 카드 — 제목 옆에 필터 체크박스(전체/접수/체결).
        def _filters(head):
            for text, var, cmd in (("전체", self.f_all, self._on_all),
                                   ("접수", self.f_pending, self._on_cat),
                                   ("체결", self.f_filled, self._on_cat)):
                tk.Checkbutton(head, text=text, variable=var, command=cmd, font=F["status"],
                               bg=C["card"], fg=C["text_sub"], selectcolor="#FFFFFF",
                               activebackground=C["card"], cursor="hand2").pack(side="left",
                                                                                padx=(8, 0))
        ord_inner = self._card(body, "주문 · 체결", extra=_filters)
        self.ord_tree = self._table(ord_inner, ORD_COLS, height=9)

    def _card(self, parent, title, extra=None, expand=True):
        """섹션 카드(흰 배경 + 강조바 + 제목). extra(head): 제목 오른쪽 위젯 빌더(필터 등).
        expand=False면 내용 높이에 맞춰 고정(안 늘어남)."""
        fill = "both" if expand else "x"
        outer = tk.Frame(parent, bg=C["card"], highlightthickness=1,
                         highlightbackground=C["border"])
        outer.pack(fill=fill, expand=expand, pady=(0, 6))
        head = tk.Frame(outer, bg=C["card"])
        head.pack(fill="x", padx=8, pady=(5, 2))
        tk.Frame(head, bg=C["accent"], width=3, height=12).pack(side="left", padx=(0, 6))
        tk.Label(head, text=title, font=F["section"], bg=C["card"], fg=C["text"]).pack(side="left")
        if extra:
            extra(head)
        inner = tk.Frame(outer, bg=C["card"], padx=6, pady=4)
        inner.pack(fill=fill, expand=expand)
        return inner

    def _table(self, parent, cols, height, expand=True):
        fill = "both" if expand else "x"
        frame = tk.Frame(parent, bg=C["card"])
        frame.pack(fill=fill, expand=expand)
        tv = ttk.Treeview(frame, columns=[c[1] for c in cols], show="headings",
                          height=height, style="Mon.Treeview")
        for title, key, w in cols:
            tv.heading(key, text=title)
            tv.column(key, width=w, minwidth=40, stretch=False,
                      anchor="e" if key not in ("expcode", "medosu", "status") else "center")
        # 행 색 — 매수 파랑 글자·연파랑 / 매도 빨강 글자·연빨강 / 체결 연녹.
        tv.tag_configure("buy", foreground=C["buy"], background=C["buy_bg"])
        tv.tag_configure("sell", foreground=C["sell"], background=C["sell_bg"])
        tv.tag_configure("fill", background=C["fill_bg"])
        sb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill=fill, expand=expand)
        sb.pack(side="right", fill="y")
        return tv

    # ---- 갱신 (외부/WS에서도 호출 가능) ----
    def update_positions(self, rows: list[dict[str, Any]]):
        self._fill(self.pos_tree, POS_COLS, rows)

    def update_orders(self, rows: list[dict[str, Any]]):
        self._fill(self.ord_tree, ORD_COLS, rows)

    @staticmethod
    def _row_tags(r: dict) -> tuple[str, ...]:
        """행 색 태그 — 매수/매도(구분) + 체결(상태)."""
        tags: list[str] = []
        medosu = str(r.get("medosu", ""))
        if "매수" in medosu:
            tags.append("buy")
        elif "매도" in medosu:
            tags.append("sell")
        if str(r.get("status", "")) == "체결":
            tags.append("fill")
        return tuple(tags)

    # 천단위 콤마를 찍을 수치 컬럼(주문번호·시간·종목·구분·상태는 제외).
    _COMMA_KEYS = {"jqty", "cqty", "pamt", "price", "dtsunik1", "qty", "cheqty", "ordrem"}

    @classmethod
    def _fmt(cls, v, key):
        """수치 컬럼이면 천단위 콤마. 정수는 정수로, 소수는 소수 유지. 숫자 아니면 그대로."""
        if key not in cls._COMMA_KEYS or v in ("", None):
            return v
        try:
            n = float(str(v).replace(",", ""))
        except (TypeError, ValueError):
            return v
        return f"{int(n):,}" if n == int(n) else f"{n:,}"

    def _fill(self, tv, cols, rows):
        tv.delete(*tv.get_children())
        for r in rows:
            vals = [self._fmt(r.get(c[1], ""), c[1]) for c in cols]
            tv.insert("", "end", values=vals, tags=self._row_tags(r))

    def _alive(self) -> bool:
        """창(위젯)이 아직 살아있는가. 종료 중이면 False → 콜백이 사라진 위젯을 안 건드림."""
        try:
            return bool(self.root.winfo_exists())
        except tk.TclError:
            return False

    def post(self, fn):
        if not self._alive():
            return
        try:
            self.root.after(0, fn)
        except tk.TclError:
            pass

    # ---- 데이터: 중앙저장소 참조. 시작 1회 + 변동(통보) 시 갱신. 주기 폴링 안 함. ----
    def start(self):
        if self._owns:
            self._start_standalone()   # 단독 모드: 자체 client+저장소+체결피드
        self._do_refresh(full=True)    # 초기: 포지션+미체결 전체 조회 + 렌더

    def _start_standalone(self):
        """단독 실행: 자체 client + 중앙저장소 + 체결피드(C01)를 만든다(앱 없이 테스트용)."""
        from lsapi import LSClient
        from lsapi.futures import FuturesAPI
        from trdbot.realtime_feed import RealtimeFeed
        self._client = LSClient(market="futures")
        self._store = MarketDataStore(FuturesAPI(self._client), code_prefix=self.code_prefix)
        try:
            acct = self._client.settings.account_for("futures")
        except Exception:
            acct = None
        if acct:
            code = (self.code_prefix + "7000") if self.code_prefix else "A7567000"
            # 통보로 저장소를 직접 갱신(접수→추가, 체결→차감, 취소→제거) — 임베디드와 동일.
            self._feed = RealtimeFeed(
                code=code, market="futures", account=acct,
                position_prefix=self.code_prefix or None,
                on_notice=self.notify,
                on_fill=self._store.apply_fill,
                on_accept=self._store.apply_accept,
                on_cancel=self._store.apply_cancel,
            )
            self._store.set_feed(self._feed)
            self._feed.start()

    # ---- 변동 알림(앱의 RealtimeFeed 또는 단독 feed) → 디바운스 갱신 ----
    def notify(self):
        """계좌 변동(C01/O01/H01) 알림 → 메인스레드에서 디바운스 재조회(WS 스레드에서 호출 안전)."""
        self.post(self._debounced_refresh)

    def _debounced_refresh(self):
        if not self._alive():
            return
        if self._pending_refresh is not None:
            try:
                self.root.after_cancel(self._pending_refresh)
            except Exception:
                pass
        # 통보 갱신은 REST 재조회 없이 캐시만 다시 그림 — 통보(C01/O01/H01)가 이미 저장소를
        # 직접 갱신했으므로 t0434 재조회는 불필요(accno 절감). 0.4초 디바운스로 연달은 통보 합침.
        try:
            self._pending_refresh = self.root.after(400, self._render)
        except tk.TclError:
            self._pending_refresh = None

    def _do_refresh(self, full: bool = False):
        """저장소 REST 갱신(백그라운드) 후 렌더. full=True면 포지션도(t0441), 아니면 미체결만."""
        self._pending_refresh = None
        threading.Thread(target=self._refresh_and_render, args=(full,),
                         name="monitor-refresh", daemon=True).start()

    def _refresh_and_render(self, full: bool):
        try:
            if self._store is not None:
                self._store.refresh() if full else self._store.refresh_orders()
        except Exception:
            logger.exception("저장소 갱신 오류")
        self.post(self._render)

    def _render(self):
        """중앙저장소를 읽어 표에 반영(메인스레드). 화면은 저장소를 참조만."""
        if self._store is None or not self._alive():
            return
        try:
            pos = self._store.positions()
            self.update_positions(pos)
            self._apply_filter()
            ts = time.strftime("%H:%M:%S")
            n_open = len(self._store.open_orders(ensure=False))
            self.status.configure(
                text=f"갱신 {ts} · 포지션 {len(pos)} · 미체결 {n_open}"
                     f" · 체결 {len(self._store.fills())}")
        except tk.TclError:
            pass   # 렌더 도중 창이 닫힘 — 무시(종료 경쟁)

    # ---- 주문/체결 필터 (다중선택 체크박스) ----
    def _on_all(self):
        """전체(마스터) 토글 → 접수·체결을 같이 켜고 끔."""
        v = self.f_all.get()
        self.f_pending.set(v)
        self.f_filled.set(v)
        self._apply_filter()

    def _on_cat(self):
        """접수/체결 개별 토글 → 둘 다 켜졌으면 전체도 켜짐 표시."""
        self.f_all.set(self.f_pending.get() and self.f_filled.get())
        self._apply_filter()

    def _apply_filter(self):
        """체크된 분류만 표에 표시(메인스레드). 접수=미체결, 체결=C01 누적. 재조회 없음."""
        self.update_orders(self._filtered_orders())

    def _filtered_orders(self) -> list[dict[str, Any]]:
        # 접수=미체결(t0434 chegb=2), 체결=C01 누적(실행 이후만). 봇 실행 이전 체결은 애초에 없음.
        if self._store is None:
            return []
        out: list[dict[str, Any]] = []
        if self.f_pending.get():
            out.extend(self._store.open_orders(ensure=False))   # 접수(미체결) — 캐시만(UI 비블로킹)
        if self.f_filled.get():
            out.extend(self._store.fills())          # 체결(실행 이후 C01 누적)
        return out

    def _on_close(self):
        if self._feed is not None:
            try:
                self._feed.stop()
            except Exception:
                pass
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
        self.root.destroy()

    def run(self):
        self.start()
        self.root.mainloop()


def main():
    import sys

    from lsapi.logger import setup_logging
    setup_logging()
    # 인자로 종목군 접두 전달 가능(예: python -m trdbot.monitor_screen A756). 없으면 전체.
    prefix = sys.argv[1] if len(sys.argv) > 1 else ""
    MonitorScreen(code_prefix=prefix).run()


if __name__ == "__main__":
    main()

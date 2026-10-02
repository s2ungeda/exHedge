"""메인 런처 — 화면 + 시그널 수신 + 집행을 한 프로세스로 묶는다.

    python -m trdbot

흐름:
    [실행 토글 ON] → SignalLink(시그널 수신) + Executor(집행) 시작
        시그널 도착 → 화면 표시 갱신(apply_signal) + executor.on_signal
        executor → 목표 계산(targets) → 가격(pricing) → SessionRoutingBroker(주간/야간 LS 주문)
    [실행 토글 OFF] → 둘 다 정지

스레드: SignalLink/Executor는 백그라운드. Tkinter 위젯 갱신은 screen.post()로 메인스레드에 위임.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime
from datetime import time as dtime

from lsapi import LSClient
from lsapi.config import get_settings
from lsapi.futures import FuturesAPI, NightFuturesAPI, session_now
from lsapi.logger import setup_logging
from lsapi.market_state import (
    JANGUBUN_DAY_FUTURES,
    JANGUBUN_NIGHT_FUTURES,
    current_time_phase,
    is_tradeable,
    phase_badge,
)
from trdbot.broker_ls import (
    USD_FUTURES_TICK,
    LSDayFuturesBroker,
    LSNightFuturesBroker,
    SessionRoutingBroker,
)
from trdbot.datastore import MarketDataStore
from trdbot.executor import Executor, recent_fill_qty
from trdbot.monitor_screen import MonitorScreen
from trdbot.order_screen import OrderConfig, OrderScreen, TimeWindow
from trdbot.pricing_log import log_pricing
from trdbot.realtime_feed import RealtimeFeed
from trdbot.signal_link import Signal, SignalLink
from trdbot.targets import target_contracts

logger = logging.getLogger(__name__)


def _now_hhmm() -> int:
    n = datetime.now()
    return n.hour * 100 + n.minute


def _test_nofill_ticks() -> int:
    """(테스트) 환경변수 TRDBOT_TEST_NOFILL_TICKS — 주문가를 비체결 방향 N틱 이동. 기본 0(정상)."""
    try:
        return max(0, int(os.environ.get("TRDBOT_TEST_NOFILL_TICKS", "0")))
    except ValueError:
        return 0


def usd_futures_options(rows: list[dict]) -> list[str]:
    """t8426(상품선물마스터) 행 → 미국달러선물 월물 코드 [근월, 차근월] 2개.

    월물만 사용: shcode가 'A756'으로 시작(‘G756’ 플렉스·‘D756’ 스프레드 제외).
    hname 'F YYYYMM' 만기로 정렬해 근월/차근월을 고른다. 콤보에는 코드만 표시한다.
    """
    cand: list[tuple[str, str]] = []
    for r in rows:
        sh = str(r.get("shcode", "") or "").strip()
        hn = str(r.get("hname", "") or "")
        if not sh.startswith("A756") or "플렉스" in hn:
            continue
        m = re.search(r"F\s*(\d{6})", hn)
        if m:
            cand.append((m.group(1), sh))   # (YYYYMM, 코드)
    cand.sort()
    return [sh for _ym, sh in cand[:2]]


def usd_near_ym(rows: list[dict]) -> str | None:
    """t8426 행 → 미국달러선물 **근월물 결제월 YYYYMM**(만기·롤오버 계산용)."""
    yms: list[str] = []
    for r in rows:
        sh = str(r.get("shcode", "") or "").strip()
        hn = str(r.get("hname", "") or "")
        if not sh.startswith("A756") or "플렉스" in hn:
            continue
        m = re.search(r"F\s*(\d{6})", hn)
        if m:
            yms.append(m.group(1))
    return min(yms) if yms else None


def _in_window(w: TimeWindow, t: dtime) -> bool:
    if w.crosses_midnight():
        return t >= w.start or t <= w.end
    return w.start <= t <= w.end


class TradingApp:
    """화면 ↔ 시그널 ↔ 집행 배선."""

    def __init__(self):
        _settings = get_settings()
        self._signal_token = _settings.signal_token   # 시그널 발신처 구분 토큰(SIGNAL_TOKEN)
        self.screen = OrderScreen(
            on_execute=self._start, on_stop=self._stop, on_apply=self._on_apply,
            on_trade_toggle=self._on_trade_toggle, on_query=self._on_query,
            on_alarm_dismiss=self._dismiss_alarms,   # 알람 배너 [해제] 버튼
            env=_settings.ls_env,               # 캡션에 모의/운영 표시
            signal_token=self._signal_token,    # 캡션·로그줄에 발신처 구분 표시
        )
        # 리스크 알람: {키: 사유}. 사용자가 해제할 때까지 1분마다 경고음(_alarm_tick).
        self._alarms: dict[str, str] = {}
        self._alarm_lock = threading.Lock()
        self._trade_enabled = False   # '주문' 체크박스 상태(False=관찰모드, 주문 안 나감)
        self.client: LSClient | None = None
        self.broker: SessionRoutingBroker | None = None
        self.executor: Executor | None = None
        self.signal: SignalLink | None = None
        self.rt: RealtimeFeed | None = None  # 실시간 JIF+호가+체결. 없으면 현재시각/REST 폴백
        self._token_day: str | None = None   # 토큰을 주간장 시작에 선제 재발급한 날짜(YYYY-MM-DD)
        # 만기 처리: 만기일/직전거래일이면 근월 매매금지, 롤오버 구간엔 시각별 확인창.
        self._near_code: str | None = None            # 근월물 코드(만기 매매금지 판정용)
        self._expiry_restricted = False               # 오늘=만기일/직전거래일 → 근월 매매금지
        self._rollover_start: object | None = None    # 만기 전주 화요일(date)
        self._rollover_end: object | None = None      # 만기 전주 금요일(date)
        self._holidays: set = set()                   # 휴일 집합(롤오버 구간 내 건너뛰기용)
        self._rollover_shown_date: object | None = None  # 롤오버 확인창 표시 리셋 기준일
        self._rollover_slots: set[str] = set()        # 오늘 이미 띄운 슬롯("0930"/"1400")
        self._ratios = (0.0, 0.0)  # (B 헤지비율, 계좌달러노출액) — 실행 시점 캡처
        self._last_signal: Signal | None = None  # 마지막 시그널(적용 시 목표 재계산용)
        self._fill_window_sec = 600.0   # 체결량 감시 시간(초). 기본 10분
        self._fill_limit = 0            # 최근 윈도우 체결량 한도(개). 0=기능 끔
        self._breaker_reset_at = 0.0    # '주문' 재개 시각(epoch) — 이후 체결만 카운트
        self._last_fill_pos_refresh = 0.0   # 체결 후 포지션 재조회 스로틀(연타 체결 accno 절감)

        # 중앙 데이터 저장소(포지션·미체결·체결의 단일 출처) — 화면은 참조만.
        # REST용 클라이언트는 앱 시작 시점부터(실행 전에도 포지션/미체결 표시).
        try:
            self._data_client: LSClient | None = LSClient(market="futures")
            self._store_api: FuturesAPI | None = FuturesAPI(self._data_client)
            self._night_api: NightFuturesAPI | None = NightFuturesAPI(self._data_client)
        except Exception:
            logger.exception("데이터 클라이언트 생성 실패 — 모니터는 빈 상태로 시작")
            self._data_client = None
            self._store_api = None
            self._night_api = None
        # 저장소는 세션별 REST 소스를 안다: 주간 t0441 / 야간 CCENQ90200(포지션 재동기화).
        self._store = MarketDataStore(
            self._store_api, code_prefix="A756",
            night_api=self._night_api, session=lambda: session_now(_now_hhmm()),
        )
        # 모니터를 같은 프로세스의 Toplevel 창으로 — 중앙저장소를 참조만(별도 프로세스/구독 없음).
        try:
            _acct = _settings.account_for("futures")   # 모니터 상단에 사용 계좌번호 표시
        except Exception:
            _acct = ""
        try:
            self._monitor: MonitorScreen | None = MonitorScreen(
                code_prefix="A756", store=self._store, master=self.screen.root,
                account=_acct, signal_token=self._signal_token)
            self._monitor.start()   # 초기 REST(bg) + 렌더
        except Exception:
            logger.exception("모니터 창 생성 실패 — 메인만 계속")
            self._monitor = None

        # 시작 시 종목 콤보 채우기(근월/차근월) — 백그라운드(토큰/조회로 GUI 막지 않음).
        threading.Thread(target=self._populate_symbols, daemon=True).start()

        # 장운영 상태 칩 주기 갱신(2초). JIF(있으면)·시각 폴백 기반 — 네트워크 호출 없음.
        self.screen.root.after(500, self._tick_market_status)
        # 리스크 알람 반복(1분마다) — 활성 알람 있으면 경고음 재생.
        self.screen.root.after(60000, self._alarm_tick)

    # ---- 장운영 상태 표시 ----
    def _market_status(self) -> tuple[str, str]:
        """현재 장운영 상태 → (표시문구, 색). JIF 수신했으면 그 진실, 아니면 시각 폴백."""
        sess = session_now(_now_hhmm())
        if sess == "closed":
            return ("장종료 (시각)", "#9FB3C8")
        jangubun = JANGUBUN_DAY_FUTURES if sess == "day" else JANGUBUN_NIGHT_FUTURES
        if self.rt is not None and self.rt.seen(jangubun):
            phase, src = self.rt.phase(jangubun), "JIF"
        else:
            phase, src = current_time_phase(_now_hhmm(), sess), "시각"
        label, color = phase_badge(phase)
        sess_kr = "주간" if sess == "day" else "야간"
        return (f"{sess_kr} · {label} ({src})", color)

    def _ensure_daily_token(self) -> None:
        """장 개시 **전(프리마켓 08:30~)** 하루 1회 토큰을 선제 재발급 + WS 재연결 — LS는 밤사이
        토큰을 무효화한다. 개장(08:45) 전에 미리 새 토큰으로 붙여둬서, 개장 순간 재연결이
        JIF 개장 이벤트와 경쟁하지 않게 한다(실측: 08:45 재연결이 개장 국면을 흔들었음).

        (재발급 안 하면 장 개시 첫 요청에서 IGW00121 → 강제 재발급으로 복구되지만, 그 깜빡임 제거.)
        토큰 만료시각(expires_in)은 실제 무효화보다 늦어 믿을 수 없으므로, 시각 기준으로 재발급.
        """
        # 08:30(개장 08:45 전) ~ 주간 마감(15:45) 사이 첫 틱에 1회. 개장 전에 미리 준비.
        now = _now_hhmm()
        if self._data_client is None or not (830 <= now <= 1545):
            return
        today = datetime.now().strftime("%Y-%m-%d")
        if self._token_day == today:
            return
        self._token_day = today   # 낙관적 표시(실패해도 IGW 강제재발급이 백스톱)

        def _refresh():
            try:
                self._data_client.tokens.get_token(force_refresh=True)   # REST 토큰 재발급
                logger.info("장 개시 전(프리마켓) — 토큰 선제 재발급 완료")
                rt = self.rt
                if rt is not None:
                    rt.reconnect()   # WS도 새 토큰(공유)으로 재연결·재구독
                    logger.info("장 개시 전(프리마켓) — WS 재연결(재구독) 요청")
            except Exception:
                logger.exception("주간장 토큰 선제 재발급/WS 재연결 실패(IGW 백스톱으로 복구됨)")

        threading.Thread(target=_refresh, name="daily-reset", daemon=True).start()

    def _check_rollover_alert(self) -> None:
        """롤오버 구간(만기 전주 화~금) 영업일마다 09:30·14:00 롤오버 확인창(각 1회)."""
        from trdbot.expiry import is_trading_day
        if self._rollover_start is None:
            return
        today = datetime.now().date()
        if today != self._rollover_shown_date:   # 날짜 바뀌면 슬롯 리셋
            self._rollover_shown_date = today
            self._rollover_slots = set()
        if not (self._rollover_start <= today <= self._rollover_end):
            return
        if not is_trading_day(today, self._holidays):   # 주말·휴일은 건너뜀
            return
        now = datetime.now().time()
        for slot, hh, mm in (("0930", 9, 30), ("1400", 14, 0)):
            if slot not in self._rollover_slots and now >= dtime(hh, mm):
                self._rollover_slots.add(slot)
                self.screen.alert("차근월물 롤오버", "차근월물 롤오버 반드시")

    def _tick_market_status(self) -> None:
        """장운영 칩을 갱신하고 2초 뒤 자기 자신을 재예약(메인스레드 타이머)."""
        try:
            self._ensure_daily_token()   # 주간장 개시 시 토큰 선제 재발급(하루 1회)
            self._check_rollover_alert()  # 롤오버일 09:30·14:00 확인창
            self.screen.set_market_status(*self._market_status())
        except Exception:
            logger.debug("장운영 상태 갱신 실패", exc_info=True)
        finally:
            try:
                self.screen.root.after(2000, self._tick_market_status)
            except Exception:
                pass

    def _populate_symbols(self) -> None:
        """t8426으로 미국달러선물 근월/차근월 코드를 조회해 종목 콤보를 채운다(기본 근월).

        만기일/만기 직전거래일이면 근월물을 콤보에서 빼고(차근월만) 확인창을 띄운다.
        """
        try:
            if self._data_client is not None:
                api = FuturesAPI(self._data_client)   # 영속 데이터 클라이언트 재사용
            else:
                api = FuturesAPI(LSClient(market="futures"))
            rows = api.get_commodity_future_master().get("t8426OutBlock", [])
            codes = usd_futures_options(rows)
            if not codes:
                return
            self._near_code = codes[0]
            self._apply_expiry_calendar(rows, api, codes)
            logger.info("종목 콤보: %s (만기제한=%s)", codes, self._expiry_restricted)
        except Exception:
            logger.exception("종목 콤보 채우기 실패 — 수동 입력 필요")

    def _apply_expiry_calendar(self, rows, api, codes) -> None:
        """오늘이 만기일/직전거래일이면 근월 제외(차근월만) + 확인창(만기·근월잔고).

        롤오버일(만기 전주 금요일)은 여기서 날짜만 잡아두고, 09:30·14:00 확인창은 tick이 띄운다.
        """
        from trdbot.expiry import expiry_day, expiry_prev_day, load_holidays, rollover_window
        near = codes[0]
        ym = usd_near_ym(rows)
        hol = load_holidays()
        self._holidays = hol
        today = datetime.now().date()
        label = ""
        if ym:
            y, mo = int(ym[:4]), int(ym[4:6])
            self._rollover_start, self._rollover_end = rollover_window(y, mo)
            if today == expiry_day(y, mo, hol):
                label = "원달러선물 만기일"
            elif today == expiry_prev_day(y, mo, hol):
                label = "원달러선물 만기 직전거래일 (내일 만기)"
        self._expiry_restricted = bool(label)
        if not self._expiry_restricted:
            self.screen.post(lambda: self.screen.set_symbol_options(codes, near))
            return
        rest = codes[1:] or codes   # 근월 제외 → 차근월만
        self.screen.post(lambda: self.screen.set_symbol_options(rest, rest[0]))
        msg = f"{label}\n\n최근월물 매매 금지 — 차근월물만 거래합니다."
        self.screen.post(lambda m=msg: self.screen.alert("미국달러선물", m))
        qty = self._near_position_qty(near, api)
        if qty != 0:
            bal = (f"최근월물 잔고 {qty:+d}계약 있음\n\n"
                   "정리 안 하면 최종결제일에 인수도(실물결제)로 넘어갑니다.\n"
                   "다른 HTS로 반드시 정리하세요.")
            self.screen.post(lambda m=bal: self.screen.alert("최근월물 잔고", m))

    def _near_position_qty(self, near_code: str, api) -> int:
        """근월물 순포지션(계약수). t0441 표준코드에 근월 단축코드가 포함된 행 합산."""
        try:
            rows = api.get_balance().get("t0441OutBlock1", [])
        except Exception:
            logger.exception("근월물 잔고 조회 실패")
            return 0
        total = 0
        for r in rows:
            if near_code in str(r.get("expcode", "")):
                q = int(r.get("jqty", 0) or 0)
                total += q if r.get("medosu") == "매수" else -q
        return total

    # ---- 실행/정지 (화면 토글 콜백) ----
    def _start(self, cfg: OrderConfig) -> None:
        # 계좌달러노출액은 OrderConfig에 없으니 화면 입력에서 직접 읽는다.
        # 천단위 콤마가 붙어 있으니 제거하고 변환.
        try:
            exp_raw = self.screen._inputs["manual_balance_adjust"].get().replace(",", "").strip()
            exposure = float(exp_raw) if exp_raw not in ("", "-") else 0.0
        except ValueError:
            exposure = 0.0
        self._ratios = (cfg.hedge_ratio, exposure)
        self._fill_window_sec = cfg.fill_window_min * 60.0
        self._fill_limit = cfg.fill_limit
        self._trade_enabled = False   # 매 실행은 관찰모드로 시작(안전) — '주문' 체크해야 실주문

        # REST 클라이언트 하나로 일원화(저장소·브로커 공용). __init__ 실패 시 지금 생성.
        if self._data_client is None or self._store_api is None:
            self._data_client = LSClient(market="futures")
            self._store_api = FuturesAPI(self._data_client)
            self._night_api = NightFuturesAPI(self._data_client)
            self._store._api = self._store_api
            self._store._night_api = self._night_api
        # 체결통보(C01) 계좌등록 키. 계좌 미설정이면 None → C01 미구독(포지션은 t0441 유지).
        try:
            acct = self._data_client.settings.account_for("futures")
        except ValueError:
            acct = None
        # 실시간 피드 — 호가/체결 출처. 체결(C01)·변동알림(O01/H01)을 저장소·모니터로 보냄.
        self.rt = RealtimeFeed(
            code=cfg.code, market="futures", account=acct, on_event=self._on_status,
            on_fill=self._on_fill,             # C01 체결 → 미체결 즉시 차감/제거
            on_accept=self._on_accept,         # O01 접수 → 미체결 즉시 추가
            on_cancel=self._on_cancel,         # H01 정정취소 → 미체결 즉시 제거
            on_notice=(self._monitor.notify if self._monitor is not None else None),
            on_disconnect=self._on_ws_disconnect,     # 예기치 않은 WS 끊김 → 리스크 알람
            token_manager=self._data_client.tokens,   # REST와 토큰 공유 → 재발급이 WS에도 반영
        )
        self._store.set_feed(self.rt)          # 포지션 C01 net·체결을 저장소에 연결
        # 브로커는 저장소(포지션·미체결)에 위임 + 주문/호가(t2112)는 공용 api로.
        day = LSDayFuturesBroker(
            self._store, self._store_api, cfg.code,
            orderbook_source=self.rt.day_orderbook,
        )
        # 야간도 같은 저장소에 위임(주간과 동일 구조) — 미체결/포지션이 pending+통보로 실시간.
        night = LSNightFuturesBroker(self._store, self._night_api, cfg.code)
        self.broker = SessionRoutingBroker(day, night, now_hhmm=_now_hhmm)

        self.executor = Executor(
            self.broker,
            qty_per_order=cfg.qty_per_order,
            order_interval_sec=cfg.order_interval_sec,
            max_position=cfg.max_position,
            compute_target=self._compute_target,
            fallback_tick=USD_FUTURES_TICK,
            test_nofill_ticks=_test_nofill_ticks(),   # 테스트: 비체결 방향 N틱(환경변수)
            in_window=lambda: self._in_session_window(cfg),
            # '주문' 체크 시 실주문. 단 만기일/직전거래일엔 근월(선택 종목=근월) 매매 금지.
            trade_enabled=lambda: self._trade_enabled and not (
                self._expiry_restricted and cfg.code == self._near_code),
            fill_breaker=self._fill_breaker,             # 체결량 한도 초과 → 주문 중지
            on_breaker_trip=self._on_breaker_trip,       # 트립 시 '주문' 자동 해제
            on_status=self._on_status,
            on_position=self._on_position,
            on_quote=lambda info: log_pricing(cfg.code, info),   # 주문가격 검증 CSV
        )
        # 시그널 토큰 설정 시 그 토큰과 일치하는 시그널만 처리(다른 발신처 거부). 빈값이면 전부 수락.
        # 발견(UDP) 포트를 토큰별로 분리 — Meme=9999, 그 외(Dalin/빈값)=8888. 발신처도 같은 포트로 보내야 함.
        udp_port = 9999 if self._signal_token == "Meme" else 8888
        self.signal = SignalLink(name="TrdBot", token=self._signal_token or None,
                                 udp_port=udp_port,
                                 on_signal=self._on_signal, on_event=self._on_status)
        # 실시간 구독 시작 — 호가(FH9)+장운영(JIF)+체결(C01). 실패해도 무해(REST/현재시각 폴백).
        self.rt.start()
        self.signal.start()
        self.executor.start()
        logger.info("실행 시작: code=%s qty=%d 캡=%d", cfg.code, cfg.qty_per_order, cfg.max_position)
        n = _test_nofill_ticks()
        if n:
            logger.warning("⚠ 테스트 모드: 주문가 비체결 방향 %d틱 이동(매수↓/매도↑)", n)
            self._on_status(f"⚠ 테스트 {n}틱 — 체결 안 되는 가격으로 주문")
        # 기존 미체결 주문은 취소하지 않고 그대로 둔다(이어서 관리). 잔고 즉시 1회 표시(백그라운드).
        threading.Thread(target=self._read_position_once, daemon=True).start()

    def _stop(self) -> None:
        # 참조를 즉시 떼어내고(재실행 대비), 종료는 백그라운드에서(GUI 멈춤 방지).
        # 데이터 클라이언트(_data_client)는 영속 — 모니터/저장소가 계속 쓰므로 여기서 안 닫음.
        ex, sig, rt = self.executor, self.signal, self.rt
        self.executor = self.signal = self.broker = self.rt = None

        def shutdown() -> None:
            try:
                if ex:
                    ex.stop()  # 집행 스레드 join까지 대기
            except Exception:
                logger.exception("executor 종료 오류")
            try:
                if sig:
                    sig.stop()
            except Exception:
                logger.exception("signal 종료 오류")
            try:
                if rt:
                    rt.stop()
            except Exception:
                logger.exception("실시간 피드 종료 오류")
            logger.info("실행 정지 완료")

        threading.Thread(target=shutdown, name="trdbot-shutdown", daemon=True).start()
        logger.info("실행 정지 요청")

    # ---- 미체결 캐시 무효화 / 수동 조회 ----
    def _on_fill(self, ordno: str, qty: int) -> None:
        """C01 체결 시(WS 스레드) → 미체결 목록에 **즉시 반영**(조회 없이) + 집행 깨우기.

        체결된 주문을 미체결 캐시에서 바로 빼므로(전량체결 시 제거), t0434를 안 기다리고
        다음 주문이 즉시 나간다 — 진짜 실시간. (드리프트는 주기 t0434 재동기화로 보정)
        """
        if self._store is not None:
            self._store.apply_fill(ordno, qty)   # C01 체결을 미체결에 즉시 차감/제거
        self._beep_fill()                         # 체결음(사운드 체크 시)
        ex = self.executor
        if ex is not None:
            ex.wake()   # 체결 즉시 재맞춤 → 다음 주문 바로 나감
        # 체결 = 잔고 변동 → 포지션도 갱신. 장 종료·정지(관찰) 상태로 reconcile이 안 돌 때도
        # 잔고가 반영되게 별도 재조회(in_window 게이트와 무관). 연타 체결은 스로틀로 accno 절감.
        self._refresh_position_after_fill()

    def _beep_fill(self) -> None:
        """체결 시 체결음 — 화면 '사운드' 체크됐을 때만. 실패해도 무해.

        config\\fill.wav(짧은 손종 소리)가 있으면 그걸 재생, 없으면 윈도우 기본 경고음.
        SND_ASYNC 로 비동기 재생 → 체결 처리 흐름을 막지 않음.
        """
        try:
            if not self.screen.sound_enabled.get():
                return
            import winsound
            from lsapi.paths import app_base_dir
            wav = app_base_dir() / "config" / "fill.wav"
            if wav.exists():
                winsound.PlaySound(str(wav), winsound.SND_FILENAME | winsound.SND_ASYNC)
            else:
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            logger.debug("체결음 재생 실패", exc_info=True)

    # ---- 리스크 알람 (경고음 + 배너, 사용자 해제 전까지 1분마다 반복) ----
    def _raise_alarm(self, key: str, message: str) -> None:
        """리스크 알람 발생 — 배너 표시 + (새 알람이면) 즉시 경고음. 해제 전까지 유지."""
        with self._alarm_lock:
            is_new = key not in self._alarms
            self._alarms[key] = message
            active = list(self._alarms.values())
        self.screen.post(lambda: self.screen.show_alarm(active))
        if is_new:
            self._play_alarm()
            logger.warning("리스크 알람: [%s] %s", key, message)

    def _dismiss_alarms(self) -> None:
        """[알람 해제] 버튼 — 모든 알람 지움(경고음 중지)."""
        with self._alarm_lock:
            had = bool(self._alarms)
            self._alarms.clear()
        self.screen.post(lambda: self.screen.show_alarm([]))
        if had:
            logger.info("리스크 알람 해제(사용자)")

    def _alarm_tick(self) -> None:
        """1분마다 — 활성 알람 있으면 경고음 반복 + 배너 유지(메인스레드 타이머)."""
        try:
            with self._alarm_lock:
                active = list(self._alarms.values())
            if active:
                self._play_alarm()
                self.screen.show_alarm(active)
        except Exception:
            logger.debug("알람 tick 실패", exc_info=True)
        finally:
            try:
                self.screen.root.after(60000, self._alarm_tick)
            except Exception:
                pass

    def _play_alarm(self) -> None:
        """경고음 — config\\alarm.wav 있으면 재생, 없으면 윈도우 위험음. '사운드' 체크와 무관(중요 알림)."""
        try:
            import winsound
            from lsapi.paths import app_base_dir
            wav = app_base_dir() / "config" / "alarm.wav"
            if wav.exists():
                winsound.PlaySound(str(wav), winsound.SND_FILENAME | winsound.SND_ASYNC)
            else:
                winsound.MessageBeep(winsound.MB_ICONHAND)
        except Exception:
            logger.debug("경고음 실패", exc_info=True)

    def _on_ws_disconnect(self, name: str) -> None:
        """실시간 WS(시세/장운영/체결) 예기치 않은 끊김 → 알람. (의도된 재연결은 제외돼 옴)"""
        self._raise_alarm(f"ws:{name}", f"{name} 연결 끊김 — 재연결 시도 중")

    def _on_accept(self, rec: dict) -> None:
        """O01 접수 시(WS 스레드) → 미체결 목록에 즉시 추가(조회 없이). 실시간 미체결."""
        if self._store is not None:
            self._store.apply_accept(rec)

    def _on_cancel(self, ordno: str) -> None:
        """H01 정정취소 시(WS 스레드) → 미체결 목록에서 즉시 제거 + 집행 깨우기."""
        if self._store is not None:
            self._store.apply_cancel(ordno)
        ex = self.executor
        if ex is not None:
            ex.wake()   # 취소 확정 → 즉시 재맞춤(다음 주문)

    def _on_query(self) -> None:
        """자동주문화면 [조회] → 봇의 미체결 캐시·포지션을 강제 갱신하고 화면 반영.

        네트워크(t0441)는 별도 스레드에서 — GUI 멈춤 방지.
        """
        b = self.broker
        if b is None:
            self._on_status("조회: 실행 중이 아닙니다")
            return

        def work() -> None:
            b.invalidate_open_orders()   # 다음 get_open_orders 때 t0434
            b.resync_position()          # 다음 get_position 때 t0441
            self._read_position_once()   # 포지션 즉시 다시 읽어 화면 갱신
            if self.executor is not None:
                self.executor.wake()     # 새 데이터로 즉시 한 번 맞춰가기
            self._on_status("조회: 미체결·포지션 갱신")

        threading.Thread(target=work, daemon=True).start()

    # ---- 체결량 서킷 브레이커 ----
    def _fill_breaker(self) -> bool:
        """최근 윈도우(+재개 이후) 누적 체결량 ≥ 한도면 True(주문 중지). 한도 0이면 끔."""
        if self._fill_limit <= 0 or self._store is None:
            return False
        qty = recent_fill_qty(self._store.fills(), time.time(), self._fill_window_sec,
                              since=self._breaker_reset_at)
        return qty >= self._fill_limit

    def _on_breaker_trip(self) -> None:
        """체결량 한도 트립(집행 스레드) → '주문' 자동 해제. 사용자가 다시 체크해야 재개(latch)."""
        self._trade_enabled = False
        self.screen.post(lambda: self.screen.trade_enabled.set(False))
        self._raise_alarm("breaker", "체결량 한도 초과 — 주문 자동 중지('주문' 다시 체크 필요)")

    # ---- 주문 안전 체크박스 ----
    def _on_trade_toggle(self, enabled: bool) -> None:
        """'주문' 체크 → 실주문 허용. 해제 → 관찰모드(주문 안 나감). 즉시 반영."""
        self._trade_enabled = enabled
        if enabled:
            self._breaker_reset_at = time.time()   # 재개 시 체결량 카운트 리셋
            self._dismiss_alarms()   # '주문' 다시 켬 = 사용자 인지 → 알람 해제
        ex = self.executor
        if ex is not None:
            ex.wake()   # 관찰→활성 전환 시 다음 시그널 안 기다리고 즉시 집행 재개
        logger.info("주문 %s", "활성(실주문)" if enabled else "비활성(관찰)")

    # ---- 실행 중 설정 적용 (적용 버튼) ----
    def _on_apply(self, live: dict) -> None:
        """실행 중 변경 항목 반영 — 비율은 _ratios, 집행 파라미터는 executor에 즉시 적용."""
        self._ratios = (live["hedge_ratio"], live["manual_balance_adjust"])
        self._fill_window_sec = live["fill_window_min"] * 60.0
        self._fill_limit = live["fill_limit"]
        ex = self.executor
        if ex is not None:
            ex.qty_per_order = live["qty_per_order"]
            ex.order_interval_sec = live["order_interval_sec"]
            ex.max_position = live["max_position"]
            ex.wake()  # 다음 사이클 기다리지 말고 즉시 다시 맞춰가기
        # 화면 목표계약수도 즉시 재계산(마지막 시그널 + 새로 적용된 값) → 표시와 집행 일치
        sig = self._last_signal
        if sig is not None:
            b, exposure = self._ratios
            self.screen.apply_signal(
                fx=sig.fx, total_coin=sig.total_coin, hedge_ratio=b, dollar_exposure=exposure)
        logger.info("실행 중 설정 적용: %s", live)

    # ---- seam: 시그널 → 부호목표 ----
    def _compute_target(self, sig: Signal) -> int:
        b, exposure = self._ratios
        # Meme(달러 기준·해외코인매도 다리)은 Dalin(원화 기준·국내코인매수 다리)과 반대 다리라 D 부호 반전.
        flip = self._signal_token == "Meme"
        _, _, t = target_contracts(exposure, sig.total_coin, b, sig.fx, flip_hedge=flip)
        return t

    def _in_session_window(self, cfg: OrderConfig) -> bool:
        # 1차 게이트: 현재시각(session_now) 세션 + 사용자가 정한 주문가능시간.
        sess = session_now(_now_hhmm())
        if sess == "closed":
            return False
        now = datetime.now().time()
        if not (_in_window(cfg.day_window, now) or _in_window(cfg.night_window, now)):
            return False
        # 2차: JIF 실시간 장운영 데이터가 들어왔으면 그 진실을 우선한다.
        #   동시호가/서킷브레이크/마감이면 신규 주문 금지.
        if self.rt is not None:
            jangubun = JANGUBUN_DAY_FUTURES if sess == "day" else JANGUBUN_NIGHT_FUTURES
            if self.rt.seen(jangubun):
                return self.rt.tradeable(jangubun)
        # 3차(폴백): JIF 미수신이면 시각 기반 국면으로 판단 — 주간 동시호가도 거른다.
        return is_tradeable(current_time_phase(_now_hhmm(), sess))

    # ---- 콜백 (백그라운드 → 메인스레드 위임) ----
    def _on_signal(self, sig: Signal) -> None:
        self._last_signal = sig   # 적용 시 목표 재계산에 재사용
        # 화면 표시 갱신은 메인스레드에서 — 헤지비율·노출액은 적용된 값(_ratios)을 넘김
        # (입력칸을 실시간으로 읽지 않음 → 입력 도중 값이 C/D에 반영되지 않게).
        b, exposure = self._ratios
        self.screen.post(lambda: self.screen.apply_signal(
            fx=sig.fx, total_coin=sig.total_coin, hedge_ratio=b, dollar_exposure=exposure))
        # 집행 엔진에 전달
        if self.executor:
            self.executor.on_signal(sig)

    def _on_position(self, pos: int) -> None:
        # 원달러선물잔고 표시칸 갱신 (메인스레드 위임)
        self.screen.post(lambda: self.screen.set_display("usd_futures_balance", f"{pos:,}"))

    def _refresh_position_after_fill(self) -> None:
        """체결 시 포지션 재조회 → 자동주문화면 잔고 + 모니터 포지션 표 갱신.

        in_window 게이트로 executor가 멈춘(장 종료·정지) 상태에서도 잔고를 반영한다.
        WS 스레드를 막지 않게 백그라운드로, 연타 체결은 1.5초 스로틀로 t0441 폭주 방지.
        """
        store = self._store
        if store is None:
            return
        now = time.time()
        if now - self._last_fill_pos_refresh < 1.5:
            return
        self._last_fill_pos_refresh = now

        def _work() -> None:
            try:
                # 자동 t0441 재조회 안 함(체결 직후 stale 조회가 실포지션 덮어써 오버슛 유발).
                # 실시간 C01 기반 net으로 표시만 갱신 — t0441 재동기화는 사용자 [Idle조회]로만.
                pos = store.net_position()        # baseline + C01 실시간 증감(정확)
                self._on_position(pos)            # 자동주문화면 원달러선물잔고
                if self._monitor is not None:
                    self._monitor.post(self._monitor._render)   # 모니터 포지션 표 다시 그림
            except Exception:
                logger.exception("체결 후 포지션 갱신 실패")

        threading.Thread(target=_work, name="fill-pos-refresh", daemon=True).start()

    def _read_position_once(self) -> None:
        # 잔고는 장 마감에도 조회 가능(t0441) → 표시칸에 즉시 반영.
        try:
            if self.broker is not None:
                self._on_position(self.broker.get_position())
        except Exception as e:
            logger.debug("초기 포지션 조회 스킵: %s", e)

    def _on_status(self, msg: str) -> None:
        self.screen.post(lambda: self.screen.set_status(msg))
        # 리스크로 주문이 막힌 상태를 알람으로 — 최대잔고한도 초과 보류(주문이 나가야 하는데 안 나감).
        if "최대잔고한도" in msg:
            self._raise_alarm("maxpos", "최대잔고한도 초과 — 주문 보류")
        # 증거금부족 거부는 재시도로 안 풀린다(입금·한도 조정 필요) → 사람이 알아야 함.
        if "주문거부" in msg and "증거금" in msg:
            self._raise_alarm("margin", "증거금 부족 — 주문 거부 중(예탁금·최대잔고한도 확인)")

    def run(self) -> None:
        try:
            self.screen.run()
        finally:
            # 메인 창 닫힘 → 중앙저장소 클라이언트 정리(모니터 Toplevel은 root와 함께 종료).
            if self._data_client is not None:
                try:
                    self._data_client.close()
                except Exception:
                    pass


def main() -> None:
    setup_logging()
    TradingApp().run()   # 모니터는 같은 프로세스의 Toplevel 창으로 함께 뜸


if __name__ == "__main__":
    main()

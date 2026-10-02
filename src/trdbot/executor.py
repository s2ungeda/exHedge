"""집행 엔진 (maker 재호가 루프).

설계: docs/execution-engine.md 2장.

원칙:
- **seam**: 전략별 목표 계산을 `compute_target(signal) → 부호목표`로 **주입**받는다.
  executor는 targets.py를 직접 모른다 → 전략이 늘어도 이 파일은 안 바뀐다.
- **BrokerPort**: 브로커 IO를 추상화. 실제는 broker_ls, 테스트는 FakeBroker.
- **reconcile_plan()**: 한 사이클의 '취소 목록 + 새 주문'을 순수 함수로 분리 → 결정적 테스트.

동작(연속 reconciler, 최신 시그널 우선):
    while 실행중:
        목표 = compute_target(최신 시그널)        # 부호(+롱/−숏) 포함
        포지션 = broker.get_position()             # 실시간(C01) — 체결이 바로 반영
        미체결 = broker.get_open_orders()          # 들고 온 주문까지(t0434)
        계획 = reconcile_plan(목표, 포지션, 미체결, …)
        → 반대방향/낡은가격/초과분 취소 + 부족분만 새 주문(이중주문 방지)
        인터벌 대기 후 다시 맞춰감(체결은 포지션으로, 호가 변동은 낡은 주문 취소·재호가로)
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Literal, Protocol

from trdbot.datastore import _norm_ordno
from trdbot.pricing import quote_from_orderbook

logger = logging.getLogger(__name__)

Side = Literal["buy", "sell"]


# ──────────────────────────────────────────────────────────
# 브로커 포트 (executor가 의존하는 추상 인터페이스)
# ──────────────────────────────────────────────────────────
class BrokerPort(Protocol):
    def get_position(self) -> int:
        """현재 순포지션(계약). 매수 +, 매도 −. (원달러선물 최근월+차근월 합)"""
        ...

    def get_orderbook(self) -> dict:
        """호가 OutBlock(dict). offerhoN/bidhoN 포함."""
        ...

    def place_order(self, side: Side, qty: int, price: float) -> str:
        """maker 지정가 주문. 주문번호 반환."""
        ...

    def cancel(self, order_no: str) -> None:
        ...

    def get_open_orders(self) -> list[dict]:
        """미체결 주문 목록 [{ordno, side, remaining, price}]. reconcile에서 사용."""
        ...


# ──────────────────────────────────────────────────────────
# 한 사이클 결정 결과 (순수)
# ──────────────────────────────────────────────────────────
@dataclass
class Decision:
    action: Literal["order", "stop", "skip"]
    reason: str = ""
    side: Side | None = None
    qty: int = 0
    price: float | None = None


# ──────────────────────────────────────────────────────────
# reconcile(맞춰가기) — 들고 온 미체결까지 고려한 한 사이클 계획 (순수)
# ──────────────────────────────────────────────────────────
@dataclass
class ReconcilePlan:
    cancels: list[str]    # 취소할 주문번호(반대방향/낡은가격/목표초과분)
    decision: Decision    # 새 주문/수렴(stop)/대기(skip)


# 주문가 비교 허용오차(부동소수 안전). 틱(0.1)보다 훨씬 작아 인접 호가와 안 헷갈림.
_PRICE_TOL = 1e-4


def recent_fill_qty(fills: list[dict], now: float, window_sec: float, since: float = 0.0) -> int:
    """최근 window_sec(초) 안 + since(리셋시점) 이후에 들어온 체결 수량 합. '_ts'(수신 epoch) 기준.

    체결량 한도(서킷 브레이커) 판단용. since는 '재개 시 카운트 리셋' — 재개 시점 이전 체결은 안 셈.
    """
    if window_sec <= 0:
        return 0
    total = 0
    for f in fills:
        ts = f.get("_ts")
        if ts is not None and (now - ts) <= window_sec and ts > since:
            total += abs(int(f.get("qty", 0) or 0))
    return total


def reconcile_plan(
    target: int,
    position: int,
    open_orders: list[dict],
    *,
    buy_price: float | None,
    sell_price: float | None,
    qty_per_order: int,
    max_position: int,
    in_window: bool,
    max_orders: int = 1,
    fresh_ordnos: set[str] = frozenset(),
) -> ReconcilePlan:
    """현재 상태(목표·포지션·미체결·maker가격)에서 '취소 목록 + 새 주문'을 계산한다.

    규칙:
    - 미체결 주문은 **목표 방향과 같을 때** 유지. 단 **가격 낡음(maker가 다름)으로 인한 취소는
      주문인터벌이 지난 주문(fresh_ordnos 밖)에만** 적용 — 인터벌 안이면 호가가 흔들려도 유지.
      (반대방향 / 목표 초과분 / 동시한도 초과분은 인터벌과 무관하게 즉시 취소)
    - **동시 주문 한도(max_orders)**: 한 번에 그 개수만큼만(기본 1). 부족분이 더 남아도 기존
      미체결이 체결돼 줄어들 때까지 기다린다 → 주문이 한꺼번에 쌓이지 않음.
    - 유지분(in-flight)을 포지션에 더한 '실효포지션'으로 부족분만 새 주문(이중주문 방지).
    """
    if not in_window:
        return ReconcilePlan([], Decision("skip", "주문가능시간 외"))

    gap0 = target - position
    need_side: Side | None = "buy" if gap0 > 0 else "sell" if gap0 < 0 else None
    budget = abs(gap0)  # 유지 가능한 in-flight 한도(목표 초과 방지)

    cancels: list[str] = []
    kept_abs = 0
    kept_count = 0
    for o in open_orders:
        side = o["side"]
        maker = buy_price if side == "buy" else sell_price
        on_price = (maker is None) or (abs(float(o["price"]) - maker) < _PRICE_TOL)
        # 인터벌 안(fresh)이면 가격이 낡아도 유지 → 취소는 인터벌 지난 뒤에만.
        price_ok = on_price or (str(o["ordno"]) in fresh_ordnos)
        rem = int(o["remaining"])
        if (side == need_side and price_ok and kept_count < max_orders
                and kept_abs + rem <= budget):
            kept_abs += rem      # 같은 방향·(제 가격 또는 인터벌 내)·동시한도 내·목표 내 → 유지
            kept_count += 1
        else:
            cancels.append(str(o["ordno"]))  # 반대/한도초과/동시초과/(인터벌지난)낡은가격 → 취소

    kept_signed = kept_abs if need_side == "buy" else -kept_abs
    effective = position + kept_signed
    diff = target - effective

    if abs(diff) < qty_per_order:
        return ReconcilePlan(cancels, Decision("stop", f"수렴(diff={diff})"))

    if kept_count >= max_orders:
        # 이미 동시한도만큼 미체결 관리 중 → 새로 안 냄(체결로 줄어들 때까지 대기)
        reason = f"미체결 {kept_count}건 관리 중(동시 {max_orders})"
        return ReconcilePlan(cancels, Decision("skip", reason))

    side = "buy" if diff > 0 else "sell"
    signed = qty_per_order if side == "buy" else -qty_per_order
    new_pos = effective + signed
    if abs(new_pos) > abs(effective) and abs(new_pos) > max_position:
        return ReconcilePlan(cancels, Decision("skip", f"최대잔고한도 초과(캡 {max_position})"))

    price = buy_price if side == "buy" else sell_price
    if price is None:
        return ReconcilePlan(cancels, Decision("skip", "호가 없음/공백 과다"))

    place = Decision("order", "재호가", side=side, qty=qty_per_order, price=price)
    return ReconcilePlan(cancels, place)


# ──────────────────────────────────────────────────────────
# 집행 엔진
# ──────────────────────────────────────────────────────────
@dataclass
class ReconcileResult:
    decision: Decision           # order / stop(수렴) / skip(대기)
    cancelled: int = 0           # 이번에 취소한 주문 수
    kept_open: int = 0           # 유지 중인 미체결 주문 수(있으면 인터벌마다 계속 관리)
    order_no: str | None = None  # 새로 낸 주문번호
    rejected: bool = False       # 주문거부 발생(run이 백오프 재시도 판단)


class Executor:
    def __init__(
        self,
        broker: BrokerPort,
        *,
        qty_per_order: int,
        order_interval_sec: float,
        max_position: int,
        max_orders: int = 1,          # 동시에 띄우는 주문 수(기본 1 — 한 개 체결돼야 다음)
        compute_target: Callable[[object], int],   # seam: signal → 부호목표
        in_window: Callable[[], bool] = lambda: True,
        trade_enabled: Callable[[], bool] = lambda: True,  # False면 관찰모드(주문 안 나감)
        fill_breaker: Callable[[], bool] = lambda: False,  # True면 체결량 한도 초과 → 주문 중지
        on_breaker_trip: Callable[[], None] | None = None,  # 트립 시(앱이 '주문' 해제)
        test_nofill_ticks: int = 0,   # (테스트) 비체결 방향 N틱 이동: 매수↓ 매도↑ (0=정상)
        fallback_tick: float | None = None,         # 호가 성길 때 틱 폴백(예: KOSPI200 0.05)
        on_status: Callable[[str], None] | None = None,
        on_position: Callable[[int], None] | None = None,  # 포지션 읽을 때마다 보고(화면 표시용)
        on_quote: Callable[[dict], None] | None = None,    # 매 사이클 호가·주문가 (가격 검증 로그)
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        poll_sec: float = 0.3,
        idle_sec: float = 1.0,
        reject_backoff_base: float = 1.0,   # 주문거부 백오프 시작(초)
        reject_backoff_cap: float = 30.0,   # 주문거부 백오프 상한(초)
    ):
        self.broker = broker
        self.qty_per_order = qty_per_order
        self.order_interval_sec = order_interval_sec
        self.max_position = max_position
        self.max_orders = max_orders
        self.compute_target = compute_target
        self.in_window = in_window
        self.trade_enabled = trade_enabled
        self.fill_breaker = fill_breaker
        self.on_breaker_trip = on_breaker_trip
        self._test_nofill_ticks = test_nofill_ticks
        self.fallback_tick = fallback_tick
        self.on_status = on_status
        self.on_position = on_position
        self.on_quote = on_quote
        self._sleep = sleep
        self._clock = clock
        self._poll_sec = poll_sec
        self._idle_sec = idle_sec
        self._reject_backoff_base = reject_backoff_base
        self._reject_backoff_cap = reject_backoff_cap

        self._latest_signal: object | None = None
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        # 이벤트 기반 idle: 수렴 후엔 폴링 멈추고 다음 시그널까지 대기(레이트리밋 절감).
        self._wake = threading.Event()
        self._new_signal = False
        self._converged = False
        self._last_target: int | None = None
        self._last_emit: str | None = None   # 마지막 표시 메시지(연속 중복 방지)
        self._last_log: dict[str, str | None] = {}   # 판단 로그 종류별 마지막 내용(바뀔 때만 기록)
        self._placed_at: dict[str, float] = {}   # 우리가 낸 주문번호 → 시각(인터벌 게이트용)
        self._canceling: set[str] = set()   # 취소 요청 후 확인(사라짐) 대기 중인 주문(정규화)
        self._reject_streak = 0   # 연속 주문거부 횟수(백오프 증가용) — 성공 시 0으로 리셋

    # ---- 시그널 입력 (signal_link.on_signal에 연결) ----
    def on_signal(self, sig: object) -> None:
        with self._lock:
            self._latest_signal = sig
            self._new_signal = True
        self._wake.set()

    def wake(self) -> None:
        """실행 중 설정(qty/인터벌/한도/비율)이 바뀌었을 때 즉시 다시 맞춰가도록 깨운다."""
        self._converged = False
        self._wake.set()

    # ---- 한 사이클 (reconcile: 맞춰가기) ----
    def reconcile(self) -> ReconcileResult:
        """한 번 맞춰가기: 낡은/반대 주문 취소 + 부족분 새 주문.

        포지션은 실시간(C01), 호가는 FH9 또는 REST(t2112), 미체결은 t0434. 체결은 다음 사이클에
        포지션(실시간)으로 반영되므로 별도 체결 폴링이 필요 없다.
        """
        with self._lock:
            sig = self._latest_signal
        if sig is None:
            return ReconcileResult(Decision("skip", "시그널 없음"))
        if not self.in_window():
            return ReconcileResult(Decision("skip", "주문가능시간 외"))

        target = self.compute_target(sig)
        self._log_change("target", f"목표계약수 {target}")
        position = self.broker.get_position()
        if self.on_position:
            try:
                self.on_position(position)
            except Exception:
                logger.exception("on_position 예외")

        # 주문 비활성(관찰모드): 주문 로직(미체결·호가 조회, 계획, 취소/주문)을 아예 돌리지 않는다.
        # 화면 포지션만 갱신하고 끝 — 시도가 없으니 로그도 없다. 활성화하면 wake로 즉시 재개.
        if not self.trade_enabled():
            return ReconcileResult(Decision("stop", "주문 비활성(관찰)"), kept_open=0)

        open_orders = self.broker.get_open_orders()
        ob = self.broker.get_orderbook()
        buy_q = quote_from_orderbook(ob, "buy", fallback_tick=self.fallback_tick)
        sell_q = quote_from_orderbook(ob, "sell", fallback_tick=self.fallback_tick)
        buy_price = buy_q.price if buy_q else None
        sell_price = sell_q.price if sell_q else None
        buy_price, sell_price = self._test_shift(buy_price, sell_price)  # 테스트: 비체결 방향 N틱

        # 추적 정리: 미체결에서 사라진 주문 = 전량체결 또는 취소확인됨 → 추적/취소대기 해제.
        open_ids = {_norm_ordno(o["ordno"]) for o in open_orders}
        self._canceling &= open_ids
        for k in [k for k in self._placed_at if _norm_ordno(k) not in open_ids]:
            del self._placed_at[k]

        # 체결량 서킷 브레이커: 한도 초과 → 미체결 전부 취소 + 주문 중지('주문' 해제, 수동 재개).
        if self.fill_breaker():
            for o in open_orders:
                try:
                    self.broker.cancel(str(o["ordno"]))
                except Exception:
                    logger.exception("브레이커 취소 실패 ordno=%s", o.get("ordno"))
            self._canceling = set()
            self._emit(f"⚠ 체결량 한도 초과 — 주문 중지(미체결 {len(open_orders)}건 취소). "
                       f"'주문' 다시 체크해야 재개")
            if self.on_breaker_trip:
                try:
                    self.on_breaker_trip()
                except Exception:
                    logger.exception("on_breaker_trip 예외")
            dec = Decision("stop", "체결량 한도 초과(주문 중지)")
            self._log_quote(buy_q, sell_q, buy_price, sell_price, target, position, dec)
            return ReconcileResult(dec, cancelled=len(open_orders), kept_open=0)

        # ── 상태기계: 미체결 우리 주문이 하나라도 있으면 새 주문 절대 안 냄. ──
        # 다음 주문은 (1)전량체결 또는 (2)인터벌 경과→취소→취소확인 으로 미체결이 0이 돼야 나간다.
        # 통보(체결/취소)로만 미체결이 사라지므로, 통보가 없으면 여기서 멈춘다(폭주 방지).
        if open_orders:
            gap = target - position
            need_side: Side | None = "buy" if gap > 0 else "sell" if gap < 0 else None
            cancelled: list[str] = []
            for o in open_orders:
                oid = _norm_ordno(o["ordno"])
                if oid in self._canceling:
                    continue   # 이미 취소 요청함 → 확인(사라짐) 대기
                placed = self._placed_at.setdefault(str(o["ordno"]), self._clock())
                interval_passed = (self._clock() - placed) >= self.order_interval_sec
                # 반대 방향은 즉시, 그 외는 인터벌 경과 후 취소(미체결이면 재호가 위해).
                if o["side"] != need_side or interval_passed:
                    try:
                        self.broker.cancel(str(o["ordno"]))
                        self._canceling.add(oid)
                        cancelled.append(f"#{o['ordno']}")
                    except Exception:
                        logger.exception("주문 취소 실패 ordno=%s", o["ordno"])
            newly = len(cancelled)
            if newly:
                self._emit(f"미체결 {newly}건 취소 요청 [{', '.join(cancelled)}]"
                           f"(인터벌 경과/반대) — 취소 확인 대기")
            dec = Decision("skip", "취소 확인 대기" if self._canceling else "체결 대기")
            orders = ", ".join(f"#{o['ordno']} {o['side']} 잔{o['remaining']} @{o['price']}"
                               for o in open_orders)
            self._log_change("state", f"대기({dec.reason}): 미체결 [{orders}] "
                                      f"(목표 {target}, 포지션 {position})")
            self._log_quote(buy_q, sell_q, buy_price, sell_price, target, position, dec)
            return ReconcileResult(dec, cancelled=newly, kept_open=len(open_orders))

        # ── 미체결 0건(모두 해소) → 부족분 있으면 1건만 주문. reconcile_plan 재사용:
        #    수렴/최대잔고한도/호가공백 판정을 그대로 태운다(잔고 한도 체크 포함). ──
        plan = reconcile_plan(
            target, position, [],   # 미체결 없음 → 취소 계산 없이 '주문/수렴/보류'만
            buy_price=buy_price, sell_price=sell_price,
            qty_per_order=self.qty_per_order, max_position=self.max_position,
            in_window=True, max_orders=self.max_orders,
        )
        dec = plan.decision
        self._log_quote(buy_q, sell_q, buy_price, sell_price, target, position, dec)
        order_no = None
        if dec.action == "order":
            assert dec.side is not None and dec.price is not None
            try:
                order_no = self.broker.place_order(dec.side, dec.qty, dec.price)
            except Exception as e:
                # 주문거부(매매불가·증거금·인증·네트워크 등) → 화면 표시 + 백오프 재시도(run이 대기).
                rej = Decision("skip", f"주문거부: {e}")
                self._log_quote(buy_q, sell_q, buy_price, sell_price, target, position, rej)
                return ReconcileResult(rej, kept_open=0, rejected=True)
            if order_no:
                self._placed_at[str(order_no)] = self._clock()
            self._emit(f"주문 {dec.side} {dec.qty}@{dec.price} #{order_no} "
                       f"(목표 {target}, 포지션 {position})")
            self._last_log["state"] = None   # 주문 뒤 첫 대기/수렴은 다시 기록
        elif dec.action == "skip":
            self._emit(f"⚠ 주문 보류 — {dec.reason} (목표 {target}, 포지션 {position})")
        else:
            self._log_change("state", f"{dec.reason} (목표 {target}, 포지션 {position})")
        return ReconcileResult(dec, kept_open=0, order_no=order_no)

    # ---- 루프 (이벤트 기반) ----
    def run(self) -> None:
        self._running = True
        self._emit("집행 시작")
        while self._running:
            with self._lock:
                sig = self._latest_signal
                new = self._new_signal
                self._new_signal = False

            # 시그널 없음/주문가능시간 밖 → 폴링 안 함, 다음 시그널까지 대기
            in_win = sig is not None and self.in_window()
            if sig is not None:
                self._log_change("window", "주문가능 구간" if in_win
                                 else "주문가능 구간 밖(시간외/동시호가/장운영) — 신규 주문 중지")
            if not in_win:
                self._idle_wait()
                continue

            # 새 시그널 없고 이미 수렴(목표 동일) → 폴링 생략 (레이트리밋 절감 핵심)
            target = self.compute_target(sig)
            if not new and self._converged and target == self._last_target:
                self._idle_wait()
                continue

            try:
                r = self.reconcile()
            except Exception:
                logger.exception("집행 사이클 예외")
                self._idle_wait()
                continue

            self._last_target = target
            # 주문거부 → 화면 표시 + 지수 백오프 대기(연속 거부 시 재시도 간격 확대, 스팸 방지).
            if r.rejected:
                self._reject_streak += 1
                wait = min(self._reject_backoff_base * (2 ** (self._reject_streak - 1)),
                           self._reject_backoff_cap)
                self._emit(f"⚠ {r.decision.reason} ({self._reject_streak}회) — {wait:.0f}초 후 재시도")
                self._converged = False
                self._backoff_wait(wait)
                continue
            self._reject_streak = 0   # 거부 없는 사이클 → 스트릭 리셋
            # 주문을 냈거나 유지 중인 미체결이 남아 있으면 → 인터벌 뒤 다시 맞춰간다.
            # (그 사이 체결되면 포지션이 실시간 반영, 호가가 움직이면 낡은 주문 취소·재호가)
            if r.decision.action == "order" or r.kept_open > 0:
                self._converged = False
                self._wait_interval()
            elif r.decision.action == "stop":
                self._converged = True   # 미체결 없고 수렴 → 다음 시그널까지 대기
                self._idle_wait()
            else:  # skip (호가공백 과다 등) — 잠깐 뒤 재시도
                self._converged = False
                self._idle_wait()
        self._emit("집행 종료")

    def _idle_wait(self) -> None:
        """다음 시그널(또는 타임아웃)까지 대기. 폴링하지 않음."""
        self._wake.wait(self._idle_sec)
        self._wake.clear()

    def _wait_interval(self) -> None:
        """주문 인터벌만큼 대기(시그널/중지 시 즉시 깸). 이 뒤 다시 reconcile."""
        self._wake.wait(self.order_interval_sec)
        self._wake.clear()

    def _backoff_wait(self, sec: float) -> None:
        """주문거부 백오프 대기 — 새 시그널로는 안 깨우고(재시도 스팸 방지) 중지에만 즉시 반응."""
        end = self._clock() + sec
        while self._running:
            remaining = end - self._clock()
            if remaining <= 0:
                break
            self._sleep(min(0.2, remaining))

    def start(self) -> None:
        if self._running:
            return
        self._thread = threading.Thread(target=self.run, name="executor", daemon=True)
        self._thread.start()

    def stop(self, *, join: bool = True, timeout: float | None = 12.0) -> None:
        """정지. join=True면 집행 스레드가 끝날 때까지 대기(진행 중 사이클 완료 후).

        이걸 기다린 뒤 브로커/클라이언트를 닫아야 'client closed' race가 안 난다.
        """
        self._running = False
        self._wake.set()  # 대기 중이면 즉시 깨워 빠르게 종료
        t = self._thread
        if join and t is not None and t is not threading.current_thread():
            t.join(timeout)

    def _test_shift(self, buy_price, sell_price):
        """(테스트) 주문가를 비체결 방향으로 N틱 이동 — 매수↓ 매도↑. 0이면 그대로."""
        n = self._test_nofill_ticks
        if not n or not self.fallback_tick:
            return buy_price, sell_price
        off = self.fallback_tick * n
        if buy_price is not None:
            buy_price = round(buy_price - off, 4)   # 매수는 더 낮게 → 체결 안 됨
        if sell_price is not None:
            sell_price = round(sell_price + off, 4)  # 매도는 더 높게 → 체결 안 됨
        return buy_price, sell_price

    def _log_quote(self, buy_q, sell_q, buy_price, sell_price, target, position, dec) -> None:
        """호가·공백·실제 주문가(테스트 이동 반영)를 on_quote로 보고(가격 검증 CSV)."""
        if self.on_quote is None:
            return
        q = buy_q or sell_q
        if q is None:
            return
        detail = (f"{dec.side} {dec.qty}@{dec.price}" if dec.action == "order" else dec.reason)
        if self._test_nofill_ticks:
            detail = f"{detail} [테스트 {self._test_nofill_ticks}틱]"
        try:
            self.on_quote({
                "best_bid": q.best_bid, "best_ask": q.best_ask, "tick": q.tick,
                "gap": q.gap, "offset": q.offset,
                "buy_price": buy_price, "sell_price": sell_price,
                "target": target, "position": position,
                "action": dec.action, "detail": detail,
            })
        except Exception:
            logger.exception("on_quote 예외")

    def _log_change(self, kind: str, msg: str) -> None:
        """판단 과정 로그(파일 전용) — 같은 종류의 내용이 바뀔 때만 남긴다(사이클마다 반복 방지)."""
        if self._last_log.get(kind) == msg:
            return
        self._last_log[kind] = msg
        logger.info("[executor] %s", msg)

    def _emit(self, msg: str) -> None:
        logger.info("[executor] %s", msg)
        if msg == self._last_emit:   # 같은 메시지 연속 표시 방지(상단 로그 덮어쓰기 줄임)
            return
        self._last_emit = msg
        if self.on_status:
            try:
                self.on_status(msg)
            except Exception:
                logger.exception("on_status 예외")

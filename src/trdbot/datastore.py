"""중앙 데이터 저장소 — 포지션·미체결·체결의 단일 출처(t0441/t0434를 한 곳에서만 조회).

화면(자동주문화면·모니터)과 실행(executor→broker)이 **모두 이 저장소를 읽는다.**
- 포지션 net : t0441 시작값(baseline) + C01 증감(RealtimeFeed) + 주기 재동기화 → net_position()
- 포지션 행  : t0441 행(표시용) → positions()
- 미체결     : t0434 chegb=2(미체결만) 캐시 + 변동(주문/취소/체결) 시 재조회
                · 실행용 형식  → open_orders_exec()  [{ordno, side, remaining, price}]
                · 표시용 원본  → open_orders()        (t0434 행)
- 체결       : RealtimeFeed(C01) 누적, 봇 실행 이후만 → fills()

이 구조라 **봇 실행 이전 체결은 애초에 들어오지 않고**(미체결만 조회+C01 누적), t0441/t0434는
한 곳에서만 조회한다. 스레드 안전: 상태 갱신은 lock으로, 네트워크 호출은 lock 밖에서.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)


def _norm_ordno(x: Any) -> str:
    """주문번호 정규화(앞자리 0 제거) — 주문응답(OrdNo)과 C01(zero-padded) 매칭용."""
    s = str(x).strip()
    return str(int(s)) if s.isdigit() else s


class MarketDataStore:
    """포지션·미체결·체결의 중앙 저장소. 실행/화면이 모두 참조한다."""

    def __init__(
        self,
        api: Any,
        *,
        code_prefix: str = "",
        feed: Any = None,
        resync_sec: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        night_api: Any = None,
        session: Callable[[], str] | None = None,
    ):
        # api: FuturesAPI(t0441/t0434). feed: RealtimeFeed(C01·C02 net+체결). prefix: 종목군 필터.
        # night_api: NightFuturesAPI(CCENQ90200) — 야간 포지션 재동기화 소스. session: 현재 세션.
        # (미체결은 야간에도 pending+통보로 추적하므로 REST는 t0434 그대로 — 야간엔 비어도 무해)
        self._api = api
        self._night_api = night_api
        self._session = session or (lambda: "day")
        self._prefix = code_prefix
        self._feed = feed
        self._resync_sec = resync_sec
        self._clock = clock
        self._lock = threading.Lock()
        # 포지션
        self._positions: list[dict] = []   # t0441 행(표시)
        self._baseline: int | None = None  # 마지막 t0441 net(시작값)
        self._baseline_net = 0             # 그 시점의 C01 net
        self._baseline_at = 0.0
        # 미체결 캐시
        self._open_raw: list[dict] = []      # t0434 chegb=2 행(표시)
        self._open_at: float | None = None   # 마지막 t0434 시각(None=한 번도 안 가져옴)
        self._open_dirty = True
        self._min_refetch = 1.0              # t0434 재조회 최소 간격(초) — 체결 폭주 시 accno 보호
        self._force_open = False             # True면 쓰로틀 무시하고 즉시 재조회(체결 직후용)
        # 발주 장부 — 살아있는 우리 종목 주문(막 낸 주문·접수·t0434에서 본 주문). 이중주문 방지.
        # **체결(C01·C02) 전량 / 취소확인(H01·H02) 통보로만 제거.** t0434에서 사라졌다고 바로
        # 지우지 않는다: t0434가 C01보다 먼저 체결을 반영하면, 포지션엔 아직 없는데 미체결에서도
        # 빠져 같은 주문이 또 나간다(운영로그 2026-09 중복주문). 통보가 끝내 안 오면(누락)
        # 한 번 t0434에서 봤거나 취소 요청한 주문이 _stale_sec 넘게 t0434에 없을 때만 지우고
        # 포지션을 t0441로 다시 앵커한다.
        self._pending: dict[str, dict] = {}
        self._stale_sec = 30.0
        # C01 전량체결로 사라진 주문번호(정규화) → 시각. t0434가 늦게 갱신돼도 재등장 막음(억제).
        self._suppress: dict[str, float] = {}
        self._suppress_ttl = 5.0

    def set_feed(self, feed: Any) -> None:
        """C01 net·체결 출처 RealtimeFeed 연결(봇 실행 시점에 주입)."""
        self._feed = feed

    # ── 포지션 ───────────────────────────────────────
    def _c01_net(self) -> int:
        return self._feed.c01_state()[1] if self._feed is not None else 0

    def _fetch_position_rows(self) -> list[dict]:
        # 야간: CCENQ90200(야간잔고)을 주간 형식(expcode/jqty/medosu)으로 정규화.
        if self._session() == "night" and self._night_api is not None:
            rows = self._night_api.get_balance().get("CCENQ90200OutBlock3", [])
            out = []
            for r in rows:
                code = str(r.get("FnoIsuNo", ""))
                if self._prefix and not code.startswith(self._prefix):
                    continue
                out.append({"expcode": code, "jqty": r.get("UnsttQty", 0),
                            "medosu": r.get("BnsTpNm", "")})
            return out
        rows = self._api.get_balance().get("t0441OutBlock1", [])
        if self._prefix:
            rows = [r for r in rows if str(r.get("expcode", "")).startswith(self._prefix)]
        return rows

    def _read_balance_net(self) -> int:
        """t0441 행 읽어 net 계산 + 표시용 행 캐시. 매수 +, 매도 −."""
        rows = self._fetch_position_rows()
        total = 0
        for r in rows:
            qty = int(r.get("jqty", 0) or 0)
            total += qty if r.get("medosu") == "매수" else -qty
        with self._lock:
            self._positions = rows
        return total

    def _anchor_positions(self) -> None:
        """t0441로 시작값을 다시 잡는다(재동기화). 항상 진실에 맞추므로 안전.

        네트워크(t0441)는 lock 밖, baseline 필드 갱신만 lock(executor·모니터 동시접근 보호).
        """
        net = self._read_balance_net()   # 네트워크 + _positions 갱신
        c01 = self._c01_net()
        now = self._clock()
        with self._lock:
            prev = None if self._baseline is None else self._baseline + (c01 - self._baseline_net)
            self._baseline = net
            self._baseline_net = c01
            self._baseline_at = now
        logger.info("포지션 앵커(%s): net=%d (C01 기준 %d, 앵커 직전 추정 %s)",
                    "야간잔고" if self._session() == "night" else "t0441", net, c01, prev)

    def net_position(self) -> int:
        """순포지션 = 시작값(t0441) + C01 실시간 증감. 자동 재동기화는 하지 않는다.

        t0441 자동 재조회를 끈다 — 체결 직후 t0441은 그 체결을 아직 반영 못 해(지연) 낡은 값을
        주고, 그게 실시간 C01 포지션을 덮어써 오주문(오버슛)을 냈다. 그래서 최초 1회만 앵커하고
        이후엔 C01만으로 추적한다. t0441 재동기화는 사용자 [Idle조회] 버튼으로만(장이 잠잠할 때).
        """
        if self._feed is None:
            return self._read_balance_net()
        with self._lock:
            never = self._baseline is None
        if never:
            self._anchor_positions()
        with self._lock:
            base = self._baseline or 0
            bnet = self._baseline_net
        return base + (self._c01_net() - bnet)

    def resync_position(self) -> None:
        """다음 net_position 때 t0441 재동기화(수동조회용)."""
        logger.info("포지션 재동기화 요청 — 다음 조회 때 잔고(t0441)로 다시 앵커")
        self._baseline = None

    def positions(self) -> list[dict]:
        """포지션 행(표시용)."""
        with self._lock:
            return list(self._positions)

    # ── 미체결(t0434 chegb=2 캐시) ────────────────────
    def _fetch_open_raw(self) -> list[dict]:
        rows = self._api.get_unfilled_orders(chegb="2").get("t0434OutBlock1", [])
        if self._prefix:
            rows = [r for r in rows if str(r.get("expcode", "")).startswith(self._prefix)]
        return rows

    def _ensure_open(self) -> None:
        """캐시가 더럽거나(주문/취소/체결) 주기 경과면 t0434 재조회. 평소엔 캐시 사용.

        단 더러워도 최소간격(_min_refetch) 안에선 재조회 안 함 → 체결 연타 시 t0434 폭주 방지.
        그동안은 약간 낡은 미체결을 쓰지만, 막 낸 주문은 pending으로, 포지션은 C01로 정확.
        """
        now = self._clock()
        if self._force_open or self._open_at is None:
            need = True                       # 체결 직후(force)·최초 → 즉시(쓰로틀 무시)
        else:
            age = now - self._open_at
            need = (self._open_dirty and age >= self._min_refetch) or age >= self._resync_sec
        if need:
            self._force_open = False
            raw = self._fetch_open_raw()
            filled = self._fills_by_ordno()   # C01 체결량(주문번호별) — store lock 밖에서
            with self._lock:
                # C01로 이미 전량체결된 주문은 t0434가 늦게 보여줘도 제외(억제). t0434가 더는 안
                # 보여주면 억제 해제. (now 기준 TTL 백스톱)
                snap_ids = {_norm_ordno(r.get("ordno")) for r in raw}
                self._suppress = {k: t for k, t in self._suppress.items()
                                  if k in snap_ids and (now - t) < self._suppress_ttl}
                self._open_raw = [r for r in raw
                                  if _norm_ordno(r.get("ordno")) not in self._suppress]
                self._open_at = now
                self._open_dirty = False
                self._pending, stale = self._reconcile_ledger(filled, now)
                if stale:
                    self._baseline = None   # 통보 누락 → 다음 조회 때 t0441로 다시 앵커
            for k in stale:
                logger.warning("장부: #%s 통보 없이 t0434에서 %.0f초 넘게 사라짐 → 제거, "
                               "포지션 t0441 재앵커", k, self._stale_sec)

    def _reconcile_ledger(self, filled: dict[str, int], now: float) -> tuple[dict, list[str]]:
        """t0434 스냅샷(_open_raw)과 장부 대조(lock 안에서 호출). (새 장부, 오래 사라진 주문) 반환.

        - t0434에 있음 → seen 표시(사라짐 타이머 해제)
        - C01 누적 체결 ≥ 주문수량 → 체결 완료(포지션에 반영됨) → 제거
        - t0434에 없음 → 그대로 유지(통보 대기). 단 seen/취소요청 주문이 _stale_sec 넘게 없으면 제거
        - t0434에만 있는 주문(기동 전 주문 등) → 장부에 편입(이후 같은 규칙)
        """
        snap = {_norm_ordno(r.get("ordno")): r for r in self._open_raw}
        kept: dict[str, dict] = {}
        stale: list[str] = []
        for k, v in self._pending.items():
            nk = _norm_ordno(k)
            if nk in snap:
                v["seen"] = True
                v.pop("missing_since", None)
            elif filled.get(nk, 0) >= v.get("qty", v["remaining"]):
                continue
            elif v.get("seen") or v.get("cancel_requested"):
                since = v.setdefault("missing_since", now)
                if now - since >= self._stale_sec:
                    stale.append(k)
                    continue
            kept[k] = v
        have = {_norm_ordno(k) for k in kept}
        for nk, r in snap.items():
            rem = int(r.get("ordrem", 0) or 0)
            if nk not in have and rem > 0:
                kept[str(r.get("ordno"))] = {
                    "side": "buy" if "매수" in str(r.get("medosu", "")) else "sell",
                    "remaining": rem, "qty": rem, "price": float(r.get("price", 0) or 0),
                    "expcode": str(r.get("expcode") or ""), "ts": now, "seen": True,
                }
        return kept, stale

    def _fills_by_ordno(self) -> dict[str, int]:
        """C01 누적 체결을 주문번호별 합으로 — pending 체결완료 판정용."""
        out: dict[str, int] = {}
        if self._feed is None:
            return out
        for f in self._feed.fills():
            oid = _norm_ordno(f.get("ordno"))
            out[oid] = out.get(oid, 0) + int(f.get("qty", 0) or 0)
        return out

    def note_placed(self, ordno: str, side: str, qty: int, price: float,
                    expcode: str = "") -> None:
        """막 낸 주문을 in-flight로 등록 — t0434에 뜨기 전에도 이중주문 안 나가게."""
        with self._lock:
            self._pending[str(ordno)] = {
                "side": side, "remaining": int(qty), "qty": int(qty), "price": float(price),
                "expcode": expcode, "ts": self._clock(),
            }

    def find_open(self, ordno: str) -> dict | None:
        """취소용 — 미체결 캐시 또는 pending에서 (expcode, remaining). 없으면 None.

        t0434에 아직 안 뜬 막 낸 주문도 찾아 취소할 수 있어야 한다(안 그러면 취소 누락→주문 쌓임).
        """
        oid = str(ordno)
        with self._lock:
            for r in self._open_raw:
                if str(r.get("ordno")) == oid:
                    return {"expcode": str(r.get("expcode") or ""),
                            "remaining": int(r.get("ordrem", 0) or 0)}
            p = self._pending.get(oid)
            if p:
                return {"expcode": "", "remaining": int(p["remaining"])}  # 우리 주문 → 호출측 code
        return None

    def mark_cancel_requested(self, ordno: str) -> None:
        """취소 요청 표시 — 장부에서 지우지는 않는다(H01 취소확인/C01 체결로 지움).

        요청 직후 지우면, 취소와 엇갈려 체결된 주문이 C01 도착 전 미체결에서 빠져 같은 주문이
        또 나간다. 표시는 '통보 없이 t0434에서도 사라짐'을 오래된 주문으로 정리하는 근거.
        """
        nid = _norm_ordno(ordno)
        with self._lock:
            for k, v in self._pending.items():
                if _norm_ordno(k) == nid:
                    v["cancel_requested"] = True

    def apply_accept(self, rec: dict) -> None:
        """O01 접수 → 미체결에 **즉시 추가**(pending에). 실시간 미체결 목록.

        rec = {ordno, orgordno, expcode, medosu, ordrem, price}. pending에 넣어 t0434 조회에도
        살아남고, O01의 정확한 잔량(unercqty)으로 갱신. t0434에 뜨면 _ensure_open이 정리한다.
        정정(orgordno 있음)이면 원주문을 캐시·pending에서 제거(대체) 후 새 주문 추가.
        """
        nid = _norm_ordno(rec.get("ordno"))
        org = _norm_ordno(rec.get("orgordno")) if rec.get("orgordno") else ""
        with self._lock:
            self._suppress.pop(nid, None)   # 신규 → 억제 해제
            if org and org != nid:          # 정정 → 원주문 대체(제거 + 억제)
                self._open_raw = [r for r in self._open_raw
                                  if _norm_ordno(r.get("ordno")) != org]
                for k in [k for k in self._pending if _norm_ordno(k) == org]:
                    del self._pending[k]
                self._suppress[org] = self._clock()
            prev: dict = {}
            for k in [k for k in self._pending if _norm_ordno(k) == nid]:
                prev = self._pending.pop(k)
            rem = int(rec.get("ordrem", 0) or 0)
            self._pending[str(rec.get("ordno"))] = {
                **prev,   # 막 낸 주문이면 qty·seen·취소요청 표시 유지
                "side": "buy" if "매수" in str(rec.get("medosu", "")) else "sell",
                "remaining": rem,
                "qty": prev.get("qty", rem),
                "price": float(rec.get("price", 0) or 0),
                "expcode": str(rec.get("expcode", "")),
                "ts": self._clock(),
            }

    def apply_cancel(self, ordno: str) -> None:
        """H01 정정취소 → 그 주문을 캐시·pending에서 즉시 제거 + 억제(t0434 지연 대비)."""
        nid = _norm_ordno(ordno)
        with self._lock:
            self._open_raw = [r for r in self._open_raw if _norm_ordno(r.get("ordno")) != nid]
            for k in [k for k in self._pending if _norm_ordno(k) == nid]:
                del self._pending[k]
            self._suppress[nid] = self._clock()

    def apply_fill(self, ordno: str, qty: int) -> None:
        """C01 체결을 미체결 캐시·pending에 **즉시** 반영(실시간) — t0434 조회 없이.

        해당 주문 잔량을 qty만큼 줄이고, 0이 되면 캐시·pending에서 제거 + 억제(t0434 지연 대비).
        → 체결 직후 다음 주문이 바로 나간다(미체결 목록이 즉시 정확).
        """
        nid = _norm_ordno(ordno)
        q = int(qty)
        with self._lock:
            gone = False
            new_raw = []
            for r in self._open_raw:
                if _norm_ordno(r.get("ordno")) == nid:
                    rem = max(0, int(r.get("ordrem", 0) or 0) - q)
                    if rem <= 0:
                        gone = True
                        continue   # 전량체결 → 캐시에서 제거
                    r = {**r, "ordrem": rem}
                new_raw.append(r)
            self._open_raw = new_raw
            for k in list(self._pending):
                if _norm_ordno(k) == nid:
                    rem = max(0, int(self._pending[k]["remaining"]) - q)
                    if rem <= 0:
                        del self._pending[k]
                        gone = True
                    else:
                        self._pending[k]["remaining"] = rem
                    break
            if gone:
                self._suppress[nid] = self._clock()   # t0434 늦어도 재등장 막기

    @staticmethod
    def _to_exec(r: dict) -> dict:
        # 정정 주문은 medosu가 '매수정정'/'매도정정' → '포함'으로 방향 판정(실측).
        return {
            "ordno": str(r.get("ordno")),
            "side": "buy" if "매수" in str(r.get("medosu", "")) else "sell",
            "remaining": int(r.get("ordrem", 0) or 0),
            "price": float(r.get("price", 0) or 0),
        }

    def open_orders_exec(self) -> list[dict]:
        """실행(reconcile)용 미체결 [{ordno, side, remaining, price}]. 잔량>0만.

        t0434 + pending(막 낸 주문 중 아직 t0434 미반영분) 합산 → 반영 지연에도 이중주문 방지.
        """
        self._ensure_open()
        with self._lock:
            raw = list(self._open_raw)
            pend = dict(self._pending)
        out = [self._to_exec(r) for r in raw if int(r.get("ordrem", 0) or 0) > 0]
        have = {_norm_ordno(o["ordno"]) for o in out}
        for oid, p in pend.items():
            # t0434에 없는 장부 주문(막 낸 주문·통보 대기 중 사라진 주문)도 미체결로 센다
            if _norm_ordno(oid) not in have and int(p.get("remaining", 0) or 0) > 0:
                out.append({"ordno": oid, "side": p["side"],
                            "remaining": p["remaining"], "price": p["price"]})
        return out

    def snapshot(self) -> tuple[int, list[dict]]:
        """집행용 (포지션, 미체결)을 **미체결 먼저 → 포지션 순서로** 읽는다.

        체결(C01) 처리는 C01 net(포지션)을 먼저 올리고 그다음 미체결에서 뺀다
        (realtime_feed._on_c01 → app._on_fill → apply_fill). 그래서 이 순서로 읽으면 체결 하나가
        '미체결에서도 빠지고 포지션에도 없는' 틈이 생기지 않는다(둘 다에 잡히는 건 미체결이 남아
        새 주문을 막으므로 안전). 반대 순서(포지션 → t0434 조회 ~1초 → 미체결)로 읽던 게
        운영로그 2026-09 중복주문(목표 ±2 진동)의 원인.
        """
        orders = self.open_orders_exec()
        return self.net_position(), orders

    def open_orders(self, *, ensure: bool = True) -> list[dict]:
        """표시용 미체결(t0434 행 + pending). 통보로 추가된 막 낸/접수 주문도 즉시 보이게.

        ensure=False(모니터 렌더): t0434 재조회 안 함 → UI 스레드 블로킹 방지. 통보가 이미
        캐시를 갱신했으므로 캐시만 읽는다. 주기 재동기화는 실행 스레드(open_orders_exec)가 함.
        """
        if ensure:
            self._ensure_open()
        with self._lock:
            rows = list(self._open_raw)
            pend = dict(self._pending)
        have = {_norm_ordno(r.get("ordno")) for r in rows}
        for oid, p in pend.items():
            if _norm_ordno(oid) in have or int(p.get("remaining", 0) or 0) <= 0:
                continue
            rows.append({
                "ordno": oid, "expcode": p.get("expcode", ""),
                "medosu": "매수" if p.get("side") == "buy" else "매도",
                "ordrem": p["remaining"], "qty": p["remaining"], "cheqty": 0,
                "price": p["price"], "status": "접수", "ordtime": "",
            })
        return rows

    def invalidate_open_orders(self) -> None:
        """다음 조회 때 t0434로 새로 가져오게 표시(주문/취소·수동조회용). 쓰로틀은 적용됨."""
        self._open_dirty = True

    def force_open_refetch(self) -> None:
        """다음 조회 때 **쓰로틀 무시하고 즉시** t0434 재조회(체결 직후 — 다음 주문 바로 나가게)."""
        self._open_dirty = True
        self._force_open = True

    # ── 체결(C01 누적, 실행 이후만) ───────────────────
    def fills(self) -> list[dict]:
        return self._feed.fills() if self._feed is not None else []

    # ── 표시 강제 갱신(모니터) ────────────────────────
    def refresh_orders(self) -> None:
        """미체결만 재조회(t0434). 체결 통보마다 호출 — 포지션 t0441은 안 읽는다.

        포지션 net/행은 C01 실시간만으로 추적(자동 t0441 재조회 없음) → accno 호출 절감.
        t0441 재동기화는 사용자 [Idle조회]로만.
        """
        if self._api is None:
            return
        self.invalidate_open_orders()
        self._ensure_open()

    def refresh(self) -> None:
        """포지션(t0441) + 미체결(t0434) 전체 강제 갱신. 수동 조회·초기 1회용."""
        if self._api is None:
            return
        self._anchor_positions()
        self.refresh_orders()

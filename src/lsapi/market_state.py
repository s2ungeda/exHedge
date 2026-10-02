"""장운영 상태 해석 (JIF 실시간 장운영정보).

JIF(실시간)는 tr_key="0"으로 **한 번만 구독**하면 모든 시장(jangubun)의 장상태 전이를
이벤트로 흘려준다. 응답 body = {jangubun(장구분), jstatus(장상태)}.

핵심 성질 — **엣지 트리거 상태머신**:
  장상태가 *바뀔 때만* 한 번씩 온다. 21(장시작)을 받으면 다음 전이(31 장후동시호가 / 41 장마감)
  전까지 '접속매매 중'이 유지된다. 따라서 마지막 이벤트로 현재 국면(Phase)을 갱신·유지한다.

maker 재호가 전략은 **접속매매(연속) 구간에서만 안전**하다:
  - 동시호가(장전 11 / 장후 31 / 선물 장중동시마감 63 / 호가접수 62)는 단일가 매칭이라
    호가 offset·취소 로직이 무의미하거나 위험 → 신규 주문 금지.
  - 장마감/장종료/서킷브레이크 → 주문 금지.

⚠️ jstatus 61/62/63은 **jangubun에 따라 의미가 다르다**:
  - 코스피/코스닥(1,2): 서킷브레이크 1/2/3단계 발동·해제
  - 선물/옵션(5): 61=당일장종료, 62=CB해제·호가접수개시, 63=CB·장중동시마감
  그래서 매핑은 반드시 jangubun으로 분기한다. (우리 봇은 5=주간선물 / 8=야간파생만 사용)
"""

from __future__ import annotations

from enum import Enum

# JIF 실시간 구독 파라미터 (공식 예제 기준).
JIF_TR_CD = "JIF"
JIF_TR_KEY = "0"  # 종목별 아님 — "0"으로 전체 장운영 1건 구독

# 우리 봇이 쓰는 장구분.
JANGUBUN_DAY_FUTURES = "5"    # 선물/옵션 (주간)
JANGUBUN_NIGHT_FUTURES = "8"  # KRX 야간파생


class Phase(str, Enum):
    """현재 장 국면. 거래 가능 여부는 is_tradeable()로 판정."""

    UNKNOWN = "unknown"        # 아직 JIF 이벤트 미수신 → 호출측이 현재시각으로 폴백
    AUCTION = "auction"        # 동시호가/호가접수 — 단일가 매칭. 신규 maker 금지
    CONTINUOUS = "continuous"  # 접속매매(연속) — 유일하게 거래 가능
    HALT = "halt"              # 서킷브레이크 등 일시정지
    CLOSED = "closed"          # 장마감/장종료


# 모든 jangubun 공통 전이 (장상태코드 → 새 국면).
_COMMON: dict[str, Phase] = {
    "11": Phase.AUCTION,     # 장전동시호가개시
    "21": Phase.CONTINUOUS,  # 장시작(접속매매)
    "31": Phase.AUCTION,     # 장후동시호가개시
    "41": Phase.CLOSED,      # 장마감
}

# 선물/옵션(jangubun=5) 전용 — 코스피의 서킷브레이크 코드와 의미가 겹치므로 분기 필수.
_FUTURES_5: dict[str, Phase] = {
    "61": Phase.CLOSED,   # 당일 장종료
    "62": Phase.AUCTION,  # 서킷브레이크 해제, 호가접수개시 (아직 연속 아님)
    "63": Phase.HALT,     # 서킷브레이크, 장중동시마감
}


def next_phase(current: Phase, jangubun: str, jstatus: str) -> Phase:
    """JIF 이벤트 하나를 받아 새 국면을 돌려준다(순수 함수).

    매핑에 없는 코드(장개시 N분전, 장마감 N분전 등 경고성)는 **현 국면을 그대로 유지**한다.
    """
    table = dict(_COMMON)
    if jangubun == JANGUBUN_DAY_FUTURES:
        table.update(_FUTURES_5)
    return table.get(jstatus, current)


def is_tradeable(phase: Phase) -> bool:
    """접속매매(연속) 구간에서만 True. 그 외(동시호가/정지/마감/미수신)는 False."""
    return phase is Phase.CONTINUOUS


# 화면 표시용 — 국면 → (한글 라벨, 색상). 색은 어두운 타이틀바 기준 밝은 톤.
PHASE_BADGE: dict[Phase, tuple[str, str]] = {
    Phase.CONTINUOUS: ("접속매매", "#34D399"),  # 거래 가능(초록)
    Phase.AUCTION: ("동시호가", "#FBBF24"),     # 단일가 — 주문 금지(주황)
    Phase.HALT: ("서킷브레이크", "#F87171"),    # 정지(빨강)
    Phase.CLOSED: ("장마감", "#9FB3C8"),        # 마감(회색)
    Phase.UNKNOWN: ("확인중", "#9FB3C8"),       # 미수신(회색)
}


def phase_badge(phase: Phase) -> tuple[str, str]:
    """국면 → (한글 라벨, 색상). 미정의 국면은 회색 '확인중'."""
    return PHASE_BADGE.get(phase, ("확인중", "#9FB3C8"))


# KRX 미국달러선물 주간 거래시간 (JIF 실측 확인: 08:45:00 jangubun=5 jstatus=21=접속매매):
#   장개시 동시호가 ~08:45 / 접속매매 08:45~15:35 / 장마감 동시호가 15:35~15:45.
# JIF 미수신 시 폴백 판단에만 쓰는 근사치 — JIF 이벤트가 오면 그 진실이 우선한다.
# (session_now의 주간 08:45 시작과 일치시킴 — 예전엔 09:00으로 잡혀 08:45~09:00을 오판했음)
DAY_OPEN_AUCTION_END = 845      # 08:45 접속매매 시작 (이 전은 장개시 동시호가)
DAY_CLOSE_AUCTION_START = 1535  # 15:35 장마감 동시호가 시작 (이 후는 단일가)

# KRX 야간파생(글로벌 거래) 시각 (거래소 공식 안내 open.krx.co.kr, FICC 미국달러선물 포함):
#   시가단일가 호가접수 17:50~18:00 / 접속거래 18:00~05:50 / 종가단일가 호가접수 05:50~06:00.
#   시가단일가(17:50~18:00)는 session_now가 'closed'로 이미 차단 → 여기선 종가단일가만 처리.
NIGHT_CLOSE_AUCTION_START = 550  # 05:50 종가단일가 호가접수 시작 (이 후 ~06:00은 단일가)


def current_time_phase(now_hhmm: int, session: str) -> Phase:
    """JIF 미수신 시 폴백용 — 현재 시각만으로 추정한 국면.

    session: session_now() 결과("day"/"night"/"closed").
    주간 동시호가(장개시 ~09:00 / 장마감 15:35~), 야간 종가단일가(05:50~06:00)는
    AUCTION으로 보아 신규 주문을 막는다. JIF 이벤트가 오면 그 진실이 우선한다.
    """
    if session == "closed":
        return Phase.CLOSED
    if session == "day":
        if now_hhmm < DAY_OPEN_AUCTION_END:        # 장개시 동시호가
            return Phase.AUCTION
        if now_hhmm >= DAY_CLOSE_AUCTION_START:     # 장마감 동시호가
            return Phase.AUCTION
        return Phase.CONTINUOUS
    # night: 접속거래 18:00~05:50, 종가단일가 05:50~06:00(=HHMM 550~559). 시가단일가는 'closed'.
    if NIGHT_CLOSE_AUCTION_START <= now_hhmm < 600:
        return Phase.AUCTION
    return Phase.CONTINUOUS


def parse_jif(msg: dict) -> tuple[str, str] | None:
    """JIF 실시간 메시지 → (jangubun, jstatus). 형식 불충족이면 None.

    공식 예제 응답: {"header": {...}, "body": {"jangubun": "5", "jstatus": "21"}}.
    """
    body = msg.get("body") or {}
    jangubun = body.get("jangubun")
    jstatus = body.get("jstatus")
    if not jangubun or not jstatus:
        return None
    return str(jangubun), str(jstatus)


# C01 체결통보의 매도/매수 구분(dosugb) 코드 — 2026-06-23 모의 실측 확인.
DOSUGB_BUY = "2"
DOSUGB_SELL = "1"


def parse_c01(msg: dict) -> tuple[str, int] | None:
    """C01 체결통보 → (expcode, 부호체결수량). 형식 불충족이면 None.

    실측 필드: expcode(표준코드, 예 'KR4A75670005'), dosugb(2=매수/1=매도), chevol(체결수량).
    반환 수량은 매수 +chevol / 매도 −chevol (포지션 증감 그대로).
    """
    body = msg.get("body") or {}
    expcode = body.get("expcode")
    dosugb = body.get("dosugb")
    chevol = body.get("chevol")
    if not expcode or not dosugb or chevol in (None, ""):
        return None
    try:
        vol = int(chevol)
    except (TypeError, ValueError):
        return None
    if vol <= 0:
        return None
    signed = vol if str(dosugb) == DOSUGB_BUY else -vol
    return str(expcode), signed


class MarketStateTracker:
    """JIF 이벤트를 누적해 시장(jangubun)별 현재 국면을 유지하는 상태머신.

    WS 핸들러가 on_jif(jangubun, jstatus)로 먹이고, executor/broker가 tradeable(jangubun)로 묻는다.
    WS 비의존 — 이벤트만 주입하면 단위테스트로 완전히 검증된다.
    """

    def __init__(self) -> None:
        self._phase: dict[str, Phase] = {}

    def on_jif(self, jangubun: str, jstatus: str) -> Phase:
        """JIF 이벤트 반영. 갱신된 해당 시장의 국면을 반환."""
        cur = self._phase.get(jangubun, Phase.UNKNOWN)
        new = next_phase(cur, jangubun, jstatus)
        self._phase[jangubun] = new
        return new

    def phase(self, jangubun: str) -> Phase:
        """해당 시장의 현재 국면. 이벤트 미수신이면 UNKNOWN."""
        return self._phase.get(jangubun, Phase.UNKNOWN)

    def tradeable(self, jangubun: str) -> bool:
        """해당 시장이 접속매매 중인가."""
        return is_tradeable(self.phase(jangubun))

    def seen(self, jangubun: str) -> bool:
        """해당 시장의 JIF 이벤트를 한 번이라도 받았는가(폴백 판단용)."""
        return jangubun in self._phase

    def reset(self) -> None:
        """저장된 국면을 전부 비운다 → seen()이 다시 False → 시각 기반 폴백으로 전환.

        JIF는 엣지 트리거(상태 변화 시에만 수신)라, WS가 끊기면 직전 국면이 stale로 남을 수 있다.
        끊김 감지 시 이걸 호출해 stale 국면을 버리고, 다음 JIF 이벤트가 올 때까지 시각 폴백에 맡긴다.
        """
        self._phase.clear()

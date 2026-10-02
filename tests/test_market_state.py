"""장운영 상태머신(market_state.py) 테스트.

JIF 장상태코드 표(공식 명세 캡처 기준)를 그대로 검증한다. WS 없이 이벤트만 주입.
"""

from lsapi.market_state import (
    JANGUBUN_DAY_FUTURES,
    JANGUBUN_NIGHT_FUTURES,
    MarketStateTracker,
    Phase,
    current_time_phase,
    is_tradeable,
    next_phase,
    parse_c01,
    parse_jif,
)

DAY = JANGUBUN_DAY_FUTURES      # "5" 선물/옵션
NIGHT = JANGUBUN_NIGHT_FUTURES  # "8" 야간파생


# ── next_phase: 공통 전이 ──────────────────────────────
def test_장시작이면_접속매매():
    assert next_phase(Phase.UNKNOWN, DAY, "21") == Phase.CONTINUOUS


def test_장전동시호가면_옥션():
    assert next_phase(Phase.UNKNOWN, DAY, "11") == Phase.AUCTION


def test_장후동시호가면_옥션():
    assert next_phase(Phase.CONTINUOUS, DAY, "31") == Phase.AUCTION


def test_장마감이면_종료():
    assert next_phase(Phase.CONTINUOUS, DAY, "41") == Phase.CLOSED


# ── next_phase: 선물(5) 전용 코드 ──────────────────────
def test_선물_당일장종료_61_종료():
    assert next_phase(Phase.CONTINUOUS, DAY, "61") == Phase.CLOSED


def test_선물_서킷브레이크_63_정지():
    assert next_phase(Phase.CONTINUOUS, DAY, "63") == Phase.HALT


def test_선물_CB해제_62_옥션():
    # 호가접수개시 — 아직 연속 아님
    assert next_phase(Phase.HALT, DAY, "62") == Phase.AUCTION


def test_경고성코드는_상태유지():
    # 장마감 5분전(44)/장개시 1분전(23) 등은 국면을 바꾸지 않음
    assert next_phase(Phase.CONTINUOUS, DAY, "44") == Phase.CONTINUOUS
    assert next_phase(Phase.AUCTION, DAY, "23") == Phase.AUCTION


def test_미정의코드_상태유지():
    assert next_phase(Phase.CONTINUOUS, DAY, "ZZ") == Phase.CONTINUOUS


# ── jangubun 분기: 61/62/63 의미가 시장마다 다름 ───────
def test_코스피_61은_종료아님():
    # 코스피(1)에서 61은 '서킷브레이크 1단계 발동' — 선물의 '당일장종료'와 다름.
    # 우리 매핑은 5 전용 코드만 적용하므로, 1에서는 미정의 → 상태 유지(오해 금지).
    assert next_phase(Phase.CONTINUOUS, "1", "61") == Phase.CONTINUOUS


def test_야간파생은_공통코드만_적용():
    # 8(야간)은 공통 전이는 받지만, 5 전용 61/62/63은 적용 안 함(명세 미정의).
    assert next_phase(Phase.UNKNOWN, NIGHT, "21") == Phase.CONTINUOUS
    assert next_phase(Phase.CONTINUOUS, NIGHT, "61") == Phase.CONTINUOUS  # 유지


# ── is_tradeable ───────────────────────────────────────
def test_접속매매만_거래가능():
    assert is_tradeable(Phase.CONTINUOUS) is True
    for p in (Phase.UNKNOWN, Phase.AUCTION, Phase.HALT, Phase.CLOSED):
        assert is_tradeable(p) is False


# ── MarketStateTracker: 시장별 상태 누적 ───────────────
def test_tracker_하루흐름():
    t = MarketStateTracker()
    assert t.tradeable(DAY) is False          # 아직 이벤트 전 → UNKNOWN
    assert t.seen(DAY) is False

    t.on_jif(DAY, "11")                        # 장전동시호가
    assert t.tradeable(DAY) is False
    assert t.seen(DAY) is True

    t.on_jif(DAY, "21")                        # 장시작
    assert t.phase(DAY) == Phase.CONTINUOUS
    assert t.tradeable(DAY) is True

    t.on_jif(DAY, "44")                        # 장마감 5분전(경고) — 유지
    assert t.tradeable(DAY) is True

    t.on_jif(DAY, "31")                        # 장후동시호가 — 중단
    assert t.tradeable(DAY) is False

    t.on_jif(DAY, "41")                        # 장마감
    assert t.phase(DAY) == Phase.CLOSED


def test_tracker_시장별_독립():
    # 한 구독으로 여러 jangubun이 섞여 들어와도 시장별로 따로 추적.
    t = MarketStateTracker()
    t.on_jif(DAY, "21")     # 주간선물 접속매매
    t.on_jif(NIGHT, "11")   # 야간은 아직 동시호가
    assert t.tradeable(DAY) is True
    assert t.tradeable(NIGHT) is False


# ── current_time_phase: JIF 미수신 폴백 (시각 기반) ───────
def test_폴백_장개시동시호가_거래불가():
    # 08:45 이전 = 장개시 동시호가 → AUCTION (그 전은 session_now가 closed로도 막음)
    assert current_time_phase(844, "day") == Phase.AUCTION
    assert is_tradeable(current_time_phase(844, "day")) is False


def test_폴백_접속매매_거래가능():
    # 08:45~15:35 = 접속매매 → CONTINUOUS (미국달러선물은 08:45 개장)
    assert current_time_phase(845, "day") == Phase.CONTINUOUS
    assert current_time_phase(900, "day") == Phase.CONTINUOUS
    assert current_time_phase(1200, "day") == Phase.CONTINUOUS
    assert current_time_phase(1534, "day") == Phase.CONTINUOUS
    assert is_tradeable(current_time_phase(845, "day")) is True


def test_폴백_장마감동시호가_거래불가():
    # 15:35~15:45 = 장마감 동시호가 → AUCTION
    assert current_time_phase(1535, "day") == Phase.AUCTION
    assert current_time_phase(1544, "day") == Phase.AUCTION
    assert is_tradeable(current_time_phase(1535, "day")) is False


def test_폴백_마감세션이면_종료():
    assert current_time_phase(1700, "closed") == Phase.CLOSED
    assert is_tradeable(current_time_phase(1700, "closed")) is False


def test_폴백_야간_접속거래_거래가능():
    # 18:00~05:50 = 접속거래 → CONTINUOUS
    assert current_time_phase(2000, "night") == Phase.CONTINUOUS
    assert current_time_phase(100, "night") == Phase.CONTINUOUS   # 01:00
    assert current_time_phase(549, "night") == Phase.CONTINUOUS   # 05:49
    assert is_tradeable(current_time_phase(2000, "night")) is True


def test_폴백_야간_종가단일가_거래불가():
    # 05:50~06:00 = 종가단일가 호가접수 → AUCTION (거래소 공식 시각)
    assert current_time_phase(550, "night") == Phase.AUCTION
    assert current_time_phase(559, "night") == Phase.AUCTION
    assert is_tradeable(current_time_phase(550, "night")) is False


def test_phase_badge_라벨_색():
    from lsapi.market_state import phase_badge
    assert phase_badge(Phase.CONTINUOUS)[0] == "접속매매"
    assert phase_badge(Phase.AUCTION)[0] == "동시호가"
    assert phase_badge(Phase.CLOSED)[0] == "장마감"
    assert phase_badge(Phase.UNKNOWN)[0] == "확인중"
    # 색상도 함께 반환(타이틀바 표시용)
    assert phase_badge(Phase.CONTINUOUS)[1].startswith("#")


def test_parse_jif_정상():
    msg = {"header": {"tr_cd": "JIF", "tr_key": "0"},
           "body": {"jangubun": "5", "jstatus": "21"}}
    assert parse_jif(msg) == ("5", "21")


def test_parse_jif_숫자도_문자열로():
    msg = {"body": {"jangubun": 5, "jstatus": 21}}
    assert parse_jif(msg) == ("5", "21")


def test_parse_jif_필드없으면_None():
    assert parse_jif({"body": {}}) is None
    assert parse_jif({}) is None
    assert parse_jif({"body": {"jangubun": "5"}}) is None  # jstatus 없음


# ── parse_c01: 체결통보 → 부호체결수량 ─────────────────
def _c01(expcode="KR4A75670005", dosugb="2", chevol="1"):
    return {"header": {"tr_cd": "C01"},
            "body": {"expcode": expcode, "dosugb": dosugb, "chevol": chevol}}


def test_parse_c01_매수는_양수():
    assert parse_c01(_c01(dosugb="2", chevol="3")) == ("KR4A75670005", 3)


def test_parse_c01_매도는_음수():
    assert parse_c01(_c01(dosugb="1", chevol="2")) == ("KR4A75670005", -2)


def test_parse_c01_표준코드_그대로():
    # expcode는 표준코드(KR4A75670005) — 단축코드 'A756'이 부분문자열로 들어있음
    expcode, _ = parse_c01(_c01())
    assert "A756" in expcode


def test_parse_c01_구독응답_None():
    assert parse_c01({"body": None}) is None
    assert parse_c01({"body": {"expcode": "KR4A75670005"}}) is None  # dosugb/chevol 없음


def test_parse_c01_수량0이하_None():
    assert parse_c01(_c01(chevol="0")) is None


def test_tracker_서킷브레이크_후_재개():
    t = MarketStateTracker()
    t.on_jif(DAY, "21")     # 접속매매
    t.on_jif(DAY, "63")     # 서킷브레이크
    assert t.phase(DAY) == Phase.HALT
    assert t.tradeable(DAY) is False
    t.on_jif(DAY, "62")     # CB해제·호가접수
    assert t.tradeable(DAY) is False   # 아직 옥션
    t.on_jif(DAY, "21")     # 장 재개
    assert t.tradeable(DAY) is True

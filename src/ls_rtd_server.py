# -*- coding: utf-8 -*-
"""
LS증권 OpenAPI 국내주식/주식선물 실시간 시세 RTD 서버 (엑셀용)
==============================================================

엑셀 =RTD() 로 LS증권 OpenAPI 의 국내주식·주식선물 실시간 시세를 받아오는
Windows COM RTD 서버. (DDE 아님, RTD 방식 — 가상자산 RTD 서버와 동일 구조)

데이터 소스:
    인증(REST)   : https://openapi.ls-sec.co.kr:8080/oauth2/token
    실시간(WS)   : wss://openapi.ls-sec.co.kr:9443/websocket
ProgID          : LS.RTD

종목 입력 규칙 (엑셀 3번째 인자):
    "005930"   -> 주식 (6자리 단축코드)
    "005930F"  -> 그 종목의 주식선물 최근월물 (코드 끝에 F)

엑셀 사용 예:
    =RTD("LS.RTD",,"005930","현재가")    ' 삼성전자 현재가
    =RTD("LS.RTD",,"005930","매수호가")
    =RTD("LS.RTD",,"005930F","현재가")   ' 삼성전자 주식선물 최근월물 현재가

인증키: 같은 폴더 ls_config.json 에 저장
    { "appkey": "...", "appsecretkey": "..." }

명령어:
    python ls_rtd_server.py --register      (관리자 권한 권장)
    python ls_rtd_server.py --unregister
    python ls_rtd_server.py --test 005930   (엑셀 없이 연결 테스트)
    python ls_rtd_server.py --test 005930F  (주식선물 최근월물 테스트)

문제 진단: 같은 폴더의 ls_rtd.log 에 동작/오류가 기록됩니다.
"""

import os
import sys
import json
import time
import ssl
import threading
import urllib.request
import urllib.parse

# ----- COM (pywin32) -----
try:
    import pythoncom
    import win32com.client
    import win32com.server.register
    from win32com import universal
    from win32com.client import gencache
    from win32com.server.exception import COMException
except ImportError:
    pythoncom = None  # --test 모드에서는 COM 불필요

# ----- WebSocket -----
try:
    import websocket  # pip install websocket-client
except ImportError:
    websocket = None


# ----- LS OpenAPI 엔드포인트 (실전투자) -----
REST_BASE = "https://openapi.ls-sec.co.kr:8080"
OAUTH_URL = REST_BASE + "/oauth2/token"
WS_URL = "wss://openapi.ls-sec.co.kr:9443/websocket"

_SSL = ssl.create_default_context()

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "ls_config.json")
LOG_PATH = os.path.join(HERE, "ls_rtd.log")

EXCEL_TLB_GUID = "{00020813-0000-0000-C000-000000000046}"

# 실시간 TR 코드
#  주식      : 통합(KRX+NXT) US3/UH1 → NXT 프리마켓(08:00)~애프터마켓(20:00) 포함
#  주식선물  : JC0/JH0 (주간 09:00~15:45, 야간 없음)
#  지수선물  : FC9/FH9 (KOSPI200 주간) + DC0/DH0 (야간 18:00~익일06:00, 동일 shcode)
#  예상체결  : 통합주식 UYS / 주식선물 YJC / 지수선물 YF9 / 야간 DYC
# 체결 TR(현재가·장상태 포함)
CHE_TRS = {"S3_", "K3_", "US3", "NS3", "JC0", "FC9", "DC0"}
# 호가 TR
HOGA_TRS = {"H1_", "HA_", "UH1", "NH1", "JH0", "FH9", "DH0"}
# 예상체결 TR
EXP_TRS = {"UYS", "YJC", "YF9", "DYC", "YS3", "YK3", "NYS"}
# 통합 호가(잔량이 unt_* 로 분리된 TR)
INTEGRATED_HOGA = {"UH1"}

# 엑셀 입력 필드명 -> 내부 키
FIELD_MAP = {
    "last": "last", "price": "last", "현재가": "last", "체결가": "last", "c": "last",
    "bid": "bid", "매수호가": "bid", "매수": "bid", "b": "bid",
    "ask": "ask", "매도호가": "ask", "매도": "ask", "a": "ask",
    "bidqty": "bidqty", "매수잔량": "bidqty", "매수호가잔량": "bidqty", "bq": "bidqty",
    "askqty": "askqty", "매도잔량": "askqty", "매도호가잔량": "askqty", "aq": "askqty",
    "change": "change", "전일대비": "change", "대비": "change",
    "drate": "drate", "등락율": "drate", "등락률": "drate", "rate": "drate",
    "volume": "volume", "거래량": "volume", "vol": "volume",
    "open": "open", "시가": "open",
    "high": "high", "고가": "high",
    "low": "low", "저가": "low",
    # 예상체결가 / 장운영정보
    "exp": "exp_price", "exp_price": "exp_price", "예상체결가": "exp_price",
    "예상체결": "exp_price", "예상가": "exp_price",
    "status": "market_status", "market_status": "market_status",
    "장운영": "market_status", "장운영정보": "market_status", "장상태": "market_status",
}

# 한 종목의 시세 슬롯
_EMPTY = {"last": None, "bid": None, "ask": None, "bidqty": None, "askqty": None,
          "change": None, "drate": None, "volume": None,
          "open": None, "high": None, "low": None,
          "exp_price": None, "market_status": None}


def _log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "  " + str(msg) + "\n")
    except Exception:
        pass


def _load_config():
    """ls_config.json 에서 appkey/appsecretkey 읽기."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg.get("appkey", "").strip(), cfg.get("appsecretkey", "").strip()
    except Exception as e:
        _log("ls_config.json 읽기 실패: %r" % (e,))
        return "", ""


def _to_num(v):
    """LS 응답 문자열(+/- 부호, 공백 포함)을 숫자로 변환."""
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s == "0" and v == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _session_text(key):
    """현재 시각 기준 장운영 상태를 한글 설명으로 반환.
    종목 종류(주식/주식선물/지수선물)별 운영시간이 달라 분기."""
    lt = time.localtime()
    if lt.tm_wday >= 5:
        return "휴장(주말)"
    mins = lt.tm_hour * 60 + lt.tm_min

    def R(a, b):
        return a <= mins < b

    is_idx = (key in ("K200", "USD"))   # 지수선물·미국달러선물: 주간+야간
    is_fut = key.endswith("F")
    if is_idx:   # 주간 + 야간 (KOSPI200/미국달러선물)
        if R(8 * 60 + 30, 9 * 60):
            return "장전동시호가"
        if R(9 * 60, 15 * 60 + 45):
            return "장중(주간)"
        if R(17 * 60 + 50, 18 * 60):
            return "야간개시전"
        if R(18 * 60, 24 * 60) or R(0, 6 * 60):
            return "야간장"
        return "장마감"
    if is_fut:   # 주식선물 (주간만)
        if R(8 * 60 + 30, 9 * 60):
            return "장전동시호가"
        if R(15 * 60 + 35, 15 * 60 + 45):
            return "마감동시호가"
        if R(9 * 60, 15 * 60 + 45):
            return "장중"
        return "장마감"
    # 주식 (통합 KRX+NXT, 08:00~20:00)
    if R(8 * 60, 8 * 60 + 30):
        return "NXT프리마켓"
    if R(8 * 60 + 30, 9 * 60):
        return "장전동시호가"
    if R(9 * 60, 15 * 60 + 20):
        return "장중"
    if R(15 * 60 + 20, 15 * 60 + 30):
        return "마감동시호가"
    if R(15 * 60 + 30, 15 * 60 + 40):
        return "장종료"
    if R(15 * 60 + 40, 16 * 60):
        return "시간외종가"
    if R(16 * 60, 18 * 60):
        return "시간외단일가"
    if R(18 * 60, 20 * 60):
        return "NXT애프터마켓"
    return "장마감"


def _nth_weekday(year, month, weekday, n):
    """해당 월의 n번째 weekday(0=월..6=일) date."""
    import datetime
    cnt = 0
    for day in range(1, 29):
        if datetime.date(year, month, day).weekday() == weekday:
            cnt += 1
            if cnt == n:
                return datetime.date(year, month, day)
    return datetime.date(year, month, 15)


def _expiry_date(yyyymm, product):
    """최종거래일. product: 'EQ'=주식/지수선물(둘째 목요일), 'USD'=미국달러선물(셋째 월요일)."""
    y, m = yyyymm // 100, yyyymm % 100
    if product == "USD":
        return _nth_weekday(y, m, 0, 3)   # 셋째 월요일
    return _nth_weekday(y, m, 3, 2)       # 둘째 목요일


def _parse_ym(hname):
    """선물 hname 에서 만기 YYYYMM 추출.
    t8401(주식선물)='... F 202506'(6자리), t9943(지수선물)='F 2606'(YYMM 4자리)."""
    import re
    m6 = re.findall(r"\d{6}", hname or "")
    if m6:
        return int(m6[-1])
    m4 = re.findall(r"\d{4}", hname or "")
    if m4:
        v = m4[-1]
        return (2000 + int(v[:2])) * 100 + int(v[2:])
    return None


def _front_is_rolled(yyyymm, product="EQ"):
    """yyyymm 월물이 롤오버 시점(최종거래일 15:45)을 지났는지.
    만기일 종가 후(야간 포함)부터는 True → 차근월물로 넘어감."""
    import datetime
    try:
        exp = _expiry_date(yyyymm, product)
    except Exception:
        return False
    lt = time.localtime()
    today = datetime.date(lt.tm_year, lt.tm_mon, lt.tm_mday)
    if today > exp:
        return True
    if today == exp and (lt.tm_hour * 60 + lt.tm_min) >= (15 * 60 + 45):
        return True
    return False


def _setup_excel_interfaces():
    """엑셀 IRtdServer 인터페이스를 win32com 에 등록(out 파라미터 마샬링용)."""
    import winreg
    versions = []
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,
                            r"TypeLib\%s" % EXCEL_TLB_GUID) as key:
            i = 0
            while True:
                try:
                    versions.append(winreg.EnumKey(key, i))
                    i += 1
                except OSError:
                    break
    except OSError:
        pass

    def parse(v):
        try:
            a, b = v.split(".")
            return (int(a, 16), int(b, 16))
        except Exception:
            return (0, 0)

    if not versions:
        raise RuntimeError("Excel 타입 라이브러리를 찾을 수 없습니다. Excel 설치 확인.")
    versions.sort(key=parse)
    major, minor = parse(versions[-1])
    gencache.EnsureModule(EXCEL_TLB_GUID, 0, major, minor)
    universal.RegisterInterfaces(
        EXCEL_TLB_GUID, 0, major, minor, ["IRtdServer", "IRTDUpdateEvent"])
    _log("Excel TLB 등록 완료 (v%d.%d)" % (major, minor))


class LSFeed:
    """LS OpenAPI WebSocket 단일 연결로 국내주식/주식선물 시세 보관 (스레드 안전)."""

    def __init__(self, on_data_changed=None):
        self.on_data_changed = on_data_changed or (lambda: None)
        self.lock = threading.Lock()
        self.data = {}              # 내부키(예 "005930","005930F") -> slot
        self.sub_counts = {}        # 내부키 -> 참조 카운트
        self.subs = {}              # 내부키 -> [(tr_cd, tr_key), ...] 실제 구독목록
        self.pending = set()        # 아직 구독 해석/요청 안 된 내부키
        self.key_by_trkey = {}      # WS tr_key(단축코드/선물코드) -> 내부키
        self.appkey = ""            # 엑셀에서 주입
        self.appsecret = ""
        self.auth_state = "인증대기"
        self.token = None
        self.token_exp = 0          # 토큰 만료(epoch)
        self.market_map = None      # shcode -> "KOSPI"/"KOSDAQ"
        self.fut_map = None         # "A005930" -> 주식선물 최근월물 shcode
        self.idx_fut = None         # "K200" -> KOSPI200 지수선물 최근월물 shcode
        self.usd_fut = None         # "USD" -> 미국달러선물 최근월물 shcode
        self.rest_poll = {}         # slot_key -> shcode (주간 REST 폴링 대상: 통화/상품선물)
        self.ws = None
        self.connected = False
        self._stop = False
        self._dbg_seen = set()      # 야간/예상 TR 필드 1회 로깅용

    # ---- 외부 제어 ----
    def start(self):
        # 인증키는 엑셀에서 주입되지만, config 파일이 있으면 즉시 토큰 발급 시도.
        threading.Thread(target=self._ensure_token, daemon=True).start()
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._token_loop, daemon=True).start()
        threading.Thread(target=self._manager, daemon=True).start()
        threading.Thread(target=self._rollover_loop, daemon=True).start()
        threading.Thread(target=self._rest_poll_loop, daemon=True).start()

    def set_credentials(self, appkey, appsecret):
        """엑셀에서 입력한 인증키 주입. 변경 시 토큰 재발급."""
        appkey = (appkey or "").strip()
        appsecret = (appsecret or "").strip()
        if not appkey or not appsecret:
            return
        with self.lock:
            changed = (appkey != self.appkey or appsecret != self.appsecret)
            self.appkey = appkey
            self.appsecret = appsecret
        if changed:
            self.token = None       # 강제 재발급
            self.auth_state = "인증중..."
            threading.Thread(target=self._ensure_token, daemon=True).start()

    def auth_status(self):
        return self.auth_state

    def stop(self):
        self._stop = True
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass

    # ---- 인증 토큰 ----
    def _ensure_token(self):
        if self.token and time.time() < self.token_exp - 60:
            return self.token
        with self.lock:
            appkey, appsecret = self.appkey, self.appsecret
        if not appkey or not appsecret:
            # 엑셀 주입 전이면 config 파일 폴백
            appkey, appsecret = _load_config()
        if not appkey or not appsecret:
            self.auth_state = "인증대기(키입력)"
            return None
        try:
            body = urllib.parse.urlencode({
                "grant_type": "client_credentials",
                "appkey": appkey,
                "appsecretkey": appsecret,
                "scope": "oob",
            }).encode()
            req = urllib.request.Request(
                OAUTH_URL, data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=10, context=_SSL) as r:
                obj = json.loads(r.read().decode("utf-8"))
            self.token = obj.get("access_token")
            exp = obj.get("expires_in", 43200)
            self.token_exp = time.time() + int(exp)
            self.auth_state = "인증OK" if self.token else "인증실패"
            _log("토큰 발급 OK (expires_in=%s)" % exp)
            return self.token
        except Exception as e:
            self.auth_state = "인증실패"
            _log("토큰 발급 실패: %r" % (e,))
            return None

    def _token_loop(self):
        while not self._stop:
            time.sleep(60)
            self._ensure_token()

    # ---- REST TR 조회 ----
    def _post_tr(self, path, tr_cd, in_block_name, in_block):
        token = self._ensure_token()
        if not token:
            return None
        url = REST_BASE + path
        body = json.dumps({in_block_name: in_block}).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json; charset=utf-8",
            "authorization": "Bearer " + token,
            "tr_cd": tr_cd,
            "tr_cont": "N",
        })
        with urllib.request.urlopen(req, timeout=10, context=_SSL) as r:
            return json.loads(r.read().decode("utf-8"))

    def _ensure_market_map(self):
        """t8436 주식종목조회로 shcode -> 시장(KOSPI/KOSDAQ) 매핑 (1회)."""
        if self.market_map is not None:
            return
        m = {}
        try:
            obj = self._post_tr("/stock/etc", "t8436",
                                "t8436InBlock", {"gubun": "0"})
            for row in (obj or {}).get("t8436OutBlock", []):
                code = (row.get("shcode") or "").strip()
                gubun = (row.get("gubun") or "").strip()  # 1=코스피, 2=코스닥
                if code:
                    m[code] = "KOSDAQ" if gubun == "2" else "KOSPI"
            _log("주식 마스터 %d종목 로드" % len(m))
        except Exception as e:
            _log("t8436 조회 오류: %r" % (e,))
        self.market_map = m

    def _ensure_fut_map(self, force=False):
        """선물 마스터 로드 → 최근월물 매핑. 만기일(둘째목 15:45) 지나면 차근월물 선택.
        force=True 면 재조회(주기적 롤오버 갱신용).
        - fut_map : 주식선물  기초자산("A005930") -> 최근월물 shcode (t8401)
        - idx_fut : 지수선물  "K200" -> KOSPI200 최근월물 shcode (t9943)
        """
        if (self.fut_map is not None and self.idx_fut is not None
                and self.usd_fut is not None and not force):
            return

        # 1) 주식선물 (t8401) — basecode("A005930")별 만기인지 최근월물
        try:
            obj = self._post_tr("/futureoption/market-data", "t8401",
                                "t8401InBlock", {"dummy": ""})
            rows = (obj or {}).get("t8401OutBlock", [])
            cand = {}   # base -> [(ym, shcode), ...]
            for row in rows:
                base = (row.get("basecode") or "").strip()
                shcode = (row.get("shcode") or "").strip()
                ym = _parse_ym(row.get("hname") or "")
                if base and shcode and ym:
                    cand.setdefault(base, []).append((ym, shcode))
            out = {}
            for base, lst in cand.items():
                for ym, shcode in sorted(lst):
                    if not _front_is_rolled(ym):
                        out[base] = shcode
                        break
            self.fut_map = out
            _log("주식선물 마스터 %d기초자산 (만기인지 최근월물)" % len(out))
        except Exception as e:
            _log("t8401 조회 오류: %r" % (e,))
            if self.fut_map is None:
                self.fut_map = {}

        # 2) 지수선물 (t9943) — KOSPI200 만기인지 최근월물 (shcode 접두 "A01")
        try:
            obj = self._post_tr("/futureoption/market-data", "t9943",
                                "t9943InBlock", {"gubun": "0"})
            rows = (obj or {}).get("t9943OutBlock", [])
            cand = []
            for row in rows:
                sh = (row.get("shcode") or "").strip()
                if not sh.startswith("A01"):   # KOSPI200 지수선물
                    continue
                ym = _parse_ym(row.get("hname") or "")
                if ym:
                    cand.append((ym, sh))
            chosen = None
            for ym, sh in sorted(cand):
                if not _front_is_rolled(ym):
                    chosen = sh
                    break
            self.idx_fut = {"K200": chosen} if chosen else {}
            _log("지수선물 마스터(t9943): KOSPI200 최근월물 = %s" % chosen)
        except Exception as e:
            _log("t9943 조회 오류: %r" % (e,))
            if self.idx_fut is None:
                self.idx_fut = {}

        # 3) 미국달러선물 (t8426 상품선물마스터) — 만기=셋째 월요일, 월물만(SP 제외)
        try:
            obj = self._post_tr("/futureoption/market-data", "t8426",
                                "t8426InBlock", {"dummy": ""})
            rows = (obj or {}).get("t8426OutBlock", [])
            cand = []
            for row in rows:
                hn = row.get("hname") or ""
                sh = (row.get("shcode") or "").strip()
                if "미국달러" not in hn or " F " not in hn:   # 스프레드(SP) 제외
                    continue
                ym = _parse_ym(hn)
                if ym:
                    cand.append((ym, sh))
            chosen = None
            for ym, sh in sorted(cand):
                if not _front_is_rolled(ym, product="USD"):
                    chosen = sh
                    break
            self.usd_fut = {"USD": chosen} if chosen else {}
            _log("상품선물 마스터(t8426): 미국달러 최근월물 = %s" % chosen)
        except Exception as e:
            _log("t8426 조회 오류: %r" % (e,))
            if self.usd_fut is None:
                self.usd_fut = {}

    # ---- 구독 해석 ----
    def _resolve(self, key):
        """내부키 -> [(체결TR, 호가TR, ws_tr_key)] 목록 구성.
        반환: (subs, slot_key)  subs = [(tr_cd, tr_key), ...]
        """
        key = key.strip().upper()
        # 미국달러선물 (USD / 달러 / 원달러) — 주간은 REST폴링(t2111/t2112), 야간은 DC0/DH0
        if key in ("USD", "달러", "원달러", "USDF", "미국달러"):
            self._ensure_fut_map()
            shcode = (self.usd_fut or {}).get("USD")
            if not shcode:
                _log("미국달러선물 코드 해석 실패")
                return [], "USD"
            self.rest_poll["USD"] = shcode        # 주간 REST 폴링 대상
            self.key_by_trkey[shcode] = "USD"     # 야간 DC0/DH0 매핑
            return [("DC0", shcode), ("DH0", shcode), ("DYC", shcode)], "USD"
        # 지수선물: KOSPI200 (K200 / KOSPI200 / 코스피200)
        if key in ("K200", "KOSPI200", "코스피200", "KS200"):
            self._ensure_fut_map()
            shcode = (self.idx_fut or {}).get("K200")
            if not shcode:
                _log("KOSPI200 선물코드 해석 실패")
                return [], "K200"
            # 주간 FC9/FH9 + 야간 DC0/DH0 + 예상체결 YF9/DYC (모두 동일 shcode)
            return [("FC9", shcode), ("FH9", shcode),
                    ("DC0", shcode), ("DH0", shcode),
                    ("YF9", shcode), ("DYC", shcode)], "K200"
        # 주식선물 최근월물 (코드 끝에 F)
        if key.endswith("F") and key[:-1].isdigit():
            self._ensure_fut_map()
            base = "A" + key[:-1]
            shcode = (self.fut_map or {}).get(base)
            if not shcode:
                _log("선물코드 해석 실패: %s (기초 %s)" % (key, base))
                return [], key
            # 체결 JC0 + 호가 JH0 + 예상체결 YJC
            return [("JC0", shcode), ("JH0", shcode), ("YJC", shcode)], key
        # 주식: 통합(KRX+NXT) — tr_key = "U" + 6자리 + 공백3 (10자)
        if key.isdigit():
            trkey = "U" + key + "   "
            # 체결 US3 + 호가 UH1 + 통합예상체결 UYS
            return [("US3", trkey), ("UH1", trkey), ("UYS", trkey)], key
        _log("알 수 없는 종목키: %s" % key)
        return [], key

    def add_symbol(self, key):
        key = key.strip().upper()
        with self.lock:
            self.sub_counts[key] = self.sub_counts.get(key, 0) + 1
            is_new = self.sub_counts[key] == 1
            self.data.setdefault(key, dict(_EMPTY))
            if is_new:
                self.pending.add(key)   # 토큰/연결 준비되면 매니저가 구독

    def remove_symbol(self, key):
        key = key.strip().upper()
        gone = False
        subs = []
        with self.lock:
            if key in self.sub_counts:
                self.sub_counts[key] -= 1
                if self.sub_counts[key] <= 0:
                    del self.sub_counts[key]
                    subs = self.subs.pop(key, [])
                    self.data.pop(key, None)
                    self.pending.discard(key)
                    self.rest_poll.pop(key, None)
                    gone = True
        if gone and self.connected:
            for tr_cd, tr_key in subs:
                self._unsubscribe(tr_cd, tr_key)

    def _manager(self):
        """토큰·연결이 준비되면 대기 중(pending)인 종목을 해석/구독한다."""
        while not self._stop:
            time.sleep(0.5)
            if not (self.connected and self.token):
                continue
            with self.lock:
                pend = list(self.pending)
            for key in pend:
                subs, slot_key = self._resolve(key)
                if not subs:
                    continue   # 선물코드 미해석 등 — 다음 루프에 재시도
                with self.lock:
                    self.subs[key] = subs
                    self.key_by_trkey[slot_key] = slot_key  # body.shcode(6자리) 매핑
                    for tr_cd, tr_key in subs:
                        self.key_by_trkey[tr_key] = slot_key
                        self.key_by_trkey[tr_key.strip()] = slot_key  # 공백제거판
                    self.pending.discard(key)
                for tr_cd, tr_key in subs:
                    self._subscribe(tr_cd, tr_key)
                _log("구독 시작 %s -> %s" % (key, subs))

    def _rollover_loop(self):
        """주기적으로 선물 마스터를 재조회해 최근월물이 바뀌면 자동 재구독.
        (연결선물 효과 — 만기일 종가 후/야간엔 차근월물로 자동 전환)"""
        while not self._stop:
            time.sleep(600)   # 10분마다 확인
            if not (self.connected and self.token):
                continue
            try:
                self._ensure_fut_map(force=True)   # 마스터 재조회
            except Exception as e:
                _log("롤오버 마스터 재조회 오류: %r" % (e,))
                continue
            with self.lock:
                keys = list(self.subs.keys())
            for key in keys:
                # 선물/지수/통화만 대상 (주식 코드는 고정)
                if not (key.endswith("F") or key in ("K200", "USD")):
                    continue
                new_subs, slot_key = self._resolve(key)
                if not new_subs:
                    continue
                with self.lock:
                    old_subs = self.subs.get(key, [])
                old_k = set(tk for _, tk in old_subs)
                new_k = set(tk for _, tk in new_subs)
                if old_k == new_k:
                    continue   # 월물 변경 없음
                _log("롤오버 전환 %s: %s -> %s" % (key, old_k, new_k))
                for tr_cd, tr_key in old_subs:
                    self._unsubscribe(tr_cd, tr_key)
                with self.lock:
                    self.subs[key] = new_subs
                    self.key_by_trkey[slot_key] = slot_key
                    for tr_cd, tr_key in new_subs:
                        self.key_by_trkey[tr_key] = slot_key
                        self.key_by_trkey[tr_key.strip()] = slot_key
                for tr_cd, tr_key in new_subs:
                    self._subscribe(tr_cd, tr_key)

    def _rest_poll_loop(self):
        """통화/상품선물(주간 WS 미제공) 현재가/호가를 REST로 ~2초마다 폴링.
        대상: self.rest_poll (예: 미국달러선물). 야간은 DC0/DH0 WS 가 별도 갱신."""
        while not self._stop:
            time.sleep(2)
            if not self.token:
                continue
            targets = dict(self.rest_poll)
            for slot_key, sh in targets.items():
                try:
                    o = self._post_tr("/futureoption/market-data", "t2111",
                                      "t2111InBlock", {"focode": sh})
                    b = (o or {}).get("t2111OutBlock", {}) or {}
                    price = _to_num(b.get("price"))
                    chg = _to_num(b.get("change"))
                    jc = _to_num(b.get("jnilclose"))
                    sign = str(b.get("sign") or "").strip()
                    if chg is not None and sign in ("4", "5"):  # 하한/하락
                        chg = -abs(chg)
                    drate = round(chg / jc * 100, 2) if (chg is not None and jc) else None
                    o2 = self._post_tr("/futureoption/market-data", "t2112",
                                       "t2112InBlock", {"shcode": sh})
                    b2 = (o2 or {}).get("t2112OutBlock", {}) or {}
                    with self.lock:
                        d = self.data.setdefault(slot_key, dict(_EMPTY))
                        d["last"] = price
                        d["change"] = chg
                        d["drate"] = drate
                        d["volume"] = _to_num(b.get("volume"))
                        d["open"] = _to_num(b.get("open"))
                        d["high"] = _to_num(b.get("high"))
                        d["low"] = _to_num(b.get("low"))
                        d["exp_price"] = _to_num(b.get("yeprice"))
                        d["ask"] = _to_num(b2.get("offerho1"))
                        d["bid"] = _to_num(b2.get("bidho1"))
                        d["askqty"] = _to_num(b2.get("offerrem1"))
                        d["bidqty"] = _to_num(b2.get("bidrem1"))
                    self.on_data_changed()
                except Exception as e:
                    _log("REST폴링 오류 %s(%s): %r" % (slot_key, sh, e))

    def get(self, key, field_key):
        k = key.strip().upper()
        if field_key == "market_status":
            return _session_text(k)   # 시간 기반 장운영 설명
        with self.lock:
            d = self.data.get(k)
            return d.get(field_key) if d else None

    # ---- WebSocket ----
    def _send(self, tr_type, tr_cd, tr_key):
        try:
            self.ws.send(json.dumps({
                "header": {"token": self.token, "tr_type": tr_type},
                "body": {"tr_cd": tr_cd, "tr_key": tr_key},
            }))
        except Exception as e:
            _log("WS send 오류: %r" % (e,))

    def _subscribe(self, tr_cd, tr_key):
        self._send("3", tr_cd, tr_key)

    def _unsubscribe(self, tr_cd, tr_key):
        self._send("4", tr_cd, tr_key)

    def _run(self):
        if websocket is None:
            _log("websocket-client 미설치 — 데이터 수신 불가")
            return
        while not self._stop:
            try:
                self.ws = websocket.WebSocketApp(
                    WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_close=self._on_close,
                    on_error=self._on_error,
                )
                self.ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                _log("WS 오류: %s" % e)
            self.connected = False
            if self._stop:
                break
            time.sleep(3)

    def _on_open(self, ws):
        self.connected = True
        self._ensure_token()
        with self.lock:
            all_subs = [s for subs in self.subs.values() for s in subs]
        for tr_cd, tr_key in all_subs:
            self._subscribe(tr_cd, tr_key)
        _log("WS 연결 — %d건 구독" % len(all_subs))

    def _on_close(self, ws, *args):
        self.connected = False

    def _on_error(self, ws, err):
        self.connected = False
        _log("WS on_error: %r" % (err,))

    def _on_message(self, ws, message):
        try:
            obj = json.loads(message)
        except Exception:
            return
        header = obj.get("header") or {}
        body = obj.get("body")
        tr_cd = header.get("tr_cd")
        tr_key = header.get("tr_key")
        if not body or not tr_cd:
            # 구독 응답/시스템 메시지 (rsp_cd 등)
            rsp = obj.get("header", {}).get("rsp_msg")
            if rsp:
                _log("WS 응답: %s" % rsp)
            return
        slot_key = None
        if tr_key:
            slot_key = self.key_by_trkey.get(tr_key)
        if slot_key is None:
            # body 안의 코드로 보조 매핑 (shcode/futcode)
            code = body.get("shcode") or body.get("futcode")
            if code:
                slot_key = self.key_by_trkey.get(code.strip())
        if slot_key is None:
            return
        # 야간/예상체결 TR 은 필드명이 미확정 → 처음 1회 body 키를 로그로 남겨 검증
        if tr_cd in ("DC0", "DH0", "DYC", "UYS", "YF9", "YJC") and tr_cd not in self._dbg_seen:
            self._dbg_seen.add(tr_cd)
            _log("[필드확인] %s body keys=%s" % (tr_cd, list(body.keys())))
        try:
            with self.lock:
                d = self.data.setdefault(slot_key, dict(_EMPTY))
                if tr_cd in EXP_TRS:
                    # 예상체결가 (선물 yeprice / 주식 price)
                    d["exp_price"] = _to_num(body.get("yeprice") or body.get("price"))
                elif tr_cd in CHE_TRS:
                    d["last"] = _to_num(body.get("price"))
                    d["change"] = _to_num(body.get("change"))
                    d["drate"] = _to_num(body.get("drate"))
                    d["volume"] = _to_num(body.get("volume"))
                    d["open"] = _to_num(body.get("open"))
                    d["high"] = _to_num(body.get("high"))
                    d["low"] = _to_num(body.get("low"))
                    # 장운영정보: 주식=status, 선물=jgubun
                    st = body.get("status")
                    if st is None or str(st).strip() == "":
                        st = body.get("jgubun")
                    if st is not None and str(st).strip() != "":
                        d["market_status"] = str(st).strip()
                elif tr_cd in HOGA_TRS:
                    d["ask"] = _to_num(body.get("offerho1"))
                    d["bid"] = _to_num(body.get("bidho1"))
                    if tr_cd in INTEGRATED_HOGA:
                        # 통합 호가 잔량은 unt_*(통합) 필드로 분리되어 있음
                        d["askqty"] = _to_num(body.get("unt_offerrem1"))
                        d["bidqty"] = _to_num(body.get("unt_bidrem1"))
                    else:
                        d["askqty"] = _to_num(body.get("offerrem1"))
                        d["bidqty"] = _to_num(body.get("bidrem1"))
        except Exception:
            return
        self.on_data_changed()


class LSRTDServer:
    """엑셀 IRtdServer COM 구현 (LS증권 국내주식/주식선물)."""

    _com_interfaces_ = ["IRtdServer"]
    _public_methods_ = [
        "ConnectData", "DisconnectData", "Heartbeat",
        "RefreshData", "ServerStart", "ServerTerminate",
    ]
    _reg_clsctx_ = pythoncom.CLSCTX_INPROC_SERVER if pythoncom else 0
    _reg_clsid_ = "{7E3C1A9D-2B4F-4A6E-8C1D-5F9E0A2B3C4D}"
    _reg_progid_ = "LS.RTD"
    _reg_desc_ = "LS Securities (KR Stock/Futures) RTD Server"
    _reg_class_spec_ = "ls_rtd_server.LSRTDServer"

    def __init__(self):
        self.alive = False
        self.topics = {}
        self.feed = None
        self.dirty = False
        self._cb_stream = None

    def ServerStart(self, CallbackObject):
        try:
            self.alive = True
            self.topics = {}
            self.dirty = False
            if CallbackObject is None:
                raise COMException(desc="Excel 콜백이 없습니다")
            self._cb_stream = pythoncom.CoMarshalInterThreadInterfaceInStream(
                pythoncom.IID_IDispatch, CallbackObject)
            self.feed = LSFeed(self._on_changed)
            self.feed.start()
            threading.Thread(target=self._notify_loop, daemon=True).start()
            _log("ServerStart OK")
            return 1
        except Exception as e:
            _log("ServerStart 오류: %r" % (e,))
            raise

    def ServerTerminate(self):
        self.alive = False
        if self.feed:
            self.feed.stop()
        _log("ServerTerminate")
        return None

    def ConnectData(self, TopicID, Strings, GetNewValues):
        try:
            strs = [s for s in Strings]
            sym = str(strs[0]).strip().upper()
            # 인증 토픽: =RTD("LS.RTD",,"AUTH", appkey, appsecretkey)
            if sym in ("AUTH", "_AUTH", "인증", "KEY", "_KEY"):
                appkey = str(strs[1]).strip() if len(strs) > 1 else ""
                appsecret = str(strs[2]).strip() if len(strs) > 2 else ""
                self.feed.set_credentials(appkey, appsecret)
                self.topics[TopicID] = ("__AUTH__", "__authstatus__")
                return (self.feed.auth_status(), True)
            field_in = str(strs[1]).strip().lower() if len(strs) > 1 else "last"
            field_key = FIELD_MAP.get(field_in)
            if field_key is None:
                self.topics[TopicID] = (sym, None)
                return ("#필드?(현재가/매수호가/매도호가)", True)
            self.topics[TopicID] = (sym, field_key)
            self.feed.add_symbol(sym)
            _log("ConnectData #%s %s/%s" % (TopicID, sym, field_key))
            return (self._value(sym, field_key), True)
        except Exception as e:
            _log("ConnectData 오류: %r" % (e,))
            return ("#오류", True)

    def DisconnectData(self, TopicID):
        topic = self.topics.pop(TopicID, None)
        if topic and self.feed:
            self.feed.remove_symbol(topic[0])

    def RefreshData(self, TopicCount):
        ids = list(self.topics.keys())
        out = [[None] * len(ids), [None] * len(ids)]
        for i, tid in enumerate(ids):
            sym, field_key = self.topics[tid]
            out[0][i] = tid
            out[1][i] = self._value(sym, field_key) if field_key else "#필드?"
        return (tuple(out), len(ids))

    def Heartbeat(self):
        return 1 if self.alive else 0

    def _value(self, sym, field_key):
        if sym == "__AUTH__":
            return self.feed.auth_status() if self.feed else "인증대기"
        v = self.feed.get(sym, field_key) if self.feed else None
        return v if v is not None else "연결중..."

    def _on_changed(self):
        self.dirty = True

    def _notify_loop(self):
        pythoncom.CoInitialize()
        try:
            callback = win32com.client.Dispatch(
                pythoncom.CoGetInterfaceAndReleaseStream(
                    self._cb_stream, pythoncom.IID_IDispatch))
            while self.alive:
                if self.dirty:
                    self.dirty = False
                    try:
                        callback.UpdateNotify()
                    except Exception as e:
                        _log("UpdateNotify 오류: %r" % (e,))
                pythoncom.PumpWaitingMessages()
                time.sleep(0.25)
        finally:
            pythoncom.CoUninitialize()


# 엑셀이 모듈을 import 할 때 IRtdServer 인터페이스 미리 등록 (--test 제외)
if pythoncom is not None and not (len(sys.argv) >= 2 and sys.argv[1] == "--test"):
    try:
        _setup_excel_interfaces()
    except Exception as _e:
        _log("Excel TLB 등록 실패: %r" % (_e,))


def _run_test(key):
    if websocket is None:
        print("websocket-client 미설치:  pip install websocket-client")
        return
    feed = LSFeed()
    feed.start()
    feed.add_symbol(key.upper())
    print("연결 중... (Ctrl+C 로 종료)\n종목:", key.upper())
    k = key.upper()
    try:
        while True:
            time.sleep(1)
            print("현재가 {!s:>10} | 매수 {!s:>10}(잔량 {!s:>8}) | "
                  "매도 {!s:>10}(잔량 {!s:>8}) | 등락율 {!s:>7}".format(
                      feed.get(k, "last"),
                      feed.get(k, "bid"), feed.get(k, "bidqty"),
                      feed.get(k, "ask"), feed.get(k, "askqty"),
                      feed.get(k, "drate")))
    except KeyboardInterrupt:
        feed.stop()
        print("\n종료")


def _pth_file_path():
    import site
    cands = []
    try:
        cands += list(site.getsitepackages())
    except Exception:
        pass
    try:
        cands.append(site.getusersitepackages())
    except Exception:
        pass
    for d in cands:
        if d and d.lower().endswith("site-packages") and os.path.isdir(d):
            return os.path.join(d, "binance_rtd.pth")  # 같은 폴더 공용 .pth
    return None


def _install_pth():
    p = _pth_file_path()
    here = os.path.dirname(os.path.abspath(__file__))
    if not p:
        print("경고: site-packages 를 찾지 못해 sys.path 자동등록 실패")
        return
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(here + "\n")
        print("sys.path 등록:", here)
    except Exception as e:
        print("경고: .pth 작성 실패:", e)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--test":
        key = sys.argv[2] if len(sys.argv) >= 3 else "005930"
        _run_test(key)
    else:
        if pythoncom is None:
            print("pywin32 미설치:  pip install pywin32")
            sys.exit(1)
        if "--unregister" not in sys.argv:
            _install_pth()
        win32com.server.register.UseCommandLine(LSRTDServer)

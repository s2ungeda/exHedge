"""외부 시그널 수신 — 델파이 udpchat 프로토콜과 상호운용하는 Python 피어.

전송 구조(델파이 예제 MainForm.pas 분석):
- UDP 8888 브로드캐스트: 피어 발견 + 하트비트
    HELLO\t<ID>\t<Name>\t<TCP포트>   (5초마다)
    BYE\t<ID>                          (종료 시)
- TCP(동적 포트, HELLO로 광고): 실제 메시지
    연결 → "<ID>\t<Name>\t<Msg>\n"(UTF-8) → 끊음

봇은 '또 하나의 피어'로 참여한다: HELLO를 브로드캐스트해 송신자가 봇을 발견하게 하고,
TCP 서버로 메시지를 받는다. Msg 본문은 JSON 시그널(아래 Signal).

설계: docs/execution-engine.md 3장.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)

TAB = "\t"
HEARTBEAT_SEC = 5
PEER_TIMEOUT_SEC = 15
_SEEN_MAX = 2000  # 멱등 처리용 최근 id 보관 수


# ──────────────────────────────────────────────────────────
# 시그널 (순수 파싱, 테스트 대상)
# ──────────────────────────────────────────────────────────
@dataclass
class Signal:
    id: str                 # 멱등키
    fx: float               # 환율
    total_domestic: float   # 토탈잔고 국내총합(KRW)
    total_coin: float       # 토탈잔고 코인합(KRW)
    token: str = ""         # 공유 시크릿
    datetime: str = ""      # 날짜시간(원문)
    raw: dict = field(default_factory=dict)
    # 매수/매도는 시그널에 없음 — 방향은 (목표−포지션) 부호로 executor가 계산.


def parse_signal(msg: str) -> Signal:
    """Msg 본문(JSON 문자열) → Signal. 오류 시 ValueError.

    필수: id, fx>0, total_domestic, total_coin.  선택: token, datetime.
    (매수/매도 없음 — 방향은 계산으로 결정)
    """
    try:
        d = json.loads(msg)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 파싱 실패: {e}")
    if not isinstance(d, dict):
        raise ValueError("JSON 객체(dict)가 아닙니다.")

    sid = str(d.get("id", "")).strip()
    if not sid:
        raise ValueError("id 없음 (멱등키 필수)")

    def num(key: str) -> float:
        v = d.get(key)
        if v is None or v == "":
            raise ValueError(f"{key} 없음")
        try:
            return float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{key} 숫자 아님: {v!r}")

    fx = num("fx")
    if fx <= 0:
        raise ValueError("fx(환율)은 0보다 커야 함")
    td = num("total_domestic")
    tc = num("total_coin")

    return Signal(
        id=sid, fx=fx, total_domestic=td, total_coin=tc,
        token=str(d.get("token", "")), datetime=str(d.get("datetime", "")), raw=d,
    )


# ──────────────────────────────────────────────────────────
# 프로토콜 인코딩 (순수, 테스트 대상)
# ──────────────────────────────────────────────────────────
def encode_hello(instance_id: str, name: str, tcp_port: int) -> bytes:
    return TAB.join(["HELLO", instance_id, name, str(tcp_port)]).encode("utf-8")


def encode_bye(instance_id: str) -> bytes:
    return TAB.join(["BYE", instance_id]).encode("utf-8")


def parse_tcp_line(line: str) -> tuple[str, str, str]:
    """'<ID>\\t<Name>\\t<Msg>' → (id, name, msg). Msg는 탭 포함 가능."""
    parts = line.split(TAB, 2)
    if len(parts) < 3:
        raise ValueError(f"TCP 라인 형식 오류(필드 3개 필요): {line!r}")
    return parts[0], parts[1], parts[2]


def encode_tcp_message(instance_id: str, name: str, msg: str) -> bytes:
    """송신용 TCP 라인. 델파이 TCPServerExecute가 그대로 파싱(`ID\\tName\\tMsg`)."""
    return (TAB.join([instance_id, name, msg]) + "\n").encode("utf-8")


# ──────────────────────────────────────────────────────────
# 피어 / 링크
# ──────────────────────────────────────────────────────────
@dataclass
class Peer:
    id: str
    name: str
    ip: str
    tcp_port: int
    last_seen: float


class SignalLink:
    """델파이 udpchat 호환 피어. 시그널 수신용.

    on_signal(Signal): 유효 시그널 수신 시 호출(중복 id·토큰불일치는 걸러진 뒤).
    on_event(str): 진단 로그용(접속/퇴장/거부 등) — 선택.
    """

    def __init__(
        self,
        *,
        name: str = "TrdBot",
        udp_port: int = 8888,
        tcp_port: int = 0,            # 0 = 동적 할당
        broadcast: str = "255.255.255.255",
        token: str | None = None,     # 설정 시 시그널 token과 일치해야 수락
        on_signal: Callable[[Signal], None] | None = None,
        on_event: Callable[[str], None] | None = None,
    ):
        self.name = name
        self.udp_port = udp_port
        self.broadcast = broadcast
        self.token = token
        self.on_signal = on_signal
        self.on_event = on_event
        self.instance_id = uuid.uuid4().hex[:8]

        self._tcp_port_req = tcp_port
        self.tcp_port = tcp_port
        self._running = False
        self._lock = threading.Lock()
        self._peers: dict[str, Peer] = {}
        self._seen: "OrderedDict[str, float]" = OrderedDict()
        self._udp: socket.socket | None = None
        self._tcp: socket.socket | None = None
        self._threads: list[threading.Thread] = []

    # ---- 수명주기 ----
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._open_tcp()
        self._open_udp()
        self._spawn(self._tcp_loop, "tcp")
        self._spawn(self._udp_loop, "udp")
        self._spawn(self._heartbeat_loop, "heartbeat")
        self._emit(f"시작됨 (id={self.instance_id}, UDP={self.udp_port}, TCP={self.tcp_port}, name={self.name})")

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        try:
            self._send_udp(encode_bye(self.instance_id))
        except OSError:
            pass
        for s in (self._udp, self._tcp):
            try:
                if s:
                    s.close()
            except OSError:
                pass
        self._emit("중지됨")

    def __enter__(self) -> "SignalLink":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def peers(self) -> list[Peer]:
        with self._lock:
            return list(self._peers.values())

    # ---- 송신 (봇 → 피어) ----
    def send(self, msg: str, *, to: str | None = None) -> int:
        """발견된 피어에게 Msg 전송(델파이와 동일 방식: TCP 연결→라인→끊음).

        to=peer_id 면 그 피어에게만. 반환: 전송 대상 수. 실제 전송은 백그라운드 스레드.
        """
        with self._lock:
            targets = [p for p in self._peers.values() if to is None or p.id == to]
        if not targets:
            self._emit("전송 대상 피어 없음")
            return 0
        payload = encode_tcp_message(self.instance_id, self.name, msg)

        def _worker() -> None:
            for p in targets:
                try:
                    with socket.create_connection((p.ip, p.tcp_port), timeout=2.0) as c:
                        c.sendall(payload)
                except OSError as e:
                    logger.debug("전송 실패 %s:%d — %s", p.ip, p.tcp_port, e)
                    self._emit(f"전송 실패: {p.name} ({p.ip}:{p.tcp_port})")

        threading.Thread(target=_worker, name="siglink-send", daemon=True).start()
        return len(targets)

    # ---- 소켓 준비 ----
    def _open_tcp(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", self._tcp_port_req))
        s.listen(8)
        s.settimeout(1.0)  # accept 타임아웃 → stop 시 루프 탈출
        self.tcp_port = s.getsockname()[1]
        self._tcp = s

    def _open_udp(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind(("", self.udp_port))
        s.settimeout(1.0)
        self._udp = s

    def _spawn(self, target, name: str) -> None:
        t = threading.Thread(target=target, name=f"siglink-{name}", daemon=True)
        t.start()
        self._threads.append(t)

    # ---- UDP: 발견/하트비트 ----
    def _heartbeat_loop(self) -> None:
        while self._running:
            try:
                self._send_udp(encode_hello(self.instance_id, self.name, self.tcp_port))
                self._cleanup_peers()
            except OSError as e:
                logger.debug("heartbeat 오류: %s", e)
            for _ in range(HEARTBEAT_SEC * 2):  # 0.5초 단위로 잘게 — 빠른 stop
                if not self._running:
                    return
                time.sleep(0.5)

    def _send_udp(self, payload: bytes) -> None:
        if self._udp:
            self._udp.sendto(payload, (self.broadcast, self.udp_port))

    def _udp_loop(self) -> None:
        while self._running:
            try:
                data, addr = self._udp.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            self._handle_udp(data, addr[0])

    def _handle_udp(self, data: bytes, peer_ip: str) -> None:
        try:
            raw = data.decode("utf-8")
        except UnicodeDecodeError:
            return
        parts = raw.split(TAB)
        if len(parts) < 2:
            return
        typ, sender_id = parts[0], parts[1]
        if sender_id == self.instance_id:
            return  # 자기 자신 무시
        if typ == "HELLO" and len(parts) >= 4:
            tcp_port = int(parts[3]) if parts[3].isdigit() else 0
            if tcp_port == 0:
                return
            with self._lock:
                new = sender_id not in self._peers
                self._peers[sender_id] = Peer(sender_id, parts[2], peer_ip, tcp_port, time.time())
            if new:
                self._emit(f"접속: {parts[2]} ({peer_ip}:{tcp_port})")
        elif typ == "BYE":
            with self._lock:
                p = self._peers.pop(sender_id, None)
            if p:
                self._emit(f"퇴장: {p.name}")

    def _cleanup_peers(self) -> None:
        now = time.time()
        with self._lock:
            stale = [pid for pid, p in self._peers.items() if now - p.last_seen > PEER_TIMEOUT_SEC]
            for pid in stale:
                self._peers.pop(pid, None)

    # ---- TCP: 메시지 수신 ----
    def _tcp_loop(self) -> None:
        while self._running:
            try:
                conn, addr = self._tcp.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._spawn(lambda c=conn, a=addr: self._handle_conn(c, a[0]), "conn")

    def _handle_conn(self, conn: socket.socket, peer_ip: str) -> None:
        conn.settimeout(3.0)
        buf = b""
        try:
            while self._running:
                try:
                    chunk = conn.recv(4096)
                except (socket.timeout, OSError):
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self._process_line(line.decode("utf-8", "replace").rstrip("\r"), peer_ip)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _process_line(self, line: str, peer_ip: str) -> None:
        if not line:
            return
        try:
            _id, name, msg = parse_tcp_line(line)
        except ValueError as e:
            self._emit(f"라인 형식 오류 from {peer_ip}: {e}")
            return
        try:
            sig = parse_signal(msg)
        except ValueError as e:
            self._emit(f"시그널 파싱 거부 from {name}: {e}")
            return
        if self.token is not None and sig.token != self.token:
            self._emit(f"토큰 불일치 — 시그널 무시 (id={sig.id}, token={sig.token})")
            return
        if self._is_dup(sig.id):
            self._emit(f"중복 시그널 무시 (id={sig.id})")
            return
        logger.info("시그널 수신: from=%s fx=%s 국내=%s 코인=%s id=%s",
                    name, sig.fx, sig.total_domestic, sig.total_coin, sig.id)
        if self.on_signal:
            try:
                self.on_signal(sig)
            except Exception:
                logger.exception("on_signal 핸들러 예외 (id=%s)", sig.id)

    def _is_dup(self, sid: str) -> bool:
        with self._lock:
            if sid in self._seen:
                return True
            self._seen[sid] = time.time()
            while len(self._seen) > _SEEN_MAX:
                self._seen.popitem(last=False)
        return False

    def _emit(self, line: str) -> None:
        logger.info("[siglink] %s", line)
        if self.on_event:
            try:
                self.on_event(line)
            except Exception:
                logger.exception("on_event 핸들러 예외")

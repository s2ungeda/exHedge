"""LSWebSocket 끊김/복구 콜백 — 예기치 않은 끊김 뒤 재연결 때만 on_reconnect.

의도된 재연결(토큰 갱신 request_reconnect)은 끊김 콜백이 없으므로 복구 콜백도 없어야 한다.
"""

import asyncio
from types import SimpleNamespace

import websockets

import lsapi.ws_client as wsmod
from lsapi.ws_client import LSWebSocket


class FakeConn:
    """websockets.connect 자리 — 연결마다 정해진 행동: drop(서버 끊김)/forced/stop."""

    def __init__(self, sock: "LSWebSocket", action: str):
        self.sock, self.action = sock, action

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, _msg):
        pass

    async def recv(self):
        if self.action == "drop":
            raise websockets.ConnectionClosed(None, None)
        if self.action == "forced":
            self.sock.request_reconnect()
        else:
            self.sock.stop()
        await asyncio.Event().wait()   # stop·재연결 신호가 이길 때까지 대기


def _run(actions: list[str], monkeypatch) -> list[str]:
    events: list[str] = []
    settings = SimpleNamespace(ws_base_url="wss://fake")
    tokens = SimpleNamespace(get_token=lambda: "tok")
    sock = LSWebSocket(settings, tokens, name="체결",
                       on_disconnect=lambda: events.append("down"),
                       on_reconnect=lambda: events.append("up"))
    sock.subscribe("C01", "acct", tr_type="1")
    queue = list(actions)

    def fake_connect(*_a, **_k):
        events.append("connect")
        return FakeConn(sock, queue.pop(0))

    async def no_sleep(_s):
        pass

    monkeypatch.setattr(wsmod.websockets, "connect", fake_connect)
    monkeypatch.setattr(wsmod.asyncio, "sleep", no_sleep)
    asyncio.run(asyncio.wait_for(sock.run(), 5))
    return events


def test_서버끊김_후_재연결되면_복구콜백_1회(monkeypatch):
    ev = _run(["drop", "drop", "stop"], monkeypatch)
    # 끊김 2번(복구 전 재끊김 포함) → 복구는 실제로 붙었을 때마다
    assert ev == ["connect", "down", "connect", "up", "down", "connect", "up"]


def test_의도된_재연결은_끊김_복구_콜백_없음(monkeypatch):
    ev = _run(["forced", "stop"], monkeypatch)
    assert ev == ["connect", "connect"]

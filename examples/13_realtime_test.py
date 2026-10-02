"""실시간 WebSocket 시세 테스트 (LS 확인: 미국달러선물 = 코스피선물 TR 공용).

    python examples\\13_realtime_test.py [종목코드]   # 기본 A7567000(미국달러선물 근월)

구독:
  FH9 = 선물 호가 (offerho/bidho)
  FC9 = 선물 체결 (price/cvolume)
  JIF = 장운영정보 (장 상태/세션)

장중(USD선물 주간 08:45~15:45 / 야간 18:00~06:00)에 돌려야 데이터가 옵니다.
구독 ack는 와도 데이터가 0건이면 장 마감이거나 모의 WS 미push.
Ctrl+C 종료.
"""

import asyncio
import json
import sys

import websockets

from lsapi.auth import TokenManager
from lsapi.config import get_settings

MOCK_WS = "wss://openapi.ls-sec.co.kr:29443/websocket"
PROD_WS = "wss://openapi.ls-sec.co.kr:9443/websocket"


def _kind(body: dict) -> str:
    if any(k.startswith("offerho") or k.startswith("bidho") for k in body):
        return "호가"
    if "price" in body or "cvolume" in body:
        return "체결"
    return "기타"


async def main() -> None:
    code = sys.argv[1] if len(sys.argv) > 1 else "A7567000"
    s = get_settings()
    tok = TokenManager(s, market="futures").get_token()
    url = MOCK_WS if s.ls_env == "mock" else PROD_WS
    subs = [("FH9", code), ("FC9", code), ("JIF", "")]

    print(f"연결: {url} (env={s.ls_env}), 종목={code}")
    ws = await websockets.connect(url, ping_interval=20)
    async with ws:
        for tr, key in subs:
            await ws.send(json.dumps({"header": {"token": tok, "tr_type": "3"},
                                      "body": {"tr_cd": tr, "tr_key": key}}))
            await asyncio.sleep(0.2)
        print("구독 전송 완료. 데이터 대기 중... (Ctrl+C 종료)\n")
        counts: dict[str, int] = {}
        while True:
            raw = await ws.recv()
            m = json.loads(raw)
            tr = m.get("header", {}).get("tr_cd")
            body = m.get("body")
            rsp = m.get("header", {}).get("rsp_msg")
            if isinstance(body, dict):
                counts[tr] = counts.get(tr, 0) + 1
                if counts[tr] <= 3:  # 코드별 처음 몇 건만 상세
                    print(f"[{tr}] {_kind(body)} #{counts[tr]}: {dict(list(body.items())[:10])}")
                elif counts[tr] % 20 == 0:
                    print(f"[{tr}] 누적 {counts[tr]}건...")
            elif rsp:
                print(f"[{tr}] ack: {rsp}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n종료.")

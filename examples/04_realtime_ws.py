"""실시간 시세 구독 (WebSocket) 예시.

장 운영 시간 (09:00~15:30) 에 실행해야 체결 메시지가 들어온다.
"""

import asyncio

from lsapi.logger import setup_logging
from lsapi.ws_client import LSWebSocket


async def on_tick(msg: dict) -> None:
    body = msg.get("body", {})
    print(f"체결: {body.get('chetime')}  현재가={body.get('price')}  거래량={body.get('cvolume')}")


async def main() -> None:
    setup_logging()
    ws = LSWebSocket()
    ws.on("S3_", on_tick)  # KOSPI 체결
    ws.subscribe("S3_", "005930")  # 삼성전자
    await ws.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n종료.")

"""선물 실시간 호가(FH0)/체결(FC0) 수신 확인용 예제.

목적: USD선물(미국달러선물) 호가를 WebSocket으로 받을 수 있는지 검증한다.
배경: LS 카탈로그상 상품선물(통화) 전용 realtime 호가/체결 TR이 안 보인다.
      FH0/FC0는 'KOSPI200선물' 호가/체결인데, 이게 USD코드로도 오는지('선물 공용') 확인이 필요.

사용법(주간 접속매매 09:00~15:35에 실행):
    python examples\\16_futures_realtime.py                 # 기본 USD근월 A7567000, 60초
    python examples\\16_futures_realtime.py A7567000 120    # 코드/시간 지정
    python examples\\16_futures_realtime.py 101W6000 60     # KOSPI200선물 코드로 대조 테스트

해석:
    - USD코드로 FH0/FC0가 들어오면 → '선물 공용' 확인, 호가 WS 전환 가능.
    - USD코드로는 안 오고 KOSPI200 코드로만 오면 → USD선물은 realtime 호가 미제공(REST 유지).
    - 둘 다 안 오면 → (모의 미지원이거나 장중 아님) 환경/시간 재확인.
"""

import asyncio
import logging
import sys

from lsapi.auth import TokenManager
from lsapi.config import get_settings
from lsapi.logger import setup_logging
from lsapi.ws_client import LSWebSocket


async def on_quote(msg: dict) -> None:
    tr = msg.get("header", {}).get("tr_cd")
    body = msg.get("body")
    # 원본 그대로 출력 — body=None이면 구독 응답(데이터 X), dict면 실제 push(필드명 확인용).
    print(f"[{tr}] header={msg.get('header')}  body={body}")


async def main() -> None:
    code = sys.argv[1] if len(sys.argv) > 1 else "A7567000"  # 미국달러선물 근월(예)
    duration = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0

    setup_logging()
    logging.getLogger("lsapi.ws_client").setLevel(logging.DEBUG)  # 구독 응답/미매칭도 보이게

    ws = LSWebSocket(token_manager=TokenManager(get_settings(), market="futures"))
    ws.on("FH9", on_quote)
    ws.on("FC9", on_quote)
    ws.subscribe("FH9", code)
    ws.subscribe("FC9", code)
    print(f"선물 실시간 구독: code={code}, TR=FH9(호가)/FC9(체결), {duration:.0f}초 후 자동종료")
    print("→ 한 줄도 안 찍히면 push 미수신. 구독 응답(rsp_cd)만 보이면 연결O/데이터X.")

    task = asyncio.create_task(ws.run())
    await asyncio.sleep(duration)
    print(f"\n{duration:.0f}초 경과 — 종료.")
    ws.stop()
    await task


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n종료.")

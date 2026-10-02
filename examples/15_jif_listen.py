"""JIF 실시간 장운영정보 수신 확인용 예제.

목적: 봇에 연결한 JIF 구독이 LS 서버에서 실제로 데이터를 받는지 눈으로 확인한다.
(app.py가 쓰는 것과 동일한 tr_cd=JIF, tr_key="0", market=futures 경로)

사용법:
    python examples\\15_jif_listen.py            # 무한 대기 (ctrl-c 종료)
    python examples\\15_jif_listen.py 60         # 60초만 듣고 자동 종료
    python examples\\15_jif_listen.py 60 stock   # 토큰을 주식계좌로 (기본 futures)

⚠️ JIF는 **장상태가 바뀔 때만** 이벤트를 보낸다(엣지 트리거):
    - 장시작(09:00 / 야간 18:00), 동시호가 개시, 장마감 등 '전환 시점'에만 1건씩.
    - 접속매매 한가운데(예: 11:00)에 접속하면 다음 전환까지 body가 안 올 수 있다.
    그래서 이 예제는 **모든 수신 메시지(구독 응답 포함)를 raw로** 출력한다.
    구독 응답(rsp_cd 정상)이 보이면 '연결·구독은 성공', 이후 장상태 이벤트는 전환 때 들어온다.
"""

import asyncio
import logging
import sys

from lsapi.auth import TokenManager
from lsapi.config import get_settings
from lsapi.logger import setup_logging
from lsapi.market_state import JIF_TR_CD, JIF_TR_KEY, MarketStateTracker, next_phase, parse_jif
from lsapi.ws_client import LSWebSocket

_tracker = MarketStateTracker()


async def on_jif(msg: dict) -> None:
    print(f"[JIF raw] {msg}")
    parsed = parse_jif(msg)
    if parsed is None:
        print("  → (장상태 body 아님 — 구독 응답이거나 형식 외)")
        return
    jangubun, jstatus = parsed
    phase = _tracker.on_jif(jangubun, jstatus)
    print(f"  → jangubun={jangubun} jstatus={jstatus}  국면={phase.value}  "
          f"(거래가능={_tracker.tradeable(jangubun)})")


async def main() -> None:
    # 인자: [duration_sec] [market]
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else None
    market = sys.argv[2] if len(sys.argv) > 2 else "futures"

    setup_logging()
    logging.getLogger("lsapi.ws_client").setLevel(logging.DEBUG)  # 미매칭 메시지도 보이게

    ws = LSWebSocket(token_manager=TokenManager(get_settings(), market=market))
    ws.on(JIF_TR_CD, on_jif)
    ws.subscribe(JIF_TR_CD, JIF_TR_KEY)
    print(f"JIF 구독 시작 (market={market}, tr_cd={JIF_TR_CD}, tr_key={JIF_TR_KEY})"
          f"{f', {duration:.0f}초 후 자동종료' if duration else ', ctrl-c 종료'}")

    task = asyncio.create_task(ws.run())
    if duration is not None:
        await asyncio.sleep(duration)
        print(f"\n{duration:.0f}초 경과 — 종료합니다.")
        ws.stop()
    await task


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n종료.")

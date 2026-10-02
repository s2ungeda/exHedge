"""외부 시그널 수신 리스너 (델파이 udpchat 호환 피어).

    python examples\\11_signal_listen.py

봇이 'TrdBot' 피어로 UDP 8888에 HELLO를 브로드캐스트하고 TCP로 시그널을 받는다.

[테스트 방법] C:\\project\\VibeCode\\udpchat\\UDPChat.exe 실행 →
  1) 이름 입력 후 Start
  2) 피어 목록에 'TrdBot'이 떠야 함(봇 발견됨)
  3) 메시지 칸에 아래 JSON을 붙여넣고 전송 (매수/매도 없음 — 방향은 봇이 계산):
     {"id":"t1","fx":1380.5,"total_domestic":500000000,"total_coin":200000000}
  4) 이 콘솔에 시그널 수신이 출력되면 성공.

Ctrl+C 로 종료.
"""

import time

from lsapi.logger import setup_logging
from trdbot.signal_link import Signal, SignalLink


def main() -> None:
    setup_logging("INFO")
    link = SignalLink(name="TrdBot")

    def on_signal(sig: Signal) -> None:
        print(f"\n>>> 시그널 수신: fx={sig.fx} "
              f"국내={sig.total_domestic:,.0f} 코인={sig.total_coin:,.0f} id={sig.id}")
        # 받은 즉시 ack 회신 → UDPChat 로그에 'TrdBot: ...' 로 보인다(양방향 확인).
        link.send(f"ACK {sig.id} 수신완료")
        # 다음 단계: targets.target_contracts(...)로 목표계약수 계산 → executor 집행.

    link.on_signal = on_signal
    link.on_event = lambda line: print(f"[link] {line}")
    link.start()
    print("시그널 수신 대기 중... (UDPChat.exe에서 'TrdBot'으로 메시지 전송) Ctrl+C 종료")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n종료 중...")
        link.stop()


if __name__ == "__main__":
    main()

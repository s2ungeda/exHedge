"""봇 → udpchat 송신 테스트 (봇이 채팅 클라이언트처럼 메시지를 보냄).

    python examples\\12_signal_send.py

UDPChat.exe(Start 상태)와 같은 네트워크에 있으면, 봇이 발견한 피어에게 입력한 메시지를 보낸다.
UDPChat 로그에 'TrdBot: <메시지>' 로 표시되면 성공.

콘솔에 메시지를 입력하고 Enter → 전송. 빈 줄 또는 'quit' → 종료.
"""

import time

from lsapi.logger import setup_logging
from trdbot.signal_link import SignalLink


def main() -> None:
    setup_logging("WARNING")  # 콘솔 입력에 집중되게 로그는 최소화
    link = SignalLink(name="TrdBot", on_event=lambda line: print(f"[link] {line}"))
    link.start()

    print("피어 발견 대기 중...")
    for _ in range(20):  # 최대 ~10초 대기
        if link.peers():
            break
        time.sleep(0.5)

    peers = link.peers()
    if peers:
        print("발견된 피어:", ", ".join(f"{p.name}({p.ip}:{p.tcp_port})" for p in peers))
    else:
        print("아직 피어 없음 — UDPChat.exe가 Start 상태인지 확인. (입력하면 발견되는 대로 전송됩니다)")

    print("\n메시지를 입력하고 Enter (빈 줄/quit → 종료):")
    try:
        while True:
            msg = input("> ").strip()
            if msg == "" or msg.lower() == "quit":
                break
            n = link.send(msg)
            print(f"  → {n}개 피어로 전송")
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        link.stop()
        print("종료.")


if __name__ == "__main__":
    main()

"""실시간 주문/잔고 모니터 실행.

    python examples\\14_monitor.py

시작 시 1회 REST 조회 → 이후 '새로고침' 버튼으로 수동 조회 (주기 폴링 안 함).
실시간(WebSocket) push는 장중 검증 후 연결 예정.
미국달러선물만 보려면 MonitorScreen(code_prefix="A756").
"""

from lsapi.logger import setup_logging
from trdbot.monitor_screen import MonitorScreen


def main() -> None:
    setup_logging()
    MonitorScreen(code_prefix="A756").run()


if __name__ == "__main__":
    main()

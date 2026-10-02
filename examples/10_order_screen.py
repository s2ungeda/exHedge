"""자동주문화면 실행 (Tkinter 입력 폼).

    python examples\\10_order_screen.py

실행버튼은 토글:
- 정지 → 클릭 → 실행 중(외부 시그널 수신 → 주문). 입력칸 잠김.
- 실행 중 → 클릭 → 중지.

현재 단계: 입력 + 검증 + 토글 + 입력값 저장/로드.
잔고/목표계약수 표시칸 배선과 실제 집행(executor)·시그널 수신은 다음 단계.
"""

from lsapi.logger import setup_logging
from trdbot.order_screen import OrderConfig, OrderScreen


def on_execute(cfg: OrderConfig) -> None:
    # TODO: 여기서 시그널 서버 + executor 기동. 지금은 설정 확인만.
    print("=== 전략 실행 시작 — 수집된 설정 ===")
    print(cfg)


def on_stop() -> None:
    # TODO: 시그널 서버 + executor 정지.
    print("=== 전략 실행 중지 ===")


def main() -> None:
    setup_logging()
    OrderScreen(on_execute=on_execute, on_stop=on_stop).run()


if __name__ == "__main__":
    main()

"""TrdBot 앱/전략 레이어.

LS 브로커 클라이언트(`lsapi`)에 의존하는 상위 레이어 — UI·시그널 전송·전략 계산·집행.
의존 방향은 trdbot → lsapi 한쪽으로만 흐른다(lsapi는 trdbot을 모른다).
"""

from trdbot.broker_ls import LSDayFuturesBroker, LSNightFuturesBroker, SessionRoutingBroker
from trdbot.executor import BrokerPort, Decision, Executor, reconcile_plan
from trdbot.order_screen import OrderConfig, OrderScreen, build_config
from trdbot.pricing import PriceQuote, order_price, quote_from_orderbook
from trdbot.signal_link import Signal, SignalLink, parse_signal
from trdbot.targets import (
    dollar_exposure_contracts,
    hedge_ratio_contracts,
    target_contracts,
)

__all__ = [
    "BrokerPort",
    "Decision",
    "Executor",
    "LSDayFuturesBroker",
    "LSNightFuturesBroker",
    "OrderConfig",
    "SessionRoutingBroker",
    "OrderScreen",
    "PriceQuote",
    "Signal",
    "SignalLink",
    "build_config",
    "dollar_exposure_contracts",
    "hedge_ratio_contracts",
    "order_price",
    "parse_signal",
    "quote_from_orderbook",
    "reconcile_plan",
    "target_contracts",
]

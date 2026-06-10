"""ブローカー具象実装 (adapter) パッケージ。

BrokerInterface (interfaces/broker.py) の実装を提供する。
"""

from .kabu_station import KabuStationBroker, KabuStationError
from .simulator import BrokerSimulator

__all__ = ["BrokerSimulator", "KabuStationBroker", "KabuStationError"]

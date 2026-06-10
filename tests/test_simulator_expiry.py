"""simulator の未約定指値の当日失効 (ExpireDay=当日 相当) のテスト。

旧実装は約定しなかった指値が PENDING のまま永久に蓄積し、再起動を跨いで
復元され続けていた (失効・再評価の仕組みが無い)。
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trading.broker_interface import Order, OrderSide, OrderStatus, OrderType  # noqa: E402
from trading.simulator import BrokerSimulator  # noqa: E402


def _pending(order_time: datetime, order_id: str = "o1") -> Order:
    return Order(
        id=order_id,
        ticker="7203.T",
        side=OrderSide.BUY,
        quantity=100,
        entry_price=1000.0,
        order_type=OrderType.LIMIT,
        order_time=order_time,
        status=OrderStatus.PENDING,
    )


def test_stale_pending_order_expires():
    """前日 (JST) の指値は get_orders で CANCELLED に落ちる。"""
    sim = BrokerSimulator({})
    sim._orders = [_pending(datetime.now(UTC) - timedelta(days=2))]
    assert sim.get_orders() == []
    cancelled = [o for o in sim._filled_orders if o.status == OrderStatus.CANCELLED]
    assert len(cancelled) == 1


def test_today_pending_order_survives():
    """当日 (JST) の指値は失効しない。"""
    sim = BrokerSimulator({})
    sim._orders = [_pending(datetime.now(UTC))]
    orders = sim.get_orders()
    assert len(orders) == 1
    assert orders[0].status == OrderStatus.PENDING


def test_get_positions_also_expires():
    """毎サイクル呼ばれる get_positions でも失効処理が走る。"""
    sim = BrokerSimulator({})
    sim._orders = [_pending(datetime.now(UTC) - timedelta(days=3))]
    sim.get_positions()
    assert sim._orders == []

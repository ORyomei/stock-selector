"""利確の段階売り (scale-out) のテスト。

背景: 利確到達で全量クローズしていたため勝ちが伸びなかった (平均勝ち
¥10k vs 最大勝ち ¥140k)。利確水準到達時は半分だけ利確し、残りを
トレーリングで走らせる。単元で割れない小口は従来どおり全量クローズ。
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from domain.models import (  # noqa: E402
    Order,
    OrderStatus,
    OrderType,
    Position,
)
from trading.order_manager import OrderManager  # noqa: E402
from trading.risk_manager import RiskManager  # noqa: E402
from trading.trade_executor import TradeExecutor  # noqa: E402


def _pos(ticker: str, qty: int, entry: float, current: float, take_profit: float) -> Position:
    return Position(
        ticker=ticker,
        quantity=qty,
        entry_price=entry,
        current_price=current,
        entry_time=datetime.now(UTC),
        take_profit=take_profit,
    )


class _StubBroker:
    """place_order を記録し即 FILLED を返す最小スタブ。"""

    def __init__(self, positions: list[Position]):
        self._positions = positions
        self.orders: list[tuple[str, str, int]] = []

    def get_positions(self) -> list[Position]:
        return self._positions

    def place_order(self, ticker, side, quantity, order_type=OrderType.MARKET, entry_price=0.0, **kw):
        self.orders.append((ticker, side.value, quantity))
        return Order(
            id="stub",
            ticker=ticker,
            side=side,
            quantity=quantity,
            entry_price=entry_price,
            order_type=order_type,
            order_time=datetime.now(UTC),
            filled_quantity=quantity,
            fill_price=110.0,
            status=OrderStatus.FILLED,
        )


def test_scale_out_quantity_halves_to_unit():
    # 日本株 (単元100): 200株 → 100株、300株 → 100株 (半分を単元に切り捨て)
    assert TradeExecutor._scale_out_quantity(_pos("7203.T", 200, 100, 110, 105)) == 100
    assert TradeExecutor._scale_out_quantity(_pos("7203.T", 300, 100, 110, 105)) == 100


def test_scale_out_quantity_zero_when_indivisible():
    # 1単元しか持っていない → 分割不可 (全量クローズにフォールバック)
    assert TradeExecutor._scale_out_quantity(_pos("7203.T", 100, 100, 110, 105)) == 0


def test_scale_out_quantity_etf_unit1():
    # ETF (単元1、config/trading_units.json): 5口 → 2口
    assert TradeExecutor._scale_out_quantity(_pos("2559.T", 5, 100, 110, 105)) == 2
    assert TradeExecutor._scale_out_quantity(_pos("2559.T", 1, 100, 110, 105)) == 0


def test_check_and_close_scales_out_on_take_profit():
    """利確到達 (200株) → 100株だけ成行売り、reason は take_profit_scale_out。"""
    pos = _pos("7203.T", 200, 100.0, 110.0, 105.0)  # +10%、TP 105 到達
    broker = _StubBroker([pos])
    rm = RiskManager({"trailing_stop_pct": 3, "trailing_activation_pct": 5})
    ex = TradeExecutor(broker, OrderManager(rm), rm)  # type: ignore[arg-type]

    results = ex.check_and_close_positions()

    assert len(results) == 1
    r = results[0]
    assert r["reason"] == "take_profit_scale_out"
    assert r["quantity"] == 100
    assert r["success"] is True
    assert r["pnl"] == (110.0 - 100.0) * 100  # fill 110 で 100株
    assert r["hold_days"] == 0  # 当日 (計測メタデータ)
    assert broker.orders == [("7203.T", "SELL", 100)]  # 半分だけ売った

"""全量クローズの実現損益記録のテスト (issue #1)。

背景: execute_signal は約定後に get_positions() から該当銘柄の pnl を拾う実装
だったため、全量クローズではポジションが消えて pnl=null が記録されていた
(実例: 2026-08-19 mech:max_loss_5pct 6503.T / 2026-08-21 mech:take_profit 7267.T)。
発注前に entry_price を控えて実現損益を計算する修正を検証する。
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from domain.models import (  # noqa: E402
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)
from trading.order_manager import OrderManager  # noqa: E402
from trading.risk_manager import RiskManager  # noqa: E402
from trading.trade_executor import TradeExecutor  # noqa: E402


class _FullCloseBroker:
    """SELL 約定でポジションが消える (実ブローカーと同じ) 最小スタブ。"""

    def __init__(self, positions: list[Position], fill_price: float):
        self._positions = positions
        self._fill = fill_price

    def get_positions(self) -> list[Position]:
        return list(self._positions)

    def get_balance(self) -> dict:
        return {"cash_jpy": 10_000_000.0, "cash_usd": 0.0}

    def place_order(self, ticker, side, quantity, order_type=OrderType.MARKET,
                    entry_price=0.0, **kw):
        if side == OrderSide.SELL:
            # 全量クローズ → ポジション消滅 (バグの再現条件)
            self._positions = [p for p in self._positions if p.ticker != ticker]
        return Order(
            id="stub",
            ticker=ticker,
            side=side,
            quantity=quantity,
            entry_price=entry_price,
            order_type=order_type,
            order_time=datetime.now(UTC),
            filled_quantity=quantity,
            fill_price=self._fill,
            status=OrderStatus.FILLED,
        )


def _executor(broker) -> TradeExecutor:
    rm = RiskManager({"trailing_stop_pct": 3, "trailing_activation_pct": 5})
    return TradeExecutor(broker, OrderManager(rm), rm)  # type: ignore[arg-type]


def test_full_close_records_realized_pnl():
    """1単元のみ保有 (分割不可) の利確到達 → 全量クローズでも pnl が数値になる。"""
    pos = Position(
        ticker="7203.T", quantity=100, entry_price=100.0, current_price=110.0,
        entry_time=datetime.now(UTC), take_profit=105.0,
    )
    ex = _executor(_FullCloseBroker([pos], fill_price=110.0))

    results = ex.check_and_close_positions()

    assert len(results) == 1
    r = results[0]
    assert r["reason"] == "take_profit"
    assert r["success"] is True
    assert r["pnl"] == (110.0 - 100.0) * 100  # None ではなく実現損益


def test_full_close_records_loss_on_stop():
    """max_loss での全量クローズ (2026-08-19 の実パターン) も損失が記録される。"""
    pos = Position(
        ticker="6503.T", quantity=100, entry_price=6173.23, current_price=5658.87,
        entry_time=datetime.now(UTC), stop_loss=5864.0,
    )
    ex = _executor(_FullCloseBroker([pos], fill_price=5658.87))

    results = ex.check_and_close_positions()

    assert len(results) == 1
    r = results[0]
    assert r["success"] is True
    assert r["pnl"] is not None
    assert round(r["pnl"]) == round((5658.87 - 6173.23) * 100)  # ≈ -51,436

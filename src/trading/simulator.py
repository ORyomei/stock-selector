"""ローカルシミュレーター

BrokerInterface を実装し、ローカルメモリでポートフォリオ・注文を管理する。
実際のAPI呼び出しは行わず、シミュレーション環境で取引テストを実施。

永続化機能:
  - to_dict(): メモリ状態を辞書に変換（JSON保存用）
  - from_dict(): 辞書からメモリ状態を復元
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime
from typing import Any

from interfaces.repositories.market_data import MarketDataRepository

from .broker_interface import (
    BrokerInterface,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)


class BrokerSimulator(BrokerInterface):
    """ローカルシミュレーター"""

    def __init__(self, config: dict, market_data: MarketDataRepository | None = None):
        """初期化

        Args:
            config: 設定辞書
                {
                    "initial_capital_jpy": float,
                    "initial_capital_usd": float,
                    "spread_pct": float  # スプレッド% (デフォルト 0.02)
                }
            market_data: 株価取得リポジトリ（省略時は自動取得）
        """
        self.config = config
        self._market_data = market_data
        self.spread_pct = config.get("spread_pct", 0.02)

        # 資金管理
        self._balance = {
            "cash_jpy": float(config.get("initial_capital_jpy", 10_000_000)),
            "cash_usd": float(config.get("initial_capital_usd", 50_000)),
            "timestamp": datetime.now(UTC),
        }

        # ポジション・注文管理
        self._positions: list[Position] = []  # 保有ポジション
        self._orders: list[Order] = []  # 未約定
        self._filled_orders: list[Order] = []  # 約定済み（履歴）

    @staticmethod
    def _finite_float(value, default: float | None = None) -> float | None:
        """有限な float のみ受け付ける。NaN/inf は default を返す。"""
        try:
            v = float(value)
            if math.isfinite(v):
                return v
        except (TypeError, ValueError):
            pass
        return default

    def get_balance(self) -> dict[str, Any]:
        """残高取得"""
        return {
            "cash_jpy": self._balance["cash_jpy"],
            "cash_usd": self._balance["cash_usd"],
            "timestamp": datetime.now(UTC),
        }

    def place_order(
        self,
        ticker: str,
        side: OrderSide,
        quantity: int,
        order_type: OrderType = OrderType.MARKET,
        entry_price: float = 0.0,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> Order:
        """注文発注"""
        if quantity <= 0:
            raise ValueError(f"Invalid quantity: {quantity}")

        # 現在価格取得
        current_price = self._fetch_price(ticker)
        if current_price is None or not math.isfinite(current_price):
            raise ValueError(f"Failed to fetch price for {ticker}")

        # 指値か成行か
        if order_type == OrderType.MARKET:
            actual_price = current_price
        elif order_type == OrderType.LIMIT:
            if entry_price <= 0:
                raise ValueError("LIMIT order requires entry_price > 0")
            actual_price = entry_price
        else:
            raise ValueError(f"Unsupported order_type: {order_type}")

        # 資金チェック＆自動クリップ（買い注文のみ）
        if side == OrderSide.BUY:
            currency = self._get_currency(ticker)
            cash_key = f"cash_{currency.lower()}"
            available = self._balance[cash_key]
            unit_cost = actual_price * (1 + self.spread_pct / 100)  # スプレッド考慮
            max_affordable = int(available / unit_cost) if unit_cost > 0 else 0
            if max_affordable <= 0:
                raise ValueError(
                    f"Insufficient funds. Unit cost: {currency} {unit_cost:,.0f}, "
                    f"Available: {currency} {available:,.0f}"
                )
            if quantity > max_affordable:
                print(f"⚠️  数量クリップ: {quantity} → {max_affordable}（資金上限）")
                quantity = max_affordable

        # Order 生成
        order = Order(
            id=str(uuid.uuid4()),
            ticker=ticker,
            side=side,
            quantity=quantity,
            entry_price=actual_price,
            order_type=order_type,
            order_time=datetime.now(UTC),
            stop_loss=stop_loss,
            take_profit=take_profit,
        )

        # 即座に約定（シミュレーターでは即約定）
        self._simulate_fill(order)

        return order

    def _simulate_fill(self, order: Order) -> None:
        """注文を約定させる

        Args:
            order: Order オブジェクト（PENDING 状態）
        """
        # 現在価格を再度取得（最新価格）
        current_price = self._fetch_price(order.ticker)
        if current_price is None or not math.isfinite(current_price):
            order.status = OrderStatus.REJECTED
            self._filled_orders.append(order)
            return

        # スプレッド適用（買い：+spread、売り：-spread）
        if order.side == OrderSide.BUY:
            fill_price = current_price * (1 + self.spread_pct / 100)
        else:
            fill_price = current_price * (1 - self.spread_pct / 100)

        # 指値注文の価格チェック
        if order.order_type == OrderType.LIMIT:
            if order.side == OrderSide.BUY and fill_price > order.entry_price:
                # 買い指値だが現在価格が指値を上回る → 約定なし
                order.status = OrderStatus.PENDING
                self._orders.append(order)
                return
            elif order.side == OrderSide.SELL and fill_price < order.entry_price:
                # 売り指値だが現在価格が指値を下回る → 約定なし
                order.status = OrderStatus.PENDING
                self._orders.append(order)
                return

        # 約定処理
        currency = self._get_currency(order.ticker)
        cash_key = f"cash_{currency.lower()}"

        if order.side == OrderSide.BUY:
            # 買い
            cost = fill_price * order.quantity
            self._balance[cash_key] -= cost

            # ポジション追加・更新
            self._add_or_update_position(
                order.ticker,
                order.quantity,
                fill_price,
                order.stop_loss,
                order.take_profit,
            )
        else:
            # 売り
            proceeds = fill_price * order.quantity
            self._balance[cash_key] += proceeds

            # ポジション削減
            self._reduce_position(order.ticker, order.quantity)

        # Order ステータス更新
        order.filled_quantity = order.quantity
        order.fill_price = fill_price
        order.status = OrderStatus.FILLED
        order.order_time = datetime.now(UTC)
        self._filled_orders.append(order)

    def cancel_order(self, order_id: str) -> bool:
        """注文キャンセル"""
        for i, order in enumerate(self._orders):
            if order.id == order_id:
                order.status = OrderStatus.CANCELLED
                self._orders.pop(i)
                self._filled_orders.append(order)
                return True
        return False

    def get_orders(self) -> list[Order]:
        """未約定の注文一覧"""
        return list(self._orders)

    def get_positions(self) -> list[Position]:
        """保有ポジション一覧（現在値で更新）"""
        for pos in self._positions:
            current_price = self._fetch_price(pos.ticker)
            if current_price is not None and math.isfinite(current_price):
                pos.current_price = current_price
                pos.__post_init__()  # pnl 再計算
        return list(self._positions)

    def get_filled_orders(self, limit: int = 100) -> list[Order]:
        """約定済み注文の履歴"""
        return list(self._filled_orders[-limit:])

    def sync_from_broker(self) -> None:
        """(実装用) 実際のAPI から状態同期"""
        pass

    def _add_or_update_position(
        self,
        ticker: str,
        quantity: int,
        price: float,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> None:
        """ポジションを追加または更新する"""
        for pos in self._positions:
            if pos.ticker == ticker:
                # 既存ポジションへの追加 → 平均取得価格を再計算
                total_qty = pos.quantity + quantity
                pos.entry_price = round(
                    (pos.entry_price * pos.quantity + price * quantity) / total_qty, 4
                )
                pos.quantity = total_qty
                if stop_loss:
                    pos.stop_loss = stop_loss
                if take_profit:
                    pos.take_profit = take_profit
                pos.__post_init__()
                return

        # 新規ポジション
        pos = Position(
            ticker=ticker,
            quantity=quantity,
            entry_price=price,
            current_price=price,
            entry_time=datetime.now(UTC),
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        self._positions.append(pos)

    def _reduce_position(self, ticker: str, quantity: int) -> None:
        """ポジションを削減する"""
        for i, pos in enumerate(self._positions):
            if pos.ticker == ticker:
                pos.quantity -= quantity
                if pos.quantity <= 0:
                    self._positions.pop(i)
                else:
                    pos.__post_init__()
                return

    @staticmethod
    def _get_currency(ticker: str) -> str:
        """ティッカーから通貨を推定"""
        if ticker.endswith(".T"):
            return "JPY"
        return "USD"

    def _fetch_price(self, ticker: str) -> float | None:
        """ティッカーの現在価格を取得"""
        if self._market_data is not None:
            return self._market_data.get_current_price(ticker)
        # fallback: lazy import for backward compat
        try:
            import yfinance as yf
            t = yf.Ticker(ticker)
            hist = t.history(period="5d", interval="1d")
            if hist is not None and not hist.empty:
                price = float(hist["Close"].iloc[-1])
                if math.isfinite(price):
                    return price
        except Exception:
            pass
        return None

    def to_dict(self) -> dict[str, Any]:
        """メモリ状態を辞書に変換（JSON保存用）"""
        return {
            "metadata": {
                "last_updated": datetime.now(UTC).isoformat(),
                "broker": "dmmfx",
                "mode": "simulator",
            },
            "balance": {
                "cash_jpy": self._balance["cash_jpy"],
                "cash_usd": self._balance["cash_usd"],
                "timestamp": self._balance["timestamp"].isoformat(),
            },
            "positions": [
                {
                    "ticker": pos.ticker,
                    "quantity": pos.quantity,
                    "entry_price": pos.entry_price,
                    "current_price": pos.current_price,
                    "entry_time": pos.entry_time.isoformat(),
                    "stop_loss": pos.stop_loss,
                    "take_profit": pos.take_profit,
                }
                for pos in self._positions
            ],
            "orders": {
                "pending": [
                    {
                        "id": o.id,
                        "ticker": o.ticker,
                        "side": o.side.name,
                        "quantity": o.quantity,
                        "entry_price": o.entry_price,
                        "order_type": o.order_type.name,
                        "order_time": o.order_time.isoformat(),
                        "status": o.status.name,
                        "stop_loss": o.stop_loss,
                        "take_profit": o.take_profit,
                    }
                    for o in self._orders
                ],
                "filled": [
                    {
                        "id": o.id,
                        "ticker": o.ticker,
                        "side": o.side.name,
                        "quantity": o.quantity,
                        "entry_price": o.entry_price,
                        "fill_price": o.fill_price,
                        "filled_quantity": o.filled_quantity,
                        "order_type": o.order_type.name,
                        "order_time": o.order_time.isoformat(),
                        "status": o.status.name,
                    }
                    for o in self._filled_orders
                ],
            },
        }

    def from_dict(self, data: dict) -> None:
        """辞書からメモリ状態を復元"""
        # 旧スキーマ（cash_jpy / cash_usd / holdings / history）も許容
        if "balance" not in data:
            self._balance = {
                "cash_jpy": self._finite_float(
                    data.get("cash_jpy"),
                    float(self.config.get("initial_capital_jpy", 10_000_000)),
                ),
                "cash_usd": self._finite_float(
                    data.get("cash_usd"),
                    float(self.config.get("initial_capital_usd", 50_000)),
                ),
                "timestamp": datetime.now(UTC),
            }
            self._positions = []
            self._orders = []
            self._filled_orders = []
            return

        # 残高を復元
        cash_jpy = self._finite_float(
            data.get("balance", {}).get("cash_jpy"),
            float(self.config.get("initial_capital_jpy", 10_000_000)),
        )
        cash_usd = self._finite_float(
            data.get("balance", {}).get("cash_usd"),
            float(self.config.get("initial_capital_usd", 50_000)),
        )
        ts_raw = data.get("balance", {}).get("timestamp")
        try:
            timestamp = datetime.fromisoformat(ts_raw) if ts_raw else datetime.now(UTC)
        except Exception:
            timestamp = datetime.now(UTC)

        self._balance = {
            "cash_jpy": cash_jpy,
            "cash_usd": cash_usd,
            "timestamp": timestamp,
        }

        # ポジションを復元
        self._positions = []
        for pos_data in data.get("positions", []):
            entry_price = self._finite_float(pos_data.get("entry_price"))
            current_price = self._finite_float(pos_data.get("current_price"), entry_price)
            if entry_price is None or current_price is None:
                continue
            try:
                entry_time = datetime.fromisoformat(pos_data["entry_time"])
            except Exception:
                entry_time = datetime.now(UTC)

            pos = Position(
                ticker=pos_data["ticker"],
                quantity=pos_data["quantity"],
                entry_price=entry_price,
                current_price=current_price,
                entry_time=entry_time,
                stop_loss=pos_data.get("stop_loss"),
                take_profit=pos_data.get("take_profit"),
            )
            self._positions.append(pos)

        # 注文を復元（ここでは filled orders のみ復元）
        self._filled_orders = []
        for order_data in data.get("orders", {}).get("filled", []):
            try:
                order_time = datetime.fromisoformat(order_data["order_time"])
            except Exception:
                order_time = datetime.now(UTC)
            order = Order(
                id=order_data["id"],
                ticker=order_data["ticker"],
                side=OrderSide[order_data["side"]],
                quantity=order_data["quantity"],
                entry_price=order_data["entry_price"],
                order_type=OrderType[order_data["order_type"]],
                order_time=order_time,
            )
            order.fill_price = self._finite_float(order_data.get("fill_price"), 0.0)
            order.filled_quantity = order_data.get("filled_quantity", 0)
            order.status = OrderStatus[order_data["status"]]
            self._filled_orders.append(order)

        # pending orders を復元
        self._orders = []
        for order_data in data.get("orders", {}).get("pending", []):
            try:
                order_time = datetime.fromisoformat(order_data["order_time"])
            except Exception:
                order_time = datetime.now(UTC)
            order = Order(
                id=order_data["id"],
                ticker=order_data["ticker"],
                side=OrderSide[order_data["side"]],
                quantity=order_data["quantity"],
                entry_price=order_data["entry_price"],
                order_type=OrderType[order_data["order_type"]],
                order_time=order_time,
                stop_loss=order_data.get("stop_loss"),
                take_profit=order_data.get("take_profit"),
            )
            order.status = OrderStatus[order_data["status"]]
            self._orders.append(order)

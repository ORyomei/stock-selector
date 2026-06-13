"""ドメイン値オブジェクト

Order / Position と注文関連の列挙型。ブローカーやリポジトリ、ドメインサービスが
共有する純粋なデータ構造で、他レイヤーに依存しない (最内層)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class OrderSide(StrEnum):
    """注文の売買方向"""

    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    """注文タイプ"""

    MARKET = "MARKET"  # 成行注文
    LIMIT = "LIMIT"  # 指値注文
    STOP = "STOP"  # 逆指値注文


class OrderStatus(StrEnum):
    """注文ステータス"""

    PENDING = "PENDING"  # 未約定
    PARTIALLY_FILLED = "PARTIALLY_FILLED"  # 部分約定
    FILLED = "FILLED"  # 約定済み
    CANCELLED = "CANCELLED"  # キャンセル済み
    REJECTED = "REJECTED"  # 却下


@dataclass
class Order:
    """取引注文

    Attributes:
        id: 注文一意識別子 (UUID)
        ticker: ティッカーシンボル (e.g., "NVDA", "7203.T")
        side: 売買方向 (BUY | SELL)
        quantity: 注文個数
        entry_price: 注文価格（成行の場合は 0.0）
        order_type: 注文タイプ (MARKET | LIMIT | STOP)
        order_time: 注文時刻 (datetime UTC)
        filled_quantity: 約定済個数
        fill_price: 実約定価格
        status: 注文ステータス
        stop_loss: 損切りライン価格（オプション）
        take_profit: 利確ポイント価格（オプション）
    """

    id: str  # UUID として生成される
    ticker: str
    side: OrderSide
    quantity: int
    entry_price: float  # 指値価格、成行の場合は 0.0
    order_type: OrderType
    order_time: datetime

    filled_quantity: int = 0
    fill_price: float | None = None
    status: OrderStatus = OrderStatus.PENDING
    stop_loss: float | None = None
    take_profit: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON シリアライズ用"""
        return {
            "id": self.id,
            "ticker": self.ticker,
            "side": self.side.value,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "order_type": self.order_type.value,
            "order_time": self.order_time.isoformat(),
            "filled_quantity": self.filled_quantity,
            "fill_price": self.fill_price,
            "status": self.status.value,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
        }


@dataclass
class Position:
    """ポジション（保有銘柄）

    Attributes:
        ticker: ティッカーシンボル
        quantity: 保有個数
        entry_price: 平均取得価格
        current_price: 現在値
        entry_time: ポジション建て時刻
        stop_loss: 損切りラインの価格
        take_profit: 利確ポイントの価格
        peak_price: 取得後の最高値（トレーリングストップ用）
        pnl: 未決済損益
        pnl_pct: 未決済損益率（%）
    """

    ticker: str
    quantity: int
    entry_price: float
    current_price: float
    entry_time: datetime

    stop_loss: float | None = None
    take_profit: float | None = None
    peak_price: float | None = None  # 取得後の最高値（トレーリングストップ用）
    timespan: str = "swing"  # 推奨ホールド期間 (short/swing/medium/long) — max_hold 判定に使用
    pnl: float = field(default=0.0, init=False)
    pnl_pct: float = field(default=0.0, init=False)

    def __post_init__(self):
        """pnl を計算し、未設定の高値を初期化"""
        self.pnl = (self.current_price - self.entry_price) * self.quantity
        self.pnl_pct = (
            ((self.current_price - self.entry_price) / self.entry_price * 100)
            if self.entry_price != 0
            else 0.0
        )
        # 高値 (トレーリングストップ用) は最低でも取得価格・現在値を下回らない
        seed = max(self.entry_price, self.current_price)
        if self.peak_price is None or self.peak_price < seed:
            self.peak_price = seed

    def to_dict(self) -> dict[str, Any]:
        """JSON シリアライズ用"""
        return {
            "ticker": self.ticker,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "current_price": self.current_price,
            "entry_time": self.entry_time.isoformat(),
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "peak_price": self.peak_price,
            "timespan": self.timespan,
            "pnl": round(self.pnl, 2),
            "pnl_pct": round(self.pnl_pct, 2),
        }

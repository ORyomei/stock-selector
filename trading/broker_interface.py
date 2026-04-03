"""取引ブローカーの抽象インターフェース

Order、Position、BrokerInterface を定義し、
シミュレーターと実取引 API を差し替え可能にする。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


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

    def to_dict(self) -> dict:
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
    pnl: float = field(default=0.0, init=False)
    pnl_pct: float = field(default=0.0, init=False)

    def __post_init__(self):
        """pnl を計算"""
        self.pnl = (self.current_price - self.entry_price) * self.quantity
        self.pnl_pct = (
            ((self.current_price - self.entry_price) / self.entry_price * 100)
            if self.entry_price != 0
            else 0.0
        )

    def to_dict(self) -> dict:
        """JSON シリアライズ用"""
        return {
            "ticker": self.ticker,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "current_price": self.current_price,
            "entry_time": self.entry_time.isoformat(),
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "pnl": round(self.pnl, 2),
            "pnl_pct": round(self.pnl_pct, 2),
        }


class BrokerInterface(ABC):
    """ブローカーの統一インターフェース

    シミュレーター、実取引 API 等を実装する際の基底クラス。
    """

    @abstractmethod
    def get_balance(self) -> dict:
        """残高取得

        Returns:
            {
                "cash_jpy": float,
                "cash_usd": float,
                "timestamp": datetime
            }
        """
        pass

    @abstractmethod
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
        """注文発注

        Args:
            ticker: ティッカーシンボル
            side: 売買方向 (BUY | SELL)
            quantity: 注文個数
            order_type: 注文タイプ (MARKET | LIMIT | STOP)
            entry_price: 指値時の価格（成行の場合は 0.0）
            stop_loss: 損切りライン
            take_profit: 利確ポイント

        Returns:
            Order オブジェクト

        Raises:
            ValueError: 資金不足、無効な引数等
        """
        pass

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """注文キャンセル

        Args:
            order_id: キャンセル対象の注文 ID

        Returns:
            成功時 True、既に約定済み等で失敗時 False
        """
        pass

    @abstractmethod
    def get_orders(self) -> list[Order]:
        """未約定の注文一覧取得

        Returns:
            Order リスト（PENDING | PARTIALLY_FILLED のもの）
        """
        pass

    @abstractmethod
    def get_positions(self) -> list[Position]:
        """保有ポジション一覧取得

        Returns:
            Position リスト（現在値で更新済み）
        """
        pass

    @abstractmethod
    def get_filled_orders(self, limit: int = 100) -> list[Order]:
        """約定済み注文の履歴取得

        Args:
            limit: 取得最大件数

        Returns:
            Order リスト（FILLED | CANCELLED のもの、新しい順）
        """
        pass

    @abstractmethod
    def sync_from_broker(self) -> None:
        """ブローカー側の最新状態を同期

        (実装用) 実際の API から現在値・ポジション・残高を取得
        """
        pass

"""ブローカーの抽象インターフェース (port)

シミュレーターと実取引 API (kabu 等) を差し替え可能にする統一ポート。
具象実装は infra/brokers/ 配下に置く。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from domain.models import Order, OrderSide, OrderType, Position


class BrokerInterface(ABC):
    """ブローカーの統一インターフェース

    シミュレーター、実取引 API 等を実装する際の基底クラス。
    """

    @abstractmethod
    def get_balance(self) -> dict[str, Any]:
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
        timespan: str = "swing",
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
    def sync(self) -> None:
        """メモリ状態をバッキングストアと同期する (pull)。

        - simulator: portfolio.json から再読込
        - kabu: 証券会社 API から現在値・ポジション・残高を再取得

        永続化 (push) はブローカーが内部で自動的に行うため、業務ロジックは
        状態の保存を意識しない (sync だけが状態ライフサイクルの公開メソッド)。
        """
        pass

    def managed_currencies(self) -> set[str] | None:
        """このブローカーが管理する通貨の集合。

        ``None`` は「全通貨を管理」(制限なし) を意味する。reconcile はこれを見て、
        ブローカーが扱わない通貨の現金・建玉をローカルに温存する
        (例: kabu は日本株のみ → ``{"JPY"}`` を返し、米国株/USD現金を消さない)。
        """
        return None

    def get_trading_unit(self, ticker: str) -> int:
        """銘柄の売買単位 (単元株数 / ETF の口数単位) を返す。

        既定は通貨ベースの推定 (日本株=100, その他=1)。実ブローカーは
        銘柄情報 API から正確な値を取得するためオーバーライドする。
        """
        return 100 if ticker.endswith(".T") else 1

"""注文マネージャー

売買判断（TradingSignal）を Order オブジェクトに変換する。
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from .broker_interface import Order, OrderSide, OrderType


class TradeAction(StrEnum):
    """取引アクション"""

    BUY = "BUY"  # 買い
    SELL = "SELL"  # 売り
    CLOSE = "CLOSE"  # ポジション全量クローズ


class TimeSpan(StrEnum):
    """取引スパン"""

    SHORT = "short"  # デイトレ・短期（1日以内）
    SWING = "swing"  # スイング（数日～1週間）
    MEDIUM = "medium"  # 中期（1週間～1ヶ月）
    LONG = "long"  # 長期（1ヶ月以上）


@dataclass
class TradingSignal:
    """売買判断シグナル

    Attributes:
        ticker: ティッカーシンボル
        action: 取引アクション (BUY | SELL | CLOSE)
        confidence: 確信度 (0.0 ~ 1.0)
        target_price: 目標価格
        entry_price: 推奨エントリー価格（成行ならば None）
        stop_loss_price: 損切りラインの価格
        take_profit_price: 利確ポイントの価格
        timespan: 推奨ホールド期間 (short | swing | medium | long)
        reason: 判断理由の説明
        score: 総合スコア (0-100)
        timestamp: シグナル生成時刻
    """

    ticker: str
    action: TradeAction
    confidence: float  # 0.0 ~ 1.0
    target_price: float
    stop_loss_price: float
    take_profit_price: float

    entry_price: float = 0.0  # 成行の場合は 0.0
    timespan: TimeSpan = TimeSpan.SWING
    reason: str = ""
    score: int = 0
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    def validate(self) -> bool:
        """シグナルの妥当性チェック"""
        if not 0.0 <= self.confidence <= 1.0:
            return False
        if self.target_price <= 0:
            return False
        if self.stop_loss_price <= 0:
            return False
        if self.take_profit_price <= 0:
            return False
        if self.entry_price < 0:
            return False

        # 買いの場合: stop_loss < entry_price < take_profit
        if self.action == TradeAction.BUY:
            if not (self.stop_loss_price < self.target_price <= self.take_profit_price):
                return False

        # 売りの場合: take_profit < entry_price < stop_loss
        elif self.action == TradeAction.SELL and not (  # noqa: SIM102
            self.take_profit_price < self.target_price <= self.stop_loss_price
        ):
            return False

        return True

    def to_dict(self) -> dict:
        """JSON シリアライズ用"""
        return {
            "ticker": self.ticker,
            "action": self.action.value,
            "confidence": self.confidence,
            "target_price": self.target_price,
            "entry_price": self.entry_price,
            "stop_loss_price": self.stop_loss_price,
            "take_profit_price": self.take_profit_price,
            "timespan": self.timespan.value,
            "reason": self.reason,
            "score": self.score,
            "timestamp": self.timestamp.isoformat(),
        }


class OrderManager:
    """売買判断 → 注文生成マネージャー"""

    def __init__(self, risk_manager=None):
        """初期化

        Args:
            risk_manager: RiskManager インスタンス（注文数量計算に用いる）
        """
        self.risk_manager = risk_manager

    def generate_order(
        self,
        signal: TradingSignal,
        current_balance: dict,
    ) -> Order | None:
        """売買判断から Order を生成する

        Args:
            signal: TradingSignal インスタンス
            current_balance: 現在の残高 (get_balance() の結果)

        Returns:
            Order インスタンス、または生成失敗時 None
        """
        if not signal.validate():
            return None

        # アクションから売買方向を決定
        if signal.action == TradeAction.BUY:
            side = OrderSide.BUY
        elif signal.action == TradeAction.SELL:
            side = OrderSide.SELL
        elif signal.action == TradeAction.CLOSE:
            # CLOSE は SELL 扱い
            side = OrderSide.SELL
        else:
            return None

        # 注文数量を計算
        quantity = self._calculate_quantity(signal, current_balance)
        if quantity <= 0:
            return None

        # 注文価格を決定
        if signal.entry_price > 0:
            order_type = OrderType.LIMIT
            entry_price = signal.entry_price
        else:
            order_type = OrderType.MARKET
            entry_price = 0.0

        # Order 生成
        order = Order(
            id="",  # broker.place_order() で ID が割り当てられる
            ticker=signal.ticker,
            side=side,
            quantity=quantity,
            entry_price=entry_price,
            order_type=order_type,
            order_time=datetime.now(UTC),
            stop_loss=signal.stop_loss_price,
            take_profit=signal.take_profit_price,
        )

        return order

    def _calculate_quantity(
        self,
        signal: TradingSignal,
        current_balance: dict,
    ) -> int:
        """信号から注文数量を計算

        高い確信度 → 多い数量
        低い確信度 → 少ない数量

        Args:
            signal: TradingSignal
            current_balance: 残高辞書

        Returns:
            注文数量（商品による。失敗時 0）
        """
        if self.risk_manager:
            # RiskManager がある場合は delegate
            try:
                qty = self.risk_manager.calculate_position_size(
                    balance=current_balance,
                    ticker=signal.ticker,
                    entry_price=signal.entry_price
                    if signal.entry_price > 0
                    else signal.target_price,
                    stop_loss_price=signal.stop_loss_price,
                    confidence=signal.confidence,
                )
                return qty
            except Exception:
                pass

        # RiskManager がない場合は簡易計算
        # confidence と score により決定
        confidence_ratio = signal.confidence  # 0.0 ~ 1.0
        score_ratio = signal.score / 100.0  # 0.0 ~ 1.0

        # 平均比率
        avg_ratio = (confidence_ratio + score_ratio) / 2.0

        # 基本数量（テスト用）
        base_qty = 10  # JPY は 1 株単位を想定

        quantity = int(base_qty * avg_ratio)

        return max(1, quantity)  # 最小 1 株

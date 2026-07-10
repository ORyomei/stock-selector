"""取引エクゼキューター

売買判断 → 注文 → 発注 → 約定確認を統合管理する。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from domain.models import OrderSide, OrderType, Position
from interfaces.broker import BrokerInterface

from .order_manager import OrderManager, TradeAction, TradingSignal
from .risk_manager import RiskManager


class TradeExecutor:
    """取引の統合オーケストレーション"""

    def __init__(
        self,
        broker: BrokerInterface,
        order_manager: OrderManager,
        risk_manager: RiskManager,
    ):
        """初期化

        Args:
            broker: BrokerInterface 実装（Simulator または Real API）
            order_manager: OrderManager インスタンス
            risk_manager: RiskManager インスタンス
        """
        self.broker = broker
        self.order_manager = order_manager
        self.risk_manager = risk_manager

    def execute_signal(self, signal: TradingSignal) -> dict[str, Any]:
        """売買判断シグナルを実行する

        メイン処理。判断 → 注文生成 → 発注 → 約定確認を行う。

        Args:
            signal: TradingSignal インスタンス

        Returns:
            {
                "success": bool,
                "order_id": str | None,
                "ticker": str,
                "action": str,
                "quantity": int,
                "entry_price": float,
                "fill_price": float | None,
                "status": str,
                "pnl": float | None,
                "reason": str,
                "timestamp": str (ISO 8601),
            }
        """
        result = {
            "success": False,
            "order_id": None,
            "ticker": signal.ticker,
            "action": signal.action.value,
            "quantity": 0,
            "entry_price": 0.0,
            "fill_price": None,
            "status": "ERROR",
            "pnl": None,
            "reason": "",
            "timestamp": datetime.now(UTC).isoformat(),
        }

        try:
            # 1. シグナルの妥当性チェック (失敗理由付き)
            validation_error = signal.validation_error()
            if validation_error is not None:
                result["reason"] = f"Invalid signal: {validation_error}"
                return result

            # 2. 現在のポジション・残高を取得
            current_positions = self.broker.get_positions()
            current_balance = self.broker.get_balance()

            # 3. リスクチェック
            is_valid, error_msg = self.risk_manager.validate_order(signal.ticker, current_positions)
            if not is_valid:
                result["reason"] = f"Risk check failed: {error_msg}"
                return result

            # 4. 注文生成
            order = self.order_manager.generate_order(signal, current_balance, current_positions)
            if order is None:
                result["reason"] = "Order generation failed"
                return result

            result["quantity"] = order.quantity
            result["entry_price"] = (
                order.entry_price if order.entry_price > 0 else signal.target_price
            )

            # 5. 注文発注
            placed_order = self.broker.place_order(
                ticker=order.ticker,
                side=order.side,
                quantity=order.quantity,
                order_type=order.order_type,
                entry_price=order.entry_price,
                stop_loss=order.stop_loss,
                take_profit=order.take_profit,
                timespan=signal.timespan.value,
            )

            result["order_id"] = placed_order.id
            result["fill_price"] = placed_order.fill_price
            result["status"] = placed_order.status.value

            # 6. 約定確認と損益計算
            if placed_order.status.value == "FILLED":
                result["success"] = True

                # ポジション更新後の損益
                updated_positions = self.broker.get_positions()
                for pos in updated_positions:
                    if pos.ticker == signal.ticker:
                        result["pnl"] = round(pos.pnl, 2)
                        break

                result["reason"] = f"Order filled: {order.quantity} @ {placed_order.fill_price}"
            else:
                result["reason"] = f"Order status: {placed_order.status.value}"

        except ValueError as e:
            result["reason"] = f"ValueError: {str(e)}"
        except Exception as e:
            result["reason"] = f"Unexpected error: {str(e)}"

        return result

    def check_and_close_positions(self) -> list[dict]:
        """保有ポジションの損切り/利確チェック

        定期実行（例：30分ごと）で、損切り・利確条件を確認し、
        自動クローズを発注する。

        Returns:
            [
                {
                    "success": bool,
                    "ticker": str,
                    "quantity": int,
                    "reason": str,  # "stop_loss" | "take_profit" | ...
                    "timestamp": str (ISO 8601),
                },
                ...
            ]
        """
        results = []

        try:
            positions = self.broker.get_positions()

            for position in positions:
                # 現在価格で自動クローズ判定（理由付き）
                should_close, reason = self.risk_manager.should_close_position(
                    position, position.current_price
                )

                if should_close:
                    # 保有期間メタデータ (期待値のバケット別計測用)
                    entry_iso = position.entry_time.isoformat() if position.entry_time else None
                    hold_days = (
                        (datetime.now(UTC) - position.entry_time).days
                        if position.entry_time
                        else None
                    )

                    # 利確到達時は半分だけ利確し、残りをトレーリングで走らせる (損小利大)。
                    # 残りが次サイクルでも利確水準以上なら再び半分…と段階的に売り上がる。
                    # 単元で半分に割れない小口は従来どおり全量利確。
                    if reason == "take_profit":
                        partial_qty = self._scale_out_quantity(position)
                        if partial_qty > 0:
                            result = self._close_partial(
                                position, partial_qty, "take_profit_scale_out"
                            )
                            result["entry_time"] = entry_iso
                            result["hold_days"] = hold_days
                            results.append(result)
                            continue

                    signal = TradingSignal(
                        ticker=position.ticker,
                        action=TradeAction.CLOSE,
                        confidence=1.0,  # 自動クローズは確実
                        target_price=position.current_price,
                        stop_loss_price=position.current_price * 0.95,  # 実質使用されない
                        take_profit_price=position.current_price * 1.05,
                        reason=f"Auto-close: {reason}",
                    )

                    # 実行
                    result = self.execute_signal(signal)

                    # 結果をまとめる
                    results.append(
                        {
                            "success": result["success"],
                            "ticker": result["ticker"],
                            "quantity": result["quantity"],
                            "reason": reason,
                            "timestamp": result["timestamp"],
                            "fill_price": result["fill_price"],
                            "pnl": result["pnl"],
                            "entry_time": entry_iso,
                            "hold_days": hold_days,
                        }
                    )

        except Exception as e:
            results.append(
                {
                    "success": False,
                    "ticker": "N/A",
                    "quantity": 0,
                    "reason": f"Error in check_and_close: {str(e)}",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "fill_price": None,
                    "pnl": None,
                }
            )

        return results

    @staticmethod
    def _scale_out_quantity(position: Position) -> int:
        """利確到達時に部分利確する株数 (半分を売買単位に丸め)。

        半分・残りの両方が 1 単元以上確保できない場合は 0 を返し、
        呼び出し側は従来どおり全量クローズにフォールバックする。
        """
        from core.trading_units import get_trading_unit

        unit = max(1, get_trading_unit(position.ticker))
        half = (position.quantity // 2 // unit) * unit
        if half >= unit and (position.quantity - half) >= unit:
            return half
        return 0

    def _close_partial(self, position: Position, quantity: int, reason: str) -> dict:
        """建玉の一部を成行で売却する (利確の段階売り用)。"""
        try:
            order = self.broker.place_order(
                ticker=position.ticker,
                side=OrderSide.SELL,
                quantity=quantity,
                order_type=OrderType.MARKET,
                entry_price=0.0,
            )
            success = order.status.value == "FILLED"
            pnl = (
                round((order.fill_price - position.entry_price) * quantity, 2)
                if success and order.fill_price is not None
                else None
            )
            return {
                "success": success,
                "ticker": position.ticker,
                "quantity": quantity,
                "reason": reason,
                "timestamp": datetime.now(UTC).isoformat(),
                "fill_price": order.fill_price,
                "pnl": pnl,
            }
        except Exception as e:
            return {
                "success": False,
                "ticker": position.ticker,
                "quantity": quantity,
                "reason": f"{reason}_failed: {e}",
                "timestamp": datetime.now(UTC).isoformat(),
                "fill_price": None,
                "pnl": None,
            }

    @staticmethod
    def _determine_close_reason(position) -> str:
        """クローズ理由を判定（後方互換用）"""
        if position.stop_loss and position.current_price <= position.stop_loss:
            return "stop_loss"
        if position.take_profit and position.current_price >= position.take_profit:
            return "take_profit"
        return "trailing_stop"

    def get_portfolio_summary(self) -> dict[str, Any]:
        """ポートフォリオの概要を取得

        Returns:
            {
                "balance": {...},
                "positions": [...],
                "total_pnl": float,
                "total_pnl_pct": float,
            }
        """
        balance = self.broker.get_balance()
        positions = self.broker.get_positions()

        total_pnl = sum(pos.pnl for pos in positions)
        total_investment = sum(pos.entry_price * pos.quantity for pos in positions)
        total_pnl_pct = (total_pnl / total_investment * 100) if total_investment > 0 else 0.0

        return {
            "balance": {
                "cash_jpy": balance["cash_jpy"],
                "cash_usd": balance["cash_usd"],
                "timestamp": balance["timestamp"].isoformat(),
            },
            "positions": [pos.to_dict() for pos in positions],
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "timestamp": datetime.now(UTC).isoformat(),
        }

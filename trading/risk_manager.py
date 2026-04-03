"""リスク管理エンジン

ポジションサイズ計算、損切り/利確判定、レバレッジ制御等を担当。
"""

from __future__ import annotations

from datetime import UTC

from .broker_interface import Position


class RiskManager:
    """リスク管理

    ポジションサイズ計算、損切り/利確判定、リスク限度チェック等を行う。
    """

    def __init__(self, config: dict):
        """初期化

        Args:
            config: リスク設定辞書
                {
                    "max_position_size_pct": float,        # 1ポジションの最大サイズ（資金比%）
                    "max_daily_loss_pct": float,            # 1日の最大損失%
                    "max_concurrent_positions": int,        # 同時保有ポジション数上限
                    "default_stop_loss_pct": float,         # デフォルト損切り計%
                    "default_take_profit_pct": float,       # デフォルト利確幅%
                    "trailing_stop_pct": float,             # トレーリングストップ幅%
                    "forbidden_tickers": list[str],         # 取引禁止銘柄
                }
        """
        self.config = config
        self.max_position_size_pct = config.get("max_position_size_pct", 5)
        self.max_daily_loss_pct = config.get("max_daily_loss_pct", 2)
        self.max_concurrent_positions = config.get("max_concurrent_positions", 5)
        self.default_stop_loss_pct = config.get("default_stop_loss_pct", 3)
        self.default_take_profit_pct = config.get("default_take_profit_pct", 5)
        self.trailing_stop_pct = config.get("trailing_stop_pct", 2)
        self.forbidden_tickers = config.get("forbidden_tickers", [])

    def calculate_position_size(
        self,
        balance: dict,
        ticker: str,
        entry_price: float,
        stop_loss_price: float,
        confidence: float = 0.5,
    ) -> int:
        """ポジションサイズを計算する

        Kelly's formula 簡易版:
        position_size = (資金 × 許容損失%) / (entry_price - stop_loss_price)

        Args:
            balance: 残高辞書 {"cash_jpy": ..., "cash_usd": ...}
            ticker: ティッカーシンボル
            entry_price: エントリー価格
            stop_loss_price: 損切り価格
            confidence: 確信度 (0.0 ~ 1.0) 高いほど大きなポジション

        Returns:
            ポジションサイズ（株数）

        Raises:
            ValueError: 無効な引数
        """
        if entry_price <= 0 or stop_loss_price <= 0:
            raise ValueError("entry_price and stop_loss_price must be > 0")

        if entry_price <= stop_loss_price:
            raise ValueError("entry_price must be > stop_loss_price for BUY")

        # 通貨を判定
        currency = self._get_currency(ticker)
        cash_key = f"cash_{currency.lower()}"
        available_cash = balance.get(cash_key, 0)

        if available_cash <= 0:
            raise ValueError(f"No available {currency} cash")

        # 許容損失額を計算 (資金 × max_position_size_pct × confidence)
        allowed_risk_amount = available_cash * (self.max_position_size_pct / 100) * confidence

        # 1 株あたりのリスク = entry_price - stop_loss_price
        risk_per_share = entry_price - stop_loss_price

        # position_size = allowed_risk_amount / risk_per_share
        position_size = int(allowed_risk_amount / risk_per_share)

        # 最小 1 株
        position_size = max(1, position_size)

        # 資金の絶対上限: 買える最大株数を超えないようクリップ
        max_affordable = int(available_cash / entry_price) if entry_price > 0 else 0
        if max_affordable > 0:
            position_size = min(position_size, max_affordable)

        return position_size

    def check_daily_loss(self, total_pnl_today: float, total_balance: float) -> bool:
        """1日の損失限度チェック

        Args:
            total_pnl_today: 本日の総損益
            total_balance: 総資金（JPY + USD 換算）

        Returns:
            損失限度を超えた場合 True（取引中止推奨）
        """
        loss_pct = abs(total_pnl_today) / total_balance * 100 if total_balance > 0 else 0
        return loss_pct > self.max_daily_loss_pct

    def validate_order(
        self,
        ticker: str,
        current_positions: list[Position],
    ) -> tuple[bool, str]:
        """注文の妥当性チェック

        Args:
            ticker: ティッカーシンボル
            current_positions: 現在のポジションリスト

        Returns:
            (is_valid, reason_if_invalid)
        """
        # 禁止銘柄チェック
        if ticker in self.forbidden_tickers:
            return False, f"{ticker} is forbidden"

        # 同時保有ポジション数チェック
        unique_tickers = set(pos.ticker for pos in current_positions)
        if ticker not in unique_tickers and len(unique_tickers) >= self.max_concurrent_positions:
            return False, f"Max concurrent positions ({self.max_concurrent_positions}) reached"

        return True, ""

    def should_close_position(
        self,
        position: Position,
        current_price: float,
    ) -> tuple[bool, str]:
        """ポジションをクローズすべきか判定

        以下の条件を順にチェックし、最初に該当した理由を返す:
          1. 損切りライン到達
          2. 利確ライン到達
          3. トレーリングストップ
          4. 大幅損失（-5% 以上）
          5. 長期保有タイムアウト（スイング: 21営業日）

        Args:
            position: Position オブジェクト
            current_price: 現在価格

        Returns:
            (should_close, reason) のタプル
        """
        if position.entry_price <= 0 or current_price <= 0:
            return False, ""

        pnl_pct = (current_price - position.entry_price) / position.entry_price * 100

        # 1. 損切りライン到達
        if position.stop_loss and current_price <= position.stop_loss:
            return True, "stop_loss"

        # 2. 利確ライン到達
        if position.take_profit and current_price >= position.take_profit:
            return True, "take_profit"

        # 3. トレーリングストップ（高値からの下落幅）
        trailing_stop = position.entry_price * (1 - self.trailing_stop_pct / 100)
        if current_price <= trailing_stop and pnl_pct < 0:
            return True, "trailing_stop"

        # 4. 大幅損失ガード（-5% 以上で自動損切り）
        max_loss_pct = self.config.get("max_loss_per_position_pct", 5)
        if pnl_pct <= -max_loss_pct:
            return True, f"max_loss_{max_loss_pct}pct"

        # 5. 保有日数タイムアウト（長く持ちすぎ）
        from datetime import datetime

        hold_days = (datetime.now(UTC) - position.entry_time).days
        max_hold = self.config.get("max_hold_days", 30)
        if hold_days >= max_hold:
            return True, f"hold_timeout_{hold_days}d"

        return False, ""

    def calculate_default_stop_loss(
        self,
        entry_price: float,
        side: str = "BUY",
    ) -> float:
        """デフォルトの損切りラインを計算

        Args:
            entry_price: エントリー価格
            side: "BUY" | "SELL"

        Returns:
            損切り価格
        """
        if side == "BUY":
            return entry_price * (1 - self.default_stop_loss_pct / 100)
        else:  # SELL
            return entry_price * (1 + self.default_stop_loss_pct / 100)

    def calculate_default_take_profit(
        self,
        entry_price: float,
        side: str = "BUY",
    ) -> float:
        """デフォルトの利確ラインを計算

        Args:
            entry_price: エントリー価格
            side: "BUY" | "SELL"

        Returns:
            利確価格
        """
        if side == "BUY":
            return entry_price * (1 + self.default_take_profit_pct / 100)
        else:  # SELL
            return entry_price * (1 - self.default_take_profit_pct / 100)

    @staticmethod
    def _get_currency(ticker: str) -> str:
        """ティッカーから通貨を判定"""
        if ticker.endswith(".T"):
            return "JPY"
        return "USD"

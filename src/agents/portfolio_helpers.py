"""Portfolio state helpers — backward-compat wrapper over container."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SRC_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(SRC_DIR))

from infra.container import get_container

# 為替の概算レート (USD建ての総資産換算用。厳密な評価が必要なら要改善)
USD_JPY_APPROX = 150.0


def load_portfolio() -> dict[str, Any] | None:
    """Load portfolio.json, returning None if absent."""
    return get_container().portfolio().load()


# 保有状態はブローカー (状態の唯一の所有者) から取得する。portfolio.json を
# 直読みせず、simulator/kabu のどちらでも同じ経路で読む。
def get_held_tickers() -> set[str]:
    return {p.ticker for p in get_container().broker().get_positions()}


def get_held_positions() -> list[dict[str, Any]]:
    return [p.to_dict() for p in get_container().broker().get_positions()]


def count_positions() -> int:
    return len(get_container().broker().get_positions())


def get_max_positions() -> int:
    return get_container().portfolio().get_max_positions()


def confidence_to_float(label: str) -> float:
    """Convert Japanese confidence label to a numeric value."""
    return {
        "高": 0.90,
        "中〜高": 0.80,
        "中": 0.70,
        "低〜中": 0.60,
        "低": 0.50,
    }.get(label, 0.70)


# ── リスク監視ヘルパー (graph_trade のサイクルで使用) ─────────────────────────


def _position_value_jpy(ticker: str, price: float, qty: int) -> float:
    """建玉評価額を JPY 概算で返す (米国株は USD→JPY 換算)。"""
    val = price * qty
    return val if ticker.endswith(".T") else val * USD_JPY_APPROX


def total_equity_jpy() -> float:
    """総資産を JPY 概算で算出 (現金 + 建玉評価額)。日次損失・集中度の分母に使う。

    ブローカー (状態の所有者) から残高・建玉を取得する (portfolio.json 直読みしない)。
    """
    broker = get_container().broker()
    bal = broker.get_balance()
    equity = float(bal.get("cash_jpy", 0) or 0) + float(bal.get("cash_usd", 0) or 0) * USD_JPY_APPROX
    for p in broker.get_positions():
        equity += _position_value_jpy(p.ticker, p.current_price, p.quantity)
    return equity


def today_realized_pnl() -> float:
    """本日 (UTC日付) に確定した実現損益の合計。diary/trades の記録から集計する。"""
    diary = get_container().diary()
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    total = 0.0
    for t in diary.load_recent_trades(days=2):
        if str(t.get("timestamp", ""))[:10] == today:
            pnl = t.get("pnl")
            if isinstance(pnl, int | float):
                total += float(pnl)
    return total


def daily_loss_exceeded(log: Any = lambda *_: None) -> bool:
    """本日の実現損失が max_daily_loss_pct を超えたか (サーキットブレーカー)。"""
    try:
        from core.trade import load_risk_limits

        max_daily = float(load_risk_limits().get("max_daily_loss_pct", 2))
        realized = today_realized_pnl()
        if realized >= 0:
            return False
        equity = total_equity_jpy()
        if equity <= 0:
            return False
        loss_pct = abs(realized) / equity * 100
        if loss_pct > max_daily:
            log(
                f"  日次実現損益 ¥{realized:,.0f} = 総資産の {loss_pct:.1f}% "
                f"(上限 {max_daily}%) → 新規買い停止"
            )
            return True
        return False
    except Exception as e:
        print(f"⚠️ daily-loss check skipped: {e}", file=sys.stderr)
        return False


def warn_overweight_positions(log: Any = lambda *_: None) -> None:
    """総資産に対する比率が max_position_size_pct を超える保有銘柄を警告する。"""
    try:
        from core.trade import load_risk_limits

        max_pct = float(load_risk_limits().get("max_position_size_pct", 30))
        # ブローカーから残高・建玉を一度だけ取得し、総資産と各比率を同じデータで算出
        broker = get_container().broker()
        bal = broker.get_balance()
        positions = broker.get_positions()
        equity = float(bal.get("cash_jpy", 0) or 0) + float(bal.get("cash_usd", 0) or 0) * USD_JPY_APPROX
        for p in positions:
            equity += _position_value_jpy(p.ticker, p.current_price, p.quantity)
        if equity <= 0:
            return
        for p in positions:
            pct = _position_value_jpy(p.ticker, p.current_price, p.quantity) / equity * 100
            if pct > max_pct:
                log(
                    f"  ⚠️ 集中超過: {p.ticker} が総資産の {pct:.0f}% "
                    f"(上限 {max_pct:.0f}%) — 一部利確を検討"
                )
    except Exception as e:
        print(f"⚠️ overweight check skipped: {e}", file=sys.stderr)

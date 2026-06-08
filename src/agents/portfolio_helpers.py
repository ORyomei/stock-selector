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


def get_held_tickers() -> set[str]:
    return get_container().portfolio().get_held_tickers()


def get_held_positions() -> list[dict[str, Any]]:
    return get_container().portfolio().get_held_positions()


def count_positions() -> int:
    return get_container().portfolio().count_positions()


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


# ── リスク監視ヘルパー (legacy run_cycle / LangGraph 双方で共有) ─────────────


def total_equity_jpy() -> float:
    """総資産を JPY 概算で算出 (現金 + 建玉評価額)。日次損失・集中度の分母に使う。"""
    pf = get_container().portfolio().load() or {}
    bal = pf.get("balance", {})
    equity = float(bal.get("cash_jpy", 0) or 0) + float(bal.get("cash_usd", 0) or 0) * USD_JPY_APPROX
    for p in pf.get("positions", []):
        price = float(p.get("current_price") or p.get("entry_price") or 0)
        val = price * int(p.get("quantity", 0) or 0)
        if not str(p.get("ticker", "")).endswith(".T"):
            val *= USD_JPY_APPROX
        equity += val
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
        equity = total_equity_jpy()
        if equity <= 0:
            return
        pf = get_container().portfolio().load() or {}
        for p in pf.get("positions", []):
            ticker = str(p.get("ticker", ""))
            price = float(p.get("current_price") or p.get("entry_price") or 0)
            val = price * int(p.get("quantity", 0) or 0)
            if not ticker.endswith(".T"):
                val *= USD_JPY_APPROX
            pct = val / equity * 100
            if pct > max_pct:
                log(
                    f"  ⚠️ 集中超過: {ticker} が総資産の {pct:.0f}% "
                    f"(上限 {max_pct:.0f}%) — 一部利確を検討"
                )
    except Exception as e:
        print(f"⚠️ overweight check skipped: {e}", file=sys.stderr)

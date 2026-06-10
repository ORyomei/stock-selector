"""summarize_auto_trades (diary/trades の自動売買パフォーマンス集計) のテスト。

旧 cmd_performance は portfolio.json の手動履歴しか見ず、デーモンの売買
(diary/trades/) が一切集計されなかった。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.portfolio_ops import summarize_auto_trades  # noqa: E402


def _trade(action, status, pnl, ticker="7203.T", success=True, ts="2026-06-10T04:00:00+00:00"):
    return {
        "success": success,
        "ticker": ticker,
        "action": action,
        "status": status,
        "pnl": pnl,
        "reason": "test",
        "timestamp": ts,
    }


SAMPLE = [
    _trade("BUY", "FILLED", -100),  # 買い直後の評価損 (スプレッド) — 実現益に含めない
    _trade("BUY", "FILLED", -50),
    _trade("CLOSE", "FILLED", 8000),  # 勝ち
    _trade("CLOSE", "FILLED", 4500),  # 勝ち
    _trade("CLOSE", "FILLED", -2500),  # 負け
    _trade("SELL", "FILLED", 1000),  # SELL も実現益に含む
    _trade("BUY", "ERROR", None, success=False),  # 失敗注文
    _trade("CLOSE", "FILLED", None),  # pnl 欠落 → 集計対象外
]


def test_realized_pnl_counts_only_closes():
    s = summarize_auto_trades(SAMPLE)
    assert s["closed_trades"] == 3 + 1  # CLOSE×3 + SELL×1 (pnl=None は除外)
    assert s["realized_pnl"] == 8000 + 4500 - 2500 + 1000  # BUY の pnl は含めない


def test_win_loss_stats():
    s = summarize_auto_trades(SAMPLE)
    assert s["wins"] == 3 and s["losses"] == 1
    assert s["win_rate"] == "75.0%"
    assert s["avg_win"] == round((8000 + 4500 + 1000) / 3, 2)
    assert s["avg_loss"] == -2500
    assert s["profit_factor"] == round(13500 / 2500, 2)


def test_activity_counts():
    s = summarize_auto_trades(SAMPLE)
    assert s["buys"] == 2  # FILLED の BUY のみ
    assert s["failed_orders"] == 1
    assert len(s["recent_closes"]) == 4


def test_empty_is_safe():
    s = summarize_auto_trades([])
    assert s["closed_trades"] == 0
    assert s["realized_pnl"] == 0
    assert s["win_rate"] is None
    assert s["profit_factor"] is None

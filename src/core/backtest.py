#!/usr/bin/env python3
"""バックテスト・推奨検証スクリプト

Usage:
  python scripts/backtest.py                     # 全推奨の検証
  python scripts/backtest.py --days 5             # 5日後の結果を検証
  python scripts/backtest.py --min-score 20       # スコア20以上の銘柄のみ
  python scripts/backtest.py --ticker SLB         # 特定銘柄のみ

SQLite の analyses テーブルから過去の推奨を取得し、
実際のその後の値動きと比較して的中率・リターンを算出する。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SRC_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = SRC_DIR.parent
sys.path.insert(0, str(SRC_DIR))

from infra.container import get_container


def get_past_analyses(min_score: int | None = None, ticker: str | None = None):
    """SQLite から過去の分析データを取得"""
    container = get_container()
    results = container.analysis_db().get_past_analyses()

    if min_score is not None:
        results = [r for r in results if r["score"] is not None and abs(r["score"]) >= min_score]
    if ticker:
        results = [r for r in results if r["ticker"] == ticker]

    return results


def verify_recommendation(rec: dict, days: int) -> dict | None:
    """推奨の実際の結果を検証する"""
    try:
        md = get_container().market_data()
        hist = md.get_price_history(rec["ticker"], period="3mo", interval="1d")
        if hist.empty or len(hist) < 5:
            return None

        close = hist["Close"]
        dates = hist.index

        rec_date = rec["date"]
        rec_price = rec["price"]
        if rec_price is None:
            return None

        # 推奨日のインデックスを探す（推奨日以降の最も近い取引日）
        date_strs = [str(d.date()) for d in dates]
        start_idx = None
        for i, ds in enumerate(date_strs):
            if ds >= rec_date:
                start_idx = i
                break

        if start_idx is None:
            return None

        # N取引日後のデータがあるか
        end_idx = start_idx + days
        if end_idx >= len(dates):
            return None

        price_after = float(close.iloc[end_idx])
        actual_return = (price_after / rec_price - 1) * 100

        # 推奨方向の判定
        score = rec["score"] or 0
        if score > 0:
            direction = "買い"
            correct = actual_return > 0
        elif score < 0:
            direction = "売り"
            correct = actual_return < 0
        else:
            direction = "中立"
            correct = abs(actual_return) < 2

        # 期間中の最大上昇・最大下落
        period_prices = [float(close.iloc[i]) for i in range(start_idx, end_idx + 1)]
        max_gain = max((p / rec_price - 1) * 100 for p in period_prices)
        max_loss = min((p / rec_price - 1) * 100 for p in period_prices)

        return {
            "ticker": rec["ticker"],
            "name": rec["name"],
            "date": rec_date,
            "score": score,
            "action": rec["action"],
            "direction": direction,
            "entry_price": rec_price,
            "price_after": round(price_after, 2),
            "return_pct": round(actual_return, 2),
            "max_gain_pct": round(max_gain, 2),
            "max_loss_pct": round(max_loss, 2),
            "correct": correct,
        }
    except Exception:
        return None


def run_backtest(
    days: int = 5, min_score: int = 0, ticker: str | None = None
) -> dict[str, Any]:
    """バックテストを実行して集計結果を返す。"""
    analyses = get_past_analyses(min_score=min_score or None, ticker=ticker)
    results = []
    for rec in analyses:
        verified = verify_recommendation(rec, days)
        if verified:
            results.append(verified)

    if not results:
        return {"summary": {"total": 0}, "details": []}

    correct = sum(1 for r in results if r["correct"])
    returns = [r["return_pct"] for r in results]
    summary = {
        "total": len(results),
        "correct": correct,
        "accuracy": round(correct / len(results) * 100, 1),
        "avg_return": round(sum(returns) / len(returns), 2),
        "max_return": round(max(returns), 2),
        "min_return": round(min(returns), 2),
    }
    return {"summary": summary, "details": results}

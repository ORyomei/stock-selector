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
import argparse
import json
import sqlite3
import sys
from pathlib import Path

import yfinance as yf

DB_PATH = Path(__file__).parent.parent / "data" / "stock_analysis.db"


def get_past_analyses(min_score: int | None = None, ticker: str | None = None):
    """SQLite から過去の分析データを取得"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    query = "SELECT * FROM analyses ORDER BY date ASC"
    rows = cur.execute(query).fetchall()
    conn.close()

    results = [dict(r) for r in rows]

    if min_score is not None:
        results = [r for r in results if r["score"] is not None and abs(r["score"]) >= min_score]
    if ticker:
        results = [r for r in results if r["ticker"] == ticker]

    return results


def verify_recommendation(rec: dict, days: int) -> dict | None:
    """推奨の実際の結果を検証する"""
    try:
        t = yf.Ticker(rec["ticker"])
        hist = t.history(period="3mo", interval="1d")
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


def main():
    parser = argparse.ArgumentParser(description="バックテスト・推奨検証")
    parser.add_argument("--days", type=int, default=5, help="検証期間（日数）")
    parser.add_argument("--min-score", type=int, default=0, help="最小スコア絶対値")
    parser.add_argument("--ticker", type=str, default=None, help="特定銘柄のみ検証")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print("ERROR: データベースが見つかりません", file=sys.stderr)
        sys.exit(1)

    analyses = get_past_analyses(
        min_score=args.min_score if args.min_score > 0 else None,
        ticker=args.ticker,
    )

    if not analyses:
        print("検証対象のデータがありません", file=sys.stderr)
        sys.exit(1)

    print(f"検証中... {len(analyses)} 件の推奨を {args.days} 日後と比較", file=sys.stderr)

    results = []
    for rec in analyses:
        v = verify_recommendation(rec, args.days)
        if v:
            results.append(v)

    if not results:
        print("検証可能なデータがありませんでした（推奨日からの日数不足の可能性）",
              file=sys.stderr)
        sys.exit(1)

    # ---- 統計サマリー ----
    total = len(results)
    correct_count = sum(1 for r in results if r["correct"])
    accuracy = round(correct_count / total * 100, 1) if total > 0 else 0

    buy_results = [r for r in results if r["direction"] == "買い"]
    sell_results = [r for r in results if r["direction"] == "売り"]

    buy_accuracy = (round(sum(1 for r in buy_results if r["correct"]) / len(buy_results) * 100, 1)
                    if buy_results else None)
    sell_accuracy = (round(sum(1 for r in sell_results if r["correct"]) / len(sell_results) * 100, 1)
                     if sell_results else None)

    avg_return = round(sum(r["return_pct"] for r in results) / total, 2) if total > 0 else 0

    # スコア帯別の的中率
    score_bands = {}
    for r in results:
        abs_score = abs(r["score"])
        if abs_score >= 30:
            band = "30+"
        elif abs_score >= 20:
            band = "20-29"
        elif abs_score >= 10:
            band = "10-19"
        else:
            band = "0-9"

        if band not in score_bands:
            score_bands[band] = {"total": 0, "correct": 0, "returns": []}
        score_bands[band]["total"] += 1
        if r["correct"]:
            score_bands[band]["correct"] += 1
        score_bands[band]["returns"].append(r["return_pct"])

    band_stats = {}
    for band, data in sorted(score_bands.items()):
        band_stats[band] = {
            "total": data["total"],
            "correct": data["correct"],
            "accuracy": round(data["correct"] / data["total"] * 100, 1) if data["total"] > 0 else 0,
            "avg_return": round(sum(data["returns"]) / len(data["returns"]), 2)
            if data["returns"] else 0,
        }

    summary = {
        "verification_period_days": args.days,
        "total_recommendations": total,
        "verified": len(results),
        "overall_accuracy": f"{accuracy}%",
        "avg_return": f"{avg_return}%",
        "buy_accuracy": f"{buy_accuracy}%" if buy_accuracy is not None else "N/A",
        "sell_accuracy": f"{sell_accuracy}%" if sell_accuracy is not None else "N/A",
        "by_score_band": band_stats,
    }

    output = {
        "summary": summary,
        "details": results,
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

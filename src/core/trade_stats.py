"""実測期待値の集計 (diary/trades ベース)。

エージェントのプロンプト注入 (agents) とダッシュボード (web) が共有する
純集計ロジック。LLM が生成する散文の教訓と違い、反論しにくいハードな
数字をそのまま判断材料にする。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

SRC_DIR = Path(__file__).resolve().parent.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from infra.container import get_container

# ── バケット関数 (純関数) ────────────────────────────────────────


def close_reason_bucket(reason: str) -> str:
    r = reason.lower()
    if "stop_loss" in r or "損切" in reason:
        return "stop_loss"
    if "take_profit" in r or "利確" in reason:
        return "take_profit"
    if "trailing" in r:
        return "trailing"
    if "max_hold" in r or "hold_timeout" in r:
        return "max_hold"
    if "manual" in r:
        return "manual/ai_exit"
    return "other"


def hold_bucket(t: dict[str, Any]) -> str:
    hd = t.get("hold_days")
    if not isinstance(hd, int | float):
        return "不明"
    if hd <= 0:
        return "当日"
    if hd <= 3:
        return "1-3日"
    if hd <= 10:
        return "4-10日"
    return "11日+"


def score_bucket(t: dict[str, Any]) -> str:
    s = t.get("entry_score")
    if not isinstance(s, int | float):
        return "不明"
    if s < 25:
        return "score<25"
    if s <= 40:
        return "score25-40"
    return "score>40"


def confidence_bucket(t: dict[str, Any]) -> str:
    c = t.get("entry_confidence")
    if not isinstance(c, int | float):
        return "不明"
    if c < 0.6:
        return "conf<0.6"
    if c <= 0.7:
        return "conf0.6-0.7"
    return "conf>0.7"


def join_entry_meta(
    closes: list[dict[str, Any]], entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """各クローズに、同一ティッカーの直近先行エントリーの score/confidence を付与 (純関数)。"""
    ents = sorted(entries, key=lambda e: str(e.get("timestamp", "")))
    joined: list[dict[str, Any]] = []
    for c in closes:
        cts = str(c.get("timestamp", ""))
        best = None
        for e in ents:
            if e.get("ticker") == c.get("ticker") and str(e.get("timestamp", "")) <= cts:
                best = e  # ents は昇順なので最後にマッチしたものが直近
        d = dict(c)
        if best is not None:
            d["entry_score"] = best.get("score")
            d["entry_confidence"] = best.get("confidence")
        joined.append(d)
    return joined


def bucket_stats(items: list[dict[str, Any]], key_fn: Any) -> dict[str, dict[str, Any]]:
    """バケット別の件数・勝率・合計/平均損益 (純関数)。"""
    buckets: dict[str, dict[str, float]] = {}
    for t in items:
        pnl = t.get("pnl")
        if not isinstance(pnl, int | float):
            continue
        b = key_fn(t)
        d = buckets.setdefault(b, {"count": 0, "wins": 0, "total": 0.0})
        d["count"] += 1
        d["wins"] += 1 if pnl > 0 else 0
        d["total"] += float(pnl)
    return {
        b: {
            "count": int(d["count"]),
            "win_rate": round(d["wins"] / d["count"] * 100, 1),
            "total_pnl": round(d["total"]),
            "avg_pnl": round(d["total"] / d["count"]),
        }
        for b, d in buckets.items()
        if d["count"]
    }


# ── 集計エントリポイント ─────────────────────────────────────────


def load_closes_entries(days: int = 120) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """diary/trades からクローズ約定とエントリー約定を読み出す。"""
    trades = get_container().diary().load_recent_trades(days=days)
    closes = [
        t
        for t in trades
        if str(t.get("action", "")).upper() in ("CLOSE", "SELL")
        and isinstance(t.get("pnl"), int | float)
    ]
    entries = [t for t in trades if str(t.get("action", "")).upper() == "BUY"]
    return closes, entries


def expectancy(days: int = 120) -> dict[str, Any]:
    """理由別・保有期間別・スコア帯別・confidence帯別の実測期待値。"""
    closes, entries = load_closes_entries(days)
    joined = join_entry_meta(closes, entries)
    return {
        "by_reason": bucket_stats(
            closes, lambda t: close_reason_bucket(str(t.get("reason", "")))
        ),
        "by_hold": bucket_stats(closes, hold_bucket),
        "by_score": bucket_stats(joined, score_bucket),
        "by_confidence": bucket_stats(joined, confidence_bucket),
        "n_closes": len(closes),
    }


def format_for_prompt(stats: dict[str, Any], min_count: int = 3) -> str:
    """期待値統計をプロンプト注入用の簡潔なテキストに整形する (純関数)。

    件数 min_count 未満のバケットと「不明」バケットはノイズなので省く。
    有効な行が一つもなければ空文字 (プロンプトに何も足さない)。
    """
    sections = [
        ("クローズ理由別", stats.get("by_reason", {})),
        ("保有期間別", stats.get("by_hold", {})),
        ("エントリースコア帯別", stats.get("by_score", {})),
        ("申告confidence帯別 (自分の申告の校正用)", stats.get("by_confidence", {})),
    ]
    lines: list[str] = []
    for title, buckets in sections:
        rows = [
            f"  - {b}: {v['count']}件 勝率{v['win_rate']}% 平均¥{v['avg_pnl']:+,}"
            for b, v in buckets.items()
            if b != "不明" and v.get("count", 0) >= min_count
        ]
        if rows:
            lines.append(f"- {title}:")
            lines.extend(rows)
    if not lines:
        return ""
    return (
        f"直近{stats.get('n_closes', '?')}件のクローズ実績から集計した実測期待値:\n"
        + "\n".join(lines)
        + "\n指針: 期待値がマイナスのバケットに該当する判断は避けるか、"
        "上回る明確な根拠を reason に記すこと。"
    )

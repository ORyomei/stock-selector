"""振り返り学習ループ — 過去のクローズ約定を LLM に振り返らせ教訓を抽出する。

diary/trades の実現損益(クローズ約定)を集計し、LLM に「次サイクルで活かす教訓」を
簡潔にまとめさせる。得られた教訓はエントリーエージェントのシステムプロンプトに注入される。

安全性: これは助言テキストの注入のみで、トレードを直接動かさない (ゲート・リスク管理は
すべて従来どおり)。失敗・データ不足・パース不能はすべて空文字 (= 教訓なし) にフォールバック。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

SRC_DIR = Path(__file__).resolve().parent.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agents.ai import call_ai
from infra.container import get_container

_CLOSE_ACTIONS = {"CLOSE", "SELL", "close", "sell"}

# 直近クローズ集合が変わらない限り LLM を再呼び出ししないためのキャッシュ
_cache: dict[str, str] = {"key": "", "lessons": ""}


def _recent_closed_trades(days: int = 30) -> list[dict[str, Any]]:
    """diary から実現損益のあるクローズ約定を抽出する (新しい順)。"""
    trades = get_container().diary().load_recent_trades(days=days)
    closed: list[dict[str, Any]] = []
    for t in trades:
        if str(t.get("action", "")) not in _CLOSE_ACTIONS:
            continue
        pnl = t.get("pnl")
        if not isinstance(pnl, int | float):
            continue
        ts = str(t.get("timestamp", ""))
        closed.append({
            "ticker": t.get("ticker", "?"),
            "pnl": round(float(pnl), 0),
            "reason": str(t.get("reason", ""))[:60],
            "date": ts[:10],
            "ts": ts,  # フル ISO タイムスタンプ (エクイティカーブの時刻軸用)
        })
    closed.sort(key=lambda c: c["ts"] or c["date"], reverse=True)
    return closed


def _aggregate(closed: list[dict[str, Any]]) -> dict[str, Any]:
    """勝率・損益・クローズ理由分布を集計する (純関数)。"""
    if not closed:
        return {"count": 0}
    wins = [c for c in closed if c["pnl"] > 0]
    losses = [c for c in closed if c["pnl"] < 0]
    total = sum(c["pnl"] for c in closed)
    return {
        "count": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1),
        "total_pnl": round(total, 0),
        "avg_win": round(sum(c["pnl"] for c in wins) / len(wins), 0) if wins else 0,
        "avg_loss": round(sum(c["pnl"] for c in losses) / len(losses), 0) if losses else 0,
    }


def _build_prompt(stats: dict[str, Any], sample: list[dict[str, Any]]) -> str:
    import json

    return (
        "あなたはトレード戦略を改善するアナリストAIです。以下は直近のクローズ済み取引の実績です。\n\n"
        f"集計:\n```json\n{json.dumps(stats, ensure_ascii=False)}\n```\n\n"
        f"個別 (新しい順、最大20件):\n```json\n{json.dumps(sample[:20], ensure_ascii=False, indent=2)}\n```\n\n"
        "この実績から、**次のエントリー判断で活かすべき教訓を3点以内**で簡潔にまとめてください。\n"
        "勝ちパターン/負けパターン、避けるべき状況、改善点に焦点を当ててください。\n"
        "箇条書きで、全体で250字以内。前置きや一般論は不要、データに基づく具体的な教訓のみ。"
    )


def reflect_on_history(
    *,
    provider: str = "copilot",
    model: str | None = None,
    days: int = 30,
    log: Any = lambda *_: None,
) -> str:
    """過去クローズ約定を LLM に振り返らせ、教訓テキストを返す (失敗時は空文字)。"""
    try:
        closed = _recent_closed_trades(days)
        if len(closed) < 3:
            log(f"  -> 振り返り対象が不足 ({len(closed)} 件) — 教訓なし")
            return ""

        # 新規クローズが無ければ前回の教訓を再利用 (LLM 呼び出しを節約)
        cache_key = f"{len(closed)}:{closed[0]['date']}:{closed[0]['ticker']}"
        if _cache["key"] == cache_key and _cache["lessons"]:
            log("  -> 新規クローズなし — 前回の教訓を再利用")
            return _cache["lessons"]

        stats = _aggregate(closed)
        prompt = _build_prompt(stats, closed)
        text = call_ai(
            prompt, provider, model,
            system_msg="トレード実績から教訓を抽出するアナリストAI。簡潔な箇条書きのみ。",
        )
        lessons = (text or "").strip()[:600]
        if lessons:
            _cache["key"] = cache_key
            _cache["lessons"] = lessons
            log(f"  -> 教訓抽出 (勝率{stats.get('win_rate_pct')}% / {stats['count']}件):")
            for line in lessons.splitlines():
                if line.strip():
                    log(f"     {line.strip()}")
        return lessons
    except Exception as e:
        log(f"  ⚠️ 振り返りスキップ: {e}")
        return ""

"""振り返り学習ループ — 過去のクローズ約定から**数値パラメータの提案**を抽出する (issue #24)。

旧: 自由文の教訓をエントリーのシステムプロンプトに注入していたが、「損切りが緩い」→ 30 分往復 →
「損切りが遠すぎ」と両極に振れて収束しなかった (回顧録 §2-3 (5))。
新: LLM の出力を「どのパラメータを・いくつから・いくつに・どの実測を根拠に」の JSON に限定し、
diary/reflection_proposals.jsonl に記録する (適用は人間が config を変えて承認)。プロンプトには
入れない (実測期待値 = hard_stats は別途数字で入る)。

安全性: 提案の記録のみで、トレードも config も直接動かさない。失敗・データ不足・パース不能は
空 (= 提案なし) にフォールバック。
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

# 提案を受け付けるパラメータ (config/risk_limits.json のキー。これ以外は捨てる)
PROPOSABLE_PARAMS: tuple[str, ...] = (
    "default_stop_loss_pct", "default_take_profit_pct", "max_loss_per_position_pct",
    "max_hold_days_by_timespan.swing", "min_hold_business_days", "reentry_cooldown_business_days",
    "max_position_size_pct", "min_cash_pct", "max_sector_pct",
    "entry_gate.max_bb_position_pct", "entry_gate.max_rsi", "entry_gate.min_score",
)

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
        "あなたはトレード戦略のパラメータを調整するアナリストAIです。以下は直近のクローズ済み取引の実績です。\n\n"
        f"集計:\n```json\n{json.dumps(stats, ensure_ascii=False)}\n```\n\n"
        f"個別 (新しい順、最大20件):\n```json\n{json.dumps(sample[:20], ensure_ascii=False, indent=2)}\n```\n\n"
        "この実績から、**数値パラメータの変更提案を最大 3 件**、JSON のみで出力してください。\n"
        f"提案できる param: {', '.join(PROPOSABLE_PARAMS)}\n"
        "自由文の教訓・一般論・根拠の無い提案は不要。提案が無ければ proposals を空配列にすること。\n"
        '```json\n{"proposals": [{"param": "default_stop_loss_pct", "current": 3, "proposed": 4, '
        '"evidence": "stop_loss 12 件の売却後 5 日中央値 -1.8% (続落) ..."}]}\n```'
    )


def _parse_proposals(text: str | None) -> list[dict[str, Any]]:
    """LLM 応答から有効な提案だけを取り出す (純関数)。param は許可リスト、数値は float。"""
    from agents.ai import parse_ai_json

    parsed = parse_ai_json(text)
    if not parsed or not isinstance(parsed.get("proposals"), list):
        return []
    out: list[dict[str, Any]] = []
    for p in parsed["proposals"]:
        if len(out) >= 3:
            break
        if not isinstance(p, dict):
            continue
        param = str(p.get("param", "")).strip()
        if param not in PROPOSABLE_PARAMS:
            continue
        try:
            cur = float(p.get("current"))
            prop = float(p.get("proposed"))
        except (TypeError, ValueError):
            continue
        if cur != cur or prop != prop:
            continue
        out.append({
            "param": param, "current": cur, "proposed": prop,
            "evidence": str(p.get("evidence", "")).strip()[:200],
        })
    return out


def _record_proposals(proposals: list[dict[str, Any]], stats: dict[str, Any]) -> None:
    """diary/reflection_proposals.jsonl に追記 (人間がレビューして config に反映する)。"""
    import json
    from datetime import UTC, datetime

    from infra.container import DIARY_DIR

    path = DIARY_DIR / "reflection_proposals.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now(UTC).isoformat(), "stats": stats, "proposals": proposals}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def reflect_on_history(
    *,
    provider: str = "claude_code",
    model: str | None = None,
    days: int = 30,
    log: Any = lambda *_: None,
) -> str:
    """過去クローズ約定から数値パラメータの提案を抽出して記録する。

    戻り値は常に空文字 (プロンプトに自由文を入れない。issue #24)。提案は
    diary/reflection_proposals.jsonl とログに残る。失敗時も空文字。
    """
    try:
        closed = _recent_closed_trades(days)
        if len(closed) < 3:
            log(f"  -> 振り返り対象が不足 ({len(closed)} 件) — 提案なし")
            return ""

        # 新規クローズが無ければ再呼び出ししない (LLM 呼び出しを節約)
        cache_key = f"{len(closed)}:{closed[0]['date']}:{closed[0]['ticker']}"
        if _cache["key"] == cache_key:
            log("  -> 新規クローズなし — 提案の再抽出は省略")
            return ""

        stats = _aggregate(closed)
        prompt = _build_prompt(stats, closed)
        text = call_ai(
            prompt, provider, model,
            system_msg="トレード実績から数値パラメータの変更提案を出すアナリストAI。JSON のみで回答。",
        )
        proposals = _parse_proposals(text)
        _cache["key"] = cache_key
        _cache["lessons"] = ""
        _record_proposals(proposals, stats)
        if proposals:
            log(f"  -> パラメータ提案 {len(proposals)} 件 (勝率{stats.get('win_rate_pct')}% / {stats['count']}件、人間承認待ち):")
            for p in proposals:
                log(f"     {p['param']}: {p['current']:g} → {p['proposed']:g} — {p['evidence']}")
        else:
            log(f"  -> パラメータ提案なし (勝率{stats.get('win_rate_pct')}% / {stats['count']}件)")
        return ""
    except Exception as e:
        log(f"  ⚠️ 振り返りスキップ: {e}")
        return ""

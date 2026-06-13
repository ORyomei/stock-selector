"""AI 手仕舞い助言 (機械ストップは床として維持)。

機械的な損切り/利確/トレーリング/最大保有 (RiskManager.should_close_position) は
別途必ず先に実行される「床」。本モジュールはそれを通過して残った保有ポジションに対し、
LLM の判断で「より早い手仕舞い」だけを助言する:

- action="exit": 全株クローズ (テーゼ崩壊・テクニカル悪化・悪材料 等)
- action="trim": 半分クローズ (利益確保・リスク低減)
- action="hold": 何もしない (= 現状維持)

安全性:
- AI は機械ストップを止められない (ここは未発火ポジションのみ対象)
- 保有していないティッカー・不正な action は却下
- LLM 応答のパース失敗/タイムアウトは「助言なし」= 機械ストップのみ (フェイルセーフ)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

SRC_DIR = Path(__file__).resolve().parent.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agents.ai import call_ai, parse_ai_json

_VALID_ACTIONS = {"exit", "trim", "hold"}


def _safe(fn, *args, **kwargs):
    """補助データ取得は失敗しても None を返す (助言を止めない)。

    core の分析関数は JSON を stdout に出力するため、ログ汚染を避けて抑制する。
    """
    import contextlib
    import io

    try:
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*args, **kwargs)
    except Exception:
        return None


def _position_context(pos: Any) -> dict[str, Any]:
    """1ポジションの判断材料を集める (建玉facts + テクニカル + センチメント)。"""
    from datetime import UTC, datetime

    ticker = pos.ticker
    entry = float(pos.entry_price or 0)
    current = float(pos.current_price or 0)
    pnl_pct = ((current - entry) / entry * 100) if entry else 0.0
    peak = float(pos.peak_price or entry or current)
    drawdown_from_peak = ((current - peak) / peak * 100) if peak else 0.0
    days_held = (datetime.now(UTC) - pos.entry_time).days if pos.entry_time else None

    ctx: dict[str, Any] = {
        "ticker": ticker,
        "entry_price": round(entry, 2),
        "current_price": round(current, 2),
        "pnl_pct": round(pnl_pct, 2),
        "peak_price": round(peak, 2),
        "drawdown_from_peak_pct": round(drawdown_from_peak, 2),
        "days_held": days_held,
        "stop_loss": pos.stop_loss,
        "take_profit": pos.take_profit,
    }

    # テクニカル概況 (スコア・アクション・主要指標)
    def _score():
        from core.scorer import compute_score

        return compute_score(ticker)

    score = _safe(_score)
    if score:
        summary = score.get("analysis_summary", {})
        ctx["technical"] = {
            "score": summary.get("total_score"),
            "action": summary.get("action"),
            "returns": score.get("returns"),
            "volatility": (score.get("volatility") or {}).get("年率換算ボラティリティ"),
        }

    # ニュースセンチメント (悪材料の検知)
    def _sent():
        from core.sentiment import run_sentiment

        return run_sentiment(ticker)

    sent = _safe(_sent)
    if sent:
        ctx["sentiment"] = {
            "label": sent.get("sentiment") or sent.get("label"),
            "score": sent.get("score"),
        }

    return ctx


def _build_prompt(contexts: list[dict[str, Any]], scenario: str) -> str:
    import json

    return (
        "あなたはリスク管理に長けたトレーダーAIです。以下は現在の保有ポジションです。\n"
        "機械的な損切り・利確・トレーリング・最大保有日数のルールは既に適用済みで、\n"
        "それらに該当しなかった (=ルール上は保有継続でよい) ポジションだけがここにあります。\n\n"
        f"現在の市場シナリオ: {scenario}\n\n"
        f"保有ポジション:\n```json\n{json.dumps(contexts, ensure_ascii=False, indent=2)}\n```\n\n"
        "各ポジションについて、機械ルールより**早く手仕舞うべき理由があるか**だけを判断してください。\n"
        "判断基準の例: 投資テーゼの崩壊、テクニカルの明確な悪化 (売り転換)、強い悪材料、\n"
        "高値から大きく押し戻されている、強い抵抗線でモメンタム喪失 など。\n"
        "**デフォルトは hold** とし、明確な根拠がある時だけ exit / trim を選んでください。\n"
        "（あなたは機械ストップを止めることはできません。より早い手仕舞いの助言のみ可能です。）\n\n"
        "次の JSON のみを出力してください:\n"
        '```json\n{"exits": [{"ticker": "XXXX.T", "action": "exit|trim|hold", '
        '"reason": "簡潔な理由"}]}\n```'
    )


def _parse_exit_response(text: str | None, held_tickers: set[str]) -> list[dict[str, Any]]:
    """LLM 応答から実行可能な手仕舞い助言だけを抽出・検証する (純関数)。

    - action は exit / trim のみ採用 (hold はノーオペなので除外)
    - 保有していないティッカーは却下 (ハルシネーション対策)
    """
    parsed = parse_ai_json(text)
    if not parsed or not isinstance(parsed.get("exits"), list):
        return []

    actions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for e in parsed["exits"]:
        if not isinstance(e, dict):
            continue
        ticker = str(e.get("ticker", "")).strip()
        action = str(e.get("action", "")).strip().lower()
        if action not in _VALID_ACTIONS or action == "hold":
            continue
        if ticker not in held_tickers or ticker in seen:
            continue
        seen.add(ticker)
        actions.append({
            "ticker": ticker,
            "action": action,
            "reason": str(e.get("reason", "")).strip()[:200],
        })
    return actions


def advise_exits(
    positions: list[Any],
    scenario: str,
    *,
    provider: str = "copilot",
    model: str | None = None,
    log: Any = lambda *_: None,
) -> list[dict[str, Any]]:
    """保有ポジションに対する AI 手仕舞い助言を返す (exit / trim のみ)。

    実行はしない (呼び出し側が dry_run 等を考慮して執行する)。失敗時は空リスト。
    """
    if not positions:
        return []

    held_tickers = {p.ticker for p in positions}
    try:
        contexts = [_position_context(p) for p in positions]
        prompt = _build_prompt(contexts, scenario)
        text = call_ai(
            prompt,
            provider,
            model,
            system_msg="保有ポジションの早期手仕舞いを助言するAI。JSONのみで回答。",
        )
        actions = _parse_exit_response(text, held_tickers)
        if actions:
            for a in actions:
                log(f"  🤖 AI手仕舞い助言: {a['action'].upper()} {a['ticker']} — {a['reason']}")
        else:
            log("  🤖 AI手仕舞い助言: 早期手仕舞い推奨なし (全て保有継続)")
        return actions
    except Exception as e:
        log(f"  ⚠️ AI手仕舞い助言スキップ (機械ストップのみ): {e}")
        return []

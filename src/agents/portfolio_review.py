"""ポートフォリオ単位の AI 推論 — セクター/テーマ集中を抑制する。

エントリー候補を実行する前に、現在の保有 + 提案中の新規買いを俯瞰し、特定セクターや
テーマへ過集中する候補を AI が drop 助言する。機械的な集中度上限 (30%/銘柄) を補う
定性的な分散チェック。

安全性: drop は明確な過集中時のみ (デフォルト keep)。失敗・パース不能・無効化時は
全シグナル維持 (フェイルセーフ)。保有外/候補外ティッカーの判定は無視。
"""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path
from typing import Any

SRC_DIR = Path(__file__).resolve().parent.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agents.ai import call_ai, parse_ai_json
from infra.container import get_container

USD_JPY = 150.0
_SECTOR_CACHE: dict[str, str] = {}


def _sector(ticker: str) -> str:
    """銘柄のセクター (yfinance, キャッシュ)。ETF等で取れなければ「分散/ETF」。"""
    if ticker in _SECTOR_CACHE:
        return _SECTOR_CACHE[ticker]
    from core.etf import is_etf

    if is_etf(ticker):
        _SECTOR_CACHE[ticker] = "分散/ETF"
        return "分散/ETF"  # ETF は quoteSummary を叩かない (404 の無駄撃ち防止)
    sec = "不明"
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            info = get_container().market_data().get_ticker_info(ticker) or {}
        sec = info.get("sector") or "分散/ETF"
    except Exception:
        pass
    _SECTOR_CACHE[ticker] = sec
    return sec


def _value_jpy(ticker: str, price: float, qty: int) -> float:
    val = price * qty
    return val if ticker.endswith(".T") else val * USD_JPY


def _parse_review(text: str | None, candidate_tickers: set[str]) -> dict[str, str]:
    """LLM 応答から drop 対象のティッカー→理由を抽出する (純関数)。

    action が drop のもののみ採用 (keep はデフォルトなので無視)。候補外は無視。
    """
    parsed = parse_ai_json(text)
    if not parsed or not isinstance(parsed.get("decisions"), list):
        return {}
    drops: dict[str, str] = {}
    for d in parsed["decisions"]:
        if not isinstance(d, dict):
            continue
        ticker = str(d.get("ticker", "")).strip()
        action = str(d.get("action", "")).strip().lower()
        if action == "drop" and ticker in candidate_tickers:
            drops[ticker] = str(d.get("reason", "")).strip()[:160]
    return drops


def _enabled() -> bool:
    try:
        from core.trade import load_config

        return bool(load_config().get("ai_portfolio_review", False))
    except Exception:
        return False


def review_portfolio(
    signals: list[dict[str, Any]],
    *,
    provider: str = "copilot",
    model: str | None = None,
    log: Any = lambda *_: None,
) -> list[dict[str, Any]]:
    """過集中する新規買い候補を drop してフィルタ済みシグナルを返す。"""
    if not signals or not _enabled():
        return signals
    try:
        broker = get_container().broker()
        bal = broker.get_balance()
        equity = float(bal.get("cash_jpy", 0) or 0) + float(bal.get("cash_usd", 0) or 0) * USD_JPY
        positions = broker.get_positions()
        pos_vals = [(p, _value_jpy(p.ticker, p.current_price, p.quantity)) for p in positions]
        equity += sum(v for _, v in pos_vals)
        if equity <= 0:
            return signals

        holdings = [
            {"ticker": p.ticker, "sector": _sector(p.ticker), "weight_pct": round(v / equity * 100, 1)}
            for p, v in pos_vals
        ]
        cand_tickers = {str(s.get("ticker", "")) for s in signals}
        candidates = [
            {"ticker": s.get("ticker"), "sector": _sector(str(s.get("ticker", ""))),
             "score": s.get("score"), "reason": (s.get("reason") or "")[:80]}
            for s in signals
        ]

        import json

        prompt = (
            "あなたはポートフォリオ全体のリスクを管理するAIです。\n"
            f"現在の保有（セクター・総資産比%）:\n```json\n{json.dumps(holdings, ensure_ascii=False)}\n```\n"
            f"提案中の新規買い候補:\n```json\n{json.dumps(candidates, ensure_ascii=False)}\n```\n\n"
            "新規買いを加えると**特定セクター/テーマに過集中**しないかを評価してください。\n"
            "各候補について keep / drop を判定。**デフォルトは keep**、同一セクターが既に大きい等の"
            "明確な過集中リスクがある候補だけ drop してください (銘柄単位の上限は別途機械的に管理済み)。\n"
            '次のJSONのみ出力:\n```json\n{"decisions": [{"ticker": "XXXX.T", "action": "keep|drop", "reason": "..."}]}\n```'
        )
        text = call_ai(prompt, provider, model, system_msg="ポートフォリオ分散を管理するAI。JSONのみ。")
        drops = _parse_review(text, cand_tickers)
        if not drops:
            log("  🧭 ポートフォリオ推論: 過集中なし (全候補維持)")
            return signals
        kept = [s for s in signals if str(s.get("ticker", "")) not in drops]
        for t, reason in drops.items():
            log(f"  🧭 ポートフォリオ推論: {t} を除外 (過集中) — {reason}")
        return kept
    except Exception as e:
        log(f"  ⚠️ ポートフォリオ推論スキップ (全候補維持): {e}")
        return signals

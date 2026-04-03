#!/usr/bin/env python3
"""Automated trading loop — screen, score, (optionally) AI-judge, and execute.

Usage examples::

    # Dry-run (no orders placed)
    python3 scripts/auto_trade.py --dry-run

    # Live run with AI judgment (default: copilot)
    python3 scripts/auto_trade.py --ai

    # Daemon mode — repeat every 10 minutes
    python3 scripts/auto_trade.py --ai --daemon --interval 600

    # Japanese market only
    python3 scripts/auto_trade.py --ai --market jp
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# --- paths ----------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
DIARY_DIR = PROJECT_DIR / "diary"
SIGNALS_DIR = DIARY_DIR / "signals"
TRADES_DIR = DIARY_DIR / "trades"
JST = timezone(timedelta(hours=9))

# --- shared helpers (from lib/) -------------------------------------------

sys.path.insert(0, str(SCRIPT_DIR))
from lib.ai import PROVIDER_NAMES, call_ai, parse_ai_json  # noqa: E402
from lib.portfolio import (  # noqa: E402
    confidence_to_float,
    count_positions,
    get_held_positions,
    get_held_tickers,
    get_max_positions,
)
from lib.runner import run_script, run_trade_cmd  # noqa: E402

# --- constants ------------------------------------------------------------

MARKET_LABELS: dict[str, str] = {"us": "米国株", "jp": "日本株", "all": "全市場"}
EXTREME_BEARISH_THRESHOLD = -30
MIN_SWAP_SCORE_DIFF_RULE = 20
MIN_SWAP_SCORE_DIFF_AI = 5


# ── helpers ───────────────────────────────────────────────────────────────────


def score_ticker(ticker: str) -> dict[str, Any] | None:
    """Score a single ticker via ``scorer.py`` and return a summary dict."""
    scorer = run_script("scorer.py", [ticker])
    if not scorer:
        return None
    summary = scorer.get("analysis_summary", {})
    price = scorer.get("current_price")
    if price is None or str(price) == "nan":
        return None
    return {
        "ticker": ticker,
        "score": summary.get("total_score", 0),
        "action": summary.get("action", ""),
        "confidence": summary.get("confidence", "低"),
        "current_price": price,
        "probability": scorer.get("probability", {}),
        "risk_management": scorer.get("risk_management", {}),
        "volatility": scorer.get("volatility", {}),
    }


def extract_candidates(
    screener_result: dict[str, Any],
    held_tickers: set[str],
) -> list[dict[str, Any]]:
    """Deduplicate and rank candidates from screener output, excluding held."""
    if not screener_result or "results" not in screener_result:
        return []
    seen: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for strategy, items in screener_result["results"].items():
        for c in items:
            ticker = c["ticker"]
            if ticker in seen or ticker in held_tickers:
                continue
            seen.add(ticker)
            candidates.append(
                {
                    "ticker": ticker,
                    "name": c.get("name", ""),
                    "strategy": strategy,
                    "screener_score": c.get("score", 0),
                }
            )
    candidates.sort(key=lambda x: x["screener_score"], reverse=True)
    return candidates


def _save_log(
    file_ts: str,
    lines: list[str],
    market: str,
    *,
    ai_used: bool = False,
) -> None:
    TRADES_DIR.mkdir(parents=True, exist_ok=True)
    ml = MARKET_LABELS.get(market, market)
    tag = "_AI" if ai_used else ""
    path = TRADES_DIR / f"{file_ts}_auto_trade{tag}_{ml}.md"
    content = f"# 自動売買ログ — {ml}\n\n" + "\n".join(lines) + "\n"
    path.write_text(content, encoding="utf-8")
    print(f"  log: {path}")


# ── AI prompts ────────────────────────────────────────────────────────────────


def _build_buy_prompt(
    candidates: list[dict[str, Any]],
    macro: dict[str, Any] | None,
    market: str,
) -> str:
    data = json.dumps(candidates, ensure_ascii=False, indent=2, default=str)
    macro_j = json.dumps(macro, ensure_ascii=False, indent=2, default=str) if macro else "{}"
    ml = MARKET_LABELS.get(market, market)
    return (
        f"自動売買の最終判断。各候補に buy/skip を判定。\n\n"
        f"市場: {ml}\nマクロ環境: {macro_j}\n候補: {data}\n\n"
        "判断ルール:\n"
        "- デッドキャットバウンス、出来高なしの上昇、過度なボラティリティを見抜く\n"
        "- 各候補に buy/skip を判定、理由を明記\n\n"
        '出力（JSONのみ）:\n{"decisions": [{"ticker": "X", "decision": "buy", '
        '"confidence": 0.8, "reason": "..."}], "market_comment": "..."}'
    )


def _build_swap_prompt(
    held: list[dict[str, Any]],
    new: list[dict[str, Any]],
    macro: dict[str, Any] | None,
    market: str,
) -> str:
    held_j = json.dumps(held, ensure_ascii=False, indent=2, default=str)
    new_j = json.dumps(new, ensure_ascii=False, indent=2, default=str)
    macro_j = json.dumps(macro, ensure_ascii=False, indent=2, default=str) if macro else "{}"
    ml = MARKET_LABELS.get(market, market)
    return (
        f"ポートフォリオ入れ替え判断。枠満杯のため保有と候補を比較。\n\n"
        f"市場: {ml}\nマクロ環境: {macro_j}\n\n保有: {held_j}\n候補: {new_j}\n\n"
        "判断ルール:\n"
        "- 新規候補が保有より明確にスコア・確率が高い場合のみ swap\n"
        "- 僅差なら手数料を考慮し hold\n"
        "- 1サイクルで入れ替えは最大2件\n\n"
        '出力（JSONのみ）:\n{"recommendation": "swap"|"hold", "swaps": '
        '[{"sell": "OLD", "sell_reason": "...", "buy": "NEW", "buy_reason": "..."}], '
        '"overall_reason": "...", "confidence": 0.7}'
    )


# ── signal execution ──────────────────────────────────────────────────────────


def _execute_signals(
    signals: list[dict[str, Any]],
    dry_run: bool,
    log: Any,
) -> list[dict[str, Any]]:
    executed: list[dict[str, Any]] = []
    if dry_run:
        for sig in signals:
            log(f"  [DRY] BUY {sig['ticker']} score={sig['score']}")
            executed.append({"ticker": sig["ticker"], "status": "DRY_RUN", "score": sig["score"]})
        return executed

    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    for sig in signals:
        sig_path = SIGNALS_DIR / f"{sig['ticker'].replace('.', '')}_auto.json"
        sig_path.write_text(json.dumps(sig, ensure_ascii=False, indent=2), encoding="utf-8")
        out, rc = run_trade_cmd(["--from-signal", str(sig_path)])
        ok = rc == 0 and "FILLED" in out
        log(f"  {'✅' if ok else '❌'} {sig['ticker']} {'約定成功' if ok else '約定失敗'}")
        executed.append(
            {"ticker": sig["ticker"], "status": "FILLED" if ok else "FAILED", "score": sig["score"]}
        )
    return executed


def _make_signal(info: dict[str, Any], reason: str) -> dict[str, Any]:
    risk = info.get("risk_management", {})
    price = info["current_price"]
    return {
        "ticker": info["ticker"],
        "action": "buy",
        "entry_price": 0,
        "target_price": risk.get("利確目標1（ATR×2）", price * 1.05),
        "stop_loss_price": risk.get("損切りライン", price * 0.97),
        "take_profit_price": risk.get("利確目標2（ATR×4）", price * 1.10),
        "confidence": confidence_to_float(info.get("confidence", "中")),
        "timespan": "swing",
        "score": info["score"],
        "reason": reason,
    }


# ── swap logic ────────────────────────────────────────────────────────────────


def _run_swap_evaluation(
    candidates: list[dict[str, Any]],
    macro: dict[str, Any] | None,
    market: str,
    max_signals: int,
    dry_run: bool,
    use_ai: bool,
    ai_provider: str,
    ai_model: str | None,
    log: Any,
) -> list[dict[str, Any]]:
    """Evaluate whether to swap held positions with better candidates."""
    executed: list[dict[str, Any]] = []
    positions = get_held_positions()

    # Score held positions
    log("\nStep 4a: 保有銘柄スコアリング...")
    held_scored: list[dict[str, Any]] = []
    for pos in positions:
        ticker = pos["ticker"]
        log(f"  -> {ticker}...")
        result = score_ticker(ticker)
        if result:
            result.update(
                quantity=pos.get("quantity", 0),
                entry_price=pos.get("entry_price", 0),
                pnl_pct=pos.get("pnl_pct", 0),
            )
            log(f"    score={result['score']}, action={result['action']}")
        else:
            log("    スコアリング失敗")
            result = {
                "ticker": ticker,
                "score": 999,
                "action": "不明",
                "confidence": "中",
                "current_price": pos.get("current_price", 0),
                "quantity": pos.get("quantity", 0),
                "probability": {},
                "risk_management": {},
            }
        held_scored.append(result)

    # Score new candidates
    log("\nStep 4b: 新規候補スコアリング...")
    new_scored: list[dict[str, Any]] = []
    for cand in candidates[: max_signals * 3]:
        ticker = cand["ticker"]
        log(f"  -> {ticker} ({cand.get('name', '')})...")
        result = score_ticker(ticker)
        if not result:
            continue
        result["name"] = cand.get("name", "")
        log(f"    score={result['score']}, action={result['action']}")
        if "売り" in result["action"]:
            log(f"    判定が{result['action']} -> skip")
            continue
        new_scored.append(result)
        log("    -> 候補 ✓")

    if not new_scored:
        log("  -> 入れ替え候補なし。")
        return executed

    worst_held = min(held_scored, key=lambda x: x["score"])
    best_new = max(new_scored, key=lambda x: x["score"])
    diff = best_new["score"] - worst_held["score"]
    log(
        f"\n  比較: 保有最低 {worst_held['ticker']}(score={worst_held['score']})"
        f" vs 候補最高 {best_new['ticker']}(score={best_new['score']})"
    )

    threshold = MIN_SWAP_SCORE_DIFF_AI if use_ai else MIN_SWAP_SCORE_DIFF_RULE
    if diff < threshold:
        log(f"  -> スコア差 {diff} < {threshold}。入れ替え不要。")
        return executed

    if use_ai:
        log(f"\nStep 5: AI入れ替え判断 ({ai_provider})...")
        prompt = _build_swap_prompt(held_scored, new_scored, macro, market)
        parsed = parse_ai_json(call_ai(prompt, ai_provider, ai_model))
        if parsed and parsed.get("recommendation") == "swap" and parsed.get("swaps"):
            for swap in parsed["swaps"][:max_signals]:
                sell_t, buy_t = swap.get("sell", ""), swap.get("buy", "")
                log(f"  SWAP: {sell_t} -> {buy_t}")
                sell_pos = next((p for p in positions if p["ticker"] == sell_t), None)
                buy_info = next((n for n in new_scored if n["ticker"] == buy_t), None)
                if sell_pos and buy_info:
                    executed.extend(
                        _execute_swap(sell_t, sell_pos.get("quantity", 0), buy_info, dry_run, log)
                    )
        elif parsed:
            log("  -> AIが hold 判定。入れ替えなし。")
        else:
            log("  AI判断失敗 -> ルールベースにフォールバック")
            if diff >= MIN_SWAP_SCORE_DIFF_RULE:
                sell_pos = next((p for p in positions if p["ticker"] == worst_held["ticker"]), None)
                if sell_pos:
                    executed.extend(
                        _execute_swap(
                            worst_held["ticker"],
                            sell_pos.get("quantity", 0),
                            best_new,
                            dry_run,
                            log,
                        )
                    )
    else:
        sell_pos = next((p for p in positions if p["ticker"] == worst_held["ticker"]), None)
        if sell_pos:
            log(
                f"\n  SWAP: {worst_held['ticker']}(score={worst_held['score']})"
                f" -> {best_new['ticker']}(score={best_new['score']})"
            )
            executed.extend(
                _execute_swap(
                    worst_held["ticker"], sell_pos.get("quantity", 0), best_new, dry_run, log
                )
            )

    return executed


def _execute_swap(
    sell_ticker: str,
    sell_qty: int,
    buy_info: dict[str, Any],
    dry_run: bool,
    log: Any,
) -> list[dict[str, Any]]:
    """Sell one position and buy a replacement."""
    executed: list[dict[str, Any]] = []
    if dry_run:
        log(f"  [DRY] SELL {sell_ticker} {sell_qty}株")
        log(f"  [DRY] BUY  {buy_info['ticker']} (score={buy_info['score']})")
        executed.append({"ticker": sell_ticker, "status": "DRY_SELL", "score": 0})
        executed.append(
            {"ticker": buy_info["ticker"], "status": "DRY_BUY", "score": buy_info["score"]}
        )
        return executed

    log(f"  売り: {sell_ticker} {sell_qty}株...")
    out, rc = run_trade_cmd(["--close", sell_ticker, str(sell_qty)])
    if rc == 0 and "FILLED" in out:
        log(f"    ✅ {sell_ticker} クローズ完了")
        executed.append({"ticker": sell_ticker, "status": "SOLD", "score": 0})
    else:
        log(f"    ❌ {sell_ticker} クローズ失敗")
        executed.append({"ticker": sell_ticker, "status": "SELL_FAILED", "score": 0})
        return executed  # don't buy if sell fails

    sig = _make_signal(buy_info, f"auto_swap: {sell_ticker}->{buy_info['ticker']}")
    log(f"  買い: {buy_info['ticker']}...")
    executed.extend(_execute_signals([sig], False, log))
    return executed


# ── main cycle ────────────────────────────────────────────────────────────────


def run_cycle(
    market: str,
    min_score: int,
    max_signals: int,
    dry_run: bool,
    use_ai: bool = False,
    ai_provider: str = "copilot",
    ai_model: str | None = None,
) -> list[dict[str, Any]]:
    now = datetime.now(JST)
    file_ts = now.strftime("%Y-%m-%d_%H%M%S")
    lines: list[str] = []

    def log(msg: str) -> None:
        print(msg)
        lines.append(msg)

    log(f"\n{'=' * 60}")
    log(f"  自動売買ループ: {now:%Y-%m-%d %H:%M:%S} JST")
    log(f"  market={market}  min_score={min_score}  max_signals={max_signals}")
    log(f"  dry_run={dry_run}  ai={use_ai}" + (f" ({ai_provider})" if use_ai else ""))
    log(f"{'=' * 60}\n")

    # Step 1: auto-close
    log("Step 1: 自動クローズ判定...")
    close_out, _ = run_trade_cmd(["--check-and-close"])
    if "クローズ対象なし" in close_out:
        log("  -> クローズ対象なし")
    else:
        for line in close_out.strip().splitlines():
            if line.strip():
                log(f"  -> {line.strip()}")

    # Step 2: macro
    log("\nStep 2: マクロ環境チェック...")
    macro = run_script("macro.py")
    env_score, env_label = 0, "不明"
    if macro:
        env = macro.get("market_environment", {})
        env_score = env.get("score", 0)
        env_label = env.get("assessment", "不明")
        for s in env.get("signals", []):
            log(f"  - {s}")
        log(f"  -> 市場環境: {env_label} (スコア={env_score})")
    else:
        log("  -> マクロ取得失敗")

    if env_score <= EXTREME_BEARISH_THRESHOLD:
        log(f"\n  市場環境が極端に弱気 (score={env_score})。新規買いスキップ。")
        _save_log(file_ts, lines, market, ai_used=use_ai)
        return []

    # Step 3: screening
    log("\nStep 3: スクリーニング...")
    screener = run_script(
        "screener.py", ["--market", market, "--strategy", "all", "--top", "5"], timeout=300
    )
    if not screener:
        log("  -> スクリーニング失敗。")
        _save_log(file_ts, lines, market, ai_used=use_ai)
        return []

    held = get_held_tickers()
    cur_pos = count_positions()
    max_pos = get_max_positions()
    available = max_pos - cur_pos
    candidates = extract_candidates(screener, held)
    log(f"  -> 候補 {len(candidates)} 銘柄 (保有済み除外)")
    log(f"  -> ポジション: {cur_pos}/{max_pos} (空き: {available})")

    # Full -> swap evaluation
    if available <= 0 and candidates:
        log("\n  枠満杯 -> 入れ替え検討モードへ")
        executed = _run_swap_evaluation(
            candidates, macro, market, max_signals, dry_run, use_ai, ai_provider, ai_model, log
        )
        _save_log(file_ts, lines, market, ai_used=use_ai)
        return executed

    if available <= 0:
        log("  -> 枠満杯 & 候補なし。")
        _save_log(file_ts, lines, market, ai_used=use_ai)
        return []

    if not candidates:
        log("  -> 新規候補なし")
        _save_log(file_ts, lines, market, ai_used=use_ai)
        return []

    # Step 4: scoring
    log("\nStep 4: 候補スコアリング...")
    scored: list[dict[str, Any]] = []
    for cand in candidates:
        if len(scored) >= max_signals * 2:
            break
        ticker = cand["ticker"]
        log(f"  -> {ticker} ({cand.get('name', '')})...")
        result = score_ticker(ticker)
        if not result:
            continue
        log(f"    score={result['score']}, action={result['action']}, conf={result['confidence']}")
        if result["score"] < min_score:
            log("    スコア不足 -> skip")
            continue
        if "売り" in result["action"]:
            log(f"    判定が{result['action']} -> skip")
            continue
        scored.append(result)
        log("    -> 通過 ✓")

    if not scored:
        log("  -> スコア通過銘柄なし")
        _save_log(file_ts, lines, market, ai_used=use_ai)
        return []

    # Step 5: AI judgment (optional)
    ai_decisions: dict[str, dict[str, Any]] = {}
    if use_ai:
        log(f"\nStep 5: AI判断 ({ai_provider})...")
        parsed = parse_ai_json(
            call_ai(_build_buy_prompt(scored, macro, market), ai_provider, ai_model)
        )
        if parsed and "decisions" in parsed:
            comment = parsed.get("market_comment", "")
            if comment:
                log(f"  AI: {comment}")
            for d in parsed["decisions"]:
                t = d.get("ticker", "")
                ai_decisions[t] = d
                emoji = "✅" if d.get("decision") == "buy" else "⛔"
                log(f"  {emoji} {t}: {d.get('decision')} — {d.get('reason', '')[:80]}")
        else:
            log("  AI判断失敗 -> ルールベースにフォールバック")

    # Build signals
    signals: list[dict[str, Any]] = []
    for s in scored:
        if len(signals) >= max_signals or len(signals) >= available:
            break
        ticker = s["ticker"]
        if use_ai and ai_decisions:
            ai_d = ai_decisions.get(ticker)
            if ai_d and ai_d.get("decision") == "skip":
                log(f"  ⛔ {ticker}: AI skip -> シグナル除外")
                continue
        reason = f"auto_trade{'[AI]' if use_ai else ''}: score={s['score']}, {s['action']}"
        signals.append(_make_signal(s, reason))

    # Step 6: execute
    log(f"\n注文実行 ({len(signals)} 件)...")
    executed = _execute_signals(signals, dry_run, log)

    log(f"\n{'=' * 60}")
    log(f"  サイクル完了: {datetime.now(JST):%H:%M:%S}")
    log(f"  環境: {env_label} (score={env_score})")
    log(f"  新規注文: {len(executed)} 件")
    for e in executed:
        log(f"    {e['status']} {e['ticker']} (score={e['score']})")
    log(f"{'=' * 60}\n")

    _save_log(file_ts, lines, market, ai_used=use_ai)
    return executed


# ── daemon ────────────────────────────────────────────────────────────────────


def daemon_loop(
    market: str,
    min_score: int,
    max_signals: int,
    interval: int,
    dry_run: bool,
    use_ai: bool,
    ai_provider: str,
    ai_model: str | None,
) -> None:
    print(f"デーモンモード: {interval}s ({interval // 60}min) ごとに自動実行")
    print(f"  market={market}  min_score={min_score}  max_signals={max_signals}  dry_run={dry_run}")
    if use_ai:
        print(f"  AI: {ai_provider} (model: {ai_model or 'default'})")
    print("  Ctrl+C で停止\n")

    cycle = 0
    while True:
        cycle += 1
        print(f"\n### サイクル #{cycle} ###")
        try:
            run_cycle(market, min_score, max_signals, dry_run, use_ai, ai_provider, ai_model)
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
        print(f"\n次回: {interval}s後 ({interval // 60}min後)")
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\nデーモン停止")
            break


# ── CLI ───────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="自動売買ループ（AI判断・入れ替え対応）")
    parser.add_argument("--market", choices=["us", "jp", "all"], default="all")
    parser.add_argument("--min-score", type=int, default=10)
    parser.add_argument("--max-signals", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--ai", action="store_true", help="AI判断を有効化")
    parser.add_argument("--ai-provider", choices=PROVIDER_NAMES, default="copilot")
    parser.add_argument("--ai-model", default=None)
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--interval", type=int, default=1800, help="seconds between cycles")
    args = parser.parse_args()

    if args.daemon:
        daemon_loop(
            args.market,
            args.min_score,
            args.max_signals,
            args.interval,
            args.dry_run,
            args.ai,
            args.ai_provider,
            args.ai_model,
        )
    else:
        run_cycle(
            args.market,
            args.min_score,
            args.max_signals,
            args.dry_run,
            args.ai,
            args.ai_provider,
            args.ai_model,
        )


if __name__ == "__main__":
    main()

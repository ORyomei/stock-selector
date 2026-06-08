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
import atexit
import contextlib
import fcntl
import json
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# --- paths ----------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent
PROJECT_DIR = SRC_DIR.parent
DIARY_DIR = PROJECT_DIR / "diary"
SIGNALS_DIR = DIARY_DIR / "signals"
TRADES_DIR = DIARY_DIR / "trades"
JST = timezone(timedelta(hours=9))

# --- shared helpers (from lib/) -------------------------------------------

sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SRC_DIR))
from agents.ai import PROVIDER_NAMES, call_ai, parse_ai_json  # noqa: E402
from agents.portfolio_helpers import (  # noqa: E402
    USD_JPY_APPROX,
    confidence_to_float,
    count_positions,
    daily_loss_exceeded,
    get_held_positions,
    get_held_tickers,
    get_max_positions,
    total_equity_jpy,
    warn_overweight_positions,
)
from agents.runner import run_script, run_trade_cmd  # noqa: E402
from infra.container import get_container

# --- constants ------------------------------------------------------------

MARKET_LABELS: dict[str, str] = {"us": "米国株", "jp": "日本株", "all": "全市場"}
EXTREME_BEARISH_THRESHOLD = -30
MIN_SWAP_SCORE_DIFF_RULE = 20
MIN_SWAP_SCORE_DIFF_AI = 5

# ── lock file (排他制御) ──────────────────────────────────────────────────────

LOCK_FILE = PROJECT_DIR / ".auto_trade.lock"
DAEMON_LOG = PROJECT_DIR / "logs" / "auto_trade_daemon.log"
_lock_fd: int | None = None


def _daemonize() -> None:
    """Double-fork でプロセスをバックグラウンドに切り離す。"""
    # Pipe to communicate grandchild PID back to parent
    r_fd, w_fd = os.pipe()

    # First fork
    pid = os.fork()
    if pid > 0:
        # Parent: wait for grandchild PID from pipe
        os.close(w_fd)
        data = os.read(r_fd, 32)
        os.close(r_fd)
        grandchild_pid = int(data.strip())
        print(f"✅ デーモン起動 (PID={grandchild_pid})")
        print(f"   ログ: {DAEMON_LOG}")
        print(f"   停止: kill {grandchild_pid}")
        sys.stdout.flush()  # os._exit はバッファを flush しないため明示的に
        os._exit(0)

    # Child: new session
    os.close(r_fd)
    os.setsid()

    # Second fork (prevent re-acquiring terminal)
    pid2 = os.fork()
    if pid2 > 0:
        # Send grandchild PID (pid2) to original parent
        os.write(w_fd, f"{pid2}\n".encode())
        os.close(w_fd)
        os._exit(0)

    os.close(w_fd)

    # Grandchild: redirect stdio to log file
    DAEMON_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_fd = os.open(str(DAEMON_LOG), os.O_CREAT | os.O_WRONLY | os.O_APPEND)
    os.dup2(log_fd, sys.stdout.fileno())
    os.dup2(log_fd, sys.stderr.fileno())
    os.close(log_fd)
    # 非TTYだと stdout がブロックバッファになり、ログが数時間滞留するため行バッファに切替
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    sys.stderr.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    # Close stdin
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, sys.stdin.fileno())
    os.close(devnull)


def _acquire_lock() -> None:
    """ロックファイルを取得。既にデーモンが動いていたら即終了。"""
    global _lock_fd
    _lock_fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_WRONLY)
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(_lock_fd)
        _lock_fd = None
        print("❌ 別のauto-tradeプロセスが実行中です。先に停止してください。", file=sys.stderr)
        sys.exit(1)
    # PIDを書き込む
    os.ftruncate(_lock_fd, 0)
    os.write(_lock_fd, f"{os.getpid()}\n".encode())
    os.fsync(_lock_fd)


def _release_lock() -> None:
    """ロックファイルを解放。"""
    global _lock_fd
    if _lock_fd is not None:
        fcntl.flock(_lock_fd, fcntl.LOCK_UN)
        os.close(_lock_fd)
        _lock_fd = None
    with contextlib.suppress(OSError):
        LOCK_FILE.unlink(missing_ok=True)


# ── helpers ───────────────────────────────────────────────────────────────────


# 急騰検知閾値（AI不使用時のルールベース判断で使用）
SURGE_RETURN_THRESHOLD = 5.0   # 1日リターンがこれ(%)以上なら急騰とみなす


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
        "score_breakdown": summary.get("score_breakdown", {}),
        "current_price": price,
        "probability": scorer.get("probability", {}),
        "risk_management": scorer.get("risk_management", {}),
        "volatility": scorer.get("volatility", {}),
        "entry_points": scorer.get("entry_points", []),
        "returns": scorer.get("returns", {}),
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
    ml = MARKET_LABELS.get(market, market)
    tag = "_AI" if ai_used else ""
    filename = f"{file_ts}_auto_trade{tag}_{ml}.md"
    body = f"# 自動売買ログ — {ml}\n\n" + "\n".join(lines) + "\n"
    diary = get_container().diary()
    diary.save_report(filename, body)
    print(f"  log: {filename}")


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
        "- 急騰銘柄(_surge=True)は特に慎重に評価せよ:\n"
        "  - ブレイクアウト初動（出来高増、レジスタンス突破、RSIがまだ中程度）→ buy\n"
        "  - 過熱終盤（RSI高すぎ、BB上限超え、短期で既に大幅上昇済み）→ skip\n"
        "  - 「今日急騰したが明日下がる可能性が高い」場合も skip\n"
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

    diary = get_container().diary()
    for sig in signals:
        sig_name = f"{sig['ticker'].replace('.', '')}_auto.json"
        sig_path = diary.save_signal(sig_name, sig)
        out, rc = run_trade_cmd(["--from-signal", sig_path])
        ok = rc == 0 and "FILLED" in out
        if not ok:
            log(f"  ❌ {sig['ticker']} 約定失敗: {out.strip()[:200]}")
        else:
            log(f"  ✅ {sig['ticker']} 約定成功")
        executed.append(
            {"ticker": sig["ticker"], "status": "FILLED" if ok else "FAILED", "score": sig["score"]}
        )
    return executed


def _parse_return_pct(returns: dict[str, str], key: str) -> float:
    """Parse '6.85%' -> 6.85"""
    val = returns.get(key, "0%")
    try:
        return float(val.replace("%", ""))
    except (ValueError, AttributeError):
        return 0.0


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
    *,
    pre_scored: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Evaluate whether to swap held positions with better candidates.

    Args:
        pre_scored: 既にスコアリング済みの候補。指定時は再スコアリングをスキップ。
    """
    executed: list[dict[str, Any]] = []
    if max_signals <= 0:
        return executed
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

    # Score new candidates (skip if pre_scored provided)
    if pre_scored:
        new_scored = pre_scored
        log(f"\n  スコア済み候補 {len(new_scored)} 銘柄を使用")
    else:
        log("\nStep 4b: 新規候補スコアリング...")
        new_scored = []
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


def _order_cost(ticker: str, price: float) -> tuple[str, int, float]:
    """ティッカーと価格から (通貨, 売買単位, 1ロットあたりコスト) を返す。

    売買単位はブローカーから取得して config に保持した値を参照する
    (ETF は 1 口単位等)。資金チェックを通貨混在せず通貨ごとに行うために使う。
    """
    from core.trading_units import get_trading_unit

    ccy = "JPY" if ticker.endswith(".T") else "USD"
    unit = get_trading_unit(ticker)
    return ccy, unit, price * unit


def _cash_by_currency(pf_balance: dict[str, Any]) -> dict[str, float]:
    return {
        "JPY": float(pf_balance.get("cash_jpy", 0) or 0),
        "USD": float(pf_balance.get("cash_usd", 0) or 0),
    }


def _execute_swap(
    sell_ticker: str,
    sell_qty: int,
    buy_info: dict[str, Any],
    dry_run: bool,
    log: Any,
) -> list[dict[str, Any]]:
    """Sell one position and buy a replacement."""
    executed: list[dict[str, Any]] = []

    # 事前チェック: 売却後に新規購入できるか？（通貨混在を避ける）
    pf_data = get_container().portfolio().load() or {}
    pf_balance = pf_data.get("balance", {})
    cash = _cash_by_currency(pf_balance)

    buy_ticker = buy_info["ticker"]
    buy_ccy, _buy_lot, buy_cost = _order_cost(buy_ticker, buy_info["current_price"])
    sell_ccy = "JPY" if sell_ticker.endswith(".T") else "USD"

    # 売却で回収できる見込み額（買付と同一通貨のときのみ買付余力に加算できる）
    sell_pos_data = next(
        (p for p in pf_data.get("positions", []) if p.get("ticker") == sell_ticker), None
    )
    sell_proceeds = sell_pos_data["current_price"] * sell_qty if sell_pos_data else 0
    proceeds_for_buy = sell_proceeds if sell_ccy == buy_ccy else 0

    cash_after_swap = cash[buy_ccy] + proceeds_for_buy
    if buy_cost > cash_after_swap:
        note = "" if sell_ccy == buy_ccy else f"（{sell_ccy}売却益は{buy_ccy}買付に充当不可）"
        log(
            f"  ⛔ SWAP中止: 売却後も{buy_ccy}資金不足{note}"
            f" (充当可能 {cash_after_swap:,.0f} {buy_ccy} < 必要 {buy_cost:,.0f} {buy_ccy})"
        )
        return executed

    if dry_run:
        log(f"  [DRY] SELL {sell_ticker} {sell_qty}株")
        log(f"  [DRY] BUY  {buy_ticker} (score={buy_info['score']})")
        executed.append({"ticker": sell_ticker, "status": "DRY_SELL", "score": 0})
        executed.append(
            {"ticker": buy_ticker, "status": "DRY_BUY", "score": buy_info["score"]}
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

    sig = _make_signal(buy_info, f"auto_swap: {sell_ticker}->{buy_ticker}")
    log(f"  買い: {buy_ticker}...")
    executed.extend(_execute_signals([sig], False, log))
    return executed


# ── reconcile helper ──────────────────────────────────────────────────────────


def _reconcile_if_needed(log) -> None:
    """kabuブローカー使用時にサイクル先頭でポートフォリオを同期する。"""
    try:
        config_path = PROJECT_DIR / "config" / "trading_config.json"
        if not config_path.exists():
            return
        import json as _json
        with open(config_path) as f:
            config = _json.load(f)
        if config.get("broker", "simulator") == "simulator":
            return
        from core.reconcile import reconcile
        from core.trade import load_or_create_broker
        broker = load_or_create_broker(config)
        result = reconcile(broker, apply=True, verbose=False)
        if result.synced:
            log("  🔄 ブローカーとローカルの同期を実行しました")
            for d in result.diffs:
                if d.action != "MATCH":
                    log(f"     {d.action}: {d.ticker} (local={d.local_qty} -> broker={d.broker_qty})")
        else:
            log("  ✅ ブローカーと同期済み")
    except Exception as e:
        log(f"  ⚠️ ブローカー同期エラー (続行): {e}")


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

    # Step 0: ブローカー同期 (kabuモード時のみ)
    _reconcile_if_needed(log)

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

    # 集中超過の警告 + 日次損失サーキットブレーカー (非LLM)
    warn_overweight_positions(log)
    if daily_loss_exceeded(log):
        log("  → 日次損失上限を超過したため新規買いをスキップ")
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

    if available <= 0 and not candidates:
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
        ret_1d = _parse_return_pct(result.get("returns", {}), "1日リターン")
        if ret_1d >= SURGE_RETURN_THRESHOLD:
            result["_surge"] = True
            log(f"    📈 急騰検知 (1日+{ret_1d:.1f}%)")

        log(f"    score={result['score']}, action={result['action']}, conf={result['confidence']}")
        if result["score"] < min_score:
            log("    スコア不足 -> skip")
            continue
        if "売り" in result["action"]:
            log(f"    判定が{result['action']} -> skip")
            continue

        # AI不使用時の急騰フィルター: RSI・BBが過熱を示している場合のみスキップ
        # AI使用時は「急騰の質」をAIに判断させるのでここでは通す
        if result.get("_surge") and not use_ai:
            scores = result.get("score_breakdown", {})
            rsi_score = scores.get("rsi", 0)
            bb_score = scores.get("bb", 0)
            # RSIが買われすぎ(-10以下) かつ BBが上限付近(-5以下) = 過熱→下がる可能性高
            if rsi_score <= -10 and bb_score <= -5:
                log(f"    ⚠️ 急騰+過熱 (RSI={rsi_score}, BB={bb_score}) -> 調整入りの可能性が高いためskip")
                continue
            else:
                log(f"    ✅ 急騰だがRSI/BBは過熱でない (RSI={rsi_score}, BB={bb_score}) -> 継続")

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

    # Build signals (通貨ごとの資金チェック → 不足なら入れ替え候補へ)
    pf_data = get_container().portfolio().load() or {}
    pf_balance = pf_data.get("balance", {})
    cash = _cash_by_currency(pf_balance)
    log(f"\n  残高: ¥{cash['JPY']:,.0f} / ${cash['USD']:,.0f}")

    # 評価額上限の事前フィルタ用 (1単元が 30% を超える高額銘柄は約定時 qty=0 になるため
    # ここで弾いて発注枠の空費と spurious な FAILED ログを防ぐ)
    equity_jpy = total_equity_jpy()
    try:
        from core.trade import load_risk_limits
        max_pos_pct = float(load_risk_limits().get("max_position_size_pct", 30))
    except Exception:
        max_pos_pct = 30.0
    max_pos_value_jpy = equity_jpy * max_pos_pct / 100 if equity_jpy > 0 else 0.0

    signals: list[dict[str, Any]] = []
    swap_candidates: list[dict[str, Any]] = []  # 資金不足で買えなかった良い候補
    for s in scored:
        if len(signals) >= max_signals or len(signals) >= available:
            break
        ticker = s["ticker"]

        if use_ai and ai_decisions:
            ai_d = ai_decisions.get(ticker)
            if ai_d and ai_d.get("decision") == "skip":
                log(f"  ⛔ {ticker}: AI skip -> シグナル除外")
                continue

        # 資金不足チェック (その銘柄の通貨の現金で判定。通貨混在しない)
        ccy, _lot, est_cost = _order_cost(ticker, s["current_price"])
        if est_cost > cash[ccy]:
            log(
                f"  💰 {ticker}: {ccy}資金不足 "
                f"(必要≈{est_cost:,.0f} {ccy}, 残高{cash[ccy]:,.0f} {ccy}) -> 入れ替え候補へ"
            )
            swap_candidates.append(s)
            continue

        # 評価額上限チェック: 1単元が総資産の max_position_size_pct を超える銘柄は買えない
        cap_in_ccy = max_pos_value_jpy if ccy == "JPY" else max_pos_value_jpy / USD_JPY_APPROX
        if max_pos_value_jpy > 0 and est_cost > cap_in_ccy:
            log(
                f"  📏 {ticker}: 1単元(≈{est_cost:,.0f} {ccy})が評価額上限"
                f"({max_pos_pct:.0f}%≈{cap_in_ccy:,.0f} {ccy})超 -> スキップ"
            )
            continue

        reason = f"auto_trade{'[AI]' if use_ai else ''}: score={s['score']}, {s['action']}"
        sig = _make_signal(s, reason)
        signals.append(sig)
        cash[ccy] -= est_cost

    # Step 6: execute new buys
    executed: list[dict[str, Any]] = []
    if signals:
        log(f"\n注文実行 ({len(signals)} 件)...")
        executed = _execute_signals(signals, dry_run, log)

    # Step 7: swap evaluation — 資金不足で買えなかった候補 vs 保有銘柄
    if swap_candidates and cur_pos > 0:
        log(f"\n入れ替え検討: {len(swap_candidates)} 銘柄が資金不足で買えず")
        swap_executed = _run_swap_evaluation(
            [],  # candidates not needed when pre_scored is provided
            macro, market, max_signals - len(executed), dry_run, use_ai, ai_provider, ai_model, log,
            pre_scored=swap_candidates,
        )
        executed.extend(swap_executed)

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


def _should_skip_cycle(market: str) -> bool:
    """取引時間外ならTrue。東証 8:30-16:00 / 米国 22:00-06:00 JST (余裕込み)."""
    JST = timezone(timedelta(hours=9))
    now = datetime.now(JST)
    weekday = now.weekday()  # 0=Mon ... 6=Sun
    hour = now.hour
    minute = now.minute
    t = hour * 60 + minute

    jp_open = 8 * 60 + 30   # 08:30
    jp_close = 16 * 60       # 16:00
    us_open = 22 * 60        # 22:00 (JST)
    us_close = 6 * 60        # 06:00 (JST, 翌朝)

    # 東証: 月〜金 08:30-16:00 JST
    jp_active = weekday <= 4 and jp_open <= t <= jp_close
    # 米国: 現地月〜金 = JST 月〜金 22:00〜 / 火〜土 〜06:00 (翌朝に跨ぐ)
    # ※月曜 00:00-06:00 JST は米国日曜のため休場、土曜早朝は金曜セッション継続中
    us_active = (weekday <= 4 and t >= us_open) or (1 <= weekday <= 5 and t <= us_close)

    if market == "jp":
        return not jp_active
    elif market == "us":
        return not us_active
    else:  # "all"
        # 日本か米国どちらかが開いていればOK
        return not (jp_active or us_active)


def daemon_loop(
    market: str,
    min_score: int,
    max_signals: int,
    interval: int,
    dry_run: bool,
    use_ai: bool,
    ai_provider: str,
    ai_model: str | None,
    foreground: bool = False,
) -> None:
    # ロック取得を先に行う（fork前）→ 2重起動を即座にブロック
    _acquire_lock()

    if not foreground:
        _daemonize()
        # fork後のgrandchildでPIDを更新
        os.lseek(_lock_fd, 0, os.SEEK_SET)  # type: ignore[arg-type]
        os.ftruncate(_lock_fd, 0)  # type: ignore[arg-type]
        os.write(_lock_fd, f"{os.getpid()}\n".encode())  # type: ignore[arg-type]
        os.fsync(_lock_fd)  # type: ignore[arg-type]

    atexit.register(_release_lock)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    print(f"デーモンモード: {interval}s ({interval // 60}min) ごとに自動実行")
    print(f"  market={market}  min_score={min_score}  max_signals={max_signals}  dry_run={dry_run}")
    if use_ai:
        print(f"  AI: {ai_provider} (model: {ai_model or 'default'})")
    print(f"  PID={os.getpid()}")
    if foreground:
        print("  Ctrl+C で停止\n")
    else:
        print(f"  ログ: {DAEMON_LOG}\n")

    cycle = 0
    while True:
        cycle += 1

        # 取引時間外スキップ（東証 9:00-15:30 / 米国 22:30-05:00 JST）
        if _should_skip_cycle(market):
            JST = timezone(timedelta(hours=9))
            now_jst = datetime.now(JST)
            print(f"\n### サイクル #{cycle} [SKIP] {now_jst.strftime('%H:%M')} JST — 取引時間外 ###")
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                print("\nデーモン停止")
                break
            continue

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
    parser.add_argument("--ai", action="store_true", help="AI判断を有効化（legacyモード用）")
    parser.add_argument("--ai-provider", choices=PROVIDER_NAMES, default="copilot")
    parser.add_argument("--ai-model", default=None)
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--interval", type=int, default=1800, help="seconds between cycles")
    parser.add_argument("--legacy", action="store_true", help="旧パイプラインを使用（LangGraphなし）")
    args = parser.parse_args()

    if args.legacy:
        # Legacy fixed-pipeline mode
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
    else:
        # LangGraph agent mode (default)
        from agents.graph_trade import run_trade_graph

        if args.daemon:
            print(f"デーモンモード (LangGraph): {args.interval}s ごとに自動実行")
            print(f"  market={args.market}  min_score={args.min_score}  dry_run={args.dry_run}")
            print(f"  provider={args.ai_provider}")
            print("  Ctrl+C で停止\n")
            cycle = 0
            while True:
                cycle += 1
                print(f"\n### サイクル #{cycle} ###")
                try:
                    run_trade_graph(
                        market=args.market,
                        min_score=args.min_score,
                        max_signals=args.max_signals,
                        dry_run=args.dry_run,
                        provider=args.ai_provider,
                        model=args.ai_model,
                    )
                except Exception as e:
                    print(f"ERROR: {e}", file=sys.stderr)
                print(f"\n次回: {args.interval}s後")
                try:
                    time.sleep(args.interval)
                except KeyboardInterrupt:
                    print("\nデーモン停止")
                    break
        else:
            run_trade_graph(
                market=args.market,
                min_score=args.min_score,
                max_signals=args.max_signals,
                dry_run=args.dry_run,
                provider=args.ai_provider,
                model=args.ai_model,
            )


if __name__ == "__main__":
    main()

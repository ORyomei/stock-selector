#!/usr/bin/env python3
"""自動売買ループ — スクリーニング→スコアリング→AI判断→発注→クローズ判定を全自動実行

Usage:
    # 1回実行（ドライラン）
    python3 scripts/auto_trade.py --dry-run

    # AI判断付き本番
    python3 scripts/auto_trade.py --ai

    # デーモンモード（30分ごと、AI付き）
    python3 scripts/auto_trade.py --ai --daemon --interval 1800

    # 日本株のみ
    python3 scripts/auto_trade.py --ai --market jp
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import shlex
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent
CONFIG_DIR = PROJECT_DIR / "config"
DIARY_DIR = PROJECT_DIR / "diary"
SIGNALS_DIR = DIARY_DIR / "signals"
TRADES_DIR = DIARY_DIR / "trades"
PORTFOLIO_FILE = PROJECT_DIR / "portfolio.json"
JST = timezone(timedelta(hours=9))

AI_DEFAULTS = {
    "copilot":   {"endpoint": None, "model": "claude-sonnet-4.6", "token_env": None},
    "github":    {"endpoint": "https://models.inference.ai.azure.com/chat/completions",
                  "model": "openai/gpt-4o", "token_env": None},
    "openai":    {"endpoint": "https://api.openai.com/v1/chat/completions",
                  "model": "gpt-4o", "token_env": "OPENAI_API_KEY"},
    "anthropic": {"endpoint": "https://api.anthropic.com/v1/messages",
                  "model": "claude-sonnet-4-20250514", "token_env": "ANTHROPIC_API_KEY"},
}


# ─── ヘルパー ─────────────────────────────────────────────

def run_script(script_name, args=None, timeout=120):
    cmd = [sys.executable, str(SCRIPT_DIR / script_name)]
    if args:
        cmd.extend(args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(PROJECT_DIR))
        if result.returncode != 0:
            print(f"  warn: {script_name} error: {result.stderr[:200]}", file=sys.stderr)
            return None
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        print(f"  warn: {script_name} timeout ({timeout}s)", file=sys.stderr)
        return None
    except json.JSONDecodeError:
        print(f"  warn: {script_name} JSON parse failed", file=sys.stderr)
        return None


def run_trade_cmd(args_list, timeout=60):
    cmd = [sys.executable, str(SCRIPT_DIR / "trade.py")] + args_list
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(PROJECT_DIR))
        return result.stdout, result.returncode
    except subprocess.TimeoutExpired:
        return "", 1


def load_portfolio():
    if PORTFOLIO_FILE.exists():
        with open(PORTFOLIO_FILE) as f:
            return json.load(f)
    return None


def get_held_tickers():
    pf = load_portfolio()
    return {p["ticker"] for p in pf.get("positions", [])} if pf else set()


def get_held_positions():
    """保有ポジション一覧を返す（dict のリスト）"""
    pf = load_portfolio()
    return pf.get("positions", []) if pf else []


def count_positions():
    pf = load_portfolio()
    return len(pf.get("positions", [])) if pf else 0


def get_max_positions():
    limits_path = CONFIG_DIR / "risk_limits.json"
    if limits_path.exists():
        with open(limits_path) as f:
            return json.load(f).get("max_concurrent_positions", 5)
    return 5


def extract_candidates(screener_result, held_tickers):
    if not screener_result or "results" not in screener_result:
        return []
    seen = set()
    candidates = []
    for strategy, cands in screener_result["results"].items():
        for c in cands:
            ticker = c["ticker"]
            if ticker in seen or ticker in held_tickers:
                continue
            seen.add(ticker)
            candidates.append({
                "ticker": ticker, "name": c.get("name", ""),
                "strategy": strategy, "screener_score": c.get("score", 0),
            })
    candidates.sort(key=lambda x: x["screener_score"], reverse=True)
    return candidates


def score_ticker(ticker, log_fn=None):
    """1銘柄をスコアリングして要約辞書を返す"""
    scorer = run_script("scorer.py", [ticker])
    if not scorer:
        if log_fn:
            log_fn(f"    スコアリング失敗: {ticker}")
        return None
    s = scorer.get("analysis_summary", {})
    price = scorer.get("current_price")
    if price is None or str(price) == "nan":
        return None
    return {
        "ticker": ticker,
        "score": s.get("total_score", 0),
        "action": s.get("action", ""),
        "confidence": s.get("confidence", "低"),
        "current_price": price,
        "probability": scorer.get("probability", {}),
        "risk_management": scorer.get("risk_management", {}),
        "volatility": scorer.get("volatility", {}),
    }


def confidence_to_float(s):
    return {"高": 0.90, "中〜高": 0.80, "中": 0.70, "低〜中": 0.60, "低": 0.50}.get(s, 0.70)


def save_cycle_log(file_ts, log_lines, executed, market, ai_used=False):
    TRADES_DIR.mkdir(parents=True, exist_ok=True)
    ml = {"us": "米国株", "jp": "日本株", "all": "全市場"}.get(market, market)
    tag = "_AI" if ai_used else ""
    filepath = TRADES_DIR / f"{file_ts}_auto_trade{tag}_{ml}.md"
    content = f"# 自動売買ログ — {ml}\n\n" + "\n".join(log_lines) + "\n"
    filepath.write_text(content, encoding="utf-8")
    print(f"  log: {filepath}")


# ─── AI ───────────────────────────────────────────────────

def get_ai_token(provider):
    cfg = AI_DEFAULTS.get(provider, {})
    env_key = cfg.get("token_env")
    if env_key:
        return os.environ.get(env_key)
    if provider == "github":
        try:
            r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                return r.stdout.strip()
        except Exception:
            pass
        return os.environ.get("GITHUB_TOKEN")
    return None


def call_copilot(prompt):
    prompt_file = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(prompt)
            prompt_file = f.name
        flags = '--excluded-tools="shell,read,write,list,search,glob,stat,create,edit" --no-custom-instructions -s'
        if len(prompt) < 10000:
            cmd = f'gh copilot -- -p {shlex.quote(prompt)} {flags}'
        else:
            cmd = f'cat {shlex.quote(prompt_file)} | gh copilot -- -p - {flags}'
        result = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                                timeout=300, cwd=str(PROJECT_DIR))
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None
    except Exception:
        return None
    finally:
        if prompt_file:
            try:
                os.unlink(prompt_file)
            except OSError:
                pass


def call_ai_api(prompt, provider, model):
    if provider == "copilot":
        return call_copilot(prompt)
    try:
        import requests
    except ImportError:
        return None
    token = get_ai_token(provider)
    if not token:
        return None
    cfg = AI_DEFAULTS.get(provider, AI_DEFAULTS["github"])
    use_model = model or cfg["model"]
    try:
        if provider == "anthropic":
            resp = requests.post(cfg["endpoint"],
                headers={"x-api-key": token, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
                json={"model": use_model, "max_tokens": 4000, "temperature": 0.2,
                      "system": "株式売買判断AI。JSON形式で回答。",
                      "messages": [{"role": "user", "content": prompt}]}, timeout=120)
            return resp.json()["content"][0]["text"] if resp.status_code == 200 else None
        else:
            resp = requests.post(cfg["endpoint"],
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"model": use_model, "temperature": 0.2, "max_tokens": 4000,
                      "messages": [{"role": "system", "content": "株式売買判断AI。JSON形式で回答。"},
                                   {"role": "user", "content": prompt}]}, timeout=120)
            return resp.json()["choices"][0]["message"]["content"] if resp.status_code == 200 else None
    except Exception:
        return None


def parse_ai_json(text):
    if not text:
        return None
    t = text.strip()
    if "```json" in t:
        t = t.split("```json", 1)[1]
        if "```" in t:
            t = t.split("```", 1)[0]
    elif "```" in t:
        parts = t.split("```")
        if len(parts) >= 3:
            t = parts[1]
    t = t.strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(t[start:end])
            except json.JSONDecodeError:
                pass
    return None


# ─── AI プロンプト ────────────────────────────────────────

def build_buy_prompt(scored_candidates, macro, market):
    data = json.dumps(scored_candidates, ensure_ascii=False, indent=2, default=str)
    macro_j = json.dumps(macro, ensure_ascii=False, indent=2, default=str) if macro else "{}"
    ml = {"us": "米国株", "jp": "日本株", "all": "全市場"}.get(market, market)
    return f"""自動売買の最終判断。各候補に buy/skip を判定。

:::: {ml}
: {macro_j}
#-f
: {data}

gh copilot -p "1+1は？数字のみで" > /tmp/copilot_stdout.txt 2> /tmp/copilot_stderr.txt:
- デッドキャットバウンス、出来高なしの上昇、過度なボラティリティを見抜く
- 各候補に buy/skip を判定、理由を明記

gh copilot -p "1+1は？数字のみで" > /tmp/copilot_stdout.txt 2> /tmp/copilot_stderr.JSONのみ）:
{{"decisions": [{{"ticker": "X", "decision": "buy", "confidence": 0.8, "reason": "..."}}], "market_comment": "..."}}"""


def build_swap_prompt(held_scored, new_scored, macro, market):
    held_j = json.dumps(held_scored, ensure_ascii=False, indent=2, default=str)
    new_j = json.dumps(new_scored, ensure_ascii=False, indent=2, default=str)
    macro_j = json.dumps(macro, ensure_ascii=False, indent=2, default=str) if macro else "{}"
    ml = {"us": "米国株", "jp": "日本株", "all": "全市場"}.get(market, market)
    return f"""ポートフォリオ入れ替え判断。ポジション枠満杯のため、保有銘柄と新規候補を比較して入れ替えるべきか判断。

:::: {ml}
: {macro_j}

gh copilot -/Tmp/Copilot_Stderr.: {held_j}/Copilot_Stderr.
#-f
gh copilot -/Tmp/Copilot_Stderr.: {new_j}/Copilot_Stderr.

gh copilot -p "1+1は？数字のみで" > /tmp/copilot_stdout.txt 2> /tmp/copilot_stderr.txt:
- 新規候補が保有銘柄より明確にスコア・確率が高い場合のみ swap
- 僅差なら手数料・スプレッドを考慮して hold 推奨
- マクロ環境も考慮（弱気ならディフェンシブ寄り）
- 1サイクルで入れ替えは最大2件まで

gh copilot -p "1+1は？数字のみで" > /tmp/copilot_stdout.txt 2> /tmp/copilot_stderr.JSONのみ）:
{{"recommendation": "swap" or "hold", "swaps": [{{"sell": "OLD", "sell_reason": "...", "buy": "NEW", "buy_reason": "..."}}], "overall_reason": "...", "confidence": 0.7}}
swap不要なら "swaps": []。"""


# ─── メインループ ─────────────────────────────────────────

def run_cycle(market, min_score, max_signals, dry_run, use_ai=False, ai_provider="copilot", ai_model=None):
    now = datetime.now(JST)
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S JST")
    file_ts = now.strftime("%Y-%m-%d_%H%M%S")
    log_lines = []

    def log(msg):
        print(msg)
        log_lines.append(msg)

    log(f"\n{'='*60}")
    log(f"  自動売買ループ: {timestamp}")
    log(f"  market={market}, min_score={min_score}, max_signals={max_signals}")
    log(f"  dry_run={dry_run}, ai={use_ai}" + (f" ({ai_provider})" if use_ai else ""))
    log(f"{'='*60}\n")

    # ── Step 1: 自動クローズ ──
    log("Step 1: 自動クローズ判定...")
    close_out, _ = run_trade_cmd(["--check-and-close"])
    if "クローズ対象なし" in close_out:
        log("  -> クローズ対象なし")
    else:
        for line in close_out.strip().split("\n"):
            if line.strip():
                log(f"  -> {line.strip()}")

    # ── Step 2: マクロ環境 ──
    log("\nStep 2: マクロ環境チェック...")
    macro = run_script("macro.py")
    env_score, env_assessment = 0, "不明"
    if macro:
        env = macro.get("market_environment", {})
        env_score = env.get("score", 0)
        env_assessment = env.get("assessment", "不明")
        for s in env.get("signals", []):
            log(f"  - {s}")
        log(f"  -> 市場環境: {env_assessment} (スコア={env_score})")
    else:
        log("  -> マクロ取得失敗")

    if env_score <= -30:
        log(f"\n  市場環境が極端に弱気 (スコア={env_score})。新規買いスキップ。")
        save_cycle_log(file_ts, log_lines, [], market, use_ai)
        return []

    # ── Step 3: スクリーニング ──
    log("\nStep 3: スクリーニング...")
    screener = run_script("screener.py", ["--market", market, "--strategy", "all", "--top", "5"], timeout=300)
    if not screener:
        log("  -> スクリーニング失敗。サイクル中止。")
        save_cycle_log(file_ts, log_lines, [], market, use_ai)
        return []

    held = get_held_tickers()
    current_pos = count_positions()
    max_pos = get_max_positions()
    available = max_pos - current_pos
    candidates = extract_candidates(screener, held)
    log(f"  -> 候補 {len(candidates)} 銘柄 (保有済み除外)")
    log(f"  -> ポジション: {current_pos}/{max_pos} (空き: {available})")

    # ── 枠満杯 → 入れ替え検討 ──
    if available <= 0 and candidates:
        log(f"\n  枠満杯 → 入れ替え検討モードへ")
        executed = _run_swap_evaluation(candidates, macro, market, min_score,
                                         max_signals, dry_run, use_ai, ai_provider,
                                         ai_model, log)
        save_cycle_log(file_ts, log_lines, executed, market, use_ai)
        return executed

    if available <= 0:
        log("  -> 枠満杯 & 新規候補なし。何もしない。")
        save_cycle_log(file_ts, log_lines, [], market, use_ai)
        return []

    if not candidates:
        log("  -> 新規候補なし")
        save_cycle_log(file_ts, log_lines, [], market, use_ai)
        return []

    # ── Step 4: スコアリング ──
    log("\nStep 4: 候補スコアリング...")
    scored = []
    for cand in candidates:
        if len(scored) >= max_signals * 2:
            break
        ticker = cand["ticker"]
        log(f"  -> {ticker} ({cand.get('name', '')})...")
        result = score_ticker(ticker, log)
        if not result:
            continue
        log(f"    score={result['score']}, action={result['action']}, conf={result['confidence']}")
        if result["score"] < min_score:
            log(f"    スコア不足 → スキップ")
            continue
        if "売り" in result["action"]:
            log(f"    判定が{result['action']} → スキップ")
            continue
        scored.append(result)
        log(f"    -> 通過 ✓")

    if not scored:
        log("  -> スコア通過銘柄なし")
        save_cycle_log(file_ts, log_lines, [], market, use_ai)
        return []

    # ── Step 5: AI判断（有効時）──
    ai_decisions = {}
    if use_ai and scored:
        log(f"\nStep 5: AI判断 ({ai_provider})...")
        prompt = build_buy_prompt(scored, macro, market)
        resp = call_ai_api(prompt, ai_provider, ai_model)
        parsed = parse_ai_json(resp)
        if parsed and "decisions" in parsed:
            comment = parsed.get("market_comment", "")
            if comment:
                log(f"  AI: {comment}")
            for d in parsed["decisions"]:
                t = d.get("ticker", "")
                dec = d.get("decision", "skip")
                ai_decisions[t] = d
                emoji = "✅" if dec == "buy" else "⛔"
                log(f"  {emoji} {t}: {dec} — {d.get('reason', '')[:80]}")
        else:
            log("  AI判断失敗 → ルールベースにフォールバック")

    # ── Step 6: 注文実行 ──
    signals = []
    for s in scored:
        if len(signals) >= max_signals or len(signals) >= available:
            break
        ticker = s["ticker"]
        if use_ai and ai_decisions:
            ai_d = ai_decisions.get(ticker)
            if ai_d and ai_d.get("decision") == "skip":
                log(f"  ⛔ {ticker}: AI skip → シグナル除外")
                continue
        risk = s["risk_management"]
        price = s["current_price"]
        signals.append({
            "ticker": ticker, "action": "buy", "entry_price": 0,
            "target_price": risk.get("利確目標1（ATR×2）", price * 1.05),
            "stop_loss_price": risk.get("損切りライン", price * 0.97),
            "take_profit_price": risk.get("利確目標2（ATR×4）", price * 1.10),
            "confidence": confidence_to_float(s["confidence"]),
            "timespan": "swing", "score": s["score"],
            "reason": f"auto_trade{'[AI]' if use_ai else ''}: score={s['score']}, {s['action']}",
        })

    log(f"\n注文実行 ({len(signals)} 件)...")
    executed = _execute_signals(signals, dry_run, log)

    log(f"\n{'='*60}")
    log(f"  サイクル完了: {datetime.now(JST).strftime('%H:%M:%S')}")
    log(f"  環境: {env_assessment} (score={env_score})")
    log(f"  新規注文: {len(executed)} 件")
    for e in executed:
        log(f"    {e['status']} {e['ticker']} (score={e['score']})")
    log(f"{'='*60}\n")

    save_cycle_log(file_ts, log_lines, executed, market, use_ai)
    return executed


# ─── 入れ替え評価 ─────────────────────────────────────────

def _run_swap_evaluation(candidates, macro, market, min_score, max_signals,
                          dry_run, use_ai, ai_provider, ai_model, log):
    """枠満杯時: 保有銘柄と新規候補を比較して入れ替えを検討"""
    executed = []

    # 保有銘柄をスコアリング
    log("\nStep 4a: 保有銘柄スコアリング...")
    positions = get_held_positions()
    held_scored = []
    for pos in positions:
        ticker = pos["ticker"]
        log(f"  -> {ticker}...")
        result = score_ticker(ticker, log)
        if result:
            result["quantity"] = pos.get("quantity", 0)
            result["entry_price"] = pos.get("entry_price", 0)
            result["pnl_pct"] = pos.get("pnl_pct", 0)
            held_scored.append(result)
            log(f"    score={result['score']}, action={result['action']}")
        else:
            log(f"    スコアリング失敗")
            # スコアリング失敗 → 保守的に残す（高スコア扱い）
            held_scored.append({
                "ticker": ticker, "score": 999, "action": "不明",
                "confidence": "中", "current_price": pos.get("current_price", 0),
                "quantity": pos.get("quantity", 0), "entry_price": pos.get("entry_price", 0),
                "pnl_pct": pos.get("pnl_pct", 0), "probability": {}, "risk_management": {},
            })

    # 新規候補をスコアリング
    log("\nStep 4b: 新規候補スコアリング...")
    new_scored = []
    for cand in candidates[:max_signals * 3]:  # 上位数銘柄のみ
        ticker = cand["ticker"]
        log(f"  -> {ticker} ({cand.get('name', '')})...")
        result = score_ticker(ticker, log)
        if not result:
            continue
        result["name"] = cand.get("name", "")
        log(f"    score={result['score']}, action={result['action']}")
        if "売り" in result["action"]:
            log(f"    判定が{result['action']} → スキップ")
            continue
        new_scored.append(result)
        log(f"    -> 候補 ✓")

    if not new_scored:
        log("  -> 入れ替え候補なし。何もしない。")
        return executed

    # 保有最低スコア vs 新規最高スコアを比較
    held_sorted = sorted(held_scored, key=lambda x: x["score"])
    new_sorted = sorted(new_scored, key=lambda x: x["score"], reverse=True)
    worst_held = held_sorted[0]
    best_new = new_sorted[0]

    log(f"\n  比較: 保有最低 {worst_held['ticker']}(score={worst_held['score']}) vs 候補最高 {best_new['ticker']}(score={best_new['score']})")

    # ルールベース判定: 新規候補のスコアが保有最低より十分高い場合のみ入れ替え
    score_diff = best_new["score"] - worst_held["score"]
    if score_diff < 15 and not use_ai:
        log(f"  -> スコア差 {score_diff} < 15。入れ替え不要。")
        return executed

    if score_diff < 5:
        log(f"  -> スコア差 {score_diff} < 5。入れ替え不要。")
        return executed

    # AI判断
    if use_ai:
        log(f"\nStep 5: AI入れ替え判断 ({ai_provider})...")
        prompt = build_swap_prompt(held_scored, new_scored, macro, market)
        resp = call_ai_api(prompt, ai_provider, ai_model)
        parsed = parse_ai_json(resp)

        if parsed:
            rec = parsed.get("recommendation", "hold")
            reason = parsed.get("overall_reason", "")
            conf = parsed.get("confidence", 0)
            log(f"  AI判定: {rec} (確信度={conf})")
            if reason:
                log(f"  理由: {reason[:120]}")

            if rec == "hold" or not parsed.get("swaps"):
                log("  -> AIが hold 判定。入れ替えなし。")
                return executed

            # AIが swap 推奨
            swaps = parsed["swaps"][:max_signals]  # 最大 max_signals 件
            for swap in swaps:
                sell_ticker = swap.get("sell", "")
                buy_ticker = swap.get("buy", "")
                log(f"  SWAP: {sell_ticker} → {buy_ticker}")
                log(f"    売り理由: {swap.get('sell_reason', '')[:80]}")
                log(f"    買い理由: {swap.get('buy_reason', '')[:80]}")

                # 売り対象の情報取得
                sell_pos = next((p for p in positions if p["ticker"] == sell_ticker), None)
                buy_info = next((n for n in new_scored if n["ticker"] == buy_ticker), None)

                if not sell_pos or not buy_info:
                    log(f"    銘柄情報不一致 → スキップ")
                    continue

                result = _execute_swap(sell_ticker, sell_pos.get("quantity", 0),
                                        buy_info, dry_run, log)
                executed.extend(result)
        else:
            log("  AI判断失敗 → ルールベースにフォールバック")
            # フォールバック: スコア差が十分なら自動入れ替え
            if score_diff >= 20:
                sell_pos = next((p for p in positions if p["ticker"] == worst_held["ticker"]), None)
                if sell_pos:
                    log(f"\n  ルールベース SWAP: {worst_held['ticker']}(score={worst_held['score']}) → {best_new['ticker']}(score={best_new['score']})")
                    result = _execute_swap(worst_held["ticker"], sell_pos.get("quantity", 0),
                                            best_new, dry_run, log)
                    executed.extend(result)
    else:
        # AI なしのルールベース: スコア差 >= 20 なら入れ替え
        if score_diff >= 20:
            sell_pos = next((p for p in positions if p["ticker"] == worst_held["ticker"]), None)
            if sell_pos:
                log(f"\n  SWAP: {worst_held['ticker']}(score={worst_held['score']}) → {best_new['ticker']}(score={best_new['score']}), diff={score_diff}")
                result = _execute_swap(worst_held["ticker"], sell_pos.get("quantity", 0),
                                        best_new, dry_run, log)
                executed.extend(result)
        else:
            log(f"  -> スコア差 {score_diff} < 20。入れ替え不要。")

    return executed


def _execute_swap(sell_ticker, sell_qty, buy_info, dry_run, log):
    """1件の入れ替え（売り→買い）を実行"""
    executed = []

    if dry_run:
        log(f"  [DRY] SELL {sell_ticker} {sell_qty}株")
        log(f"  [DRY] BUY  {buy_info['ticker']} (score={buy_info['score']})")
        executed.append({"ticker": sell_ticker, "status": "DRY_SELL", "score": 0})
        executed.append({"ticker": buy_info["ticker"], "status": "DRY_BUY", "score": buy_info["score"]})
        return executed

    # 売り実行
    log(f"  売り: {sell_ticker} {sell_qty}株...")
    out, rc = run_trade_cmd(["--close", sell_ticker, str(sell_qty)])
    if rc == 0 and "FILLED" in out:
        log(f"    ✅ {sell_ticker} クローズ完了")
        executed.append({"ticker": sell_ticker, "status": "SOLD", "score": 0})
    else:
        log(f"    ❌ {sell_ticker} クローズ失敗")
        executed.append({"ticker": sell_ticker, "status": "SELL_FAILED", "score": 0})
        return executed  # 売れなかったら買いもしない

    # 買いシグナル生成 & 実行
    risk = buy_info.get("risk_management", {})
    price = buy_info["current_price"]
    sig = {
        "ticker": buy_info["ticker"], "action": "buy", "entry_price": 0,
        "target_price": risk.get("利確目標1（ATR×2）", price * 1.05),
        "stop_loss_price": risk.get("損切りライン", price * 0.97),
        "take_profit_price": risk.get("利確目標2（ATR×4）", price * 1.10),
        "confidence": confidence_to_float(buy_info.get("confidence", "中")),
        "timespan": "swing", "score": buy_info["score"],
        "reason": f"auto_swap: {sell_ticker}→{buy_info['ticker']}, score={buy_info['score']}",
    }
    log(f"  買い: {buy_info['ticker']}...")
    result = _execute_signals([sig], False, log)
    executed.extend(result)
    return executed


def _execute_signals(signals, dry_run, log):
    """シグナルリストを注文実行"""
    executed = []
    if dry_run:
        for sig in signals:
            log(f"  [DRY] BUY {sig['ticker']} score={sig['score']}")
            executed.append({"ticker": sig["ticker"], "status": "DRY_RUN", "score": sig["score"]})
        return executed

    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    for sig in signals:
        sig_path = SIGNALS_DIR / f"{sig['ticker'].replace('.', '')}_auto.json"
        with open(sig_path, "w") as f:
            json.dump(sig, f, ensure_ascii=False, indent=2)
        out, rc = run_trade_cmd(["--from-signal", str(sig_path)])
        success = rc == 0 and "FILLED" in out
        emoji = "✅" if success else "❌"
        log(f"  {emoji} {sig['ticker']} {'約定成功' if success else '約定失敗'}")
        executed.append({"ticker": sig["ticker"], "status": "FILLED" if success else "FAILED", "score": sig["score"]})
    return executed


# ─── デーモン ─────────────────────────────────────────────

def daemon_mode(market, min_score, max_signals, interval, dry_run, use_ai, ai_provider, ai_model):
    print(f"デーモンモード: {interval}秒 ({interval // 60}分) ごとに自動実行")
    print(f"  market={market}, min_score={min_score}, max_signals={max_signals}, dry_run={dry_run}")
    if use_ai:
        print(f"  AI: {ai_provider} (model: {ai_model or 'default'})")
    print(f"  Ctrl+C で停止\n")
    count = 0
    while True:
        count += 1
        print(f"\n### サイクル #{count} ###")
        try:
            run_cycle(market, min_score, max_signals, dry_run, use_ai, ai_provider, ai_model)
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
        print(f"\n次回: {interval}秒後 ({interval // 60}分後)")
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\nデーモン停止")
            break


def main():
    parser = argparse.ArgumentParser(description="自動売買ループ（AI判断・入れ替え対応）")
    parser.add_argument("--market", choices=["us", "jp", "all"], default="all")
    parser.add_argument("--min-score", type=int, default=10, help="最低スコア (default: 10)")
    parser.add_argument("--max-signals", type=int, default=2, help="最大新規注文数 (default: 2)")
    parser.add_argument("--dry-run", action="store_true", help="ドライラン")
    parser.add_argument("--ai", action="store_true", help="AI判断を有効化")
    parser.add_argument("--ai-provider", choices=["copilot", "github", "openai", "anthropic"],
                        default="copilot", help="AIプロバイダー (default: copilot)")
    parser.add_argument("--ai-model", default=None, help="AIモデル")
    parser.add_argument("--daemon", action="store_true", help="デーモンモード")
    parser.add_argument("--interval", type=int, default=1800, help="間隔(秒) (default: 1800)")
    args = parser.parse_args()

    if args.daemon:
        daemon_mode(args.market, args.min_score, args.max_signals, args.interval,
                    args.dry_run, args.ai, args.ai_provider, args.ai_model)
    else:
        run_cycle(args.market, args.min_score, args.max_signals, args.dry_run,
                  args.ai, args.ai_provider, args.ai_model)


if __name__ == "__main__":
    main()

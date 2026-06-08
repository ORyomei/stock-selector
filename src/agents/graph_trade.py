"""LangGraph-based trading agent.

Replaces the fixed-step pipeline in ``auto_trade.py`` with a ReAct agent
that can dynamically choose which tools to call and how deeply to analyze.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from langgraph.prebuilt import create_react_agent

SRC_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(SRC_DIR))

from agents.llm import get_chat_model
from agents.portfolio_helpers import (
    count_positions,
    get_held_positions,
    get_held_tickers,
    get_max_positions,
)
from agents.runner import run_trade_cmd
from agents.tools import ALL_TOOLS
from infra.container import get_container

JST = timezone(timedelta(hours=9))
DIARY_DIR = Path(__file__).resolve().parent.parent.parent.parent / "diary"
CONFIG_DIR = Path(__file__).resolve().parent.parent.parent.parent / "config"
PROMPT_SCENARIOS_PATH = CONFIG_DIR / "prompt_scenarios.json"


# ---------------------------------------------------------------------------
# Scenario detection
# ---------------------------------------------------------------------------

def _load_prompt_scenarios() -> dict[str, Any]:
    """Load prompt scenario config. Returns empty dict on failure."""
    try:
        with open(PROMPT_SCENARIOS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"⚠️  prompt_scenarios.json load failed: {e}", file=sys.stderr)
        return {}


def _detect_scenario(macro_result: dict[str, Any] | None, scenarios_cfg: dict[str, Any]) -> str:
    """Map a macro result to a scenario name: risk_on / neutral / risk_off.

    Falls back to 'neutral' on any error or missing data.
    """
    if not macro_result or "error" in macro_result:
        return "neutral"
    try:
        score = float(macro_result.get("market_environment", {}).get("score", 0))
    except (TypeError, ValueError):
        return "neutral"

    thresholds = scenarios_cfg.get("score_thresholds", {})
    on_th = float(thresholds.get("risk_on", 20))
    off_th = float(thresholds.get("risk_off", -20))
    if score >= on_th:
        return "risk_on"
    if score <= off_th:
        return "risk_off"
    return "neutral"


def _fetch_macro_safe() -> dict[str, Any] | None:
    """Fetch macro data without raising. Used for scenario routing."""
    try:
        import contextlib
        import io

        from core.macro import fetch_macro  # imported lazily to avoid slow startup

        # fetch_macro prints JSON; capture to avoid log noise
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = fetch_macro()
        return result
    except Exception as e:
        print(f"⚠️  macro fetch failed: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# System prompt builder
# ---------------------------------------------------------------------------

def _build_system_prompt(
    market: str,
    min_score: int,
    max_signals: int,
    dry_run: bool,
    scenario: str = "neutral",
    scenarios_cfg: dict[str, Any] | None = None,
) -> str:
    """Build the system prompt with current portfolio + market-regime context."""
    held = get_held_tickers()
    positions = get_held_positions()
    cur_pos = count_positions()
    max_pos = get_max_positions()
    available = max_pos - cur_pos

    held_summary = "なし"
    if positions:
        lines = []
        for p in positions:
            pnl = p.get("pnl_pct", 0)
            lines.append(f"  - {p['ticker']}: 取得価格{p.get('entry_price', '?')}, 損益{pnl:+.1f}%")
        held_summary = "\n".join(lines)

    market_labels = {"us": "米国株", "jp": "日本株", "all": "全市場"}
    ml = market_labels.get(market, market)

    # --- Scenario-specific blocks ---
    scenarios_cfg = scenarios_cfg or {}
    scen = scenarios_cfg.get("scenarios", {}).get(scenario, {})
    scen_label = scen.get("label", scenario)
    scen_desc = scen.get("description", "")
    questions = scen.get("question_order") or [
        "テクニカルシグナルは明確か",
        "リスク管理は十分か",
        "ファンダメンタルは矛盾しないか",
    ]
    extra_rules = scen.get("extra_rules", [])

    questions_block = "\n".join(f"  {i+1}. {q}" for i, q in enumerate(questions))
    extra_rules_block = "\n".join(f"- {r}" for r in extra_rules) if extra_rules else "- （追加ルールなし）"

    # Stringency by scenario (aligns with validation_rules.json)
    stringency = {
        "risk_on": "fail_conditions は 1 項目以上、invalidation_conditions は 0 項目以上（任意）",
        "neutral": "fail_conditions は 1 項目以上、invalidation_conditions は 1 項目以上",
        "risk_off": "fail_conditions は 3 項目以上、invalidation_conditions は 2 項目以上（厳格）",
    }.get(scenario, "fail_conditions は 1 項目以上、invalidation_conditions は 1 項目以上")

    return f"""あなたは株式売買判断を行うプロのトレーダーAIです。
与えられたツールを自由に使い、市場を分析して売買シグナルを生成してください。

## 現在の設定
- 対象市場: {ml}
- 最小スコア閾値: {min_score}
- 最大シグナル数: {max_signals}
- モード: {"ドライラン（注文なし）" if dry_run else "本番"}

## 🌐 市場シナリオ: {scen_label} ({scenario})
{scen_desc}

## ポートフォリオ状況
- ポジション: {cur_pos}/{max_pos} (空き: {available})
- 保有銘柄:
{held_summary}

## 思考順序（このシナリオで重視する質問を上から順に検討すること）
{questions_block}

## このシナリオ特有の追加ルール
{extra_rules_block}

## 分析ツール（必要に応じて自由に呼び出す）

- `check_macro`: 市場全体のマクロ環境確認（VIX・金利・為替・主要指数）
- `analyze_events`: 地政学・金利・関税等のイベント影響分析
- `screen_stocks(market="{market}")`: 銘柄スクリーニング（保有銘柄 {', '.join(held) if held else 'なし'} は除外）
- `score_stock`: テクニカルスコア（score が {min_score} 以上のみ採用）
- `analyze_fundamentals`: 財務状況確認
- `check_sentiment`: ニュースセンチメント
- `get_technical` / `get_news` / `get_prices`: 補助データ
{("- 枠満杯。新規候補のスコアが保有最低スコアより 5 以上高い場合のみ `action: \"swap\"` で入れ替え" if available <= 0 else "")}

## 出力フォーマット（最終回答） — 必読

分析が完了したら、最終回答の **冒頭** に以下の JSON ブロックを **必ず最初に** 出力してください。
JSON の後にマークダウンの分析コメントをつけても構いません。

**JSON ブロックがない回答は無効です。必ず ```json ... ``` で囲んだ JSON を最初に出力すること。**
シグナルが0件でも `"signals": []` として JSON を出力してください。
分析コメントは JSON の後に **簡潔に（500字以内で）** 記述してください。

```json
{{
  "signals": [
    {{
      "ticker": "AAPL",
      "action": "buy",
      "score": 45,
      "confidence": 0.8,
      "reason": "RSI売られすぎ + MACD ゴールデンクロス接近",
      "entry_price": 0,
      "target_price": 185.0,
      "stop_loss_price": 170.0,
      "take_profit_price": 195.0,
      "timespan": "swing",
      "fail_conditions": [
        "RSI逆張り失敗 (現在RSI=75, 過去10日高点更新の可能性)",
        "MACD乖離からの反発リスク (乖離が中期的に修正される可能性)",
        "出来高不足でのスリップ (平均出来高比30%以下の可能性)"
      ],
      "invalidation_conditions": [
        "マクロリスク中は様子見 (VIX>25の場合は買い控え)",
        "セクター集中警告 (テック比率既に20%, 更に+5%で過集中)"
      ],
      "exit_plan": "逆指値 170.0 で損切り、もしくは +5% で利確"
    }}
  ],
  "market_comment": "市場環境の概要と判断理由",
  "skipped": [
    {{"ticker": "MSFT", "reason": "スコア不足 (score=5)"}}
  ]
}}
```

## 重要ルール
- デッドキャットバウンス、出来高なしの上昇、過度なボラティリティには注意
- 「売り」判定の銘柄はシグナルに含めない
- 根拠のない推測は避け、データに基づいて判断
- 不確実性が高い場合は正直にその旨を伝える
- シグナルが0件でも構わない（無理に買わない）

## 🚨 必須: 反証ゲート対応（現シナリオ「{scen_label}」の要件）

各シグナルには以下の **必須フィールド** を含めてください。機械的ゲートで検証されます。

- **fail_conditions** (配列): シグナルが失敗する具体的条件
- **invalidation_conditions** (配列): シグナルを無効化する条件
- **exit_plan** (文字列): 撤退・利確条件の具体的な記述

**現シナリオでの項目数要件**: {stringency}

**これらのフィールドが不足するとシグナルは自動で却下されます。** 必ず全て含めてください。

## 🚨 最重要: 最終回答には必ず JSON ブロックを含めること
分析が完了したら、マークダウンの分析コメントに加えて、
必ず上記フォーマットの ```json ブロックを含めてください。
JSON ブロックがないと注文が実行されません。
シグナルが0件でも "signals": [] として必ず JSON を出力してください。
"""



# ---------------------------------------------------------------------------
# Core graph execution
# ---------------------------------------------------------------------------

def run_trade_graph(
    market: str = "all",
    min_score: int = 10,
    max_signals: int = 2,
    dry_run: bool = True,
    provider: str = "copilot",
    model: str | None = None,
) -> dict[str, Any]:
    """Execute the LangGraph trading agent and return results.

    Returns a dict with keys: ``signals``, ``executed``, ``log``.
    """
    now = datetime.now(JST)
    file_ts = now.strftime("%Y-%m-%d_%H%M%S")
    log_lines: list[str] = []

    def log(msg: str) -> None:
        print(msg, flush=True)
        log_lines.append(msg)

    log(f"\n{'=' * 60}")
    log(f"  LangGraph 売買エージェント: {now:%Y-%m-%d %H:%M:%S} JST")
    log(f"  market={market}  min_score={min_score}  max_signals={max_signals}")
    log(f"  dry_run={dry_run}  provider={provider}")
    log(f"{'=' * 60}\n")

    # Step 1: Auto-close (non-LLM — safety-critical)
    log("Step 1: 自動クローズ判定...")
    close_out, _ = run_trade_cmd(["--check-and-close"])
    if "クローズ対象なし" in close_out:
        log("  -> クローズ対象なし")
    else:
        for line in close_out.strip().splitlines():
            if line.strip():
                log(f"  -> {line.strip()}")

    # Step 1.5: Detect market scenario (non-LLM)
    log("\nStep 1.5: 市場シナリオ判定...")
    scenarios_cfg = _load_prompt_scenarios()
    macro_result = _fetch_macro_safe()
    scenario = _detect_scenario(macro_result, scenarios_cfg)
    macro_score = None
    if macro_result and "market_environment" in macro_result:
        macro_score = macro_result["market_environment"].get("score")
    scen_label = scenarios_cfg.get("scenarios", {}).get(scenario, {}).get("label", scenario)
    log(f"  -> シナリオ: {scenario} ({scen_label})  macro_score={macro_score}")

    # Step 2: ReAct agent analysis
    log("\nStep 2: AI分析エージェント起動...")
    llm = get_chat_model(provider=provider, model=model)
    system_prompt = _build_system_prompt(
        market, min_score, max_signals, dry_run,
        scenario=scenario, scenarios_cfg=scenarios_cfg,
    )
    agent = create_react_agent(llm, ALL_TOOLS, prompt=system_prompt)

    user_msg = (
        f"現在 {now:%Y-%m-%d %H:%M} JST です。"
        f"市場（{market}）を分析して、売買シグナルを生成してください。"
        f"（現在の市場シナリオ: {scenario} / {scen_label}）"
    )

    # メインエージェント呼び出し（threading.Thread + join(timeout) による wall-clock タイムアウト）
    # asyncio.wait_for はブロッキングI/O中にキャンセル不可なので、スレッドレベルで制御する
    import threading

    _AGENT_TIMEOUT = 180
    _result_holder: list[Any] = [None]
    _error_holder: list[Any] = [None]

    def _invoke_sync() -> None:
        try:
            _result_holder[0] = agent.invoke(
                {"messages": [("user", user_msg)]},
                config={"recursion_limit": 10},
            )
        except Exception as exc:
            _error_holder[0] = exc

    _agent_thread = threading.Thread(target=_invoke_sync, daemon=True)
    _agent_thread.start()
    _agent_thread.join(timeout=_AGENT_TIMEOUT)

    if _agent_thread.is_alive():
        log(f"  -> ⚠️ AI エージェントが {_AGENT_TIMEOUT}s でタイムアウト。シグナルなしで続行します。")
        result = {"messages": []}
    elif _error_holder[0] is not None:
        log(f"  -> ⚠️ AI エージェントエラー: {_error_holder[0]}")
        result = {"messages": []}
    else:
        result = _result_holder[0] or {"messages": []}
    messages = result.get("messages", [])

    # Extract final AI message
    final_text = ""
    for msg in reversed(messages):
        if hasattr(msg, "content") and msg.content and not hasattr(msg, "tool_call_id"):
            final_text = msg.content
            break

    log("\n--- AI分析結果 ---")
    log(final_text[:2000] if len(final_text) > 2000 else final_text)

    # Step 3: Parse signals from AI output (non-LLM)
    log("\nStep 3: シグナル抽出...")
    signals = _extract_signals(final_text, max_signals)

    # If no signals found, ask LLM for a structured JSON summary
    if not signals and final_text:
        log("  -> JSON 未検出。LLM に構造化出力を要求...")
        followup_prompt = (
            "以下の分析結果を元に、売買シグナルを JSON 形式で出力してください。\n"
            "シグナルが0件なら signals を空配列にしてください。\n\n"
            f"分析結果:\n{final_text[:3000]}\n\n"
            '{"signals": [{"ticker": "XXX", "action": "buy", "score": N, "confidence": 0.X, '
            '"reason": "...", "entry_price": 0, "target_price": N, "stop_loss_price": N, '
            '"take_profit_price": N, "timespan": "swing", '
            '"fail_conditions": ["..."], '
            '"invalidation_conditions": ["..."], '
            '"exit_plan": "..."}], '
            '"market_comment": "...", "skipped": []}\n\n'
            "JSON のみを出力してください。"
        )
        try:
            from langchain_core.messages import HumanMessage
            followup_resp = llm.invoke(
                [HumanMessage(content=followup_prompt)],
                config={"timeout": 60},
            )
            followup_text = followup_resp.content if hasattr(followup_resp, "content") else ""
        except Exception as e:
            log(f"  -> フォローアップ失敗: {e}")
            followup_text = ""
        if followup_text:
            log(f"  -> フォローアップ: {followup_text[:300]}")
            signals = _extract_signals(followup_text, max_signals)

    log(f"  -> {len(signals)} 件のシグナル")

    # Step 3.5: Counterargument gate (non-LLM — signal-quality check)
    rejected: list[dict[str, Any]] = []
    if signals:
        log("\nStep 3.5: 反証ゲート検証...")
        # Map scenario to market_environment label used by validate_signals_batch
        env_map = {"risk_on": "risk_on", "neutral": "neutral", "risk_off": "risk_off"}
        market_env = env_map.get(scenario, "neutral")
        valid_sigs, invalid_sigs, details = _apply_counterargument_gate(
            signals, market_environment=market_env
        )
        for _ticker, is_valid, summary in details:
            prefix = "  ✅" if is_valid else "  ❌"
            log(f"{prefix} {summary}")
        if invalid_sigs:
            log(f"  -> {len(invalid_sigs)} 件却下、{len(valid_sigs)} 件通過")
        signals = valid_sigs
        rejected = invalid_sigs

    # Step 4: Execute signals (non-LLM — safety-critical)
    executed: list[dict[str, Any]] = []
    if signals:
        log(f"\nStep 4: 注文実行 ({len(signals)} 件)...")
        executed = _execute_signals(signals, dry_run, log)
    else:
        log("\nStep 4: 有効シグナルなし — 注文スキップ")

    # Save log
    log(f"\n{'=' * 60}")
    log(f"  サイクル完了: {datetime.now(JST):%H:%M:%S}")
    log(f"  シナリオ: {scenario} ({scen_label})")
    log(f"  注文: {len(executed)} 件")
    for e in executed:
        log(f"    {e['status']} {e['ticker']} (score={e.get('score', '?')})")
    if rejected:
        log(f"  却下: {len(rejected)} 件")
        for r in rejected:
            log(f"    ❌ {r.get('ticker', '?')}: {r.get('_gate_rejection_reason', '')}")
    log(f"{'=' * 60}\n")

    _save_log(
        file_ts, log_lines, market,
        ai_text=final_text, scenario=scenario, rejected=rejected,
    )

    return {
        "signals": signals,
        "executed": executed,
        "rejected": rejected,
        "scenario": scenario,
        "log": "\n".join(log_lines),
        "ai_output": final_text,
    }


# ---------------------------------------------------------------------------
# Signal extraction & execution (non-LLM nodes)
# ---------------------------------------------------------------------------

def _extract_signals(ai_text: str, max_signals: int) -> list[dict[str, Any]]:
    """Parse trading signals from the AI's final text output."""
    from agents.ai import parse_ai_json
    from core.screener import JP_UNIVERSE, US_UNIVERSE

    # スクリーナーのユニバースに含まれるティッカーのみ許可
    _valid_tickers = set(US_UNIVERSE + JP_UNIVERSE)

    parsed = parse_ai_json(ai_text)
    if not parsed:
        return []

    raw_signals = parsed.get("signals", [])
    signals: list[dict[str, Any]] = []
    for s in raw_signals[:max_signals]:
        action = s.get("action", "buy")
        if action not in ("buy", "swap"):
            continue
        ticker = s.get("ticker", "")
        if ticker not in _valid_tickers:
            print(f"  ⚠️ ティッカー '{ticker}' はユニバース外 — スキップ", flush=True)
            continue
        sig = {
            "ticker": s["ticker"],
            "action": "buy",
            "entry_price": s.get("entry_price", 0),
            "target_price": s.get("target_price", 0),
            "stop_loss_price": s.get("stop_loss_price", 0),
            "take_profit_price": s.get("take_profit_price", 0),
            "confidence": s.get("confidence", 0.5),
            "timespan": s.get("timespan", "swing"),
            "score": s.get("score", 0),
            "reason": s.get("reason", "LangGraph agent"),
            # Phase 1 counterargument gate fields
            "fail_conditions": s.get("fail_conditions", []),
            "invalidation_conditions": s.get("invalidation_conditions", []),
            "exit_plan": s.get("exit_plan", ""),
        }
        if action == "swap":
            sig["sell_ticker"] = s.get("sell_ticker", "")
        signals.append(sig)
    return signals


def _apply_counterargument_gate(
    signals: list[dict[str, Any]],
    market_environment: str = "neutral",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[tuple[str, bool, str]]]:
    """Validate signals through the counterargument gate.

    Returns ``(valid, invalid, details)``. On any internal failure, all
    signals are passed through as-is (fail-open) so the gate cannot
    silently block all trading due to a bug in the gate itself.
    """
    try:
        from agents.gates import validate_signals_batch
        return validate_signals_batch(signals, market_environment=market_environment)
    except Exception as e:
        print(f"⚠️  counterargument gate error (fail-open): {e}", file=sys.stderr)
        details = [(s.get("ticker", "?"), True, f"gate-bypass: {e}") for s in signals]
        return list(signals), [], details


def _execute_signals(
    signals: list[dict[str, Any]],
    dry_run: bool,
    log: Any,
) -> list[dict[str, Any]]:
    """Execute parsed trading signals."""
    executed: list[dict[str, Any]] = []
    if dry_run:
        for sig in signals:
            log(f"  [DRY] {sig.get('action', 'buy').upper()} {sig['ticker']} score={sig.get('score', '?')}")
            executed.append({"ticker": sig["ticker"], "status": "DRY_RUN", "score": sig.get("score", 0)})
        return executed

    diary = get_container().diary()
    for sig in signals:
        # Handle swap: sell first, then buy
        if sig.get("sell_ticker"):
            sell_ticker = sig["sell_ticker"]
            positions = get_held_positions()
            sell_pos = next((p for p in positions if p["ticker"] == sell_ticker), None)
            if sell_pos:
                qty = sell_pos.get("quantity", 0)
                log(f"  売り: {sell_ticker} {qty}株...")
                out, rc = run_trade_cmd(["--close", sell_ticker, str(qty)])
                ok = rc == 0 and "FILLED" in out
                log(f"    {'✅' if ok else '❌'} {sell_ticker} {'クローズ完了' if ok else 'クローズ失敗'}")
                if not ok:
                    executed.append({"ticker": sell_ticker, "status": "SELL_FAILED", "score": 0})
                    continue
                executed.append({"ticker": sell_ticker, "status": "SOLD", "score": 0})

        # Buy — 成行注文（シミュレーターが指値PENDINGに非対応のため）
        buy_sig = {k: v for k, v in sig.items() if k != "sell_ticker"}
        buy_sig["entry_price"] = 0  # MARKET order
        sig_name = f"{sig['ticker'].replace('.', '')}_auto.json"
        sig_path = diary.save_signal(sig_name, buy_sig)
        out, rc = run_trade_cmd(["--from-signal", sig_path])
        ok = rc == 0 and "FILLED" in out
        log(f"  {'✅' if ok else '❌'} {sig['ticker']} {'約定成功' if ok else '約定失敗'}")
        executed.append(
            {"ticker": sig["ticker"], "status": "FILLED" if ok else "FAILED", "score": sig.get("score", 0)}
        )
    return executed


def _save_log(
    file_ts: str,
    lines: list[str],
    market: str,
    *,
    ai_text: str = "",
    scenario: str = "neutral",
    rejected: list[dict[str, Any]] | None = None,
) -> None:
    market_labels = {"us": "米国株", "jp": "日本株", "all": "全市場"}
    ml = market_labels.get(market, market)
    filename = f"{file_ts}_auto_trade_agent_{ml}.md"
    body = f"# 🤖 LangGraph 売買エージェント — {ml}\n\n"
    body += f"- **シナリオ**: {scenario}\n\n"
    body += "\n".join(lines) + "\n"
    if rejected:
        body += "\n---\n\n## 反証ゲートで却下されたシグナル\n\n"
        for r in rejected:
            body += f"- **{r.get('ticker', '?')}**: {r.get('_gate_rejection_reason', '')}\n"
            missing = r.get("_missing_fields") or []
            if missing:
                body += f"  - missing: {', '.join(missing)}\n"
    if ai_text:
        body += f"\n---\n\n## AI分析全文\n\n{ai_text}\n"
    diary = get_container().diary()
    diary.save_report(filename, body)
    print(f"  log: diary/{filename}")

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
from pydantic import BaseModel, Field

SRC_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(SRC_DIR))

from agents.llm import get_chat_model
from agents.portfolio_helpers import (
    count_positions,
    daily_loss_exceeded,
    get_held_positions,
    get_held_tickers,
    get_max_positions,
    warn_overweight_positions,
)
from agents.runner import run_trade_cmd
from agents.tools import ALL_TOOLS
from infra.container import get_container

JST = timezone(timedelta(hours=9))
# __file__ = src/agents/graph_trade.py → 3 つ上がプロジェクトルート
# (旧 src/scripts/lib/ 時代の 4 階層が残っていて config が読めていなかった)
DIARY_DIR = Path(__file__).resolve().parent.parent.parent / "diary"
CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"
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
    lessons: str = "",
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

    # 過去トレードからの教訓 (振り返りループ)。空なら丸ごと省略。
    lessons_block = (
        f"\n## 📚 過去トレードからの教訓（直近実績を踏まえ判断に反映すること）\n{lessons.strip()}\n"
        if lessons.strip()
        else ""
    )

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
{lessons_block}
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
- `submit_signals`: **シグナル提出（最終ステップ・必須）**
{("- 枠満杯。新規候補のスコアが保有最低スコアより 5 以上高い場合のみ `action: \"swap\"` + sell_ticker で入れ替え" if available <= 0 else "")}

## 出力方法 — 必読

分析が完了したら、**最後に必ず `submit_signals` ツールを呼び出して**シグナルを提出してください。
- **シグナルが 0 件でも `signals=[]` で必ず呼ぶこと**（無理に買わない判断も正当）
- テキストに JSON を書くのではなく、ツール呼び出しで提出すること
- 提出後は簡潔な分析コメント（500字以内）で回答を締めくくる

各フィールドの記入例（この具体性を目安にすること）:
- reason: "RSI売られすぎ + MACD ゴールデンクロス接近"
- fail_conditions: ["RSI逆張り失敗 (現在RSI=75, 過去10日高点更新の可能性)", "出来高不足でのスリップ (平均出来高比30%以下)"]
- invalidation_conditions: ["VIX>25 に上昇した場合は買い控え", "セクター集中超過 (テック比率+5%で過集中)"]
- exit_plan: "逆指値 170.0 で損切り、もしくは +5% (195.0) で利確" ← **必ず価格・割合の数値を含める**

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
# AI exit advisor (Step 1.7) — 機械ストップは床として維持
# ---------------------------------------------------------------------------

def _ai_exit_enabled() -> bool:
    """trading_config の ai_exit_advisor フラグ (既定 False = 安全側)。"""
    try:
        from core.trade import load_config

        return bool(load_config().get("ai_exit_advisor", False))
    except Exception:
        return False


def _run_ai_exit_advisor(
    scenario: str, provider: str, model: str | None, dry_run: bool, log: Any
) -> None:
    """機械クローズ後に残った保有へ AI の早期手仕舞い助言を適用する。

    AI は exit (全株) / trim (半分) のみ助言でき、機械ストップは止められない。
    助言が得られなければ何もしない (= 機械ストップのみ)。
    """
    if not _ai_exit_enabled():
        return
    try:
        from agents.exit_advisor import advise_exits
        from core.trading_units import get_trading_unit

        log("\nStep 1.7: AI手仕舞い助言 (機械ストップは適用済み)...")
        positions = get_container().broker().get_positions()
        if not positions:
            log("  -> 保有なし")
            return
        by_ticker = {p.ticker: p for p in positions}
        actions = advise_exits(positions, scenario, provider=provider, model=model, log=log)

        for a in actions:
            ticker = a["ticker"]
            pos = by_ticker.get(ticker)
            if pos is None:
                continue
            held_qty = int(pos.quantity)
            if a["action"] == "trim":
                unit = get_trading_unit(ticker)
                qty = (held_qty // 2 // unit) * unit if unit > 0 else held_qty // 2
                if qty <= 0:
                    log(f"  -> {ticker}: trim 不可 (単元未満) — スキップ")
                    continue
            else:  # exit
                qty = held_qty

            if dry_run:
                log(f"  [DRY] AI{a['action']}: {ticker} {qty}株 — {a['reason']}")
                continue
            out, rc = run_trade_cmd(["--close", ticker, str(qty), "--source", f"ai_{a['action']}"])
            ok = rc == 0 and '"status": "FILLED"' in out
            log(f"  {'✅' if ok else '❌'} AI{a['action']}: {ticker} {qty}株 "
                f"({'約定' if ok else '失敗'}) — {a['reason']}")
    except Exception as e:
        log(f"  ⚠️ AI手仕舞いステップでエラー (機械ストップは適用済み): {e}")


def _reflect_if_enabled(provider: str, model: str | None, log: Any) -> str:
    """trading_config の ai_reflection が有効なら過去実績の教訓を返す (既定 False)。"""
    try:
        from core.trade import load_config

        if not load_config().get("ai_reflection", False):
            return ""
        from agents.reflection import reflect_on_history

        log("\nStep 1.8: 振り返り学習 (過去のクローズ実績から教訓抽出)...")
        return reflect_on_history(provider=provider, model=model, log=log)
    except Exception as e:
        log(f"  ⚠️ 振り返りステップでエラー: {e}")
        return ""


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

    # Step 0: ブローカー状態を同期 (単一インスタンスの鮮度確保)。
    #   simulator: portfolio.json 再読込 (別プロセスの CLI 変更を取り込む)
    #   kabu: 証券会社 API から再取得
    get_container().broker().sync()
    # kabu モード時はローカル portfolio.json をブローカー実態に reconcile
    from agents.auto_trade import _reconcile_if_needed

    _reconcile_if_needed(log)

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

    # Step 1.6: ポートフォリオ健全性チェック (非LLM) — 集中超過の警告
    warn_overweight_positions(log)

    # Step 1.7: AI 手仕舞い助言 (機械ストップは Step 1 で実行済み=床)。
    #   未発火の保有に対し「より早い手仕舞い」だけを助言。AI はストップを止められない。
    _run_ai_exit_advisor(scenario, provider, model, dry_run, log)

    # Step 1.8: 振り返り学習 — 過去のクローズ実績から教訓を抽出しエントリー判断に反映
    lessons = _reflect_if_enabled(provider, model, log)

    # Step 2: ReAct agent analysis
    log("\nStep 2: AI分析エージェント起動...")
    llm = get_chat_model(provider=provider, model=model)
    system_prompt = _build_system_prompt(
        market, min_score, max_signals, dry_run,
        scenario=scenario, scenarios_cfg=scenarios_cfg, lessons=lessons,
    )
    # シグナル提出は構造化ツール経由 (スキーマ強制でフィールド欠落を根絶)
    captured: dict[str, Any] = {}
    submit_tool = _make_submit_signals_tool(captured)
    agent = create_react_agent(llm, [*ALL_TOOLS, submit_tool], prompt=system_prompt)

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
                # submit_signals の呼び出し+応答で 2 ステップ余分に消費する
                config={"recursion_limit": 12},
            )
        except Exception as exc:
            _error_holder[0] = exc

    _agent_thread = threading.Thread(target=_invoke_sync, daemon=True)
    _agent_thread.start()
    _agent_thread.join(timeout=_AGENT_TIMEOUT)

    timed_out = _agent_thread.is_alive()
    if timed_out:
        log(f"  -> ⚠️ AI エージェントが {_AGENT_TIMEOUT}s でタイムアウト。シグナルなしで続行します。")
        result = {"messages": []}
    elif _error_holder[0] is not None:
        log(f"  -> ⚠️ AI エージェントエラー: {_error_holder[0]}")
        result = {"messages": []}
    else:
        result = _result_holder[0] or {"messages": []}
    messages = result.get("messages", [])
    # タイムアウト時はゾンビスレッドが後から captured を埋める可能性があるため読まない
    tool_signals = None if timed_out else captured.get("signals")

    # Extract final AI message
    final_text = ""
    for msg in reversed(messages):
        if hasattr(msg, "content") and msg.content and not hasattr(msg, "tool_call_id"):
            final_text = msg.content
            break

    # 思考トレース (ツール呼び出し列・推論) を永続化 — 可視化用。失敗してもサイクルは止めない
    try:
        _save_trace(file_ts, market, _serialize_trace(messages))
    except Exception as e:
        log(f"  ⚠️ トレース保存スキップ: {e}")

    log("\n--- AI分析結果 ---")
    log(final_text[:2000] if len(final_text) > 2000 else final_text)

    # Step 3: Parse signals from AI output (non-LLM)
    log("\nStep 3: シグナル抽出...")
    if tool_signals is not None:
        # 構造化ツール経由 (主経路): スキーマ検証済みなので正規化のみ
        log(f"  -> submit_signals ツール経由で受理 ({len(tool_signals)} 件)")
        signals = _normalize_signals(tool_signals, max_signals)
    else:
        # フォールバック: ツール未呼び出し時はテキストから JSON を抽出
        signals = _extract_signals(final_text, max_signals)

    # If no signals found, ask LLM for a structured JSON summary
    if tool_signals is None and not signals and final_text:
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

    # Step 3.6: 日次損失サーキットブレーカー (非LLM) — 自動クローズは実行済みだが新規買いは止める
    if signals and daily_loss_exceeded(log):
        log("  → 日次損失上限を超過したため新規買いをスキップ")
        rejected = rejected + [
            {"ticker": s.get("ticker", "?"), "_gate_rejection_reason": "daily_loss_limit"}
            for s in signals
        ]
        signals = []

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

def _normalize_signals(
    raw_signals: list[dict[str, Any]], max_signals: int
) -> list[dict[str, Any]]:
    """生シグナルを検証・正規化する (ユニバース外却下・action 限定・フィールド整形)。

    submit_signals ツール経由とテキスト JSON パース経由の両方で共通利用する。
    """
    from core.screener import JP_UNIVERSE, US_UNIVERSE

    # スクリーナーのユニバースに含まれるティッカーのみ許可
    _valid_tickers = set(US_UNIVERSE + JP_UNIVERSE)

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


def _extract_signals(ai_text: str, max_signals: int) -> list[dict[str, Any]]:
    """Parse trading signals from the AI's final text output (fallback path)."""
    from agents.ai import parse_ai_json

    parsed = parse_ai_json(ai_text)
    if not parsed:
        return []
    return _normalize_signals(parsed.get("signals", []), max_signals)


class TradeSignalArg(BaseModel):
    """売買シグナル 1 件。"""

    ticker: str = Field(description="ティッカー (例 7203.T)")
    action: str = Field(default="buy", description='"buy" または "swap"')
    score: float = Field(default=0, description="テクニカルスコア")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    reason: str = Field(default="", description="売買根拠")
    entry_price: float = Field(default=0, description="0 = 成行")
    target_price: float = 0
    stop_loss_price: float = 0
    take_profit_price: float = 0
    timespan: str = Field(default="swing", description='"short"|"swing"|"medium"')
    fail_conditions: list[str] = Field(
        default_factory=list, description="このトレードが失敗する具体的条件"
    )
    invalidation_conditions: list[str] = Field(
        default_factory=list, description="シグナルの前提が崩れる条件"
    )
    exit_plan: str = Field(default="", description="価格水準を含む具体的な撤退計画")
    sell_ticker: str = Field(default="", description="swap 時のみ: 売却対象ティッカー")


class SubmitSignalsArgs(BaseModel):
    """submit_signals ツールの引数スキーマ。"""

    signals: list[TradeSignalArg] = Field(description="提出する売買シグナル (0件なら空配列)")
    market_comment: str = Field(default="", description="市場環境の概要と判断理由")


def _make_submit_signals_tool(captured: dict[str, Any]) -> Any:
    """シグナル提出用の構造化ツールを生成する。

    エージェントの最終出力をテキスト JSON ではなくツール呼び出しにすることで、
    フィールド欠落・JSON パース失敗という故障クラスをスキーマ検証層で根絶する
    (検証エラーは LangGraph がエージェントに差し戻して再試行させる)。
    ※ pydantic モデルはモジュールレベルに置くこと — `from __future__ import annotations`
      環境では関数内ローカル型を tool デコレータが解決できない。
    """
    from langchain_core.tools import StructuredTool

    def _submit(signals: list[TradeSignalArg], market_comment: str = "") -> str:
        captured["signals"] = [s.model_dump() for s in signals]
        captured["market_comment"] = market_comment
        return f"{len(signals)} 件のシグナルを受理しました。簡潔な分析コメントで回答を終えてください。"

    return StructuredTool.from_function(
        func=_submit,
        name="submit_signals",
        description=(
            "売買シグナルを提出する。分析が完了したら最後に必ずこのツールを呼ぶこと。"
            "シグナルが 0 件の場合も signals=[] で必ず呼ぶ。"
        ),
        args_schema=SubmitSignalsArgs,
    )


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
                out, rc = run_trade_cmd(["--close", sell_ticker, str(qty), "--source", "swap"])
                ok = rc == 0 and '"status": "FILLED"' in out
                log(f"    {'✅' if ok else '❌'} {sell_ticker} {'クローズ完了' if ok else 'クローズ失敗'}")
                if not ok:
                    executed.append({"ticker": sell_ticker, "status": "SELL_FAILED", "score": 0})
                    continue
                executed.append({"ticker": sell_ticker, "status": "SOLD", "score": 0})

        # 資金・評価額の事前チェック (買えないシグナルで発注枠と FAILED ログを浪費しない)
        # swap (売り→買い) は売却代金が入るためスキップし、RiskManager に委ねる
        est_price = float(sig.get("target_price") or 0)
        if not sig.get("sell_ticker") and est_price > 0:
            from agents.auto_trade import _cash_by_currency, _order_cost
            from agents.portfolio_helpers import USD_JPY_APPROX, total_equity_jpy

            cash = _cash_by_currency(get_container().broker().get_balance())
            ccy, _lot, est_cost = _order_cost(sig["ticker"], est_price)
            if est_cost > cash[ccy]:
                log(
                    f"  💰 {sig['ticker']}: {ccy}資金不足 "
                    f"(1単元≈{est_cost:,.0f} {ccy}, 残高{cash[ccy]:,.0f} {ccy}) -> スキップ"
                )
                executed.append({"ticker": sig["ticker"], "status": "SKIPPED_FUNDS", "score": sig.get("score", 0)})
                continue
            try:
                from core.trade import load_risk_limits

                max_pos_pct = float(load_risk_limits().get("max_position_size_pct", 30))
            except Exception:
                max_pos_pct = 30.0
            equity = total_equity_jpy()
            cap = equity * max_pos_pct / 100 if equity > 0 else 0.0
            cap_ccy = cap if ccy == "JPY" else cap / USD_JPY_APPROX
            if cap > 0 and est_cost > cap_ccy:
                log(
                    f"  📏 {sig['ticker']}: 1単元(≈{est_cost:,.0f} {ccy})が評価額上限"
                    f"({max_pos_pct:.0f}%≈{cap_ccy:,.0f} {ccy})超 -> スキップ"
                )
                executed.append({"ticker": sig["ticker"], "status": "SKIPPED_CAP", "score": sig.get("score", 0)})
                continue

        # Buy — 成行注文（シミュレーターが指値PENDINGに非対応のため）
        buy_sig = {k: v for k, v in sig.items() if k != "sell_ticker"}
        buy_sig["entry_price"] = 0  # MARKET order
        sig_name = f"{sig['ticker'].replace('.', '')}_auto.json"
        sig_path = diary.save_signal(sig_name, buy_sig)
        out, rc = run_trade_cmd(["--from-signal", sig_path])
        ok = rc == 0 and '"status": "FILLED"' in out
        log(f"  {'✅' if ok else '❌'} {sig['ticker']} {'約定成功' if ok else '約定失敗'}")
        executed.append(
            {"ticker": sig["ticker"], "status": "FILLED" if ok else "FAILED", "score": sig.get("score", 0)}
        )
    return executed


def _serialize_trace(messages: list[Any]) -> list[dict[str, Any]]:
    """LangGraph の messages を可視化用の構造化ステップ列に変換する (純関数)。

    各ステップ: user / reasoning / tool_call(tool,args) / tool_result(tool,summary) / final
    """
    steps: list[dict[str, Any]] = []
    for m in messages:
        content = getattr(m, "content", "") or ""
        mtype = getattr(m, "type", "")  # LangChain BaseMessage.type: human/ai/tool
        if mtype == "tool" or getattr(m, "tool_call_id", None) is not None:  # ToolMessage
            steps.append({
                "type": "tool_result",
                "tool": getattr(m, "name", "?"),
                "summary": str(content)[:400],
            })
            continue
        tool_calls = getattr(m, "tool_calls", None)
        if tool_calls:  # AIMessage with tool calls
            if str(content).strip():
                steps.append({"type": "reasoning", "text": str(content)[:600]})
            for tc in tool_calls:
                steps.append({
                    "type": "tool_call",
                    "tool": tc.get("name", "?") if isinstance(tc, dict) else "?",
                    "args": tc.get("args", {}) if isinstance(tc, dict) else {},
                })
            continue
        if mtype == "human" or type(m).__name__ == "HumanMessage":
            steps.append({"type": "user", "text": str(content)[:300]})
        elif str(content).strip():  # final AIMessage
            steps.append({"type": "final", "text": str(content)[:2000]})
    return steps


def _save_trace(file_ts: str, market: str, steps: list[dict[str, Any]]) -> None:
    """思考トレースを diary/traces/<cycle>.json に保存する。"""
    if not steps:
        return
    traces_dir = DIARY_DIR / "traces"
    traces_dir.mkdir(parents=True, exist_ok=True)
    (traces_dir / f"{file_ts}_{market}.json").write_text(
        json.dumps({"file_ts": file_ts, "market": market, "steps": steps},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


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

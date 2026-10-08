"""LangGraph-based trading agent.

Replaces the fixed-step pipeline in ``auto_trade.py`` with a ReAct agent
that can dynamically choose which tools to call and how deeply to analyze.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel, Field, model_validator

SRC_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(SRC_DIR))

from agents.llm import get_chat_model
from agents.portfolio_helpers import (
    count_positions,
    daily_loss_exceeded,
    get_held_positions,
    get_held_tickers,
    get_max_positions,
    record_equity_snapshot,
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


def _fetch_regime_safe() -> Any:
    """レジームゲート (issue #19) の判定。失敗時は None (呼び出し側でフェイルオープン)。"""
    try:
        from core.regime import fetch_regime

        return fetch_regime()
    except Exception as e:
        print(f"⚠️  regime gate error (fail-open): {e}", file=sys.stderr)
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
    hard_stats: str = "",
    max_unit_cost_jpy: float | None = None,
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

    # 現金・セクターの現況 (issue #21)。現金下限・セクター上限は機械的に強制されるため、
    # 執行不能な候補を選ばないよう数字を見せる (従来は現金残高がプロンプトに無かった)
    try:
        from agents.auto_trade import _cash_by_currency
        from agents.portfolio_helpers import USD_JPY_APPROX

        lim21 = _sizing_limits()
        cash21 = _cash_by_currency(get_container().broker().get_balance())
        cash_jpy = cash21["JPY"] + cash21["USD"] * USD_JPY_APPROX
        holdings21, equity21 = _holdings_with_sectors()
        cash_pct = cash_jpy / equity21 * 100 if equity21 > 0 else 0.0
        sec_w: dict[str, float] = {}
        for _t, _s, _v in holdings21:
            sec_w[_s] = sec_w.get(_s, 0.0) + (_v / equity21 * 100 if equity21 > 0 else 0.0)
        sector_line = ", ".join(
            f"{s_} {w:.0f}%" for s_, w in sorted(sec_w.items(), key=lambda kv: -kv[1])
        ) or "なし"
        cash_block = (
            f"- 現金: ¥{cash_jpy:,.0f}（総資産比 {cash_pct:.0f}%。下限 {lim21['min_cash_pct']:.0f}% を"
            "割る買いは執行されない）\n"
            f"- 機械的上限: 1 銘柄 {lim21['max_position_size_pct']:.0f}% / 同一セクター "
            f"{lim21['max_sector_pct']:.0f}%（年率ボラ {lim21['vol_half_size_above_pct']:.0f}% 超は"
            f"サイズ 1/2、{lim21['vol_third_size_above_pct']:.0f}% 超は 1/3）\n"
            f"- 保有セクター比率: {sector_line}"
        )
    except Exception as e:
        print(f"⚠️  cash/sector block unavailable: {e}", file=sys.stderr)
        cash_block = ""

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

    # 実測期待値 (ハード統計)。LLM 散文の教訓と別に、反論しにくい数字を渡す。
    hard_stats_block = (
        f"\n## 📊 実測期待値（あなた自身の過去成績。数字に基づき判断すること）\n{hard_stats.strip()}\n"
        if hard_stats.strip()
        else ""
    )

    # 執行可能上限: 1単元コストがこれを超える銘柄は執行段階で必ず弾かれるため、
    # シグナル枠の無駄撃ちを防ぐようプロンプトで事前に伝える。
    affordability_line = (
        f"- 執行可能上限: 1単元コスト ¥{max_unit_cost_jpy:,.0f} 以下の銘柄のみ"
        "（超える銘柄はシグナルを出しても執行されない。選ばないこと）"
        if max_unit_cost_jpy and max_unit_cost_jpy > 0
        else ""
    )

    # 回転抑制の実値 (config が真実) と再エントリー禁止中の銘柄 (issue #19)。
    # 禁止銘柄を提示しないと、反証ゲートで却下される銘柄を毎サイクル分析し直す
    try:
        from core.churn_guard import (
            DEFAULT_MIN_HOLD_BDAYS,
            DEFAULT_REENTRY_COOLDOWN_BDAYS,
            reentry_banned,
        )
        from core.trade import load_risk_limits

        _lim = load_risk_limits()
        min_hold_bdays = int(_lim.get("min_hold_business_days", DEFAULT_MIN_HOLD_BDAYS))
        cooldown_bdays = int(
            _lim.get("reentry_cooldown_business_days", DEFAULT_REENTRY_COOLDOWN_BDAYS)
        )
        banned = reentry_banned(cooldown_bdays=cooldown_bdays)
    except Exception as e:
        print(f"⚠️  churn config/banned list unavailable: {e}", file=sys.stderr)
        min_hold_bdays, cooldown_bdays, banned = 2, 20, {}
    banned_line = (
        "- **再エントリー禁止中（機械的に却下される。候補に含めないこと）**: "
        + ", ".join(f"{t} (〜{d:%m/%d})" for t, d in banned.items())
        if banned
        else ""
    )

    # 入口の測定値ゲート (issue #20) の閾値 — プロンプトと機械却下で同じ数字を使う
    try:
        from core.entry_gate import load_entry_gate_config

        _eg = load_entry_gate_config()
    except Exception:
        _eg = {}
    eg_enabled = bool(_eg.get("enabled", True))
    eg_bb = float(_eg.get("max_bb_position_pct", 85))
    eg_rsi = float(_eg.get("max_rsi", 65))
    eg_score = float(_eg.get("min_score", 40))
    eff_min_score = max(float(min_score), eg_score) if eg_enabled else float(min_score)
    entry_gate_block = (
        f"- **入口の測定値ゲート（submit 時に score_stock を再計測して機械的に却下）**: "
        f"BB 位置 {eg_bb:.0f}% 以下・RSI {eg_rsi:.0f} 以下・score {eg_score:.0f} 以上を全て満たす銘柄のみ提出可。\n"
        "  過熱した銘柄（BB 85% 超は 10 件中 7 敗で計 -103,959円、RSI 65 超は 3 件全敗）は押し目を待つこと。\n"
        "  confidence の自己申告や fail_conditions の文面はゲートに使わない（数字でしか通らない）"
        if eg_enabled
        else "- **入口の原則**: BB 上限超え・RSI 過熱・出来高薄の銘柄は買わずに押し目を待つこと"
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
- 最小スコア閾値: {eff_min_score:.0f}
- 最大シグナル数: {max_signals}
- モード: {"ドライラン（注文なし）" if dry_run else "本番"}
{affordability_line}

## 🌐 市場シナリオ: {scen_label} ({scenario})
{scen_desc}
{lessons_block}{hard_stats_block}
## ポートフォリオ状況
- ポジション: {cur_pos}/{max_pos} (空き: {available})
- 保有銘柄:
{held_summary}
{cash_block}

## 思考順序（このシナリオで重視する質問を上から順に検討すること）
{questions_block}

## このシナリオ特有の追加ルール
{extra_rules_block}

## 分析ツール（必要に応じて自由に呼び出す）

- `check_macro`: 市場全体のマクロ環境確認（VIX・金利・為替・主要指数）
- `analyze_events`: 地政学・金利・関税等のイベント影響分析
- `screen_stocks(market="{market}")`: 銘柄スクリーニング（保有銘柄 {', '.join(held) if held else 'なし'} は除外）
- `score_stock`: テクニカルスコア（score が {eff_min_score:.0f} 以上のみ採用。BB 位置・RSI の実値は indicators に出る）
- `analyze_fundamentals`: 財務状況確認
- `check_sentiment`: ニュースセンチメント
- `get_technical` / `get_news` / `get_prices`: 補助データ
- `sector_strength`: セクター別騰落率（候補銘柄のセクターが逆風でないか確認）
- `market_calendar`: **今後の経済イベント予定（FOMC・日銀会合・米CPI・雇用統計・SQ）**。
  重要イベントの直前は新規エントリーを避けるか確信度を下げること。1サイクル1回まで
- `deep_research`: **ニュース記事の本文まで読んだ詳細リサーチ**（決算・上方修正・自社株買い等の
  具体的な数値を抽出）。他ツールは見出しと指標しか見ないため、これが唯一本文を読む手段。
  内部で別 AI が動くため 30〜60 秒かかる。**submit_signals に載せる最終候補 1〜2 銘柄にだけ**
  使い、スクリーニング段階では使わないこと
- `submit_signals`: **シグナル提出（最終ステップ・必須）**
{("- 枠満杯。新規候補のスコアが保有最低スコアより 5 以上高い場合のみ `action: \"swap\"` + sell_ticker で入れ替え" if available <= 0 else "")}
- **回転抑制ルール（機械的に強制される）**: 取得から{min_hold_bdays}営業日未満の銘柄は swap で売れない
  （fail_conditions の発動を reason に明記した場合を除く）。売却から{cooldown_bdays}営業日未満の銘柄は
  買い直せない（過去実績: 再エントリー 14 件は勝率 21%・計 -71,919円）。
  短期の往復はスプレッドを失うだけなので、確信が持てない銘柄はそもそも買わないこと
{banned_line}
{entry_gate_block}

## 出力方法 — 必読

分析が完了したら、**最後に必ず `submit_signals` ツールを呼び出して**シグナルを提出してください。
- **シグナルが 0 件でも `signals=[]` で必ず呼ぶこと**（無理に買わない判断も正当）
- **価格は `stop_loss < target <= take_profit` を厳守**。`target_price` は執行の目標
  （現在値近辺）であり、アナリスト目標株価などの上値メドは reason に書くこと
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

        # 最小保有期間ゲート: 建てた直後の AI 手仕舞いは早刈り (勝ちを伸ばせない +
        # 同日回転のコスト) になりやすい。min_hold 暦日未満は助言対象から外す。
        # 下方リスクは機械ストップ (損切り/max_loss) が引き続き守る。
        from agents.portfolio_helpers import held_calendar_days
        from core.trade import load_config

        min_hold = int(load_config().get("ai_exit_min_hold_days", 1))
        eligible = [p for p in positions if held_calendar_days(p.entry_time) >= min_hold]
        if len(eligible) < len(positions):
            log(
                f"  -> {len(positions) - len(eligible)} 件は保有 {min_hold} 暦日未満のため"
                "対象外 (機械ストップのみ)"
            )
        if not eligible:
            log("  -> 対象なし (全て最小保有期間内)")
            return

        by_ticker = {p.ticker: p for p in eligible}
        actions = advise_exits(eligible, scenario, provider=provider, model=model, log=log)

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

            # trim 連射による最低保有ガード迂回の封鎖 (issue #13)。
            # 最低保有期間内は当日AI売り合算50%まで。機械ストップはこの経路を通らない
            from core.churn_guard import check_ai_sell

            ok_sell, sell_msg = check_ai_sell(
                pos.entry_time, ticker, qty, held_qty
            )
            if not ok_sell:
                log(f"  🔒 AI{a['action']} 却下 ({ticker}): {sell_msg}")
                continue

            if dry_run:
                log(f"  [DRY] AI{a['action']}: {ticker} {qty}株 — {a['reason']}")
                continue
            out, rc = run_trade_cmd(["--close", ticker, str(qty), "--source", f"ai_{a['action']}"])
            ok = rc == 0 and '"status": "FILLED"' in out
            if ok:
                log(f"  ✅ AI{a['action']}: {ticker} {qty}株 (約定) — {a['reason']}")
            else:
                log(f"  ❌ AI{a['action']}: {ticker} {qty}株 (失敗 rc={rc}: {_result_reason(out)}) — {a['reason']}")
    except Exception as e:
        log(f"  ⚠️ AI手仕舞いステップでエラー (機械ストップは適用済み): {e}")


_AI_FLAG_KEYS = ("ai_exit_advisor", "ai_reflection", "ai_portfolio_review")


def _record_ai_flags() -> None:
    """AIフラグ構成が前回と変われば diary/ai_flags.jsonl に追記 (期間A/B計測用)。"""
    try:
        from core.trade import load_config

        cfg = load_config()
        flags = {k: bool(cfg.get(k, False)) for k in _AI_FLAG_KEYS}
        path = DIARY_DIR / "ai_flags.jsonl"
        last = None
        if path.exists():
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            if lines:
                last = json.loads(lines[-1]).get("flags")
        if last != flags:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(
                    {"ts": datetime.now(UTC).isoformat(), "flags": flags},
                    ensure_ascii=False,
                ) + "\n")
    except Exception as e:
        print(f"⚠️ ai_flags 記録スキップ: {e}", file=sys.stderr)


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

def _hard_stats_safe() -> str:
    """実測期待値のプロンプト注入テキスト。失敗時は空 (プロンプトに足さない)。"""
    try:
        from core.trade_stats import expectancy, format_for_prompt

        return format_for_prompt(expectancy(days=120))
    except Exception:
        return ""


def _max_unit_cost_jpy_safe() -> float | None:
    """執行可能な1単元コスト上限 (総資産 × max_position_size_pct)。失敗時 None。"""
    try:
        from agents.portfolio_helpers import total_equity_jpy
        from core.trade import load_risk_limits

        max_pct = float(load_risk_limits().get("max_position_size_pct", 20))
        equity = total_equity_jpy()
        return equity * max_pct / 100 if equity > 0 else None
    except Exception:
        return None


def _log_signal_outcomes(
    executed: list[dict[str, Any]], rejected: list[dict[str, Any]], scenario: str
) -> None:
    """全シグナル結果を diary/signals_log.jsonl に追記する (P5: 仮想追跡)。

    採用 (FILLED) だけでなく却下・スキップも記録することで、
    「ゲートは良いシグナルを殺していないか」を後から検証できる。
    失敗してもサイクルは止めない。
    """
    try:
        diary = get_container().diary()
        ts = datetime.now(UTC).isoformat()
        for e in executed:
            if e.get("status") == "DRY_RUN":
                continue
            diary.append_signal_log({
                "ts": ts,
                "scenario": scenario,
                "ticker": e.get("ticker"),
                "status": e.get("status"),
                "score": e.get("score"),
                "measured": e.get("measured"),
            })
        for r in rejected:
            diary.append_signal_log({
                "ts": ts,
                "scenario": scenario,
                "ticker": r.get("ticker"),
                "status": f"REJECTED:{r.get('_gate_rejection_reason', 'gate')}",
                "score": r.get("score"),
            })
    except Exception as e:
        print(f"⚠️ signal log skipped: {e}", file=sys.stderr)


def _filter_affordable_signals(
    signals: list[dict[str, Any]], log: Any
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """1単元コストが評価額上限を超える買いシグナルを事前に除外する。

    swap (売却代金が入る) と価格不明のシグナルは判断できないため通す。
    失敗時は全件通す (執行段階の SKIPPED_CAP が最終防衛線)。
    """
    try:
        cap_jpy = _max_unit_cost_jpy_safe()
        if not cap_jpy:
            return signals, []
        from agents.auto_trade import _order_cost
        from agents.portfolio_helpers import USD_JPY_APPROX

        kept: list[dict[str, Any]] = []
        dropped: list[dict[str, Any]] = []
        for sig in signals:
            est_price = float(sig.get("target_price") or 0)
            if sig.get("sell_ticker") or est_price <= 0:
                kept.append(sig)
                continue
            try:
                ccy, _lot, est_cost = _order_cost(sig["ticker"], est_price)
            except Exception:
                kept.append(sig)
                continue
            cost_jpy = est_cost if ccy == "JPY" else est_cost * USD_JPY_APPROX
            if cost_jpy > cap_jpy:
                log(
                    f"  📏 {sig['ticker']}: 1単元≈¥{cost_jpy:,.0f} が評価額上限"
                    f" ¥{cap_jpy:,.0f} 超 → ゲート前に除外"
                )
                dropped.append({
                    "ticker": sig.get("ticker", "?"),
                    "score": sig.get("score", 0),
                    "_gate_rejection_reason": "unaffordable_unit_cost",
                })
            else:
                kept.append(sig)
        return kept, dropped
    except Exception:
        return signals, []


def run_trade_graph(
    market: str = "all",
    min_score: int = 10,
    max_signals: int = 2,
    dry_run: bool = True,
    provider: str = "claude_code",
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
    _record_ai_flags()  # 期間A/B計測用にこのサイクルの AI フラグ構成を記録
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

    # Step 1.55: レジームゲート (非LLM, issue #19)。指数が上昇トレンド中 (TOPIX > SMA20 かつ
    #   5日騰落 > 0) でなければ新規エントリー判断 (Step 2) を起動しない。
    #   41 建玉の実測: 成立局面 21 件 +92,790 円 (勝率 52%) / 不成立局面 20 件 -120,761 円 (35%)。
    #   出口 (Step 1 機械ストップ・Step 1.7 AI 手仕舞い) は局面に関係なく常時稼働する
    log("\nStep 1.55: レジームゲート判定...")
    regime = _fetch_regime_safe()
    regime_summary = regime.summary() if regime is not None else "判定不能 → フェイルオープン"
    entries_allowed = regime is None or bool(regime.ok)
    log(f"  -> レジーム: {regime_summary}")

    def _finalize(
        signals: list[dict[str, Any]],
        executed: list[dict[str, Any]],
        rejected: list[dict[str, Any]],
        final_text: str,
    ) -> dict[str, Any]:
        """サイクル末尾の共通処理 (追跡ログ・資産スナップショット・ログ保存・戻り値)。"""
        # シグナル追跡ログ (P5): 採用/却下/スキップの全結果を追記し、
        # 「却下したシグナルはその後どうなったか」を仮想追跡できるようにする
        _log_signal_outcomes(executed, rejected, scenario)

        # 総資産スナップショット (ベンチマーク比較用の時系列。失敗しても続行)
        record_equity_snapshot()

        log(f"\n{'=' * 60}")
        log(f"  サイクル完了: {datetime.now(JST):%H:%M:%S}")
        log(f"  シナリオ: {scenario} ({scen_label})")
        log(f"  レジーム: {regime_summary}")
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
            ai_text=final_text, scenario=scenario, rejected=rejected, regime=regime_summary,
        )

        return {
            "signals": signals,
            "executed": executed,
            "rejected": rejected,
            "scenario": scenario,
            "regime": regime.to_dict() if regime is not None else None,
            "entries_allowed": entries_allowed,
            "log": "\n".join(log_lines),
            "ai_output": final_text,
        }

    # Step 1.6: ポートフォリオ健全性チェック (非LLM) — 集中超過の警告
    warn_overweight_positions(log)

    # Step 1.7: AI 手仕舞い助言 (機械ストップは Step 1 で実行済み=床)。
    #   未発火の保有に対し「より早い手仕舞い」だけを助言。AI はストップを止められない。
    _run_ai_exit_advisor(scenario, provider, model, dry_run, log)

    if not entries_allowed:
        log("\nStep 2: レジーム不成立のため新規エントリー判断 (メインエージェント) をスキップ")
        log("  -> 既存ポジションは機械ストップと AI 手仕舞いのみで管理。次サイクルで再判定")
        return _finalize([], [], [], "")

    # Step 1.8: 振り返り学習 — 過去のクローズ実績から教訓を抽出しエントリー判断に反映
    lessons = _reflect_if_enabled(provider, model, log)

    # Step 2: ReAct agent analysis
    log("\nStep 2: AI分析エージェント起動...")
    system_prompt = _build_system_prompt(
        market, min_score, max_signals, dry_run,
        scenario=scenario, scenarios_cfg=scenarios_cfg, lessons=lessons,
        hard_stats=_hard_stats_safe(), max_unit_cost_jpy=_max_unit_cost_jpy_safe(),
    )
    # シグナル提出は構造化ツール経由 (スキーマ強制でフィールド欠落を根絶)
    captured: dict[str, Any] = {}
    submit_tool = _make_submit_signals_tool(captured)

    user_msg = (
        f"現在 {now:%Y-%m-%d %H:%M} JST です。"
        f"市場（{market}）を分析して、売買シグナルを生成してください。"
        f"（現在の市場シナリオ: {scenario} / {scen_label}）"
    )

    timed_out = False
    final_text = ""
    trace_steps: list[dict[str, Any]] = []

    if provider == "claude_code":
        # Claude Agent SDK (サブスク認証・headless) 経路 — litellm を使わない
        from agents.claude_agent import run_trade_agent_via_sdk

        log("  -> Claude Agent SDK (サブスク) 経路")
        try:
            final_text, trace_steps = run_trade_agent_via_sdk(
                system_prompt, user_msg, [*ALL_TOOLS, submit_tool],
                model=model or "sonnet", log=log,
            )
        except TimeoutError:
            log("  -> ⚠️ AI エージェントがタイムアウト。シグナルなしで続行します。")
            timed_out = True
        except Exception as exc:
            log(f"  -> ⚠️ AI エージェントエラー: {exc}")
    else:
        # LangGraph ReAct + litellm 経路 (copilot / anthropic / openai)
        llm = get_chat_model(provider=provider, model=model)
        agent = create_react_agent(llm, [*ALL_TOOLS, submit_tool], prompt=system_prompt)

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

        # Extract final AI message
        for msg in reversed(messages):
            if hasattr(msg, "content") and msg.content and not hasattr(msg, "tool_call_id"):
                final_text = msg.content
                break
        trace_steps = _serialize_trace(messages)

    # タイムアウト時はゾンビ実行が後から captured を埋める可能性があるため読まない
    tool_signals = None if timed_out else captured.get("signals")

    # 思考トレース (ツール呼び出し列・推論) を永続化 — 可視化用。失敗してもサイクルは止めない
    try:
        _save_trace(file_ts, market, trace_steps)
    except Exception as e:
        log(f"  ⚠️ トレース保存スキップ: {e}")

    log("\n--- AI分析結果 ---")
    log(final_text[:2000] if len(final_text) > 2000 else final_text)

    # Step 3: Parse signals from AI output (non-LLM)
    log("\nStep 3: シグナル抽出...")
    rejected: list[dict[str, Any]] = []
    if tool_signals is not None:
        # 構造化ツール経由 (主経路): スキーマ検証 + 入口ゲート済みなので正規化のみ
        log(f"  -> submit_signals ツール経由で受理 ({len(tool_signals)} 件)")
        signals = _normalize_signals(tool_signals, max_signals)
        for t, msg in captured.get("entry_gate_rejected") or []:
            log(f"  ❌ {t}: {msg}")
            rejected.append({"ticker": t, "_gate_rejection_reason": f"entry_gate: {msg}"})
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
            from agents.ai import call_ai

            followup_text = call_ai(followup_prompt, provider, model) or ""
        except Exception as e:
            log(f"  -> フォローアップ失敗: {e}")
            followup_text = ""
        if followup_text:
            log(f"  -> フォローアップ: {followup_text[:300]}")
            signals = _extract_signals(followup_text, max_signals)

    if tool_signals is None and signals:
        # フォールバック経路 (テキスト JSON) も同じ入口ゲート (issue #20) を通す
        kept: list[dict[str, Any]] = []
        for s in signals:
            ok, msg, measured = _evaluate_entry_safe(s["ticker"])
            if ok:
                s["_entry_measured"] = measured
                kept.append(s)
            else:
                log(f"  ❌ {s['ticker']}: {msg}")
                rejected.append({"ticker": s["ticker"], "_gate_rejection_reason": f"entry_gate: {msg}"})
        signals = kept

    log(f"  -> {len(signals)} 件のシグナル")

    # Step 3.45: 執行可能性フィルタ (非LLM) — 1単元コストが評価額上限を超える銘柄は
    # 執行段階で必ず弾かれるため、反証ゲート (LLM) の前に落としてシグナル枠と
    # ゲート呼び出しの浪費を防ぐ (例: 6367.T が毎サイクル無駄撃ちされていた)
    if signals:
        signals, unaffordable = _filter_affordable_signals(signals, log)
        rejected = rejected + unaffordable

    # Step 3.5: Counterargument gate (non-LLM — signal-quality check)
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
        rejected = rejected + invalid_sigs

    # Step 3.55: ポートフォリオ単位の AI 推論 — セクター/テーマ過集中を抑制 (機械的上限に加える定性チェック)
    if signals:
        from agents.portfolio_review import review_portfolio

        log("\nStep 3.55: ポートフォリオ分散チェック (AI)...")
        signals = review_portfolio(signals, provider=provider, model=model, log=log)

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
        scen_exposure = (
            scenarios_cfg.get("scenarios", {}).get(scenario, {}).get("max_exposure_pct")
        )
        executed = _execute_signals(
            signals, dry_run, log,
            exposure_cap_pct=float(scen_exposure) if scen_exposure else None,
        )
    else:
        log("\nStep 4: 有効シグナルなし — 注文スキップ")

    return _finalize(signals, executed, rejected, final_text)


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
            # 入口ゲートの測定値 (issue #20)。signals_log に残し、後から入口条件と成績を突き合わせる
            "_entry_measured": s.get("_entry_measured"),
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
    """売買シグナル 1 件。

    価格関係の検証をこのスキーマで行う (issue #6)。executor まで到達してから
    却下されると次サイクルまで執行が遅れるが、ツール境界での ValidationError は
    エージェントに即座に差し戻され同一ターン内で修正できる。
    """

    ticker: str = Field(description="ティッカー (例 7203.T)")
    action: str = Field(default="buy", description='"buy" または "swap"')
    score: float = Field(default=0, description="テクニカルスコア")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    reason: str = Field(default="", description="売買根拠")
    entry_price: float = Field(default=0, description="0 = 成行")
    target_price: float = Field(
        default=0,
        description=(
            "執行の目標価格 (現在値近辺の現実的な水準)。"
            "stop_loss_price より上、take_profit_price 以下であること。"
            "アナリスト目標株価などの長期上値メドはここに入れず reason に書く"
        ),
    )
    stop_loss_price: float = Field(default=0, description="損切り価格 (target_price より下)")
    take_profit_price: float = Field(
        default=0, description="利確価格 (target_price 以上)"
    )
    timespan: str = Field(default="swing", description='"short"|"swing"|"medium"')
    fail_conditions: list[str] = Field(
        default_factory=list, description="このトレードが失敗する具体的条件"
    )
    invalidation_conditions: list[str] = Field(
        default_factory=list, description="シグナルの前提が崩れる条件"
    )
    exit_plan: str = Field(default="", description="価格水準を含む具体的な撤退計画")
    sell_ticker: str = Field(default="", description="swap 時のみ: 売却対象ティッカー")

    @model_validator(mode="after")
    def _validate_price_relation(self) -> TradeSignalArg:
        """executor の validation_error() と同じ制約をツール境界で先取りする。

        過去19件の却下のほぼ全てが「target_price にアナリスト目標等の上値メドを
        入れて take_profit_price を超える」という意味の取り違えだった。
        エラーメッセージは「どう直すか」が一意に決まる文面にする。
        """
        act = (self.action or "buy").lower()
        if act not in ("buy", "swap"):
            raise ValueError(f'action は "buy" か "swap" ({act!r} は不可)')
        if act == "swap" and not self.sell_ticker.strip():
            raise ValueError("swap には sell_ticker (売却対象) が必須")

        for name in ("target_price", "stop_loss_price", "take_profit_price"):
            if getattr(self, name) <= 0:
                raise ValueError(
                    f"{name} は正の価格が必須 (0 や省略は不可)。現在値を基準に具体的な水準を設定する"
                )
        if not (self.stop_loss_price < self.target_price <= self.take_profit_price):
            raise ValueError(
                f"価格関係が不正: stop_loss({self.stop_loss_price}) < target({self.target_price}) "
                f"<= take_profit({self.take_profit_price}) を満たすこと。"
                "target_price は執行の目標 (現在値近辺) であり、アナリスト目標株価などの"
                "上値メドを入れる欄ではない。上値メドは reason / exit_plan に書き、"
                "target_price は take_profit_price 以下に修正して再提出すること"
            )

        # 入口の品質は submit_signals 側の測定値ゲート (core/entry_gate, issue #20) で見る。
        # #14 の「語句照合 ∧ confidence ≥ 0.6」は表記ゆれと 0.55 申告で素通りしたため撤去 (#17)
        return self


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
        # 入口の測定値ゲート (issue #20): 自己申告ではなく score_stock の実測で検査し、
        # 却下はこのツール応答でエージェントに即時に返す (同一ターン内で別候補を検討できる)
        accepted: list[dict[str, Any]] = []
        rejected: list[tuple[str, str]] = []
        for sig in signals:
            ok, msg, measured = _evaluate_entry_safe(sig.ticker)
            d = sig.model_dump()
            d["_entry_measured"] = measured
            d["_entry_gate"] = msg
            if ok:
                accepted.append(d)
            else:
                rejected.append((sig.ticker, msg))
        captured["signals"] = accepted
        captured["market_comment"] = market_comment
        captured["entry_gate_rejected"] = rejected
        if rejected:
            detail = "; ".join(f"{t}: {m}" for t, m in rejected)
            return (
                f"{len(accepted)} 件受理、{len(rejected)} 件を入口ゲートで却下 — {detail}。"
                "却下銘柄は測定値が条件を満たすまで提出不可 (confidence や文面を変えても通らない)。"
                "別候補があれば受理済み分も含めて全件を再提出し、なければ簡潔な分析コメントで回答を終えること"
            )
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


def _evaluate_entry_safe(ticker: str) -> tuple[bool, str, dict[str, Any] | None]:
    """入口の測定値ゲート (issue #20)。ゲート自体の不具合はフェイルオープン。"""
    try:
        from core.entry_gate import evaluate_entry

        return evaluate_entry(ticker)
    except Exception as e:
        print(f"⚠️  entry gate error (fail-open): {e}", file=sys.stderr)
        return True, f"入口ゲート不調のため通過: {e}", None


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


def _result_reason(out: str) -> str:
    """run_trade_cmd の出力から失敗理由を取り出す。

    cmd_execute_signal / cmd_close_position は末尾に ```json {result} ``` を
    出力しており、その ``reason``/``status`` を拾う。JSON が見つからない場合は
    出力末尾を短く返す(理由が完全に消えないためのフォールバック)。
    """
    try:
        start = out.rindex("{")
        end = out.rindex("}")
        if start < end:
            data = json.loads(out[start : end + 1])
            reason = data.get("reason") or data.get("status")
            if reason:
                return str(reason)
    except (ValueError, json.JSONDecodeError):
        pass
    tail = out.strip().splitlines()[-1:] if out.strip() else []
    return tail[0][:200] if tail else "理由不明 (出力なし)"


def _sizing_limits() -> dict[str, float]:
    """サイズ・現金・セクターの機械ルール (issue #21) の実値。config が真実、失敗時は既定値。"""
    from core.sizing import (
        DEFAULT_MAX_SECTOR_PCT,
        DEFAULT_MIN_CASH_PCT,
        DEFAULT_VOL_HALF_SIZE_ABOVE_PCT,
        DEFAULT_VOL_THIRD_SIZE_ABOVE_PCT,
    )

    try:
        from core.trade import load_risk_limits

        lim = load_risk_limits()
    except Exception:
        lim = {}
    return {
        "max_position_size_pct": float(lim.get("max_position_size_pct", 20)),
        "min_cash_pct": float(lim.get("min_cash_pct", DEFAULT_MIN_CASH_PCT)),
        "max_sector_pct": float(lim.get("max_sector_pct", DEFAULT_MAX_SECTOR_PCT)),
        "vol_half_size_above_pct": float(
            lim.get("vol_half_size_above_pct", DEFAULT_VOL_HALF_SIZE_ABOVE_PCT)
        ),
        "vol_third_size_above_pct": float(
            lim.get("vol_third_size_above_pct", DEFAULT_VOL_THIRD_SIZE_ABOVE_PCT)
        ),
    }


def _sector_of(ticker: str) -> str:
    """銘柄のセクター (yfinance、プロセス内キャッシュ)。取れなければ '不明'。"""
    try:
        from agents.portfolio_review import _sector

        return _sector(ticker)
    except Exception:
        return "不明"


def _holdings_with_sectors() -> tuple[list[tuple[str, str, float]], float]:
    """保有の [(ticker, sector, 評価額 JPY)] と総資産 JPY。"""
    from agents.portfolio_helpers import _position_value_jpy, total_equity_jpy

    holdings: list[tuple[str, str, float]] = []
    for p in get_held_positions():
        price = float(p.get("current_price") or p.get("entry_price") or 0)
        qty = int(p.get("quantity") or 0)
        holdings.append((p["ticker"], _sector_of(p["ticker"]), _position_value_jpy(p["ticker"], price, qty)))
    return holdings, total_equity_jpy()


def _sector_cap_ok(
    sig: dict[str, Any], limits: dict[str, float], exclude_ticker: str | None = None
) -> tuple[bool, str]:
    """セクター上限 (issue #21)。見込みサイズは「総資産 × 1 銘柄上限 × ボラ係数」
    (評価額上限が効く前提の保守的な見積もり)。判定不能は通す。"""
    try:
        from core.sizing import sector_cap_check, vol_size_factor

        holdings, equity = _holdings_with_sectors()
        measured = sig.get("_entry_measured") or {}
        factor = vol_size_factor(
            measured.get("annualized_vol_pct"),
            limits["vol_half_size_above_pct"], limits["vol_third_size_above_pct"],
        )
        prospective = equity * limits["max_position_size_pct"] / 100 * factor
        return sector_cap_check(
            holdings, sig["ticker"], _sector_of(sig["ticker"]), prospective, equity,
            limits["max_sector_pct"], exclude_ticker=exclude_ticker,
        )
    except Exception as e:
        print(f"⚠️  sector cap check skipped: {e}", file=sys.stderr)
        return True, f"セクター判定不能のため通過: {e}"


def _swap_feasible(
    sig: dict[str, Any], sell_pos: dict[str, Any], limits: dict[str, float]
) -> tuple[bool, str]:
    """片肺 swap 防止 (issue #21): 売る前に買い足 1 単元が賄えるかを検査。判定不能は通す。"""
    try:
        from agents.auto_trade import _cash_by_currency, _order_cost
        from agents.portfolio_helpers import USD_JPY_APPROX, _position_value_jpy, total_equity_jpy
        from core.sizing import swap_feasibility

        est_price = float(sig.get("target_price") or 0)
        if est_price <= 0:
            return True, "買い足の価格不明のため通過"
        cash = _cash_by_currency(get_container().broker().get_balance())
        cash_jpy = cash["JPY"] + cash["USD"] * USD_JPY_APPROX
        sell_value = _position_value_jpy(
            sell_pos["ticker"],
            float(sell_pos.get("current_price") or sell_pos.get("entry_price") or 0),
            int(sell_pos.get("quantity") or 0),
        )
        ccy, _unit, est_cost = _order_cost(sig["ticker"], est_price)
        est_jpy = est_cost if ccy == "JPY" else est_cost * USD_JPY_APPROX
        return swap_feasibility(cash_jpy, sell_value, est_jpy, total_equity_jpy(), limits["min_cash_pct"])
    except Exception as e:
        print(f"⚠️  swap feasibility check skipped: {e}", file=sys.stderr)
        return True, f"判定不能のため通過: {e}"


def _execute_signals(
    signals: list[dict[str, Any]],
    dry_run: bool,
    log: Any,
    exposure_cap_pct: float | None = None,
) -> list[dict[str, Any]]:
    """Execute parsed trading signals.

    exposure_cap_pct: シナリオ別の最大エクスポージャ (建玉評価額/総資産 の上限%)。
    None なら制限なし。risk_off で買い進んでフルインベストになるのを防ぐ。
    機械ルール (issue #21): 現金下限・セクター上限・片肺 swap 防止・ボラ連動サイズ。
    """
    executed: list[dict[str, Any]] = []
    if dry_run:
        for sig in signals:
            log(f"  [DRY] {sig.get('action', 'buy').upper()} {sig['ticker']} score={sig.get('score', '?')}")
            executed.append({"ticker": sig["ticker"], "status": "DRY_RUN", "score": sig.get("score", 0)})
        return executed

    diary = get_container().diary()
    limits = _sizing_limits()
    for sig in signals:
        # Handle swap: sell first, then buy
        if sig.get("sell_ticker"):
            sell_ticker = sig["sell_ticker"]
            positions = get_held_positions()
            sell_pos = next((p for p in positions if p["ticker"] == sell_ticker), None)
            if sell_pos:
                # 回転抑制ガード (issue #11): 保有N営業日未満の swap 売りを拒否。
                # fail_conditions 発動を明記した売りは thesis 崩壊として通す
                from core.churn_guard import check_min_hold

                reason_text = str(sig.get("reason", ""))
                thesis_broken = "fail_condition" in reason_text or "invalidation" in reason_text
                ok_hold, hold_msg = check_min_hold(sell_pos.get("entry_time"))
                if not ok_hold and not thesis_broken:
                    log(f"  🔒 swap 却下 ({sell_ticker}): {hold_msg}")
                    executed.append(
                        {"ticker": sell_ticker, "status": "SWAP_BLOCKED_MIN_HOLD", "score": 0}
                    )
                    continue

                # 片肺 swap 防止 (issue #21): 買い足 1 単元が賄えない swap は売り足も出さない
                ok_swap, swap_msg = _swap_feasible(sig, sell_pos, limits)
                if not ok_swap:
                    log(f"  🔒 swap 却下 ({sell_ticker} → {sig['ticker']}): {swap_msg}")
                    executed.append(
                        {"ticker": sig["ticker"], "status": "SWAP_BLOCKED_FUNDS", "score": sig.get("score", 0)}
                    )
                    continue
                # セクター上限 (issue #21): 売り足を除いた保有で判定
                ok_sec, sec_msg = _sector_cap_ok(sig, limits, exclude_ticker=sell_ticker)
                if not ok_sec:
                    log(f"  🧭 swap 却下 ({sig['ticker']}): {sec_msg}")
                    executed.append(
                        {"ticker": sig["ticker"], "status": "SKIPPED_SECTOR_CAP", "score": sig.get("score", 0)}
                    )
                    continue

                qty = sell_pos.get("quantity", 0)
                log(f"  売り: {sell_ticker} {qty}株...")
                out, rc = run_trade_cmd(["--close", sell_ticker, str(qty), "--source", "swap"])
                ok = rc == 0 and '"status": "FILLED"' in out
                if ok:
                    log(f"    ✅ {sell_ticker} クローズ完了")
                else:
                    log(f"    ❌ {sell_ticker} クローズ失敗 (rc={rc}): {_result_reason(out)}")
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
            max_pos_pct = limits["max_position_size_pct"]
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

            cash_total_jpy = cash["JPY"] + cash["USD"] * USD_JPY_APPROX
            est_jpy = est_cost if ccy == "JPY" else est_cost * USD_JPY_APPROX

            # シナリオ別エクスポージャ上限 (risk_off で買い進みすぎない)
            if exposure_cap_pct is not None and equity > 0:
                positions_value = max(0.0, equity - cash_total_jpy)
                new_exposure_pct = (positions_value + est_jpy) / equity * 100
                if new_exposure_pct > exposure_cap_pct:
                    log(
                        f"  🌡️ {sig['ticker']}: 買い後エクスポージャ {new_exposure_pct:.0f}% が"
                        f"シナリオ上限 {exposure_cap_pct:.0f}% 超 -> スキップ"
                    )
                    executed.append(
                        {"ticker": sig["ticker"], "status": "SKIPPED_EXPOSURE", "score": sig.get("score", 0)}
                    )
                    continue

            # 現金下限 (issue #21): 1 単元買っても現金比率が下限を割らないこと
            #   (実サイズは RiskManager が同じ下限で計算する。ここは早期スキップとログのため)
            from core.sizing import cash_floor_check

            ok_cash, cash_msg = cash_floor_check(cash_total_jpy, est_jpy, equity, limits["min_cash_pct"])
            if not ok_cash:
                log(f"  💴 {sig['ticker']}: {cash_msg} -> スキップ")
                executed.append(
                    {"ticker": sig["ticker"], "status": "SKIPPED_CASH_FLOOR", "score": sig.get("score", 0)}
                )
                continue

            # セクター上限 (issue #21)
            ok_sec, sec_msg = _sector_cap_ok(sig, limits)
            if not ok_sec:
                log(f"  🧭 {sig['ticker']}: {sec_msg} -> スキップ")
                executed.append(
                    {"ticker": sig["ticker"], "status": "SKIPPED_SECTOR_CAP", "score": sig.get("score", 0)}
                )
                continue

        # ボラ連動サイズ (issue #21): 年率ボラ > 60% で 1/2、> 80% で 1/3 (RiskManager が適用)
        from core.sizing import vol_size_factor

        measured = sig.get("_entry_measured") or {}
        size_factor = vol_size_factor(
            measured.get("annualized_vol_pct"),
            limits["vol_half_size_above_pct"], limits["vol_third_size_above_pct"],
        )
        if size_factor < 1.0:
            log(
                f"  📉 {sig['ticker']}: 年率ボラ {float(measured.get('annualized_vol_pct')):.0f}% "
                f"→ サイズ係数 {size_factor:.2f}"
            )

        # Buy — 成行注文（シミュレーターが指値PENDINGに非対応のため）
        buy_sig = {k: v for k, v in sig.items() if k != "sell_ticker" and not k.startswith("_")}
        buy_sig["entry_price"] = 0  # MARKET order
        buy_sig["size_factor"] = size_factor
        sig_name = f"{sig['ticker'].replace('.', '')}_auto.json"
        sig_path = diary.save_signal(sig_name, buy_sig)
        out, rc = run_trade_cmd(["--from-signal", sig_path])
        ok = rc == 0 and '"status": "FILLED"' in out
        if ok:
            log(f"  ✅ {sig['ticker']} 約定成功")
        else:
            log(f"  ❌ {sig['ticker']} 約定失敗 (rc={rc}): {_result_reason(out)}")
        executed.append(
            {
                "ticker": sig["ticker"],
                "status": "FILLED" if ok else "FAILED",
                "score": sig.get("score", 0),
                "measured": sig.get("_entry_measured"),
            }
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
    regime: str = "",
) -> None:
    market_labels = {"us": "米国株", "jp": "日本株", "all": "全市場"}
    ml = market_labels.get(market, market)
    filename = f"{file_ts}_auto_trade_agent_{ml}.md"
    body = f"# 🤖 LangGraph 売買エージェント — {ml}\n\n"
    body += f"- **シナリオ**: {scenario}\n"
    body += f"- **レジーム**: {regime or '未判定'}\n\n"
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

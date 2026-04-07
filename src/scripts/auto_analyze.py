#!/usr/bin/env python3
"""Automated analysis pipeline — screen, score, (optionally) AI-analyze, and report.

Usage examples::

    # Template report (no AI)
    python3 scripts/auto_analyze.py --market jp --span medium --depth standard

    # AI-enhanced report
    python3 scripts/auto_analyze.py --market jp --depth standard --ai

    # Daemon mode — repeat every 30 minutes
    python3 scripts/auto_analyze.py --ai --daemon --interval 1800
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
SRC_DIR = SCRIPT_DIR.parent
PROJECT_DIR = SRC_DIR.parent
DIARY_DIR = PROJECT_DIR / "diary"
JST = timezone(timedelta(hours=9))

# --- shared helpers -------------------------------------------------------

sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SRC_DIR))
from infra.container import get_container
from lib.ai import PROVIDER_NAMES, call_ai  # noqa: E402
from lib.runner import run_script  # noqa: E402

# --- constants ------------------------------------------------------------

MARKET_LABELS: dict[str, str] = {"us": "米国株", "jp": "日本株", "all": "全市場"}
SPAN_LABELS: dict[str, str] = {
    "short": "短期(1-5日)",
    "swing": "スイング(1-3週間)",
    "medium": "中期(1-3ヶ月)",
    "all": "全スパン",
}
SPAN_LABELS_SHORT: dict[str, str] = {
    "short": "短期",
    "swing": "スイング",
    "medium": "中期",
    "all": "全スパン",
}
MAX_ANALYZE_BY_DEPTH: dict[str, int] = {"quick": 3, "standard": 5, "detailed": 8}


# ── helpers ───────────────────────────────────────────────────────────────────


def get_top_tickers(
    screener_result: dict[str, Any],
    max_tickers: int = 5,
) -> list[dict[str, Any]]:
    """Deduplicate and rank candidates from screener output."""
    if not screener_result or "results" not in screener_result:
        return []
    seen: set[str] = set()
    top: list[dict[str, Any]] = []
    for strategy, candidates in screener_result["results"].items():
        for c in candidates:
            ticker = c["ticker"]
            if ticker in seen:
                continue
            seen.add(ticker)
            top.append(
                {
                    "ticker": ticker,
                    "name": c.get("name", ""),
                    "strategy": strategy,
                    "screener_score": c.get("score", 0),
                    "current_price": c.get("current_price"),
                    "currency": c.get("currency", ""),
                    "reasons": c.get("reasons", []),
                }
            )
    top.sort(key=lambda x: x["screener_score"], reverse=True)
    return top[:max_tickers]


# ── AI prompt ─────────────────────────────────────────────────────────────────


def _build_ai_prompt(
    collected_data: dict[str, Any],
    market: str,
    span: str,
    depth: str,
) -> str:
    """Build the prompt sent to the AI provider for analysis."""
    data_json = json.dumps(collected_data, ensure_ascii=False, indent=2, default=str)
    if len(data_json) > 20000:
        data_json = data_json[:20000] + "\n... (truncated)"

    event_impact = collected_data.get("event_impact")
    event_section = ""
    if event_impact and event_impact.get("triggered_rules"):
        rules = ", ".join(v["label"] for v in event_impact["triggered_rules"].values())
        direction = event_impact.get("market_direction", "neutral")
        event_section = (
            "\n## 現在のマクロイベント因果分析\n"
            f"- **検知済みイベント**: {rules}\n"
            f"- **市場方向**: {direction}\n"
            "- セクター影響・資産影響・注目銘柄は `event_impact` フィールドを参照\n\n"
            "**重要**: 上記イベントが各銘柄に与える因果的な影響を分析に反映してください。\n"
        )

    ml = MARKET_LABELS.get(market, market)
    sl = SPAN_LABELS.get(span, span)

    return (
        "あなたは株式売買判断の専門アナリストです。以下の収集データを総合分析し、"
        "売買判断レポートを作成してください。\n\n"
        f"## 分析条件\n- 市場: {ml}\n- 期間: {sl}\n- 深度: {depth}\n"
        f"{event_section}\n"
        f"## 収集データ（JSON）\n```json\n{data_json}\n```\n\n"
        "## レポート作成ルール\n"
        "各銘柄について以下を含めること:\n"
        "- 判断(強い買い〜強い売り)、総合スコア/100、確信度\n"
        "- 推奨タイムスパン別の確率と目標価格\n"
        "- エントリー価格・損切りライン・利確目標(リスクリワード比付き)\n"
        "- テクニカル/ファンダメンタル/センチメント/マクロの根拠\n\n"
        "## 追加分析（必須）\n"
        "1. マクロ環境の影響\n2. セクター動向\n3. 銘柄間比較・優先順位\n"
        "4. ポートフォリオ提案（分散考慮）\n5. リスクシナリオ\n\n"
        "> これは参考情報であり投資助言ではありません\n\n"
        "Markdown形式で出力してください。"
    )


# ── report rendering ──────────────────────────────────────────────────────────


def _render_event_impact_section(event_impact: dict[str, Any] | None) -> list[str]:
    """Render the event-impact analysis section as Markdown lines."""
    lines: list[str] = []
    if not event_impact or not event_impact.get("triggered_rules"):
        lines += [
            "## 📡 イベント因果分析",
            "",
            "現在のニュースから有意なマクロイベントは検知されませんでした。",
            "",
            "---",
            "",
        ]
        return lines

    dir_map = {
        "risk_off": "⚠️ リスクオフ",
        "risk_on": "✅ リスクオン",
        "mixed": "⚡ 方向性まちまち",
        "neutral": "➖ 中立",
    }
    mag_sym = {"high": "◎", "medium": "○", "low": "△"}
    direction = event_impact.get("market_direction", "neutral")

    lines += [
        "## 📡 イベント因果分析",
        "",
        f"**総合市場方向: {dir_map.get(direction, direction)}**",
        "",
        "### 検知されたマクロイベント",
    ]
    for _key, info in event_impact["triggered_rules"].items():
        lines.append(f"- **{info['label']}** ({info['count']}件ヒット)")
        for h in info.get("headlines", [])[:2]:
            lines.append(f"  - {h}")
    lines.append("")

    # Sector impacts
    sector_impacts = event_impact.get("sector_impacts", {})
    if sector_impacts:
        lines.append("### セクター別予測影響")
        for direction_key in ("positive", "negative"):
            filtered = {k: v for k, v in sector_impacts.items() if v["direction"] == direction_key}
            if not filtered:
                continue
            arrow = "↑ 恩恵を受ける" if direction_key == "positive" else "↓ 影響を受ける"
            lines.append(f"**{arrow}セクター:**")
            for sector, info in sorted(
                filtered.items(),
                key=lambda x: {"high": 3, "medium": 2, "low": 1}[x[1]["magnitude"]],
                reverse=True,
            ):
                lines.append(f"  - {mag_sym[info['magnitude']]} **{sector}**: {info['reason']}")
        lines.append("")

    # Asset impacts
    asset_impacts = event_impact.get("asset_impacts", {})
    if asset_impacts:
        asset_labels = {
            "oil": "原油",
            "gold": "金",
            "bonds": "国債",
            "usdjpy": "ドル円",
            "dxy": "ドルインデックス",
            "nikkei": "日経平均",
        }
        lines.append("### 資産・為替への波及")
        for asset, info in asset_impacts.items():
            arrow = "↑" if info["direction"] == "positive" else "↓"
            label = asset_labels.get(asset, asset)
            lines.append(f"- {label}: **{arrow}** ({info['magnitude']}) — {info['reason']}")
        lines.append("")

    # Tickers to watch
    tickers = event_impact.get("tickers_to_watch", {})
    if tickers.get("positive") or tickers.get("negative"):
        lines.append("### イベント由来の注目銘柄")
        if tickers.get("positive"):
            lines.append(f"- **買い候補**: {', '.join(tickers['positive'])}")
        if tickers.get("negative"):
            lines.append(f"- **売り・回避**: {', '.join(tickers['negative'])}")
        lines.append("")

    lines += ["---", ""]
    return lines


def _generate_report(
    macro: dict[str, Any] | None,
    screener: dict[str, Any] | None,
    analyses: list[dict[str, Any]],
    market: str,
    span: str,
    depth: str,
    timestamp: str,
    ai_commentary: str | None = None,
    event_impact: dict[str, Any] | None = None,
) -> str:
    """Generate a Markdown analysis report."""
    ml = MARKET_LABELS.get(market, market)
    sl = SPAN_LABELS_SHORT.get(span, span)

    lines: list[str] = [
        f"# 📝 自動分析レポート — {ml} {sl}",
        f"- **日時**: {timestamp}",
        f"- **対象**: {ml}",
        f"- **実行モード**: {'🤖 AI分析' if ai_commentary else '🤖 自動実行'} (depth={depth})",
        "- **種別**: 売買判断",
        "",
        "### 🔧 使用ツール",
        f"- `scripts/auto_analyze.py --market {market} --span {span} --depth {depth}"
        f"{' --ai' if ai_commentary else ''}`",
        "- `scripts/event_impact_analyzer.py` (マクロイベント因果分析)",
    ]
    if ai_commentary:
        lines.append("- AI分析 (Copilot Premium)")
    lines += ["", "---", ""]

    # Event impact
    lines.extend(_render_event_impact_section(event_impact))

    # AI commentary (main content when available)
    if ai_commentary:
        lines += [ai_commentary, "", "---", "", "## 収集データサマリー（参考）", ""]

    # Macro
    if macro:
        env = macro.get("market_environment", {})
        indicators = macro.get("indicators", {})
        lines.append(
            f"## 📊 市場環境: **{env.get('assessment', '不明')}** "
            f"(スコア: {env.get('score', 'N/A')})"
        )
        lines += ["", "| 指標 | 現在値 | 20日変動 |", "|------|--------|----------|"]
        for key, ind in indicators.items():
            lines.append(
                f"| {ind.get('label', key)} | {ind.get('current', 'N/A')} "
                f"| {ind.get('change_20d', 'N/A')} |"
            )
        signals = env.get("signals", [])
        if signals:
            lines += ["", "**シグナル:** " + " / ".join(signals)]
        lines += ["", "---", ""]

    # Screening summary
    if screener and "summary" in screener:
        s = screener["summary"]
        lines.append(
            f"## 🔍 スクリーニング "
            f"({s.get('data_obtained', '?')}/{s.get('scan_universe', '?')} 銘柄)"
        )
        lines += ["", "| 戦略 | ヒット | 上位銘柄 |", "|------|--------|---------|"]
        for strat, info in s.get("strategies", {}).items():
            picks = ", ".join(info.get("top_picks", [])[:3])
            lines.append(f"| {strat} | {info.get('count', 0)} | {picks} |")
        lines += ["", "---", ""]

    # Per-ticker data
    if not ai_commentary:
        lines += ["## 📊 売買判断", ""]

    for i, a in enumerate(analyses, 1):
        info = a["info"]
        scorer = a.get("scorer")
        fund = a.get("fundamentals")
        sent = a.get("sentiment")
        ticker = info["ticker"]
        name = info.get("name", ticker)
        cs = "¥" if info.get("currency") == "JPY" else "$"

        if ai_commentary:
            lines.append(f"<details><summary>{i}. {ticker} ({name}) — 詳細データ</summary>")
        else:
            lines.append(f"### {i}. {ticker} ({name})")
        lines.append("")

        if scorer:
            sm = scorer.get("analysis_summary", {})
            prob = scorer.get("probability", {})
            entry = scorer.get("entry_points", [])
            risk = scorer.get("risk_management", {})

            lines += [
                f"- **判断**: {sm.get('action', '?')} | **スコア**: "
                f"{sm.get('total_score', '?')} | **確信度**: {sm.get('confidence', '?')}",
                f"- **現在値**: {cs}{scorer.get('current_price', '?')}",
                "",
            ]
            if prob:
                lines += [
                    "| 期間 | 上昇確率 | +α達成 | 下落リスク |",
                    "|------|---------|--------|-----------|",
                    f"| 5日 | {prob.get('5日後に上昇', '?')} | +3%: "
                    f"{prob.get('5日後に+3%以上', '?')} | -3%: "
                    f"{prob.get('5日後に-3%以下', '?')} |",
                    f"| 20日 | {prob.get('20日後に上昇', '?')} | +5%: "
                    f"{prob.get('20日後に+5%以上', '?')} | -5%: "
                    f"{prob.get('20日後に-5%以下', '?')} |",
                    f"| 60日 | {prob.get('60日後に上昇', '?')} | +10%: "
                    f"{prob.get('60日後に+10%以上', '?')} | — |",
                    "",
                ]
            if entry:
                for e in entry:
                    lines.append(f"- **{e.get('type', '')}**: {cs}{e.get('price', '?')}")
                lines += [
                    f"- 損切り: {cs}{risk.get('損切りライン', '?')} / "
                    f"利確1: {cs}{risk.get('利確目標1（ATR×2）', '?')} / "
                    f"利確2: {cs}{risk.get('利確目標2（ATR×4）', '?')}",
                    "",
                ]

        if fund:
            fs = fund.get("fundamental_score", {})
            val = fund.get("valuation", {})
            prof = fund.get("profitability", {})
            analyst = fund.get("analyst", {})
            lines.append(
                f"- ファンダ: {fs.get('score', '?')}/{fs.get('max_score', 70)} | "
                f"PER={val.get('PER', '?')} PBR={val.get('PBR', '?')} ROE={prof.get('ROE', '?')}"
            )
            if analyst:
                lines.append(
                    f"- アナリスト: {analyst.get('推奨', '?')} "
                    f"目標{cs}{analyst.get('目標株価(平均)', '?')}"
                )
            lines.append("")

        if sent:
            lines += [
                f"- センチメント: ポジ{sent.get('positive_pct', '?')}% / "
                f"ネガ{sent.get('negative_pct', '?')}% ({sent.get('total', '?')}件)",
                "",
            ]

        if ai_commentary:
            lines += ["</details>", ""]
        else:
            lines += ["---", ""]

    lines.append("> ⚠️ これは自動生成レポートであり、投資助言ではありません。")
    return "\n".join(lines)


# ── main pipeline ─────────────────────────────────────────────────────────────


def run_analysis(
    market: str,
    span: str,
    depth: str,
    use_ai: bool = False,
    ai_provider: str = "copilot",
    ai_model: str | None = None,
) -> str | None:
    """Execute the full analysis pipeline and save a report."""
    now = datetime.now(JST)
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S JST")
    file_ts = now.strftime("%Y-%m-%d_%H%M%S")

    steps = "6" if use_ai else "5"
    print(f"\n{'=' * 60}")
    print(f"  自動分析開始: {timestamp}")
    print(f"  market={market}, span={span}, depth={depth}, ai={use_ai}")
    print(f"{'=' * 60}\n")

    # 1. Macro
    print(f"1/{steps} マクロ環境...")
    macro = run_script("macro.py")

    # 2. Event impact
    print(f"2/{steps} イベント因果分析...")
    event_impact = run_script("event_impact_analyzer.py", timeout=90)
    if event_impact:
        n_rules = len(event_impact.get("triggered_rules", {}))
        direction = event_impact.get("market_direction", "neutral")
        print(f"  -> マクロイベント {n_rules}種検知, 市場方向={direction}")
    else:
        print("  -> イベント因果分析スキップ")
        event_impact = {}

    # 3. Screening
    print(f"3/{steps} スクリーニング...")
    top_n = MAX_ANALYZE_BY_DEPTH.get(depth, 5)
    screener = run_script(
        "screener.py",
        ["--market", market, "--strategy", "all", "--top", str(top_n)],
        timeout=300,
    )
    if not screener:
        print("ERROR: スクリーニング失敗", file=sys.stderr)
        return None

    # 4. Detailed analysis
    max_analyze = MAX_ANALYZE_BY_DEPTH.get(depth, 5)
    top_tickers = get_top_tickers(screener, max_tickers=max_analyze)
    print(f"4/{steps} 上位{len(top_tickers)}銘柄を分析...")

    analyses: list[dict[str, Any]] = []
    for t in top_tickers:
        ticker = t["ticker"]
        result: dict[str, Any] = {"info": t}
        print(f"  -> {ticker}...")
        result["scorer"] = run_script("scorer.py", [ticker])
        if depth in ("standard", "detailed"):
            result["fundamentals"] = run_script("fundamentals.py", [ticker])
            result["sentiment"] = run_script("fetch_sentiment.py", [t.get("name", ticker)])
        analyses.append(result)

    # 5. AI analysis (optional)
    ai_commentary: str | None = None
    if use_ai:
        print(f"5/{steps} AI分析...")
        collected_data: dict[str, Any] = {
            "macro": macro,
            "event_impact": event_impact,
            "screener_summary": screener.get("summary") if screener else None,
            "analyses": [],
        }
        keep_keys = [
            "current_price",
            "analysis_summary",
            "probability",
            "entry_points",
            "risk_management",
            "volatility",
            "returns",
            "technical_indicators",
        ]
        for a in analyses:
            entry: dict[str, Any] = {
                "ticker": a["info"]["ticker"],
                "name": a["info"].get("name", ""),
            }
            if a.get("scorer"):
                entry["scorer"] = {k: a["scorer"][k] for k in keep_keys if k in a["scorer"]}
            if a.get("fundamentals"):
                entry["fundamentals"] = a["fundamentals"]
            if a.get("sentiment"):
                entry["sentiment"] = a["sentiment"]
            collected_data["analyses"].append(entry)

        prompt = _build_ai_prompt(collected_data, market, span, depth)
        ai_commentary = call_ai(prompt, ai_provider, ai_model)
        print("  AI分析完了 ✓" if ai_commentary else "  AI分析失敗 — テンプレートにフォールバック")

    # 6. Report
    print(f"{steps}/{steps} レポート生成...")
    report = _generate_report(
        macro, screener, analyses, market, span, depth, timestamp, ai_commentary, event_impact
    )

    ml = MARKET_LABELS.get(market, market)
    sl = SPAN_LABELS_SHORT.get(span, span)
    ai_tag = "_AI" if ai_commentary else ""
    filename = f"{file_ts}_自動分析{ai_tag}_{ml}_{sl}.md"
    get_container().diary().save_report(filename, report)

    print(f"\n{'=' * 60}")
    print(f"  完了! diary/{filename}")
    print(f"{'=' * 60}")

    for a in analyses:
        sc = a.get("scorer", {})
        sm = sc.get("analysis_summary", {}) if sc else {}
        prob = sc.get("probability", {}) if sc else {}
        print(
            f"  {a['info']['ticker']:10s} | {sm.get('action', '?'):6s} "
            f"| スコア {str(sm.get('total_score', '?')):>4s} "
            f"| 60日↑ {prob.get('60日後に上昇', '?')}"
        )

    return str(filepath)


# ── daemon ────────────────────────────────────────────────────────────────────


def daemon_loop(
    market: str,
    span: str,
    depth: str,
    interval: int,
    use_ai: bool = False,
    ai_provider: str = "copilot",
    ai_model: str | None = None,
) -> None:
    """Run analysis on a loop at the specified interval."""
    print(f"デーモンモード: {interval}秒({interval // 60}分)ごとに自動実行")
    if use_ai:
        print(f"AI分析: {ai_provider} (model: {ai_model or 'default'})")
    print("Ctrl+C で停止\n")

    cycle = 0
    while True:
        cycle += 1
        print(f"\n### 実行 #{cycle} ###")
        try:
            run_analysis(market, span, depth, use_ai, ai_provider, ai_model)
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
        print(f"\n次回: {interval}秒後 ({interval // 60}分後)")
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\nデーモン停止")
            break


# ── CLI ───────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="自動分析スクリプト（AI分析対応）")
    parser.add_argument(
        "--market", choices=["us", "jp", "all"], default="jp", help="市場 (default: jp)"
    )
    parser.add_argument("--span", choices=["short", "swing", "medium", "all"], default="medium")
    parser.add_argument("--depth", choices=["quick", "standard", "detailed"], default="standard")
    parser.add_argument("--ai", action="store_true", help="AI分析を有効化")
    parser.add_argument("--ai-provider", choices=PROVIDER_NAMES, default="copilot")
    parser.add_argument("--ai-model", default=None)
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--interval", type=int, default=1800, help="間隔(秒) (default: 1800=30分)")
    args = parser.parse_args()

    if args.daemon:
        daemon_loop(
            args.market,
            args.span,
            args.depth,
            args.interval,
            args.ai,
            args.ai_provider,
            args.ai_model,
        )
    else:
        run_analysis(args.market, args.span, args.depth, args.ai, args.ai_provider, args.ai_model)


if __name__ == "__main__":
    main()

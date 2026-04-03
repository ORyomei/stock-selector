#!/usr/bin/env python3
"""
.devcontainer .git .github .gitignore .venv .vscode 1.md README.md config data diary pat portfolio.json Requirements. — /analyze プロンプトの自動実行版（AI分析対応）

:::::::::
    # データ収集のみ（テンプレートレポート）
    python3 scripts/auto_analyze.py --market jp --span medium --depth standard

    # AI分析付き（Copilot / GitHub Models / OpenAI / Anthropic）
    python3 scripts/auto_analyze.py --market jp --span medium --depth standard --ai

    # 30分ごとに自動実行（デーモンモード）
    python3 scripts/auto_analyze.py --market all --span medium --depth standard --ai --daemon --interval 1800

AI設定:
    --ai-provider: copilot (default) / github / openai / anthropic
    --ai-model: 使用モデル（省略時はプロバイダーのデフォルト）
    環境変数: OPENAI_API_KEY, ANTHROPIC_API_KEY（プロバイダーに応じて必要）
"""

import argparse
import contextlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent
DIARY_DIR = PROJECT_DIR / "diary"
JST = timezone(timedelta(hours=9))

# AI プロバイダー設定
AI_DEFAULTS = {
    "copilot": {
        "endpoint": None,  # gh copilot -p を使用
        "model": "claude-sonnet-4.6",
        "token_env": None,
    },
    "github": {
        "endpoint": "https://models.inference.ai.azure.com/chat/completions",
        "model": "openai/gpt-4o",
        "token_env": None,  # gh auth token を使用
    },
    "openai": {
        "endpoint": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o",
        "token_env": "OPENAI_API_KEY",
    },
    "anthropic": {
        "endpoint": "https://api.anthropic.com/v1/messages",
        "model": "claude-sonnet-4-20250514",
        "token_env": "ANTHROPIC_API_KEY",
    },
}


def run_script(script_name, args=None, timeout=120):
    """スクリプトを実行してJSON結果を返す"""
    cmd = [sys.executable, str(SCRIPT_DIR / script_name)]
    if args:
        cmd.extend(args)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=str(PROJECT_DIR)
        )
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


def get_top_tickers(screener_result, max_tickers=5):
    """スクリーナー結果から重複排除した上位銘柄を抽出"""
    if not screener_result or "results" not in screener_result:
        return []
    seen = set()
    top = []
    for strategy, candidates in screener_result["results"].items():
        for c in candidates:
            ticker = c["ticker"]
            if ticker not in seen:
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


# ─── AI ────────────────────────────────────────────────────


def get_ai_token(provider):
    """AIプロバイダーのトークンを取得"""
    cfg = AI_DEFAULTS.get(provider, {})
    env_key = cfg.get("token_env")
    if env_key:
        token = os.environ.get(env_key)
        if token:
            return token
        print(f"  warn: {env_key} が設定されていません", file=sys.stderr)
        return None
    if provider == "github":
        try:
            result = subprocess.run(
                ["gh", "auth", "token"], capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return os.environ.get("GITHUB_TOKEN")
    return None


def build_ai_prompt(collected_data, market, span, depth):
    """AI分析用プロンプトを構築"""
    labels_market = {"us": "米国株", "jp": "日本株", "all": "全市場"}
    labels_span = {
        "short": "短期(1-5日)",
        "swing": "スイング(1-3週間)",
        "medium": "中期(1-3ヶ月)",
        "all": "全スパン",
    }

    data_json = json.dumps(collected_data, ensure_ascii=False, indent=2, default=str)
    if len(data_json) > 20000:
        data_json = data_json[:20000] + "\n... (truncated)"

    # イベント因果分析の内容をプロンプトに追加
    event_impact = collected_data.get("event_impact")
    event_section = ""
    if event_impact and event_impact.get("triggered_rules"):
        rules = ", ".join(v["label"] for v in event_impact["triggered_rules"].values())
        direction = event_impact.get("market_direction", "neutral")
        event_section = f"""
## ⚡ 現在のマクロイベント因果分析
- **検知済みイベント**: {rules}
- **市場方向**: {direction}
- セクター影響・資産影響・注目銘柄は `event_impact` フィールドを参照

**重要**: 上記イベントが各銘柄に与える因果的な影響を分析に反映してください。
例: 地政学リスクが検知された場合 → 防衛株・原油株は上昇期待、航空株・テック株は下落リスク
"""

    return f"""あなたは株式売買判断の専門アナリストです。以下の収集データを総合分析し、売買判断レポートを作成してください。

## 分析条件
- 市場: {labels_market.get(market, market)}
- 期間: {labels_span.get(span, span)}
- 深度: {depth}
{event_section}
## 収集データ（JSON）
```json
{data_json}
```

## レポート作成ルール

gh copilot -p "1+1は？数字のみで" > /tmp/copilot_stdout.txt 2> /tmp/copilot_stderr.txt :

### 銘柄: <ティッカー> (<企業名>)
- **判断**: 強い買い / 買い / やや買い / 様子見 / やや売り / 売り / 強い売り
- **総合スコア**: xx / 100（各指標の内訳も記載）
- **確信度**: 高 / 中 / 低

#### 推奨タイムスパン
- 短期/スイング/中期それぞれの確率と目標価格

#### 確率予測
| 期間 | 上昇確率 | +α達成 | 下落リスク |
|------|---------|--------|-----------|
| 5日後 | xx% | +3%以上: xx% | -3%以下: xx% |
| 20日後 | xx% | +5%以上: xx% | -5%以下: xx% |
| 60日後 | xx% | +10%以上: xx% | — |

#### 具体的なエントリー戦略
- エントリー価格（指値/逆指値）
- 損切りライン
- 利確目標1, 2（リスクリワード比付き）

#### 根拠
- テクニカル/ファンダメンタル/センチメント/マクロの各観点から

## 追加分析（必ず含めること）
1. **マクロ環境の影響**: 市場環境スコアと各銘柄への影響
2. **セクター動向**: AI・半導体・高配当等のテーマ性
3. **銘柄間比較**: 優先順位とその理由
4. **ポートフォリオ提案**: セクター分散・リスク分散を考慮した配分案
5. **リスクシナリオ**: 最悪ケースの想定

 これは参考情報であり投資助言ではありません」と記載してください。
Markdown形式で出力してください。"""


def call_copilot(prompt):
    """gh copilot -p を使って分析結果を取得（ツール無効・silent モード）"""
    import shlex
    import tempfile

    print("  AI: gh copilot (Copilot Premium) に分析を依頼中...")

    prompt_file = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            f.write(prompt)
            prompt_file = f.name

        # --excluded-tools で全ツール無効化、--no-custom-instructions でワークスペース指示無視、-s でメタ情報除去
        copilot_flags = '--excluded-tools="shell,read,write,list,search,glob,stat,create,edit" --no-custom-instructions -s'

        if len(prompt) < 10000:
            cmd = f"gh copilot -- -p {shlex.quote(prompt)} {copilot_flags}"
        else:
            cmd = f"cat {shlex.quote(prompt_file)} | gh copilot -- -p - {copilot_flags}"

        result = subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True,
            text=True,
            timeout=600,
            cwd=str(PROJECT_DIR),
        )

        if result.returncode != 0:
            print(
                f"  warn: gh copilot error (rc={result.returncode}): {result.stderr[:300]}",
                file=sys.stderr,
            )
            return None

        output = result.stdout.strip()
        if not output:
            print("  warn: gh copilot returned empty response", file=sys.stderr)
            return None

        return output
    except subprocess.TimeoutExpired:
        print("  warn: gh copilot タイムアウト (600s)", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  warn: gh copilot エラー: {e}", file=sys.stderr)
        return None
    finally:
        if prompt_file:
            with contextlib.suppress(OSError):
                os.unlink(prompt_file)


def call_ai_api(prompt, provider, model):
    """AI APIを呼び出して分析結果を取得"""
    if provider == "copilot":
        return call_copilot(prompt)

    try:
        import requests
    except ImportError:
        print("  error: requests モジュールが必要です (pip install requests)", file=sys.stderr)
        return None

    token = get_ai_token(provider)
    if not token:
        return None

    cfg = AI_DEFAULTS.get(provider, AI_DEFAULTS["github"])
    endpoint = cfg["endpoint"]
    use_model = model or cfg["model"]

    print(f"  AI: {provider}/{use_model} に分析を依頼中...")

    try:
        if provider == "anthropic":
            response = requests.post(
                endpoint,
                headers={
                    "x-api-key": token,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json={
                    "model": use_model,
                    "max_tokens": 8000,
                    "temperature": 0.3,
                    "system": "あなたは株式市場の専門アナリストです。データに基づいた客観的な分析を行い、具体的な数値を含む売買判断を提供します。",
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=180,
            )
            if response.status_code != 200:
                print(
                    f"  warn: Anthropic API error {response.status_code}: {response.text[:300]}",
                    file=sys.stderr,
                )
                return None
            result = response.json()
            return result["content"][0]["text"]
        else:
            response = requests.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": use_model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "あなたは株式市場の専門アナリストです。データに基づいた客観的な分析を行い、具体的な数値を含む売買判断を提供します。",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 8000,
                },
                timeout=180,
            )
            if response.status_code != 200:
                print(
                    f"  warn: {provider} API error {response.status_code}: {response.text[:300]}",
                    file=sys.stderr,
                )
                return None
            result = response.json()
            return result["choices"][0]["message"]["content"]

    except Exception as e:
        print(f"  warn: AI API エラー: {e}", file=sys.stderr)
        return None


# ─── レポート生成 ──────────────────────────────────────────


def generate_report(
    macro, screener, analyses, market, span, depth, timestamp, ai_commentary=None, event_impact=None
):
    """Markdownレポート生成"""
    labels_market = {"us": "米国株", "jp": "日本株", "all": "全市場"}
    labels_span = {"short": "短期", "swing": "スイング", "medium": "中期", "all": "全スパン"}
    ml = labels_market.get(market, market)
    sl = labels_span.get(span, span)

    lines = []
    lines.append(f"# 📝 自動分析レポート — {ml} {sl}")
    lines.append(f"- **日時**: {timestamp}")
    lines.append(f"- **対象**: {ml}")
    mode_label = "🤖 AI分析" if ai_commentary else "🤖 自動実行"
    lines.append(f"- **実行モード**: {mode_label} (depth={depth})")
    lines.append("- **種別**: 売買判断")
    lines.append("")
    lines.append("### 🔧 使用ツール")
    ai_flag = " --ai" if ai_commentary else ""
    lines.append(
        f"- `scripts/auto_analyze.py --market {market} --span {span} --depth {depth}{ai_flag}`"
    )
    lines.append("- `scripts/event_impact_analyzer.py` (マクロイベント因果分析)")
    if ai_commentary:
        lines.append("- gh copilot (Copilot Premium / Claude Sonnet 4.6)")
    lines.append("")
    lines.append("### 📚 参照情報")
    lines.append(
        "- スクリーナー、テクニカル指標、ファンダメンタル、センチメント、マクロ指標（各スクリプト出力）"
    )
    lines.append("- Google News RSS（日英）ニュースによるイベント因果分析")
    lines.append("")
    lines.append("---")
    lines.append("")

    # イベント因果分析セクション（常に表示）
    lines.extend(_render_event_impact_section(event_impact))

    # AI分析がある場合、AI生成レポートをメインに
    if ai_commentary:
        lines.append(ai_commentary)
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("## 📈 収集データサマリー（参考）")
        lines.append("")

    # マクロ
    if macro:
        env = macro.get("market_environment", {})
        indicators = macro.get("indicators", {})
        lines.append(
            f"## 📊 市場環境: **{env.get('assessment', '不明')}** (スコア: {env.get('score', 'N/A')})"
        )
        lines.append("")
        lines.append("| 指標 | 現在値 | 20日変動 |")
        lines.append("|------|--------|----------|")
        for key, ind in indicators.items():
            lines.append(
                f"| {ind.get('label', key)} | {ind.get('current', 'N/A')} | {ind.get('change_20d', 'N/A')} |"
            )
        lines.append("")
        signals = env.get("signals", [])
        if signals:
            lines.append("**シグナル:** " + " / ".join(signals))
        lines.append("")
        lines.append("---")
        lines.append("")

    # スクリーニング
    if screener and "summary" in screener:
        s = screener["summary"]
        lines.append(
            f"## 🔍 スクリーニング ({s.get('data_obtained', '?')}/{s.get('scan_universe', '?')} 銘柄)"
        )
        lines.append("")
        lines.append("| 戦略 | ヒット | 上位銘柄 |")
        lines.append("|------|--------|---------|")
        for strat, info in s.get("strategies", {}).items():
            picks = ", ".join(info.get("top_picks", [])[:3])
            lines.append(f"| {strat} | {info.get('count', 0)} | {picks} |")
        lines.append("")
        lines.append("---")
        lines.append("")

    # 個別銘柄データ
    if not ai_commentary:
        lines.append("## 📊 売買判断")
        lines.append("")

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

            lines.append(
                f"- **判断**: {sm.get('action', '?')} | **スコア**: {sm.get('total_score', '?')} | **確信度**: {sm.get('confidence', '?')}"
            )
            lines.append(f"- **現在値**: {cs}{scorer.get('current_price', '?')}")
            lines.append("")

            if prob:
                lines.append("| 期間 | 上昇確率 | +α達成 | 下落リスク |")
                lines.append("|------|---------|--------|-----------|")
                lines.append(
                    f"| 5日 | {prob.get('5日後に上昇', '?')} | +3%: {prob.get('5日後に+3%以上', '?')} | -3%: {prob.get('5日後に-3%以下', '?')} |"
                )
                lines.append(
                    f"| 20日 | {prob.get('20日後に上昇', '?')} | +5%: {prob.get('20日後に+5%以上', '?')} | -5%: {prob.get('20日後に-5%以下', '?')} |"
                )
                lines.append(
                    f"| 60日 | {prob.get('60日後に上昇', '?')} | +10%: {prob.get('60日後に+10%以上', '?')} | — |"
                )
                lines.append("")

            if entry:
                for e in entry:
                    lines.append(f"- **{e.get('type', '')}**: {cs}{e.get('price', '?')}")
                lines.append(
                    f"- 損切り: {cs}{risk.get('損切りライン', '?')} / 利確1: {cs}{risk.get('利確目標1（ATR×2）', '?')} / 利確2: {cs}{risk.get('利確目標2（ATR×4）', '?')}"
                )
                lines.append("")

        if fund:
            fs = fund.get("fundamental_score", {})
            val = fund.get("valuation", {})
            prof = fund.get("profitability", {})
            analyst = fund.get("analyst", {})
            lines.append(
                f"- ファンダ: {fs.get('score', '?')}/{fs.get('max_score', 70)} | PER={val.get('PER', '?')} PBR={val.get('PBR', '?')} ROE={prof.get('ROE', '?')}"
            )
            if analyst:
                lines.append(
                    f"- アナリスト: {analyst.get('推奨', '?')} 目標{cs}{analyst.get('目標株価(平均)', '?')}"
                )
            lines.append("")

        if sent:
            lines.append(
                f"- センチメント: ポジ{sent.get('positive_pct', '?')}% / ネガ{sent.get('negative_pct', '?')}% ({sent.get('total', '?')}件)"
            )
            lines.append("")

        if ai_commentary:
            lines.append("</details>")
            lines.append("")
        else:
            lines.append("---")
            lines.append("")

    lines.append("")
    lines.append("> ⚠️ これは自動生成レポートであり、投資助言ではありません。")

    return "\n".join(lines)


def _render_event_impact_section(event_impact: dict) -> list[str]:
    """イベント因果分析セクションのMarkdown行リストを返す"""
    lines = []
    if not event_impact or not event_impact.get("triggered_rules"):
        lines.append("## 📡 イベント因果分析")
        lines.append("")
        lines.append("現在のニュースから有意なマクロイベントは検知されませんでした。")
        lines.append("")
        lines.append("---")
        lines.append("")
        return lines

    dir_map = {
        "risk_off": "⚠️ リスクオフ",
        "risk_on": "✅ リスクオン",
        "mixed": "⚡ 方向性まちまち",
        "neutral": "➖ 中立",
    }
    mag_sym = {"high": "◎", "medium": "○", "low": "△"}
    direction = event_impact.get("market_direction", "neutral")

    lines.append("## 📡 イベント因果分析")
    lines.append("")
    lines.append(f"**総合市場方向: {dir_map.get(direction, direction)}**")
    lines.append("")

    # トリガーされたルール
    lines.append("### 🔑 検知されたマクロイベント")
    for _key, info in event_impact["triggered_rules"].items():
        lines.append(f"- **{info['label']}** ({info['count']}件ヒット)")
        for h in info.get("headlines", [])[:2]:
            lines.append(f"  - {h}")
    lines.append("")

    # セクター影響
    sector_impacts = event_impact.get("sector_impacts", {})
    if sector_impacts:
        lines.append("### 📊 セクター別予測影響")
        pos_s = {k: v for k, v in sector_impacts.items() if v["direction"] == "positive"}
        neg_s = {k: v for k, v in sector_impacts.items() if v["direction"] == "negative"}
        if pos_s:
            lines.append("**↑ 恩恵を受けるセクター:**")
            for sector, info in sorted(
                pos_s.items(),
                key=lambda x: {"high": 3, "medium": 2, "low": 1}[x[1]["magnitude"]],
                reverse=True,
            ):
                lines.append(f"  - {mag_sym[info['magnitude']]} **{sector}**: {info['reason']}")
        if neg_s:
            lines.append("**↓ 影響を受けるセクター:**")
            for sector, info in sorted(
                neg_s.items(),
                key=lambda x: {"high": 3, "medium": 2, "low": 1}[x[1]["magnitude"]],
                reverse=True,
            ):
                lines.append(f"  - {mag_sym[info['magnitude']]} **{sector}**: {info['reason']}")
        lines.append("")

    # 資産影響
    asset_impacts = event_impact.get("asset_impacts", {})
    if asset_impacts:
        lines.append("### 💱 資産・為替への波及")
        asset_labels = {
            "oil": "原油",
            "gold": "金",
            "bonds": "国債",
            "usdjpy": "ドル円",
            "dxy": "ドルインデックス",
            "nikkei": "日経平均",
        }
        for asset, info in asset_impacts.items():
            arrow = "↑" if info["direction"] == "positive" else "↓"
            label = asset_labels.get(asset, asset)
            lines.append(f"- {label}: **{arrow}** ({info['magnitude']}) — {info['reason']}")
        lines.append("")

    # 注目銘柄
    tickers = event_impact.get("tickers_to_watch", {})
    if tickers.get("positive") or tickers.get("negative"):
        lines.append("### 🎯 イベント由来の注目銘柄")
        if tickers.get("positive"):
            lines.append(f"- **買い候補**: {', '.join(tickers['positive'])}")
        if tickers.get("negative"):
            lines.append(f"- **売り・回避**: {', '.join(tickers['negative'])}")
        lines.append("")

    lines.append("---")
    lines.append("")
    return lines


def run_analysis(market, span, depth, use_ai=False, ai_provider="copilot", ai_model=None):
    """分析パイプライン実行"""
    now = datetime.now(JST)
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S JST")
    file_ts = now.strftime("%Y-%m-%d_%H%M%S")

    steps = "6" if use_ai else "5"
    print(f"\n{'=' * 60}")
    print(f"  自動分析開始: {timestamp}")
    print(f"  market={market}, span={span}, depth={depth}, ai={use_ai}")
    print(f"{'=' * 60}\n")

    # 1. マクロ
    print(f"1/{steps} マクロ環境...")
    macro = run_script("macro.py")

    # 2. イベント因果分析
    print(f"2/{steps} イベント因果分析...")
    event_impact = run_script("event_impact_analyzer.py", timeout=90)
    if event_impact:
        n_rules = len(event_impact.get("triggered_rules", {}))
        direction = event_impact.get("market_direction", "neutral")
        print(f"  -> マクロイベント {n_rules}種検知, 市場方向={direction}")
    else:
        print("  -> イベント因果分析スキップ（タイムアウト or エラー）")
        event_impact = {}

    # 3. スクリーニング
    print(f"3/{steps} スクリーニング...")
    top_n = 3 if depth == "quick" else 5
    screener = run_script(
        "screener.py", ["--market", market, "--strategy", "all", "--top", str(top_n)], timeout=300
    )
    if not screener:
        print("ERROR: スクリーニング失敗", file=sys.stderr)
        return None

    # 4. 詳細分析
    max_a = {"quick": 3, "standard": 5, "detailed": 8}.get(depth, 5)
    top_tickers = get_top_tickers(screener, max_tickers=max_a)
    print(f"4/{steps} 上位{len(top_tickers)}銘柄を分析...")

    analyses = []
    for t in top_tickers:
        ticker = t["ticker"]
        result = {"info": t}
        print(f"  -> {ticker}...")
        result["scorer"] = run_script("scorer.py", [ticker])
        if depth in ("standard", "detailed"):
            result["fundamentals"] = run_script("fundamentals.py", [ticker])
            result["sentiment"] = run_script("fetch_sentiment.py", [t.get("name", ticker)])
        analyses.append(result)

    # 5. AI分析
    ai_commentary = None
    if use_ai:
        print(f"5/{steps} AI分析...")
        collected_data = {
            "macro": macro,
            "event_impact": event_impact,
            "screener_summary": screener.get("summary") if screener else None,
            "analyses": [],
        }
        for a in analyses:
            entry = {"ticker": a["info"]["ticker"], "name": a["info"].get("name", "")}
            if a.get("scorer"):
                entry["scorer"] = {
                    k: a["scorer"][k]
                    for k in [
                        "current_price",
                        "analysis_summary",
                        "probability",
                        "entry_points",
                        "risk_management",
                        "volatility",
                        "returns",
                        "technical_indicators",
                    ]
                    if k in a["scorer"]
                }
            if a.get("fundamentals"):
                entry["fundamentals"] = a["fundamentals"]
            if a.get("sentiment"):
                entry["sentiment"] = a["sentiment"]
            collected_data["analyses"].append(entry)

        prompt = build_ai_prompt(collected_data, market, span, depth)
        ai_commentary = call_ai_api(prompt, ai_provider, ai_model)

        if ai_commentary:
            print("  AI分析完了 ✓")
        else:
            print("  AI分析失敗 — テンプレートレポートにフォールバック")

    # 6. レポート生成 & 保存
    print(f"{steps}/{steps} レポート生成...")
    report = generate_report(
        macro, screener, analyses, market, span, depth, timestamp, ai_commentary, event_impact
    )

    DIARY_DIR.mkdir(exist_ok=True)
    ml = {"us": "米国株", "jp": "日本株", "all": "全市場"}.get(market, market)
    sl = {"short": "短期", "swing": "スイング", "medium": "中期", "all": "全スパン"}.get(span, span)
    ai_tag = "_AI" if ai_commentary else ""
    filename = f"{file_ts}_自動分析{ai_tag}_{ml}_{sl}.md"
    filepath = DIARY_DIR / filename
    filepath.write_text(report, encoding="utf-8")

    print(f"\n{'=' * 60}")
    print(f"  完了! diary/{filename}")
    if ai_commentary:
        print("  🤖 AI分析レポート生成済み")
    print(f"{'=' * 60}")

    for a in analyses:
        info = a["info"]
        sc = a.get("scorer", {})
        sm = sc.get("analysis_summary", {}) if sc else {}
        p = sc.get("probability", {}) if sc else {}
        print(
            f"  {info['ticker']:10s} | {sm.get('action', '?'):6s} | スコア {str(sm.get('total_score', '?')):>4s} | 60日↑ {p.get('60日後に上昇', '?')}"
        )

    return str(filepath)


def daemon_mode(market, span, depth, interval, use_ai=False, ai_provider="copilot", ai_model=None):
    """デーモンモード"""
    print(f"デーモンモード: {interval}秒({interval // 60}分)ごとに自動実行")
    if use_ai:
        print(f"AI分析: {ai_provider} (model: {ai_model or 'default'})")
    print("Ctrl+C で停止\n")
    count = 0
    while True:
        count += 1
        print(f"\n### 実行 #{count} ###")
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


def main():
    parser = argparse.ArgumentParser(description="自動分析スクリプト（AI分析対応）")
    parser.add_argument(
        "--market", choices=["us", "jp", "all"], default="jp", help="市場 (default: jp)"
    )
    parser.add_argument(
        "--span",
        choices=["short", "swing", "medium", "all"],
        default="medium",
        help="スパン (default: medium)",
    )
    parser.add_argument(
        "--depth",
        choices=["quick", "standard", "detailed"],
        default="standard",
        help="深さ (default: standard)",
    )
    parser.add_argument(
        "--ai",
        action="store_true",
        help="AI分析を有効化（Copilot / GitHub Models / OpenAI / Anthropic）",
    )
    parser.add_argument(
        "--ai-provider",
        choices=["copilot", "github", "openai", "anthropic"],
        default="copilot",
        help="AIプロバイダー (default: copilot)",
    )
    parser.add_argument(
        "--ai-model",
        type=str,
        default=None,
        help="使用するAIモデル（省略時はプロバイダーのデフォルト）",
    )
    parser.add_argument("--daemon", action="store_true", help="デーモンモード")
    parser.add_argument("--interval", type=int, default=1800, help="間隔(秒) (default: 1800=30分)")
    args = parser.parse_args()

    if args.daemon:
        daemon_mode(
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

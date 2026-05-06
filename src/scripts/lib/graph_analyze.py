"""LangGraph-based analysis report agent.

Replaces the fixed-step pipeline in ``auto_analyze.py`` with a ReAct agent
that freely explores market data and generates comprehensive analysis reports.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from langgraph.prebuilt import create_react_agent

SRC_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(SRC_DIR))

from scripts.lib.llm import get_chat_model
from scripts.lib.tools import ALL_TOOLS

from infra.container import get_container

JST = timezone(timedelta(hours=9))

MARKET_LABELS = {"us": "米国株", "jp": "日本株", "all": "全市場"}
SPAN_LABELS = {
    "short": "短期(1-5日)",
    "swing": "スイング(1-3週間)",
    "medium": "中期(1-3ヶ月)",
    "all": "全スパン",
}
SPAN_LABELS_SHORT = {"short": "短期", "swing": "スイング", "medium": "中期", "all": "全スパン"}
DEPTH_TICKERS = {"quick": 3, "standard": 5, "detailed": 8}


# ---------------------------------------------------------------------------
# System prompt builder
# ---------------------------------------------------------------------------

def _build_system_prompt(
    market: str,
    span: str,
    depth: str,
) -> str:
    ml = MARKET_LABELS.get(market, market)
    sl = SPAN_LABELS.get(span, span)
    max_tickers = DEPTH_TICKERS.get(depth, 5)

    return f"""あなたは株式売買判断の専門アナリストです。
与えられたツールを自由に使い、市場を分析して包括的な売買判断レポートを作成してください。

## 分析条件
- 対象市場: {ml}
- 推奨タイムスパン: {sl}
- 分析深度: {depth} (上位 {max_tickers} 銘柄程度を目安)

## 分析手順（推奨だが自由に判断してよい）

1. **マクロ環境確認**: `check_macro` で市場全体のリスクオン/オフを把握
2. **イベント因果分析**: `analyze_events` で地政学・金利・関税等の影響を確認
3. **銘柄スクリーニング**: `screen_stocks` で market="{market}" をスキャン
4. **詳細分析**: 有望な上位候補に対して:
   - `score_stock`: テクニカルスコア（必須）
   - `analyze_fundamentals`: 財務分析（standard/detailed で推奨）
   - `check_sentiment`: センチメント分析（standard/detailed で推奨）
   - `get_technical`: テクニカル指標の生データ
   - `get_news`: 直近ニュース
   - `get_prices`: 株価データ
5. **追加調査**: 分析中に気になることがあれば追加でツールを呼ぶ
   - 例: 同セクター比較、イベント影響の深掘り、関連銘柄の確認

## レポートの出力フォーマット

最終回答は以下の構成の **Markdown レポート** としてください:

### 構成
1. **市場環境サマリー**: マクロ環境スコア、リスクオン/オフ判定、主要指標の動向
2. **イベント因果分析**: 検知されたマクロイベントとセクター/資産への影響
3. **銘柄別売買判断**: 各銘柄について以下を含む:
   - 判断（強い買い〜強い売り）、総合スコア/100、確信度
   - 推奨タイムスパン別の確率と目標価格
   - エントリー価格・損切りライン・利確目標（リスクリワード比付き）
   - テクニカル/ファンダメンタル/センチメント/マクロの根拠
4. **銘柄間比較・優先順位**: どの銘柄が最も有望か
5. **ポートフォリオ提案**: 分散を考慮した組み合わせ提案
6. **リスクシナリオ**: 主要な下落リスクと対処法

### 各銘柄のフォーマット例

```
### 銘柄: AAPL (Apple Inc.)
- **判断**: 買い
- **総合スコア**: 45 / 100
- **確信度**: 中〜高

#### 確率予測
| 期間 | 上昇確率 | +α達成 | 下落リスク |
|------|---------|--------|-----------|
| 5日後 | 62% | +3%以上: 28% | -3%以下: 15% |
| 20日後 | 68% | +5%以上: 35% | -5%以下: 12% |
| 60日後 | 72% | +10%以上: 25% | — |

#### エントリー戦略
- エントリー価格: $178.50（指値買い）
- 損切りライン: $171.00
- 利確目標1: $185.00（リスクリワード比: 1:0.87）
- 利確目標2: $195.00

#### 根拠
- テクニカル: RSI=35（売られすぎ接近）、MACD反転兆候...
- ファンダメンタル: PER 28.5x (業界平均 32x)、ROE 160%...
- センチメント: ポジティブ 65%、新製品発表への期待...
```

## 重要ルール
- 具体的な数値（確率・価格・日数）を必ず含める
- 根拠のない推測は避け、ツールから得たデータに基づいて判断
- 不確実性が高い場合は正直にその旨を伝える
- レポートの最後に「⚠️ これは自動生成レポートであり、投資助言ではありません。」を付記
"""


# ---------------------------------------------------------------------------
# Core graph execution
# ---------------------------------------------------------------------------

def run_analyze_graph(
    market: str = "jp",
    span: str = "medium",
    depth: str = "standard",
    provider: str = "copilot",
    model: str | None = None,
) -> dict[str, Any]:
    """Execute the LangGraph analysis agent and return the report.

    Returns a dict with keys: ``report``, ``filepath``.
    """
    now = datetime.now(JST)
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S JST")
    file_ts = now.strftime("%Y-%m-%d_%H%M%S")
    ml = MARKET_LABELS.get(market, market)
    sl = SPAN_LABELS_SHORT.get(span, span)

    print(f"\n{'=' * 60}")
    print(f"  LangGraph 分析エージェント: {timestamp}")
    print(f"  market={market}, span={span}, depth={depth}, provider={provider}")
    print(f"{'=' * 60}\n")

    # Build agent
    llm = get_chat_model(provider=provider, model=model)
    system_prompt = _build_system_prompt(market, span, depth)
    agent = create_react_agent(llm, ALL_TOOLS, prompt=system_prompt)

    user_msg = (
        f"現在 {now:%Y-%m-%d %H:%M} JST です。"
        f"市場（{ml}）を分析して、{sl} の売買判断レポートを作成してください。"
        f"分析深度は {depth} です。"
    )

    print("エージェント実行中...")
    result = agent.invoke({"messages": [("user", user_msg)]})
    messages = result.get("messages", [])

    # Extract final AI message (the report)
    report = ""
    for msg in reversed(messages):
        if hasattr(msg, "content") and msg.content and not hasattr(msg, "tool_call_id"):
            report = msg.content
            break

    if not report:
        print("WARNING: エージェントからレポートが返されませんでした", file=sys.stderr)
        return {"report": "", "filepath": ""}

    # Add header
    header = (
        f"# 📝 LangGraph 自動分析レポート — {ml} {sl}\n"
        f"- **日時**: {timestamp}\n"
        f"- **対象**: {ml}\n"
        f"- **実行モード**: 🤖 LangGraph ReAct Agent (depth={depth})\n"
        f"- **種別**: 売買判断\n\n"
        f"### 🔧 使用ツール\n"
        f"- LangGraph ReAct Agent (provider={provider})\n"
        f"- 利用可能ツール: {', '.join(t.name for t in ALL_TOOLS)}\n\n"
        f"---\n\n"
    )
    full_report = header + report

    # Save
    filename = f"{file_ts}_自動分析_agent_{ml}_{sl}.md"
    diary = get_container().diary()
    diary.save_report(filename, full_report)

    print(f"\n{'=' * 60}")
    print(f"  完了! diary/{filename}")
    print(f"{'=' * 60}")

    return {"report": full_report, "filepath": f"diary/{filename}"}

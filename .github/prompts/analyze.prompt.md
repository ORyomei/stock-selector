---
description: "株式の売買分析を実行する。「分析して」「銘柄を調べて」「株を分析」「儲かる株を探して」「おすすめ教えて」などと言われたときに使う。"
agent: "agent"
---

## 手順

1. まず `vscode_askQuestions` ツールを使って、以下の項目をユーザーに質問する:
   - **分析モード**（自動発掘 or 指定銘柄）
   - **売買スパン**（短期/スイング/中期/全スパン）
   - **分析の深さ**（クイック/標準/詳細）

2. 回答に応じて情報を収集する:

### 「🔍 自動発掘」が選ばれた場合
   - `python3 scripts/screener.py --market all --strategy all --top 5` でスキャン
   - ヒットした上位銘柄に対して `python3 scripts/scorer.py <ticker>` で詳細分析
   - ニュース・センチメントも確認

### 特定の銘柄/戦略が選ばれた場合
   - `python3 scripts/fetch_prices.py <ticker>` — 株価
   - `python3 scripts/technical.py <ticker>` — テクニカル指標
   - `python3 scripts/scorer.py <ticker>` — 総合スコアリング
   - `python3 scripts/fetch_news.py <query>` — ニュース
   - `python3 scripts/fetch_sentiment.py <query>` — センチメント

3. 結果を [copilot-instructions.md](../.github/copilot-instructions.md) の出力フォーマットに従って報告する

4. diary/ にタイムスタンプ付きで記録する

## 質問テンプレート

`vscode_askQuestions` を以下の引数で呼び出すこと:

```json
{
  "questions": [
    {
      "header": "モード",
      "question": "どのように銘柄を探しますか？",
      "options": [
        { "label": "🔍 自動発掘（市場全体をスキャン）", "description": "米国+日本の約130銘柄をスキャンし有望銘柄を発見", "recommended": true },
        { "label": "🔍 米国株だけスキャン", "description": "NASDAQ/NYSE の約90銘柄をスキャン" },
        { "label": "🔍 日本株だけスキャン", "description": "東証の約40銘柄をスキャン" },
        { "label": "📌 銘柄を指定する", "description": "特定のティッカーを入力して分析" }
      ],
      "allowFreeformInput": true
    },
    {
      "header": "スパン",
      "question": "どのタイムスパンで分析しますか？",
      "options": [
        { "label": "短期（1-5営業日）", "description": "デイトレ〜数日の短期売買" },
        { "label": "スイング（1-3週間）", "description": "数日〜数週間のスイング", "recommended": true },
        { "label": "中期（1-3ヶ月）", "description": "中長期で保有" },
        { "label": "全スパン", "description": "すべてのスパンを網羅的に分析" }
      ],
      "allowFreeformInput": false
    },
    {
      "header": "深さ",
      "question": "分析の深さを選んでください",
      "options": [
        { "label": "クイック", "description": "最速。スコアと確率だけ確認したいとき" },
        { "label": "標準", "description": "テクニカル＋ニュース＋スコア。通常はこれ", "recommended": true },
        { "label": "詳細", "description": "全データ＋Web調査＋複数ソース。時間をかけて徹底分析" }
      ],
      "allowFreeformInput": false
    }
  ]
}
```

ティッカーが「モード」にフリーテキストで入力された場合（例: `AAPL`, `7203.T`）は、その銘柄を直接分析する。

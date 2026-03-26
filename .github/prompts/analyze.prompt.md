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
   - `python scripts/screener.py --market all --strategy all --top 5` でスキャン
   - ヒットした上位銘柄に対して `python scripts/scorer.py <ticker>` で詳細分析
   - ニュース・センチメントも確認

### 特定の銘柄/戦略が選ばれた場合
   - `python scripts/fetch_prices.py <ticker>` — 株価
   - `python scripts/technical.py <ticker>` — テクニカル指標
   - `python scripts/scorer.py <ticker>` — 総合スコアリング
   - `python scripts/fetch_news.py <query>` — ニュース
   - `python scripts/fetch_sentiment.py <query>` — センチメント

3. 結果を [copilot-instructions.md](../.github/copilot-instructions.md) の出力フォーマットに従って報告する

4. diary/ にタイムスタンプ付きで記録する

## 質問テンプレート

`vscode_askQuestions` で以下の質問を表示すること:

### 質問1: 分析モード

- header: "モード"
- question: "どのように銘柄を探しますか？"
- options:
  - "🔍 自動発掘（市場全体をスキャン）" — description: "米国+日本の約130銘柄をAIがスキャンし、有望銘柄を発見します", recommended
  - "🔍 米国株だけスキャン" — description: "NASDAQ/NYSE の約90銘柄をスキャン"
  - "🔍 日本株だけスキャン" — description: "東証の約40銘柄をスキャン"
  - "📌 銘柄を指定する" — description: "特定のティッカーを入力して分析"
- allowFreeformInput: true（ティッカーを直接入力可。例: AAPL, 7203.T）

### 質問2: 売買スパン

- header: "スパン"
- question: "どのタイムスパンで分析しますか？"
- options:
  - "短期（1-5営業日）" — description: "デイトレ〜数日の短期売買"
  - "スイング（1-3週間）" — description: "数日〜数週間のスイング", recommended
  - "中期（1-3ヶ月）" — description: "中長期で保有"
  - "全スパン" — description: "すべてのスパンを網羅的に分析"
- allowFreeformInput: false

### 質問3: 分析の深さ

- header: "深さ"
- question: "分析の深さを選んでください"
- options:
  - "クイック" — description: "最速。スコアと確率だけ確認したいとき"
  - "標準" — description: "テクニカル＋ニュース＋スコア。通常はこれ", recommended
  - "詳細" — description: "全データ＋Web調査＋複数ソース。時間をかけて徹底分析"
- allowFreeformInput: false

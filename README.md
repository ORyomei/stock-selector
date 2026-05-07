# Stock Selector — AI 自動株式売買システム

LangGraph ReAct Agent が**市場スキャン → 多角的分析 → 売買判断 → 注文執行 → リスク管理**を自律的に回す、AI 駆動の自動売買システム。デーモンモードで放置運用が可能。

## アーキテクチャ

```
┌─────────────────────────────────────────────────┐
│              auto_trade.py (daemon)              │
│         30分間隔で自動サイクルを実行              │
└──────────────────────┬──────────────────────────┘
                       │
          ┌────────────▼────────────┐
          │  LangGraph ReAct Agent  │  ← LLM が自律的にツールを
          │  (graph_trade.py)       │    選択・呼び出し・判断
          └────────────┬────────────┘
                       │ ツール呼び出し (9種)
    ┌──────────┬───────┼───────┬──────────┐
    ▼          ▼       ▼       ▼          ▼
 screener  scorer  technical  macro  fundamentals ...
    │          │       │       │          │
    └──────────┴───────┴───────┴──────────┘
                       │
              JSON シグナル抽出
                       │
          ┌────────────▼────────────┐
          │     TradeExecutor       │  ← 注文実行は非LLM（安全）
          │  RiskManager で検証     │
          └────────────┬────────────┘
                       │
          ┌────────────▼────────────┐
          │   Broker (Sim / kabu)   │  → portfolio.json に永続化
          │   シミュレーター or 実API │  → diary/ にログ保存
          └─────────────────────────┘
```

### 自動売買サイクルの流れ

1. **自動クローズ判定**（非 LLM）— 損切り・利確・トレーリングストップ・最大保有日数到達のチェック
1.5. **市場シナリオ判定** — マクロ指標からリスクオン/ニュートラル/リスクオフを判定し、シナリオ別プロンプトを選択
2. **ReAct Agent 起動** — ポートフォリオ状況・リスク設定・シナリオをプロンプトに注入し、LLM がツールを自律的に呼び出して市場分析（180秒タイムアウト付き）
3. **シグナル抽出**（非 LLM）— AI 出力から JSON シグナルをパース。失敗時はフォローアップで構造化出力を再要求。**ユニバース外ティッカーは自動却下**
3.5. **反証ゲート**（非 LLM）— シグナルの必須フィールド・市場環境整合性を検証し、不適格シグナルを却下
4. **注文実行**（非 LLM）— RiskManager で検証後、成行注文で約定。ポジション枠満杯時は自動で入れ替え（swap）モードに移行

> 売買の最終実行は LLM の外で行い、安全性を確保している。

### 各ステップの詳細シーケンス

#### Step 1: 自動クローズ判定（非 LLM）

```
trade.py --check-and-close
    │
    ├─ portfolio.json から全保有ポジションを読み込み
    │
    ├─ 各ポジションについて:
    │   ├─ 現在値を yfinance で取得
    │   ├─ 損切りライン到達？      → 成行売り (STOP_LOSS)
    │   ├─ 利確ライン到達？        → 成行売り (TAKE_PROFIT)
    │   ├─ トレーリングストップ？   → 高値から 2% 逆行で売り (TRAILING_STOP)
    │   └─ 最大保有日数 (30日) 超過？ → 成行売り (MAX_HOLD_DAYS)
    │
    └─ クローズ結果をログ出力（対象なしなら "クローズ対象なし"）
```

- LLM を一切介さない安全重視の機械的判定
- 損切り 3%・利確 5%・トレーリング 2% のデフォルト値は `config/risk_limits.json` で変更可能
- 個別ポジションの損切り/利確はシグナル生成時に銘柄ごとに設定される

#### Step 2: ReAct Agent による市場分析

```
create_react_agent(llm, 9_tools, system_prompt)
    │
    │  system_prompt に注入される情報:
    │  ├─ 対象市場 (us/jp/all)、最小スコア閾値、最大シグナル数
    │  ├─ ポートフォリオ状況 (ポジション数/上限、各保有銘柄の損益)
    │  └─ 枠満杯時は入れ替えルールを追加
    │
    ├─ LLM の自律ツール呼び出し（典型的な流れ）:
    │   │
    │   ├─ 1. check_macro()
    │   │      → VIX / 金利 / 為替 / 原油 / 主要指数
    │   │      → 環境スコア算出 → -30 以下なら新規買い見送り判断
    │   │
    │   ├─ 2. analyze_events()
    │   │      → 地政学 / 金利 / 関税 / 規制 / テック / エネルギー / パンデミック
    │   │      → 7 カテゴリのイベント因果分析
    │   │
    │   ├─ 3. screen_stocks(market, strategy="all")
    │   │      → oversold / momentum / breakout / value の 4 戦略
    │   │      → 各戦略上位 N 件の候補を返却（保有銘柄は除外して検討）
    │   │
    │   ├─ 4. 有望候補ごとに詳細分析（LLM が必要と判断した分だけ呼ぶ）:
    │   │      ├─ score_stock(ticker)         … テクニカルスコア (-100〜+100)
    │   │      ├─ analyze_fundamentals(ticker) … PER/PBR/ROE/FCF/アナリスト予想
    │   │      ├─ check_sentiment(ticker)      … ニュースセンチメント
    │   │      ├─ get_technical(ticker)        … RSI/MACD/BB/SMA/EMA 生データ
    │   │      ├─ get_news(ticker)             … 直近ヘッドライン
    │   │      └─ get_prices(ticker)           … 株価・時価総額・52週高安
    │   │
    │   └─ 5. 追加調査（LLM の判断で任意）
    │          └─ セクター比較、別銘柄のスコア確認、マクロ深掘り等
    │
    └─ 最終回答: JSON シグナル + マークダウン分析コメント
```

- ツール呼び出しの順序・回数は LLM が状況に応じて自律的に決定する（上記は典型例）
- 枠満杯時: 保有最低スコアの銘柄と新規候補を比較し、スコア差 +5 以上なら swap シグナルを出す
- LLM がシグナル 0 件と判断するのも正当（無理に買わない）

#### Step 3: シグナル抽出（非 LLM）

```
AI 最終回答テキスト
    │
    ├─ parse_ai_json() で ```json ブロックを検索・パース
    │   ├─ 成功 → signals 配列を取得
    │   └─ 失敗（JSON なし）
    │       │
    │       └─ フォローアップ: 同じ Agent に再度リクエスト
    │          「分析結果を元に JSON 形式で出力してください」
    │          └─ 再度 parse_ai_json() でパース
    │
    ├─ 各シグナルのバリデーション:
    │   ├─ action が "buy" または "swap" のみ通過
    │   ├─ max_signals 件に制限
    │   └─ entry_price / target_price / stop_loss_price / confidence を正規化
    │
    └─ signals: list[dict] を返却
```

シグナル JSON の構造:

```json
{
  "signals": [
    {
      "ticker": "8035.T",
      "action": "buy",           // "buy" | "swap"
      "score": 45,               // テクニカルスコア
      "confidence": 0.8,         // 確信度 0.0〜1.0
      "reason": "RSI 売られすぎ + MACD GC 接近",
      "entry_price": 0,          // 0 = 成行
      "target_price": 50000,
      "stop_loss_price": 42000,
      "take_profit_price": 52000,
      "timespan": "swing",       // "short" | "swing" | "medium"
      "sell_ticker": "9983.T"    // swap 時のみ: 売却対象
    }
  ],
  "market_comment": "市場環境の概要",
  "skipped": [{"ticker": "MSFT", "reason": "スコア不足"}]
}
```

#### Step 4: 注文実行（非 LLM）

```
signals (list)
    │
    ├─ ドライラン → ログ出力のみ、portfolio.json は変更しない
    │
    └─ 本番:
        ├─ swap シグナルの場合:
        │   ├─ 1. sell_ticker の全株を成行売り (trade.py --close)
        │   │      ├─ 成功 → SOLD ログ → 次へ
        │   │      └─ 失敗 → SELL_FAILED ログ → この銘柄スキップ
        │   └─ 2. 新規 ticker を買い
        │
        ├─ buy シグナルの場合:
        │   ├─ シグナル JSON を diary/signals/ に保存
        │   ├─ trade.py --from-signal <path> を実行
        │   │   ├─ RiskManager が検証:
        │   │   │   ├─ ポジション上限チェック (max 10)
        │   │   │   ├─ 1ポジション資金比率チェック (max 5%)
        │   │   │   ├─ 日次損失チェック (max 2%)
        │   │   │   └─ 重複銘柄チェック
        │   │   ├─ OrderManager が注文作成
        │   │   └─ BrokerSimulator が約定
        │   │       ├─ 現在値を取得 + スプレッド 0.02% 適用
        │   │       ├─ 資金から差し引き
        │   │       └─ portfolio.json を更新
        │   ├─ 成功 (FILLED) → 約定記録を diary/trades/ に保存
        │   └─ 失敗 → FAILED ログ
        │
        └─ 全シグナル処理完了 → diary/ にサイクルログ保存
```

- RiskManager が不適格と判断した注文は約定前に却下される
- シミュレーターのスプレッドは 0.02%（往復 0.04%）
- 約定記録・シグナル・分析ログはすべて `diary/` 配下に永続化

#### デーモンループ

```
main() --daemon --interval 1800
    │
    └─ while True:
        ├─ run_trade_graph(market, min_score, max_signals, dry_run)
        │   └─ Step 1〜4 を 1 サイクル実行
        ├─ エラー発生時 → catch して次サイクルへ継続
        ├─ sleep(interval) … デフォルト 1800秒 (30分)
        └─ Ctrl+C → 正常終了
```

## 主な機能

| 機能 | 説明 |
|---|---|
| **AI 自動売買** | LangGraph ReAct Agent が分析→判断→発注を自律実行。デーモンで放置運用 |
| **銘柄発掘** | 4戦略（売られすぎ・モメンタム・ブレイクアウト・バリュー）で日米市場を横断スキャン |
| **多角的分析** | テクニカル + ファンダメンタル + ニュースセンチメント + マクロ環境 + イベント因果分析 |
| **リスク管理** | 損切り・利確・トレーリングストップ・最大保有日数・ポジション上限・マクロ安全弁を自動適用 |
| **仮想売買** | シミュレーターで注文・損益追跡（将来的に実ブローカー接続可能な設計） |
| **分析レポート** | 売買判断・テクニカル・ファンダメンタル等の全記録を `diary/` に自動保存 |
| **バックテスト** | 過去の推奨を実際の値動きと比較し、的中率を検証 |
| **Copilot Chat 連携** | VS Code Chat から自然言語で含み益確認・個別分析・手動売買も可能 |

## セットアップ

```bash
# Dev Container を開く（推奨）、または手動で:
uv sync                    # 依存パッケージのインストール
source .venv/bin/activate  # 仮想環境の有効化
```

> Python 3.12+、パッケージ管理は [uv](https://docs.astral.sh/uv/) を使用。

## 自動売買の起動

### デーモンモード（推奨）

```bash
source .venv/bin/activate

# 全市場を30分間隔で自動売買
python3 src/scripts/auto_trade.py --daemon --interval 1800

# 日本株のみ
python3 src/scripts/auto_trade.py --market jp --daemon --interval 1800

# バックグラウンド実行
nohup python3 -u src/scripts/auto_trade.py --daemon --interval 1800 \
  > logs/auto_trade_daemon.log 2>&1 &
echo $! > logs/daemon.pid
```

各サイクルで以下が自動実行される:
- 保有銘柄の損切り/利確チェック → 該当があれば自動クローズ
- ReAct Agent が市場環境・スクリーニング・個別分析を実施
- 買い/入れ替えシグナルを生成 → RiskManager 検証 → 約定
- 分析ログを `diary/` に、シグナルを `diary/signals/` に保存

### ドライラン（注文なしテスト）

```bash
python3 src/scripts/auto_trade.py --dry-run
```

### レガシーモード（固定パイプライン）

LangGraph を使わず、固定ステップ（screen → score → 判断 → execute）で実行:

```bash
python3 src/scripts/auto_trade.py --legacy --daemon --interval 1800
```

### 自動分析レポート（売買なし）

```bash
# 日本株の中期分析レポートを生成
python3 src/scripts/auto_analyze.py --market jp --span medium --depth standard

# デーモンモードで定期レポート
python3 src/scripts/auto_analyze.py --daemon --interval 1800
```

## リスク管理

`config/risk_limits.json` で制御。マクロ環境が悪化（スコア -30 以下）すると新規買いを自動スキップする安全弁付き。

| パラメータ | デフォルト | 説明 |
|---|---:|---|
| 1ポジション最大資金比率 | 5% | 集中投資の防止 |
| 1日最大損失率 | 2% | 日次ドローダウン制限 |
| 同時保有上限 | 10 | ポジション数の制限 |
| デフォルト損切り | 3% | エントリー価格からの下落幅 |
| デフォルト利確 | 5% | エントリー価格からの上昇幅 |
| トレーリングストップ | 2% | 高値からの逆行幅 |
| 最大保有日数 | 30日 | 超過で自動クローズ |

## LLM プロバイダー

LiteLLM 経由で複数プロバイダーに対応。デフォルトは Copilot。

| プロバイダー | モデル | API キー |
|---|---|---|
| `copilot`（デフォルト） | `claude-haiku-4.5` | 不要（Copilot トークン） |
| `github` | `gpt-4o` | 不要 |
| `openai` | `gpt-4o` | `OPENAI_API_KEY` |
| `anthropic` | `claude-sonnet-4` | `ANTHROPIC_API_KEY` |

## Agent ツール一覧

ReAct Agent が自律的に選択・呼び出す 9 つのツール:

| ツール | 説明 |
|---|---|
| `check_macro` | VIX・金利・為替・原油・主要指数 → リスクオン/オフ判定 |
| `screen_stocks` | 4戦略で銘柄発掘（oversold / momentum / breakout / value） |
| `score_stock` | テクニカルスコア（-100〜+100）、確率・目標価格・損切りライン算出 |
| `analyze_fundamentals` | PER・PBR・ROE・FCF・アナリスト予想 → ファンダメンタルスコア |
| `check_sentiment` | Google News ヘッドラインの日英センチメント分析 |
| `analyze_events` | 地政学・金利・関税等 7 カテゴリのイベント因果分析 |
| `get_technical` | RSI / MACD / BB / SMA / EMA の生データ取得 |
| `get_news` | Google News RSS ヘッドライン取得 |
| `get_prices` | 株価・時価総額・PER・52週高安 |

## CLI リファレンス

```bash
source .venv/bin/activate
```

### 自動売買・分析

```bash
# 自動売買（デフォルト: LangGraph Agent）
python3 src/scripts/auto_trade.py [--market us|jp|all] [--daemon] [--interval N] [--dry-run] [--legacy]

# 自動分析レポート
python3 src/scripts/auto_analyze.py [--market us|jp|all] [--span short|swing|medium|all] [--depth quick|standard|detailed] [--daemon]
```

### 個別スクリプト

```bash
# 銘柄スクリーニング
python3 src/scripts/screener.py --market all --strategy all --top 10

# 総合スコアリング
python3 src/scripts/scorer.py AAPL

# テクニカル指標
python3 src/scripts/technical.py 8035.T

# ファンダメンタル分析
python3 src/scripts/fundamentals.py NVDA

# マクロ指標
python3 src/scripts/macro.py

# ニュース / センチメント
python3 src/scripts/fetch_news.py "NVIDIA"
python3 src/scripts/fetch_sentiment.py "トヨタ自動車"

# 株価データ
python3 src/scripts/fetch_prices.py 7203.T

# アラート（ウォッチリスト監視 / ポートフォリオチェック）
python3 src/scripts/alert.py [--check-portfolio]

# バックテスト
python3 src/scripts/backtest.py [--days 30] [--min-score 60]

# ポートフォリオ管理
python3 src/scripts/portfolio.py status|performance|buy|sell
```

## Copilot Chat からの操作

VS Code Chat で自然言語でも操作可能。自動売買と並行して使用できる。

```
含み益確認                          → 保有銘柄の現在値・含み損益を表示
保有銘柄一覧                        → ポートフォリオ全体の状況
NVIDIAを調べて売買判断して           → 個別銘柄の総合分析レポート
日本株で有望な銘柄を探して           → スクリーニング → 詳細分析
東京エレクトロンを100万円分買って     → 仮想売買の手動実行
損切りに引っかかってる銘柄ある？      → ポートフォリオのアラートチェック
今の市場環境は？                    → マクロ指標・リスクオン/オフ判定
バックテストして成績を見せて          → 過去推奨の的中率検証
```

## ディレクトリ構成

```
stock-selector/
├── src/
│   ├── scripts/                     # 分析・売買スクリプト
│   │   ├── lib/
│   │   │   ├── graph_trade.py       #   自動売買 ReAct Agent
│   │   │   ├── graph_analyze.py     #   分析レポート ReAct Agent
│   │   │   ├── tools.py             #   LangGraph ツール定義 (9種)
│   │   │   ├── llm.py               #   LiteLLM ↔ LangChain ブリッジ
│   │   │   ├── ai.py                #   AI プロバイダー統合
│   │   │   ├── runner.py            #   スクリプト実行ヘルパー
│   │   │   └── portfolio.py         #   ポートフォリオ操作
│   │   ├── auto_trade.py            # 自動売買エントリーポイント
│   │   ├── auto_analyze.py          # 自動分析エントリーポイント
│   │   ├── screener.py              # 銘柄スクリーナー
│   │   ├── scorer.py                # 総合スコアリング
│   │   ├── technical.py             # テクニカル指標算出
│   │   ├── fundamentals.py          # ファンダメンタル分析
│   │   ├── macro.py                 # マクロ経済指標
│   │   ├── fetch_prices.py          # 株価データ取得
│   │   ├── fetch_news.py            # ニュース取得
│   │   ├── fetch_sentiment.py       # センチメント分析
│   │   ├── alert.py                 # アラート・監視
│   │   ├── backtest.py              # バックテスト
│   │   ├── portfolio.py             # ポートフォリオ管理 CLI
│   │   ├── trade.py                 # 個別売買 CLI
│   │   └── event_impact_analyzer.py # イベントインパクト分析
│   ├── trading/                     # 売買エンジン
│   │   ├── broker_interface.py      #   ブローカー抽象インターフェース
│   │   ├── simulator.py             #   仮想ブローカー（シミュレーター）
│   │   ├── brokers/
│   │   │   └── kabu_station.py      #   auカブコム kabuステーション API
│   │   ├── order_manager.py         #   注文管理
│   │   ├── risk_manager.py          #   リスク管理
│   │   └── trade_executor.py        #   売買実行オーケストレーター
│   └── infra/                       # インフラ層（DI コンテナ・リポジトリ）
├── config/
│   ├── watchlist.json               # 監視銘柄リスト
│   ├── risk_limits.json             # リスク管理パラメータ
│   ├── trading_config.json          # 売買設定（ブローカー切替）
│   ├── prompt_scenarios.json        # シナリオ別プロンプト設定
│   └── validation_rules.json        # シグナルバリデーションルール
├── diary/                           # 分析・売買の全記録
│   ├── signals/                     #   AI 生成シグナル (JSON)
│   └── trades/                      #   約定記録
├── logs/                            # デーモンログ
├── docs/
│   └── TRADING_SPEC.md              # 売買エンジン設計書
├── .github/
│   └── copilot-instructions.md      # Copilot Agent 指示
├── portfolio.json                   # ポートフォリオ状態
└── pyproject.toml                   # プロジェクト定義・依存管理
```

## 開発

```bash
uv run ruff check src/ trading/      # リント
uv run ruff format src/ trading/     # フォーマット
uv run pyright src/ trading/         # 型チェック
uv run pytest                        # テスト
```

## 注意事項

- **投資助言ではない** — 出力は参考情報であり、投資判断の責任はユーザーにある
- **ブローカー切替可能** — `trading_config.json` の `broker` で `"simulator"` / `"kabu"` を切り替え。kabuステーション API 接続は `KABU_API_PASSWORD` 環境変数が必要
- API キーは `.env` で管理（`.gitignore` に含まれる）

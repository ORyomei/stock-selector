# Stock Selector — AI 自動株式売買システム

Claude Agent (headless / Agent SDK) が**市場スキャン → 多角的分析 → 売買判断 → 注文執行 → リスク管理**を自律的に回す、AI 駆動の自動売買システム。デーモンモードで放置運用が可能。

## アーキテクチャ

```
┌─────────────────────────────────────────────────┐
│       stock-selector auto-trade (daemon)         │
│         30分間隔で自動サイクルを実行              │
└──────────────────────┬──────────────────────────┘
                       │
          ┌────────────▼────────────┐
          │   メイン取引エージェント   │  ← Claude Agent SDK (既定) or
          │  (agents/claude_agent /  │    LangGraph ReAct (litellm 系)
          │   agents/graph_trade)    │    が自律的にツールを選択・判断
          └────────────┬────────────┘
                       │ ツール呼び出し (agents/tools.py の ALL_TOOLS)
    ┌──────────┬───────┼────────┬──────────────┐
    ▼          ▼       ▼        ▼              ▼
 screener  scorer  technical  macro  deep_research(入れ子AI) ...
    └──────────┴───────┼────────┴──────────────┘
                       │ submit_signals ツールで提出
          ┌────────────▼────────────┐
          │  反証ゲート → 分散チェック │  ← バリデーション + AI レビュー
          └────────────┬────────────┘
          ┌────────────▼────────────┐
          │     TradeExecutor       │  ← 注文実行は非LLM（安全）
          │   RiskManager で検証    │
          └────────────┬────────────┘
          ┌────────────▼────────────┐
          │   Broker (Sim / kabu)   │  → portfolio.json に永続化
          └─────────────────────────┘
```

### 自動売買サイクルの流れ

| Step | 内容 | LLM |
|---|---|---|
| 1 | **自動クローズ判定** — 損切り・利確(半分ずつ段階利確)・トレーリング・保有日数 | なし |
| 1.5 | **市場シナリオ判定** — リスクオン/ニュートラル/リスクオフ → シナリオ別プロンプト | なし |
| 1.6 | ポートフォリオ健全性チェック（集中超過の警告） | なし |
| 1.7 | **AI 手仕舞い助言** — 機械ルール非該当のポジションに早期 exit/trim を助言 | 補助AI |
| 1.8 | **振り返り学習** — 過去のクローズ実績から教訓を抽出しエントリー判断に注入 | 補助AI |
| 2 | **メインエージェント** — ツールを自律的に呼び分析（SDK 経路 420 秒タイムアウト） | メインAI |
| 3 | **シグナル抽出** — `submit_signals` ツール経由で構造化受理。ユニバース外は却下 | なし |
| 3.45 | 執行可能性フィルタ — 1単元コストが評価額上限を超える銘柄を除外 | なし |
| 3.5 | **反証ゲート** — 必須フィールド・市場環境整合性を検証し不適格を却下 | なし |
| 3.55 | **ポートフォリオ分散チェック** — セクター過集中となるシグナルを除外 | 補助AI |
| 4 | **注文実行** — RiskManager 検証 → 成行約定。枠満杯時は swap（入れ替え） | なし |

> 売買の最終実行は LLM の外で行い、安全性を確保している。シグナル 0 件（無理に買わない）も正当な判断。

### 売りの3系統

1. **機械ストップ**（Step 1・非LLM）— 損切り / 利確（半分ずつスケールアウト）/ トレーリング（含み益 +5% で武装、高値から 3% 逆行）/ 大幅損失ガード / timespan 別保有日数タイムアウト
2. **AI 手仕舞い助言**（Step 1.7）— 機械ルールに該当しなかったポジションだけを対象に、エントリー時の thesis（fail_conditions / invalidation_conditions）崩壊・決算接近などを根拠に exit / trim。デフォルトは hold で、機械ストップを止める方向には介入できない
3. **swap**（Step 2）— ポジション枠満杯時、新規候補のスコアが保有最低スコア +5 以上の場合のみ入れ替え

メインエージェントが出せるアクションは `buy` / `swap` のみで、純粋な売り判断は上記 1・2 に分離されている。

さらに**回転抑制ガード**（`core/churn_guard.py`）が短期往復を機械的に禁止する:
取得から2営業日未満の swap 売り（fail_conditions 発動時を除く）と、売却から2営業日
未満の買い直しは却下される。機械ストップ・部分利確（trim）は対象外。

## エージェントツール

ツールの定義・一覧は `src/agents/tools.py` の `ALL_TOOLS` が唯一の真実（現在 12 種）。

| ツール | 説明 |
|---|---|
| `check_macro` | VIX・金利・為替・原油・主要指数の現在値 → リスクオン/オフ判定 |
| `screen_stocks` | 4戦略（oversold / momentum / breakout / value）で日米市場をスキャン |
| `score_stock` | テクニカルスコア（-100〜+100）、目標価格・損切りライン算出 |
| `analyze_fundamentals` | PER・PBR・ROE・成長率・アナリスト予想・決算日 |
| `check_sentiment` | ニュース見出しの辞書センチメント分析 |
| `analyze_events` | 7カテゴリのキーワードベースイベント分類 |
| `get_technical` / `get_news` / `get_prices` | 生データ取得 |
| `sector_strength` | TOPIX-17 ETF / 米セクターETF の騰落率（相対強度） |
| `market_calendar` 🤖 | **今後の経済イベント予定**（FOMC・日銀・米CPI・雇用統計等）。3時間キャッシュ |
| `deep_research` 🤖 | **ニュース記事本文まで読む詳細リサーチ**。日本株は Yahoo!ファイナンスを一次ソースに |

🤖 = 内部で入れ子 AI (sonnet) が動くツール（`AI_TOOLS` に列挙）。低速だが、見出しや数値だけでは得られない情報を構造化して返す。他は全て決定的。

## セットアップ

```bash
uv sync    # Python 3.12+ / パッケージ管理は uv
```

すべてのコマンドは `uv run stock-selector <command>` で実行する（`.venv` の有効化は不要）。

## LLM プロバイダー

既定は `claude_code` — **Claude サブスク (Max) の headless モード**。Claude Code CLI のログインをそのまま使うため API キー・追加課金なし（メインは Agent SDK、補助AIも同 SDK 経由）。

| プロバイダー | 経路 | モデル | 認証 |
|---|---|---|---|
| `claude_code`（既定） | Claude Agent SDK | `sonnet` | Claude Code ログイン |
| `copilot` | litellm | `claude-sonnet-4.5` | `stock-selector auth`（device flow） |
| `anthropic` | litellm | `claude-sonnet-5` | `ANTHROPIC_API_KEY` |
| `openai` | litellm | `gpt-4o` | `OPENAI_API_KEY` |

定義は `src/infra/repositories/litellm_ai.py` の `AI_PROVIDERS`。litellm 系プロバイダーではメインエージェントが LangGraph ReAct になる。

## 自動売買の起動

**本番運用は systemd 管理（推奨）**。自動復旧・マシン起動時の自動開始・死活監視付き:

```bash
bash deploy/systemd/install.sh                     # 初回インストール（要 sudo）
sudo systemctl start stock-selector-trader         # 起動
sudo systemctl restart stock-selector-trader       # コード変更の反映
systemctl status stock-selector-trader             # 状態確認
```

- watchdog（15分毎）が取引時間内のサービス停止・ログ無更新（ハング）を検知し
  `logs/alerts.log` に通知（ダッシュボードに表示、`.env` の `NTFY_TOPIC` 設定でスマホプッシュも可）
- クラッシュは60秒で自動再起動。`stock-selector stop` は正常終了扱いで再起動されない

手動実行（開発・検証用）:

```bash
uv run stock-selector auto-trade --market jp --daemon --interval 1800   # 二重フォークで常駐
uv run stock-selector stop                  # 停止
uv run stock-selector auto-trade --market jp            # 1サイクルだけ実行
uv run stock-selector auto-trade --market jp --dry-run  # ドライラン（注文なし）
```

- ログ: `logs/auto_trade_daemon.log`、二重起動防止: `.auto_trade.lock`（systemd と手動は排他）
- 取引時間外（東証 8:30–16:00 / 米国 22:00–翌6:00 JST 以外）のサイクルは自動 SKIP
- 主なオプション: `--market us|jp|all`（既定 `jp`）、`--min-score`、`--max-signals`、`--ai-provider`、`--ai-model`

## ダッシュボード（Streamlit・読み取り専用）

```bash
uv run stock-selector dashboard                    # http://127.0.0.1:8501
uv run stock-selector dashboard --daemon           # 常駐
uv run stock-selector dashboard --stop             # 停止
uv run stock-selector dashboard --host <IP>        # バインド先変更（Tailscale 等）
```

ポートフォリオ・総資産グラフ（約定マーカー・日経/TOPIX 同額投資オーバーレイ付き）・AI 思考トレース（シーケンス図 / サイクル別タイムライン）・障害アラート（watchdog 検知分）を表示。共有ファイルを読むだけでブローカー・デーモンには触れない。

## リスク管理

値の唯一の真実は `config/risk_limits.json`（以下は現在の既定値）。

| パラメータ | 値 | 説明 |
|---|---:|---|
| 同時保有上限 | 5 | ポジション数の上限。満杯時は swap のみ |
| 1ポジション最大比率 | 30% | 集中投資の防止（超過は警告・分散チェック対象） |
| 1日最大損失率 | 5% | 日次ドローダウン制限 |
| デフォルト損切り | 3% | シグナル生成時に銘柄ごと上書き可 |
| デフォルト利確 | 5% | 到達時は半分ずつ段階利確（スケールアウト） |
| トレーリングストップ | 3% | 高値が取得価格 +5%（activation）に達してから武装 |
| 大幅損失ガード | 5% | 強制クローズ |
| 最大保有日数 | timespan 別 | short 5 / swing 21 / medium 60 / long 180 日（フォールバック 30 日） |
| 最低保有期間 | 2営業日 | AI 判断の swap 売りを禁止（機械ストップ・trim は対象外） |
| 再入場クールダウン | 2営業日 | 売却直後の買い直しを反証ゲートで却下 |

マクロ環境スコアが悪化した場合は新規買いを自動スキップする安全弁付き。

## CLI リファレンス

コマンドの唯一の真実は `src/cli/app.py`。`uv run stock-selector --help` で全一覧。

```bash
# 分析
uv run stock-selector screen --market all --strategy all --top 10
uv run stock-selector score 7203.T
uv run stock-selector technical 7203.T / fundamentals NVDA / prices 7203.T
uv run stock-selector macro / sector-strength --market jp
uv run stock-selector market-calendar --days 7        # 経済イベント予定（入れ子AI）
uv run stock-selector deep-research 5401.T            # ニュース本文リサーチ（入れ子AI）
uv run stock-selector news "トヨタ" / sentiment "トヨタ" / event-impact --query "関税"

# ポートフォリオ・トレード
uv run stock-selector portfolio status|performance
uv run stock-selector trade --from-signal diary/signals/xxx.json
uv run stock-selector alert --check-portfolio
uv run stock-selector backtest --days 30

# 自動化・運用
uv run stock-selector auto-trade ... / auto-analyze --market jp --ai
uv run stock-selector dashboard / stop / auth
uv run stock-selector kabu-check / trading-units      # kabuステーション API
```

## ディレクトリ構成

```
stock-selector/
├── src/
│   ├── core/          # ビジネスロジック（純粋関数・AIなし）
│   │                  #   screener / scorer / technical / fundamentals / macro
│   │                  #   sentiment / news / event_impact / sector_strength ...
│   ├── agents/        # AI エージェント・デーモン
│   │   ├── claude_agent.py    #   メインエージェント (Claude Agent SDK)
│   │   ├── graph_trade.py     #   売買サイクル本体 + LangGraph 経路
│   │   ├── tools.py           #   ツール定義 (ALL_TOOLS / AI_TOOLS)
│   │   ├── deep_research.py   #   入れ子AI: ニュース本文リサーチ
│   │   ├── market_calendar.py #   入れ子AI: 経済イベント予定
│   │   ├── exit_advisor.py    #   補助AI: 早期手仕舞い助言
│   │   ├── reflection.py      #   補助AI: 教訓抽出
│   │   └── portfolio_review.py#   補助AI: 分散チェック
│   ├── cli/           # typer CLI (エントリーポイント: stock-selector)
│   ├── trading/       # 売買実行レイヤ (TradeExecutor / RiskManager / OrderManager)
│   ├── infra/         # DI コンテナ・リポジトリ実装 (broker / AI / market data)
│   ├── interfaces/    # 抽象インターフェース
│   └── web/           # Streamlit ダッシュボード
├── config/            # risk_limits / trading_config / watchlist / シナリオ別プロンプト
├── deploy/systemd/    # デーモンの unit ファイル + install.sh
├── scripts/           # 通知 (notify.sh)・死活監視 (watchdog.sh)
├── diary/             # 分析・シグナル・約定・思考トレース・日次評価の全記録 (gitignore)
├── logs/              # デーモンログ・総資産スナップショット・障害アラート (gitignore)
├── docs/              # TRADING_SPEC / ARCHITECTURE_REVIEW
└── .claude/skills/    # 開発用スキル (sync-docs 等)
```

## 開発

```bash
uv run ruff check src/ && uv run ruff format src/
uv run pyright src/
uv run pytest
```

コード変更時はドキュメント同期チェック（`.claude/skills/sync-docs`）に従うこと。デーモン関連コードを変更したら、構文チェックだけでなく実際に 1 サイクル動かして検証する。

## 注意事項

- **投資助言ではない** — 出力は参考情報であり、投資判断の責任はユーザーにある
- **ブローカー切替** — `config/trading_config.json` の `broker` で `"simulator"` / `"kabu"` を切替。kabuステーション API は `KABU_API_PASSWORD` 環境変数が必要。既定はシミュレーター（仮想売買）
- API キーは `.env` で管理（gitignore 済み）

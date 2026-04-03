# Stock Selector — AI 駆動型 株式売買判断システム

GitHub Copilot Agent がワークスペースを起点に、**市場スキャン → テクニカル / ファンダメンタル分析 → AI 判断 → 仮想売買** を自律的に実行するシステム。

## 主な特徴

- **自動銘柄発掘**: スクリーナーで米国株 / 日本株を横断スキャン（売られすぎ・モメンタム・ブレイクアウト・バリュー）
- **多角的分析**: テクニカル指標 + ファンダメンタル + ニュースセンチメント + マクロ環境を統合
- **AI 判断**: Copilot / GitHub Models / OpenAI / Anthropic による売買最終判断
- **仮想売買**: シミュレーターで注文・損益追跡・リスク管理を自動実行
- **自動運用**: デーモンモードで定期スキャン・自動売買ループを実行

## セットアップ

```bash
# Dev Container を開く（推奨）、または手動で:
uv sync                    # 依存パッケージのインストール
source .venv/bin/activate  # 仮想環境の有効化
```

> Python 3.12+、パッケージ管理は [uv](https://docs.astral.sh/uv/) を使用。

## 使い方

### Copilot Agent 経由（主要な使い方）

VS Code Chat で指示するだけ:

```

NVIDIAを調べて売買判断して」

```

### CLI スクリプト

```bash
# スクリーニング（銘柄発掘）
python3 scripts/screener.py --market all --strategy all --top 10

# 個別銘柄スコアリング
python3 scripts/scorer.py AAPL

# マクロ環境チェック
python3 scripts/macro.py

# 自動売買ループ（AI判断付き・ドライラン）
python3 scripts/auto_trade.py --ai --dry-run

# 自動分析レポート生成
python3 scripts/auto_analyze.py --market jp --span medium --depth standard

# デーモンモード（30分ごとに自動実行）
python3 scripts/auto_trade.py --ai --daemon --interval 1800
```

## ディレクトリ構成

```
stock-selector/
 scripts/                    # 分析・データ取得スクリプト
   ├── lib/                    # 共通ユーティリティ
   │   ├── runner.py           #   スクリプト実行ヘルパー
   │   ├── ai.py               #   AI プロバイダー統合
   │   └── portfolio.py        #   ポートフォリオ操作
   ├── screener.py             # 銘柄スクリーナー
   ├── scorer.py               # 総合スコアリング
   ├── technical.py            # テクニカル指標算出
   ├── fundamentals.py         # ファンダメンタル分析
   ├── macro.py                # マクロ経済指標
   ├── fetch_prices.py         # 株価データ取得
   ├── fetch_news.py           # ニュース取得
   ├── fetch_sentiment.py      # センチメント分析
   ├── alert.py                # アラート・監視
   ├── backtest.py             # バックテスト
   ├── portfolio.py            # ポートフォリオ管理 CLI
   ├── trade.py                # 個別売買 CLI
   ├── auto_trade.py           # 自動売買ループ
   ├── auto_analyze.py         # 自動分析レポート
   └── event_impact_analyzer.py # イベントインパクト分析
 trading/                    # 売買エンジン
   ├── broker_interface.py     # ブローカー抽象インターフェース
   ├── simulator.py            # 仮想ブローカー（シミュレーター）
   ├── order_manager.py        # 注文管理
   ├── risk_manager.py         # リスク管理
   └── trade_executor.py       # 売買実行オーケストレーター
 config/
   ├── watchlist.json          # 監視銘柄リスト
   ├── risk_limits.json        # リスク管理パラメータ
   └── trading_config.json     # 売買設定
 diary/                      # 分析・売買の記録
 docs/
   └── TRADING_SPEC.md         # 売買エンジン設計書
 .github/
   └── copilot-instructions.md # Copilot Agent 指示
 portfolio.json              # 仮想ポートフォリオ状態（gitignore対象）
 pyproject.toml              # プロジェクト定義・依存・ツール設定
```

## 開発

```bash
uv run ruff check scripts/ trading/     # リント
uv run ruff format scripts/ trading/    # フォーマット
uv run pyright scripts/ trading/        # 型チェック
uv run pytest                           # テスト
```

## 注意事項

- **投資助言ではない**: 出力は参考情報。投資判断の責任はユーザーにある
- **仮想売買のみ**: 実際の証券口座への発注は行わない
- API キーは `.env` で管理（`.gitignore` に含まれる）

# 自動取引エンジン仕様書

## 概要

**自動取引エンジン** は、`/analyze` の売買判断を受け取り、実際の取引を自動実行するシステムです。
シミュレーターと実取引APIを抽象化し、設定で切り替え可能な設計です。

**実行フロー:**
1. ユーザーが Copilot Chat で `/trade` コマンドを実行
2. `trade.py` が最新の売買判断（`/analyze` 結果）を読み込み
3. `TradeExecutor` がリスク計算 → 注文生成 → 発注
4. 取引ログを `diary/trades/` に記録
5. Chat で結果報告

---

## アーキテクチャ

```
src/trading/
├── __init__.py
├── broker_interface.py      # 抽象基底クラス
├── simulator.py              # ローカルシミュレーター
├── position_manager.py       # ポジション・ポートフォリオ管理
├── order_manager.py          # 判断 → 注文への変換
├── risk_manager.py           # リスク計算・損切り/利確判定
└── trade_executor.py         # 統合オーケストレーション

src/scripts/
└── trade.py                  # エントリーポイント

config/
├── trading_config.json       # 取引設定（シミュ vs 本番）
└── risk_limits.json          # リスク管理ルール

diary/
└── trades/                   # 取引ログ（別フォルダ）
```

---

## Phase 1: コア実装（本フェーズ）

### 1. `broker_interface.py` - 抽象化層

**責務:**
- 取引 API の統一インターフェース定義
- ブローカーロジック（API、シミュレーター）を差し替え可能に

**主要クラス:**

```python
# データクラス
Order:
  - id: str (UUID)
  - ticker: str
  - side: "BUY" | "SELL"
  - quantity: int
  - entry_price: float
  - order_type: "MARKET" | "LIMIT" | "STOP"  
  - order_time: datetime
  - filled_quantity: int = 0
  - fill_price: float | None = None
  - status: "PENDING" | "FILLED" | "PARTIALLY_FILLED" | "CANCELLED"
  - stop_loss: float | None = None
  - take_profit: float | None = None

Position:
  - ticker: str
  - quantity: int
  - entry_price: float
  - current_price: float
  - entry_time: datetime
  - stop_loss: float | None = None
  - take_profit: float | None = None
  - pnl: float (計算済み)
  - pnl_pct: float (計算済み)

# 抽象基底クラス
BrokerInterface (ABC):
  - get_balance() -> dict  # {"cash_jpy": float, "cash_usd": float}
  - place_order() -> Order
  - cancel_order(order_id: str) -> bool
  - get_orders() -> list[Order]  # 未約定のもの
  - get_positions() -> list[Position]
  - get_filled_orders() -> list[Order]  # 約定済み（履歴）
  - sync_from_broker()  # ブローカー側の状態を取得（本実装用）
```

**実装詳細:**
- `Order` と `Position` は `@dataclass` で実装
- `BrokerInterface` は ABC (Abstract Base Class)
- シミュレーターでは全量をメモリ管理

---

### 2. `simulator.py` - シミュレーター

**責務:**
- ローカルでポートフォリオ・注文を管理
- `BrokerInterface` を実装
- 現実的な約定ロジック（スプレッド、約定遅延等）

**BrokerSimulator クラス:**

```python
class BrokerSimulator(BrokerInterface):
  - __init__(initial_capital: dict, config: dict)
  - _balance: dict = {"cash_jpy": ..., "cash_usd": ...}
  - _positions: list[Position] = []
  - _orders: list[Order] = []  # 未約定
  - _filled_orders: list[Order] = []  # 約定済み

  - place_order():
      1. 資金チェック（不足なら失敗）
      2. Order を生成・PENDING に
      3. _orders に追加
      4. 即座に約定判定（MARKET order）
      return order

  - _simulate_fill():
      1. ticker の現在価格取得
      2. スプレッドを適用（買い：+spread、売り：-spread）
      3. 約定判定 (LIMIT order なら price チェック)
      4. Position に追加・資金を更新
      5. Order ステータスを FILLED に
      6. _filled_orders に移動

  - get_balance(): _balance を返す
  - get_positions(): _positions をコピーして返す
  - get_orders(): 未約定のみ
  - get_filled_orders(): 履歴
```

**シミュレーション仕様:**
- 初期資金: JPY 1000万、USD 5万
- デフォルトスプレッド: 0.02% (config で変更可)
- About 定執行: 即座に約定（リアルをシミュレートする場合は遅延を入れる）

---

### 3. `order_manager.py` - 注文生成

**責務:**
- 売買判断（signal）を Order オブジェクトに変換
- リスク管理により注文数量を決定

**TradingSignal クラス:**

```python
@dataclass
class TradingSignal:
  ticker: str
  action: "BUY" | "SELL" | "CLOSE"  # CLOSE = ポジション全量売却
  confidence: float  # 0.0 ~ 1.0
  target_price: float
  entry_price: float | None = None  # 推奨エントリー（なければ成行）
  stop_loss_price: float | None = None
  take_profit_price: float | None = None
  timespan: "short" | "swing" | "medium"  # スパン（ポジション保持期間参考用）
  reason: str = ""  # 判断理由
  score: int = 0  # 総合スコア (0-100)
  timestamp: datetime = field(default_factory=datetime.now)
```

**OrderManager クラス:**

```python
class OrderManager:
  - __init__(risk_manager: RiskManager, broker: BrokerInterface)
  
  - generate_order(signal: TradingSignal, 
                  existing_position: Position | None = None) -> Order:
      1. signal.action を判定
      2. confidence と score から注文数量計算
      3. signal.entry_price があれば LIMIT order、なければ MARKET
      4. stop_loss、take_profit を Order に設定
      return Order (ただしまだ発注はしていない)
```

**数量計算ロジック:**
```
confidence 0.5以下: 投機的 → 数量少
confidence 0.7以上: 高確信 → 数量多
score 高 → 数量多、stop-loss 狭い
```

---

### 4. `risk_manager.py` - リスク管理

**責務:**
- ポジションサイズ計算（ケリー基準等）
- 損切り・利確判定
- レバレッジ・最大ポジション数制御

**RiskManager クラス:**

```python
class RiskManager:
  - __init__(config: dict)  # risk_limits.json から読み込み
  
  - calculate_position_size(balance: dict, ticker: str,
                           entry_price: float,
                           stop_loss_price: float,
                           confidence: float) -> int:
      # Kelly's formula 簡易版
      # position_size = (資金 * 許容損失pct) / (entry_price - stop_loss_price)
      # 上限チェック: max_position_size_pct により制限
      return quantity
  
  - check_daily_loss(pnl_today: float) -> bool:
      # 今日の損失が max_daily_loss_pct を超えたか？
      return should_stop_trading
  
  - should_close_position(position: Position, 
                         current_price: float) -> bool:
      # stop_loss、take_profit ラインに達したか
      # トレーリングストップ判定
      return should_close
  
  - validate_order(order: Order, broker: BrokerInterface) -> bool:
      # 最大同時ポジション数、禁止銘柄 等をチェック
      return is_valid
```

**設定（config/risk_limits.json）:**
```json
{
  "max_position_size_pct": 5,        # 1ポジションの最大サイズ
  "max_daily_loss_pct": 2,           # 1日の最大損失
  "max_concurrent_positions": 5,     # 同時保有数上限
  "default_stop_loss_pct": 3,        # デフォルト損切り幅
  "default_take_profit_pct": 5,      # デフォルト利確幅
  "trailing_stop_pct": 2,            # トレーリングストップ
  "forbidden_tickers": []
}
```

---

### 5. `trade_executor.py` - 統合オーケストレーション

**責務:**
- 売買判断 → 注文 → 発注 → 約定確認を一元管理
- エラーハンドリング・ロールバック

**TradeExecutor クラス:**

```python
class TradeExecutor:
  - __init__(broker: BrokerInterface, 
             order_mgr: OrderManager,
             risk_mgr: RiskManager)
  
  - execute_signal(signal: TradingSignal) -> dict:
      """主要メソッド"""
      1. リスクチェック（daily_loss、禁止銘柄等）
      2. 既存ポジション確認
      3. 注文生成
      4. broker.place_order()
      5. 約定確認
      return {
        "success": bool,
        "order_id": str,
        "ticker": str,
        "quantity": int,
        "entry_price": float,
        "status": str,
        "pnl": float | None,
        "error": str | None
      }
  
  - check_and_close_positions() -> list[dict]:
      """ポジションの損切り・利確チェック（定期実行）"""
      for position in broker.get_positions():
        if risk_mgr.should_close_position(position, current_price):
          signal = TradingSignal(action="CLOSE", ...)
          result = execute_signal(signal)
      return results
```

---

### 6. Phase 1 実装の config ファイル

**config/trading_config.json:**
```json
{
  "mode": "simulator",
  "simulator": {
    "initial_capital_jpy": 10000000,
    "initial_capital_usd": 50000,
    "spread_pct": 0.02
  },
  "live": {
    "broker": "dmmfx",
    "api_endpoint": "https://api.dmmfx.com/v1",
    "api_key_env": "DMMFX_API_KEY",
    "sandbox_mode": false
  }
}
```

**config/risk_limits.json:**
```json
{
  "max_position_size_pct": 5,
  "max_daily_loss_pct": 2,
  "max_concurrent_positions": 5,
  "default_stop_loss_pct": 3,
  "default_take_profit_pct": 5,
  "trailing_stop_pct": 2,
  "forbidden_tickers": []
}
```

---

### 7. `trade.py` - メインスクリプト（Phase 1では基本骨組みのみ）

**Usage:**
```bash
python src/scripts/trade.py --mode simulate --signal-file diary/2026-04-01_latest_signal.json
python src/scripts/trade.py --check-positions  # ポジション確認のみ
python src/scripts/trade.py --close NVDA 100   # 強制クローズ
```

**基本流れ:**
1. CLI 引数パース
2. config / risk_limits 読み込み
3. Broker（simulator）初期化
4. signal 読み込み
5. TradeExecutor 実行
6. 結果をJSON + diary に記録
7. JSON 出力（Copilot chat で表示）

---

## 実装ステップ案

| フェーズ | 内容 |
|---------|------|
| Phase 1 | ✅ **完了** `broker_interface.py` + `simulator.py` + `order_manager.py` + `risk_manager.py` + `trade_executor.py` + `trade.py` ベース実装 |
| Phase 2 | `position_manager.py` 完成、シミュレーション精度向上 |
| Phase 3 | `trade.py` 完全実装、`/trade` プロンプト統合 |
| Phase 4 | 実取引 API (`brokers/dmmfx.py`) 実装 |
| Phase 5 | バックテスト・パフォーマンス分析 |

### Phase 1 実装内容

✅ **完了したファイル:**
- [docs/TRADING_SPEC.md](docs/TRADING_SPEC.md) — 本仕様書
- [src/trading/broker_interface.py](src/trading/broker_interface.py) — Order, Position, BrokerInterface
- [src/trading/simulator.py](src/trading/simulator.py) — BrokerSimulator（ローカルメモリ管理）
- [src/trading/order_manager.py](src/trading/order_manager.py) — TradingSignal, OrderManager
- [src/trading/risk_manager.py](src/trading/risk_manager.py) — RiskManager
- [src/trading/trade_executor.py](src/trading/trade_executor.py) — TradeExecutor（統合オーケストレーション）
- [src/scripts/trade.py](src/scripts/trade.py) — メインスクリプト（CLI）
- [config/trading_config.json](config/trading_config.json) — 取引設定
- [config/risk_limits.json](config/risk_limits.json) — リスク限度設定

✅ **テスト済み:**
```bash
# ポートフォリオ確認
python3 src/scripts/trade.py --check-positions

# 取引実行（テスト）
python3 src/scripts/trade.py --ticker AAPL --action buy

# 損切り/利確チェック
python3 src/scripts/trade.py --check-and-close
```

### Phase 1 の特徴

1. **抽象化層**: BrokerInterface によりシミュレータと実 API を差し替え可能
2. **シミュレーター**: ローカルメモリで完全なポートフォリオ管理
3. **リスク管理**: ポジションサイズ計算、損切り/利確自動判定
4. **JSON I/O**: すべての結果が JSON 出力（Copilot Chat 連携用）
5. **ログ記録**: diary/trades に自動保存

---

---

## 全体データフロー

```
/analyze 実行
  ↓
diary/signals/2026-04-01_latest_signal.json 生成
  ↓
/trade 実行
  ↓
trade.py が signal 読み込み
  ↓
TradeExecutor が処理:
  1. リスクチェック
  2. 注文生成
  3. broker.place_order()
  ↓
diary/trades/2026-04-01_trade_result.json 記録
  ↓
Copilot chat で結果報告
```

---

## 実装チェックリスト

### broker_interface.py
- [ ] `Order` dataclass
- [ ] `Position` dataclass
- [ ] `BrokerInterface` ABC

### simulator.py
- [ ] `BrokerSimulator` 実装
- [ ] `_simulate_fill()` ロジック
- [ ] JSON シリアライズ対応

### order_manager.py
- [ ] `TradingSignal` dataclass
- [ ] `OrderManager` クラス
- [ ] 注文数量計算式

### risk_manager.py
- [ ] `RiskManager` クラス
- [ ] Kelly's formula 実装
- [ ] 損切り/利確判定

### trade_executor.py
- [ ] `TradeExecutor` クラス
- [ ] `execute_signal()` メソッド
- [ ] `check_and_close_positions()` メソッド

### config ファイル
- [ ] `trading_config.json` 作成
- [ ] `risk_limits.json` 作成

### trade.py
- [ ] CLI インターフェース
- [ ] signal 読み込み
- [ ] 結果出力（JSON）

---

## 補足

- **テスト:** 各モジュールで unittest を用意予定
- **ロギング:** 全取引を diary に記録、監査可能に
- **エラーハンドリング:** API エラー時も graceful degrade（dummy 約定等）

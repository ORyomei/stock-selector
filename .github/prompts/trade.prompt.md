---
name: Trade
description: 自動取引エグゼキューション＆ポジション管理（手動トリガー）
context: /workspaces/stock-selector
enableQuickReply: false
---

# /trade — 自動取引エグゼキューション

**トリガー**: 手動（30分ごとに実行可能）

## 手順

1. `vscode_askQuestions` で実行する機能を選択（マルチセレクト可）
2. **自動クローズ判定を必ず実行**（ユーザー選択不要）
3. 各選択に応じた処理を順次実行
4. 結果を diary/trades/ に記録

**重要**: `/trade` 実行時は常に `python3 scripts/trade.py --check-and-close` を最初に実行する。
これにより以下の条件を自動判定し、該当すれば即座にクローズする:

| 判定条件 | トリガー | 理由 |
|---------|---------|------|
| 損切り到達 | 現在値 ≤ stop_loss | 損失拡大防止 |
| 利確到達 | 現在値 ≥ take_profit | 利益確定 |
| トレーリングストップ | 建値から-2%下落 | 含み益消失防止 |
| 大幅損失 | -5%以上下落 | 緊急損切り |
| 保有タイムアウト | 30日以上保有 | 資金拘束防止 |

---

## 質問テンプレート

### Q1: 実行する機能を選択（複数選択可）

```json
{
  "questions": [
    {
      "header": "トレード機能",
      "question": "実行する機能を選んでください（複数選択可）",
      "options": [
        { "label": "📊 分析（最新のシグナル確認）", "description": "/analyzeで生成した売買判断を表示", "recommended": true },
        { "label": "💼 ポジション確認・損益照会", "description": "現在の保有ポジション・含み益損を確認" },
        { "label": "🎯 シグナルから自動売買実行", "description": "生成されたシグナルを実行（注文発注）" },
        { "label": "❌ ポジションをクローズ", "description": "指定銘柄のポジションを決済" },
        { "label": "⚙️ リスク設定確認", "description": "ポジションサイズ・損切り・利確ルール確認" }
      ],
      "multiSelect": true,
      "allowFreeformInput": false
    }
  ]
}
```

---

## 各機能の実装フロー

### 📊 分析（シグナル確認）

1. `diary/` から最新の分析レポート（YYYY-MM-DD_HHMMSS_*.md）を検索
2. 内容を抽出し、推奨銘柄＆判断を表示
3. ユーザーが興味ある銘柄を選択
4. その銘柄の詳細（目標価格・確率・根拠）を表示

**実行コマンド例**:
```bash
ls -t diary/*.md | head -1  # 最新分析ファイル取得
cat <file>  # 内容表示
```

---

### 💼 ポジション確認・損益照会

1. `python3 scripts/trade.py --check-positions` を実行
2. 現在の保有ポジション一覧を表示
   - ティッカー、エントリー価格、現在値、含み益損（%）、数量
3. 総資金 / 現金 / 使用中資金を表示
4. 収益性ランキング（最高益＆最大損失）を表示

**JSON 出力例**:
```json
{
  "total_balance": { "JPY": 10000000, "USD": 50000 },
  "used_capital": { "JPY": 2500000, "USD": 25000 },
  "positions": [
    { "ticker": "7203.T", "entry": 3200, "current": 3300, "pnl_pct": 3.1, "qty": 100 },
    { "ticker": "AAPL", "entry": 150, "current": 152, "pnl_pct": 1.3, "qty": 100 }
  ],
  "total_pnl": { "realized": 15000, "unrealized": 55000 }
}
```

**実行コマンド**:
```bash
python3 scripts/trade.py --check-positions
```

---

### 🎯 シグナルから自動売買実行

1. `ls -t diary/signals/*.json 2>/dev/null | head -10` で利用可能なシグナルファイルを取得
2. `vscode_askQuestions` でシグナル元を選択：
   - **自動生成**: 直近の `diary/*.md` 分析レポートから推奨銘柄を抽出し、`diary/signals/` に JSON シグナルを生成
   - **ファイル選択**: `diary/signals/*.json` の既存ファイルをリストしてユーザーが選択

   ```json
   {
     "questions": [
       {
         "header": "シグナル元",
         "question": "シグナルをどう取得しますか？",
         "options": [
           { "label": "🤖 最新分析から自動生成", "description": "直近の /analyze 結果から推奨銘柄をシグナル化", "recommended": true },
           { "label": "📂 ファイルを指定", "description": "diary/signals/ 内の既存シグナル JSON を選択" }
         ],
         "allowFreeformInput": true
       }
     ]
   }
   ```

3. シグナル内容を表示（ティッカー・判断・目標価格・リスク）
   - action は大文字/小文字どちらでも受付可能（`buy`, `BUY` 等）
   - 数量が資金を超える場合は自動クリップされる

4. `python3 scripts/trade.py --from-signal <signal_file>` を実行

5. 実行結果を表示＆ `diary/trades/` に記録

**シグナル JSON 形式**:
```json
{
  "ticker": "7203.T",
  "action": "buy",
  "entry_price": 3200,
  "target_price": 3400,
  "stop_loss_price": 3100,
  "take_profit_price": 3400,
  "confidence": 0.85,
  "timespan": "swing",
  "score": 60,
  "reason": "テクニカル反発 + ファンダ良好"
}
```

**実行コマンド**:
```bash
python3 scripts/trade.py --from-signal <path_to_signal.json>
```

---

### ❌ ポジションをクローズ

1. `python3 scripts/trade.py --check-positions` で現在の保有ポジション一覧を表示
2. `vscode_askQuestions` でクローズ対象を確認：

   ```json
   {
     "questions": [
       {
         "header": "クローズ対象",
         "question": "クローズするティッカーと数量を入力（例: 7203.T 100）",
         "allowFreeformInput": true
       }
     ]
   }
   ```

3. `python3 scripts/trade.py --close <ticker> <qty>` で決済
4. 実行結果（決済益損）を表示

**実行コマンド**:
```bash
python3 scripts/trade.py --close 7203.T 100
```

---

### ⚙️ リスク設定確認

1. `config/risk_limits.json` を読み込み表示
   - ポジション最大サイズ（% of capital）
   - 1日の最大損失（% of capital）
   - 同時保有ポジション上限
   - デフォルト損切り %・利確 %

2. ユーザーが「閲覧のみ」or「設定変更」を選択
   - **閲覧**: JSON を pretty-print で表示
   - **変更**: 対話形式で各項目を更新 → 保存

**設定ファイル例**:
```json
{
  "max_position_size_pct": 5,
  "max_daily_loss_pct": 2,
  "max_concurrent_positions": 5,
  "default_stop_loss_pct": 2,
  "default_take_profit_pct": 5
}
```

**実行コマンド**:
```bash
cat config/risk_limits.json  # 閲覧
# UI から編集可能にする
```

---

## 実装の流れ（Copilot 内処理）

```
1. vscode_askQuestions で機能選択（マルチセレクト）
   ↓
2. 選択結果に応じて機能を順次実行
   ↓
3. 各機能の結果を Markdown で組立
   ↓
4. diary/trades/YYYY-MM-DD_HHMMSS_execution.md に記録
   ↓
5. 最終サマリーを Chat に表示
```

---

## 記録ファイル規則

### トレード実行ログ

**ファイル名**: `diary/trades/YYYY-MM-DD_HHMMSS_execution.md`

**内容テンプレート**:
```markdown
# 🎯 トレード実行ログ
- **日時**: YYYY-MM-DD HH:MM:SS
- **実行機能**: 分析 / ポジション確認 / シグナル実行 / クローズ / リスク確認

## 📊 実行内容

### 1. 分析確認
- 最新分析: diary/YYYY-MM-DD_HHMMSS_*.md
- 推奨銘柄: トヨタ(7203.T), デンソー(6902.T)
- 判断: スイング買い

### 2. ポジション状況
| ティッカー | エントリー | 現在値 | 含み益損 | 数量 |
|-----------|-----------|-------|---------|------|
| 7203.T | ¥3,200 | ¥3,300 | +3.1% | 100 |

### 3. 実行結果
✅ 7203.T 100株 購入
❌ 9984.T 50株 クローズ（決済益 ¥150,000）

### 4. リスク確認
- 使用資金: ¥2.5M / ¥10M（25%）
- 本日損失: -¥80,000（-0.8%）
- ポジション数: 5 / 5（上限）

---

## 次回推奨アクション
- トヨタ: サポート ¥3,143 まで押し目で追加買い検討
- デンソー: 目標 ¥2,300 に接近中、利確検討開始
```

---

## 使用例

### 例1: 30分ごとに定期チェック

```
時刻 14:00 → `/trade` 実行
Q: 機能選択 → ☑️ 分析 ☑️ ポジション確認 ☑️ リスク確認

↓ 結果
- 最新分析: トヨタ推奨（スイング買い）
- ポジション: 5保有、含み益 ¥230,000
- 本日損失: -¥50,000（OK）

時刻 14:30 → `/trade` 実行
Q: 機能選択 → ☑️ ポジション確認 ☑️ シグナル実行

↓ 結果
- ポジション確認: 特に変化なし
- シグナル実行: 新規買い 1件（ホンダ避け、トヨタ追加）
```

### 例2: 手動トレード実行

```
ユーザー: `/trade` → 「シグナルから自動売買実行」選択

↓ 処理
1. 最新の分析から有望銘柄を抽出
2. TradingSignal を自動生成（確率・目標価格・リスク含む）
3. ユーザー確認「実行」→ トレード.executor で発注
4. 約定結果を diary/trades に記録
```

---

## 注意事項

- **シミュレーションモード前提**: 本取引は `config/trading_config.json` で `"mode": "live"` に設定後に有効化
- **リスク管理厳格実施**: 損切り自動実行、1日損失上限チェック必須
- **データ定期バックアップ**: portfolio.json / diary/trades/ は定期バックアップ推奨

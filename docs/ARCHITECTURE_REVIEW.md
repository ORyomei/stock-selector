# アーキテクチャレビュー: シミュレータ/実取引の切り替え

**日付**: 2026-06-11
**対象**: コミット `67d84df` 時点(legacy パイプライン削除後)
**観点**: シミュレータと実取引(kabu)の切り替え機構と、その切り分けの綺麗さ

---

## 全体構成(現状)

```
cli/app.py (typer)
   ↓
agents/   … LangGraph Agent・デーモン・安全弁     ← LLM はここに閉じ込め
   ↓ run_trade_cmd(["--from-signal", path])      ← ⚠️ 文字列境界
core/trade.py … load_or_create_broker / cmd_*    ← 切り替えの中心
   ↓
trading/  … TradeExecutor → OrderManager → RiskManager → BrokerInterface
                                              ↙           ↘
                                  BrokerSimulator      KabuStationBroker
infra/    … DI コンテナ (market_data, portfolio, diary, ai...) ※broker は対象外
```

## 切り替え機構の実体

切り替えは3点セットで実現されている:

1. **設定キー**: `config/trading_config.json` の `"broker": "simulator" | "kabu"`
2. **ファクトリ**: `load_or_create_broker()` (`src/core/trade.py:54`) の1箇所だけが分岐を持つ
3. **抽象**: 両実装が `BrokerInterface` を実装
   (get_balance / place_order / get_positions / sync_from_broker / managed_currencies / get_trading_unit)

`TradeExecutor`・`OrderManager`・`RiskManager` は **interface のみに依存**し、
kabu/simulator を知らない。発注経路に `if simulator:` 分岐は(1箇所を除き)存在しない。

## ✅ 綺麗にできている点

1. **教科書通りの Strategy + Factory** — 分岐がファクトリ1箇所に集約され、エンジン層は
   ポリモーフィックに動く。新ブローカー追加は「クラス実装 + ファクトリに2行」で済む構造
2. **差異を interface のメソッドとして表現** — `managed_currencies()` (kabu=JPYのみ) や
   `get_trading_unit()` のように、「ブローカーごとの性質の違い」が if 分岐でなく**多態の seam**
   になっている
3. **LLM とブローカーが二層離れている** — Agent はツールしか持たず、発注は
   agents → core → trading と非LLM層を2回経由。実取引切り替え時も LLM 側のコード変更ゼロ
4. **kabu の同期が自動** — サイクル先頭の `_reconcile_if_needed` が simulator なら no-op、
   kabu なら照合同期。切替後の運用手順が増えない

## ⚠️ 漏れている点(重要度順)

### 1. 「状態の真実」の所有権が暗黙(最大の構造的問題)

- **simulator**: `portfolio.json` が真実。`save_broker_state()` が毎コマンド書き込む
- **kabu**: 証券会社が真実。`portfolio.json` は reconcile による**キャッシュ**

この決定的な違いが**型に現れず**、2つの暗黙ルールに散らばっている:
`save_broker_state` の `isinstance(broker, BrokerSimulator)` (`trade.py:85`) と reconcile の有無。
`isinstance` 分岐は抽象化の「破れ」そのもので、interface に `persist_state(repo)`
(kabu では no-op) を持たせれば多態で消せる。

さらに深刻なのは、**`portfolio.json` を broker を介さず直接読む層が多数ある**こと:
`portfolio_helpers` (日次損失・集中度)、`graph_trade` の資金事前チェック、
`portfolio_ops` (CLI status)、`alert.py`。simulator なら常に新鮮だが、kabu では
「サイクル先頭の reconcile 直後だけ新鮮」。**ファイルがキャッシュであるという前提を
誰も明示していない**ので、kabu 移行後に「reconcile 前に読んで古い残高で判断する」
バグを作り込みやすい構造。

### 2. agents → core の境界が文字列ベース(技術的負債の核心)

`agents/runner.py` は subprocess 時代の名残で、**in-process 直接呼び出しなのに**
CLI 形式の引数 (`["--from-signal", path]`) で渡し、`redirect_stdout` で出力を捕まえ、
呼び出し側が `"FILLED" in out` で成否を **grep 判定**している。

`TradeExecutor.execute_signal` はせっかく構造化 dict (`success`/`status`/`fill_price`) を
返すのに、それを文字列に潰してから復元している。「kabu で PARTIALLY_FILLED が FILLED に
部分一致して誤判定」リスクの根本原因。シグナルの受け渡しも一度 JSON ファイルに書いて
パスを渡す間接方式。

**実取引切り替え前に直すべき箇所を1つ選ぶならここ**
(`run_trade_cmd` を廃止して `TradeExecutor` を直接呼び、dict で判定する)。

### 3. broker のライフサイクルがコマンド単位

`run_trade_cmd` のたびに `load_or_create_broker` が走るため、1サイクルで **3〜4回
ブローカーを生成**する。simulator は毎回 `from_dict` でファイルから復元
(=ファイルが実質 DB のトランザクションスクリプト)、**kabu では毎回トークン取得 + sync の
API コール**が走る。動きはするが、レート制限・レイテンシの無駄で、サイクル単位で
1インスタンスを使い回すべき。

他リポジトリは DI コンテナ管理なのに **broker だけコンテナ外**という非対称もここに起因
(`core/kabu_check.py:27` はファクトリすら通らない第2の生成経路)。

### 4. リスクロジックの二重実装

評価額上限 (30%) が `RiskManager.calculate_position_size` (エンジン層) と
`graph_trade._execute_signals` の事前チェック (agents 層) の **2箇所に別実装**である。
意図的な多層防御ではあるが、上限値の解釈変更時に2箇所直す必要があり、片方だけ変わると
挙動が黙って食い違う。事前チェックがエンジン層の同じ関数を呼ぶ形に寄せるのが筋。

### 5. 設定の残骸

`trading_config.json` の `"mode"` キーと `"live"` (DMMFX) ブロックは**どこからも参照されない
死に設定**。`simulator.to_dict()` の `"broker": "dmmfx"` メタデータも誤記の残骸。
切り替えキーが `mode` と `broker` の2つ見えるのは事故の元なので削除推奨。

## 総合評価

**切り替え機構そのものは B+** — interface + factory + 設定キーという正攻法で、エンジン層の
抽象は規律が保たれている (`isinstance` の破れ1箇所のみ)。

**ただし「切り替えても壊れないか」という観点では C** — 問題は分岐の置き方ではなく、

1. **状態所有権が暗黙**で、portfolio.json 直読み層が kabu の鮮度前提を知らないこと
2. **文字列境界**が kabu の多様な注文状態を表現できないこと

どちらも simulator では顕在化せず、**kabu に切り替えた日に初めて踏む**タイプの設計リスク。

## 実取引前の推奨修正順

| # | 内容 | 規模 |
|---|---|---|
| 1 | `run_trade_cmd` の typed API 化 (grep 判定廃止、TradeExecutor を直接呼ぶ) | 1〜2時間 |
| 2 | broker をサイクル単位の単一インスタンスに (DI コンテナ登録) | 〃 |
| 3 | `save_broker_state` の isinstance を多態化 + 状態所有権を SPEC に明文化 | 30分 |
| 4 | 死に設定 (`mode` / `live` / `dmmfx` メタデータ) の削除 | 〃 |

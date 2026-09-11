# 引き継ぎ資料 — 開発マシン移行 (tp → dev, 2026-09-12)

旧マシン `tp` から新マシン `dev` への移行時点のスナップショットと運用手順。
コード・設計の詳細は README を正とし、ここには**リポジトリに載らない運用知識**を書く。

## 1. システム概要（1分版）

- Claude サブスク (Max) の headless で動く日本株の自動売買シミュレーター。
  デーモンが30分毎に「機械クローズ → AI手仕舞い → メインエージェント分析 →
  ゲート検証 → 執行」を回す。実弾 (kabu) は未接続
- 稼働開始 2026-08-04、元本 ¥3,000,000
- 本番運用は systemd (`stock-selector-trader.service` + watchdog timer)

## 2. 移行時点の状態 (2026-09-12)

| 項目 | 値 |
|---|---|
| 総資産 | ¥3,132,659（累計 +4.42%） |
| 対指数 | 日経 +4.34pt / TOPIX +2.65pt |
| 確定成績 | 32勝26敗（勝率55.2%）+¥145,048 |
| 保有 | 9433.T / 8058.T / 9984.T / 4661.T（現金 43.6%） |
| 自己DD | ピーク(9/1 +9.17%)から −4.35% |
| 実弾移行タイミング | 🟡 準備（TOPIX 60日DD −4.13%、判定は日次評価参照） |

直近の文脈: 8月は少売買・高勝率で指数超え。9月に「浅い入口→翌日撤退」型の
負けが続き（5件 −78k）、9/11 に #13/#14 の対策を導入したばかり。
**対策の効果測定はまだ1営業日も走っていない** — 週明けからの日次評価が初データ。

## 3. 移行されるもの / されないもの

### リポジトリ (git clone で入る)
コード全部、config/、deploy/systemd/、.claude/skills/（sync-docs, daily-eval）

### 手動移送が必要 (gitignore 対象の運用状態)
- `portfolio.json` — ポートフォリオの現在状態（**これが飛ぶとシミュ全記録が無意味になる**）
- `diary/` — trades（全約定）/ traces（AI思考）/ evals（日次評価とその baseline）/ signals
- `logs/` — equity_history.jsonl（総資産スナップショット）/ alerts.log / デーモンログ
- `.env` — あれば（NTFY_TOPIC 等）
- `data/` — market_calendar キャッシュ等（消えても再生成される）

### 移行されないもの（新マシンで再設定）
- **Claude CLI の認証**: `claude` を対話ログインする（サブスク認証。これが無いと
  メインAI・補助AI・入れ子AIすべて動かない）
- **systemd 登録**: `bash deploy/systemd/install.sh`（要 sudo）。ユーザー/ホームが
  同一 (`/home/ryomei`) なので unit の書き換えは不要
- **Tailscale funnel**（ダッシュボード公開する場合）: `tailscale funnel --bg 8501`。
  tailnet の HTTPS 有効化は済んでいるが、**ノード単位の funnel 承認**が必要
  （初回実行時に表示される URL を踏む）。公開URLはノード名で変わる
- **日次評価の定例ジョブ**: Claude Code セッション内 cron のため消える。
  新セッションで `/daily-eval arm`（スキルは repo に入っている）

## 4. 日常運用

```bash
systemctl status stock-selector-trader        # デーモン状態
sudo systemctl restart stock-selector-trader  # コード反映
uv run stock-selector stop                    # 手動停止 (systemd は再起動しない)
uv run stock-selector dashboard --daemon      # ダッシュボード
uv run stock-selector portfolio status        # 残高確認
tail -f logs/auto_trade_daemon.log            # ログ監視
```

- 日次評価: 平日 16:11 JST に `/daily-eval`（Claude Code セッションが開いている前提）。
  手動なら `/daily-eval`、ジョブ再アームは `/daily-eval arm`（7日で失効する）
- 障害通知: watchdog → `logs/alerts.log` → ダッシュボード表示。
  スマホプッシュは `.env` に `NTFY_TOPIC=<ランダム文字列>` で有効化

## 5. ハマりどころ（全部実際に踏んだ）

1. **diary/trades のファイル名は UTC 日付** — JST 朝9時前の約定は前日名になる。
   日付で grep せず中身の timestamp を JST 変換して判定（#12 誤報の原因）
2. **確定成績は手集計しない** — `dashboard_data.performance()['stats']` が唯一の真実
3. **`stock-selector macro` は JSON を2個連結して出力することがある** —
   `json.JSONDecoder().raw_decode` で先頭のみパース
4. **yfinance は窓の端で異常値を返す** — 中央値から3倍乖離の行は除外
   （TOPIX +1051% 事故）。指数比較の period は '3mo' 以上（15d 窓事故）
5. **systemd --fg 経路は PYTHONUNBUFFERED=1 必須**（unit に設定済み。
   無いとログが最大16時間滞留し watchdog が誤報する）
6. **ANTHROPIC_API_KEY を環境に置かない** — あると Claude Code がサブスクではなく
   従量課金にフォールバックする（コードは実行時に自動で外すが、原則置かない）
7. デーモンとダッシュボードは flock で排他/独立。手動起動と systemd も排他

## 6. Open issues（実弾移行の前提）

`label:real-trading-gap` 参照。移行判断の前提ゲートは:
- 🔴 **#2 kabu API E2E** — kabuステーション + `KABU_API_PASSWORD` が必要（ユーザー作業）
- 🔴 **#4 税・手数料モデル** — 未着手。回転頻度の正当性評価に必須
- 🟡 #5（ブローカー逆指値。スリッページ実測2点あり: −5%設定→−8.3% / −6.3%）
  / #7（縮退通知）/ #8（バックアップ）/ #14（入口品質、計測中）
- 🟢 #9（統計検証）/ #10（シミュ約定モデル）

実弾移行タイミングの判定ルールは `.claude/skills/daily-eval/SKILL.md` の
「実弾移行タイミング評価」節（両指数の浅い方の60日DDで 待機/準備/好機圏/ナイフ）。

## 7. 移行監査で見つかった漏れと対処 (2026-09-12 追記)

- **CLAUDE.md を新設** — dev の新しい Claude Code セッションが文脈ゼロで始まらないよう、
  セッション開始時の必読順 (HANDOVER → 最新 eval → open issues) と鉄則を記載
- **dev の git identity** — user.name / user.email を tp と同一に設定済み
- **gh CLI** — dev に ~/.local/bin へバイナリ導入済み。**認証は未** —
  dev で `gh auth login` を実行するまで issue 運用 (作成/クローズ/コメント) が使えない
- .env は tp に存在しなかった (NTFY_TOPIC 未設定 = スマホプッシュは元々未使用)
- Claude Code のプロジェクトメモリ (tp 側) は空 — 移行対象なし
- dev のタイムゾーンは Asia/Tokyo 確認済み — cron「16:11」がそのまま正しい
- git stash / ローカルブランチ — なし (main のみ、クリーン)

## 8. 旧マシン (tp) の後始末

- systemd unit は disable 済み（ファイルは /etc/systemd/system に残置。
  完全削除するなら `sudo rm /etc/systemd/system/stock-selector-*` + daemon-reload）
- ダッシュボードの funnel (443→8501) は解除済み。**SSH 用の tcp:10000 funnel は
  ユーザー設定のため触っていない**
- リポジトリと運用状態は残置（バックアップとして。二重稼働はしないこと —
  portfolio.json が分岐する）

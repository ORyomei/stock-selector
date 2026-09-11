# CLAUDE.md — セッション開始時に読むこと

AI 自動株式売買システム（シミュレーション運用中・実弾は未接続）。
**新しいセッションで作業を始める前に、必ず以下を読むこと:**

1. `docs/HANDOVER.md` — 運用状態・ハマりどころ・再設定手順（2026-09-12 のマシン移行時に作成）
2. `diary/evals/` の最新ファイル — 日次評価の基準値（成績・ウォッチ項目・実弾移行タイミング信号）
3. `gh issue list --label real-trading-gap` — 実弾移行の前提タスク

## 絶対に守ること

- **確定成績は手集計しない** — `src/web/dashboard_data.py` の `performance()['stats']` が唯一の真実
- **diary/trades のファイル名は UTC 日付** — 当日分の抽出は中身の timestamp を JST 変換して判定
- **ANTHROPIC_API_KEY を環境に置かない** — サブスク認証が従量課金にフォールバックする
- コード変更したら: `uv run ruff check src/ tests/` → `uv run pytest` →
  `sudo systemctl restart stock-selector-trader`（実際に動かして確認するまで完了としない）
- ドキュメント同期: 実装変更時は `.claude/skills/sync-docs` の手順に従う
- コミットは論理単位で分割し、理由・実測値・issue 番号をメッセージに残す（`git log` の流儀を踏襲）

## 定例運用

- **日次評価**: 平日 16:11 JST に `/daily-eval`（スキル: `.claude/skills/daily-eval/SKILL.md`）。
  セッション cron は7日で失効するため `/daily-eval arm` で再アームし続ける
- デーモン状態: `systemctl status stock-selector-trader` / ログ: `logs/auto_trade_daemon.log`
- 障害: `logs/alerts.log`（watchdog が書く。ダッシュボードにも表示）

## 数字の現在地は書かない

成績・保有・信号は毎日変わるため、このファイルには書かない。
`diary/evals/` の最新 baseline ブロックが常に正。

## 唯一の真実の所在

| 何 | どこ |
|---|---|
| エージェントのツール一覧 | `src/agents/tools.py` の `ALL_TOOLS` / `AI_TOOLS` |
| CLI コマンド | `src/cli/app.py` |
| リスク管理の値 | `config/risk_limits.json` |
| 確定成績 | `dashboard_data.performance()['stats']` |
| 日次評価の基準値 | `diary/evals/` 最新ファイルの baseline ブロック |

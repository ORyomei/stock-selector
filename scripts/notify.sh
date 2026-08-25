#!/usr/bin/env bash
# 障害・縮退の通知 (issue #3)。
#   使い方: notify.sh "メッセージ"
# 1) logs/alerts.log に追記 (ダッシュボードが表示)
# 2) .env に NTFY_TOPIC があれば ntfy.sh にプッシュ (任意・ベストエフォート)
set -u
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MSG="${1:-stock-selector: 障害検知 (詳細不明)}"
TS="$(date '+%Y-%m-%d %H:%M:%S')"

mkdir -p "$PROJECT_DIR/logs"
echo "$TS $MSG" >> "$PROJECT_DIR/logs/alerts.log"

# 任意: ntfy プッシュ (スマホ通知)。.env に NTFY_TOPIC=<トピック名> を書くと有効化
if [ -f "$PROJECT_DIR/.env" ]; then
  NTFY_TOPIC="$(grep -E '^NTFY_TOPIC=' "$PROJECT_DIR/.env" | tail -1 | cut -d= -f2- | tr -d '"' || true)"
  if [ -n "${NTFY_TOPIC:-}" ]; then
    curl -fsS --max-time 10 \
      -H "Title: stock-selector" -H "Priority: high" -H "Tags: rotating_light" \
      -d "$TS $MSG" "https://ntfy.sh/$NTFY_TOPIC" >/dev/null 2>&1 || true
  fi
fi
exit 0

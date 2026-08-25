#!/usr/bin/env bash
# デーモンの死活監視 (issue #3)。systemd timer から15分毎に実行。
# 検知対象:
#   1. trader サービスが inactive/failed (取引時間内)
#   2. サービスは生きているがログが 40 分以上更新されていない (ハング。サイクルは30分毎)
# 検知したら notify.sh 経由で通知。同一障害の連投を防ぐため 60 分のクールダウン付き。
set -u
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$PROJECT_DIR/logs/auto_trade_daemon.log"
COOLDOWN_MARK="$PROJECT_DIR/logs/.watchdog_last_alert"
SERVICE="stock-selector-trader.service"

# 取引時間 (JST 8:15-16:15, 平日) 以外は監視しない — 夜間は SKIP サイクルだけでも
# ログは更新されるが、メンテでの手動停止を夜間に騒がれると鬱陶しいため
dow="$(TZ=Asia/Tokyo date +%u)"   # 1=Mon .. 7=Sun
hm="$(TZ=Asia/Tokyo date +%H%M)"
if [ "$dow" -gt 5 ] || [ "$hm" -lt 0815 ] || [ "$hm" -gt 1615 ]; then
  exit 0
fi

# クールダウン: 直近60分以内に通知済みなら黙る
if [ -f "$COOLDOWN_MARK" ]; then
  last="$(stat -c %Y "$COOLDOWN_MARK")"
  now="$(date +%s)"
  if [ $((now - last)) -lt 3600 ]; then
    exit 0
  fi
fi

alert() {
  touch "$COOLDOWN_MARK"
  "$PROJECT_DIR/scripts/notify.sh" "$1"
}

# 1. サービス生死
if ! systemctl is-active --quiet "$SERVICE"; then
  state="$(systemctl is-active "$SERVICE" 2>/dev/null || true)"
  alert "⚠️ 取引デーモンが停止しています (state=$state, 取引時間内)。保有ポジションのストップ監視が止まっています。"
  exit 0
fi

# 2. ログ鮮度 (ハング検知)
if [ -f "$LOG" ]; then
  age=$(( $(date +%s) - $(stat -c %Y "$LOG") ))
  if [ "$age" -gt 2400 ]; then
    alert "⚠️ 取引デーモンのログが $((age / 60)) 分更新されていません (ハングの疑い)。サイクルは30分毎のはず。"
  fi
fi
exit 0

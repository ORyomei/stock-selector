#!/usr/bin/env bash
# systemd unit のインストール (要 sudo)。再実行可 (更新にも使う)。
set -eu
DIR="$(cd "$(dirname "$0")" && pwd)"

sudo cp "$DIR"/stock-selector-trader.service \
        "$DIR"/stock-selector-notify@.service \
        "$DIR"/stock-selector-watchdog.service \
        "$DIR"/stock-selector-watchdog.timer \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable stock-selector-trader.service stock-selector-watchdog.timer

echo "インストール完了。起動するには:"
echo "  sudo systemctl start stock-selector-trader.service stock-selector-watchdog.timer"
echo "※ 旧・手動デーモン (二重フォーク起動) が動いている場合は先に 'uv run stock-selector stop'"

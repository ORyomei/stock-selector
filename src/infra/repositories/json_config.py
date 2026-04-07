"""JSON ファイルによる ConfigRepository 実装。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from interfaces.repositories.config import ConfigRepository


class JsonConfigRepository(ConfigRepository):
    """config/ 配下の JSON ファイルから設定を読み込む。"""

    def __init__(self, config_dir: Path) -> None:
        self._config_dir = config_dir

    def load_trading_config(self) -> dict[str, Any]:
        path = self._config_dir / "trading_config.json"
        if not path.exists():
            print(f"ERROR: Config file not found: {path}", file=sys.stderr)
            return {}
        with open(path) as f:
            return json.load(f)

    def load_risk_limits(self) -> dict[str, Any]:
        path = self._config_dir / "risk_limits.json"
        if not path.exists():
            print(f"ERROR: Risk limits file not found: {path}", file=sys.stderr)
            return {}
        with open(path) as f:
            return json.load(f)

    def load_watchlist(self) -> list[dict[str, Any]]:
        path = self._config_dir / "watchlist.json"
        if not path.exists():
            return []
        with open(path) as f:
            data = json.load(f)
        return data.get("watchlist", [])

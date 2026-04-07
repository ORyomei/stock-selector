"""ファイルシステムによる DiaryRepository 実装。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from interfaces.repositories.diary import DiaryRepository


class FileDiaryRepository(DiaryRepository):
    """diary/ 配下にレポート・シグナル・トレード結果をファイル保存する。"""

    def __init__(self, diary_dir: Path) -> None:
        self._diary_dir = diary_dir
        self._signals_dir = diary_dir / "signals"
        self._trades_dir = diary_dir / "trades"

    def save_report(self, filename: str, content: str) -> Path:
        self._diary_dir.mkdir(parents=True, exist_ok=True)
        if not filename.endswith(".md"):
            filename = f"{filename}.md"
        path = self._diary_dir / filename
        path.write_text(content, encoding="utf-8")
        return path

    def list_reports(self, days: int = 30) -> list[Path]:
        if not self._diary_dir.exists():
            return []
        cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
        return sorted(
            p for p in self._diary_dir.glob("*.md") if p.name >= cutoff
        )

    def save_signal(self, filename: str, signal_data: dict[str, Any]) -> str:
        self._signals_dir.mkdir(parents=True, exist_ok=True)
        path = self._signals_dir / filename
        path.write_text(
            json.dumps(signal_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return str(path)

    def load_signal(self, path: str | Path) -> dict[str, Any]:
        with open(path) as f:
            return json.load(f)

    def list_signals(self) -> list[Path]:
        if not self._signals_dir.exists():
            return []
        return sorted(self._signals_dir.glob("*.json"))

    def save_trade(self, trade_data: dict[str, Any]) -> str:
        self._trades_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        timestamp = now.strftime("%Y-%m-%d_%H%M%S")
        ticker = trade_data.get("ticker", "unknown")
        filename = f"{timestamp}_{ticker}_trade.json"
        path = self._trades_dir / filename
        with open(path, "w") as f:
            json.dump(trade_data, f, ensure_ascii=False, indent=2)
        return str(path)

    def load_recent_trades(self, days: int = 30) -> list[dict[str, Any]]:
        if not self._trades_dir.exists():
            return []
        cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
        results: list[dict[str, Any]] = []
        for p in sorted(self._trades_dir.glob("*.json")):
            if p.name >= cutoff:
                with open(p) as f:
                    results.append(json.load(f))
        return results

"""SQLite による AnalysisDBRepository 実装。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from interfaces.repositories.analysis_db import AnalysisDBRepository


class SQLiteAnalysisRepository(AnalysisDBRepository):
    """data/stock_analysis.db から過去の分析データを読み書きする。"""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def get_past_analyses(
        self,
        *,
        min_score: int | None = None,
        ticker: str | None = None,
    ) -> list[dict[str, Any]]:
        if not self._db_path.exists():
            return []
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM analyses ORDER BY date ASC").fetchall()
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()

        results = [dict(r) for r in rows]
        if min_score is not None:
            results = [
                r for r in results
                if r.get("score") is not None and abs(r["score"]) >= min_score
            ]
        if ticker:
            results = [r for r in results if r.get("ticker") == ticker]
        return results

    def save_analysis(self, data: dict[str, Any]) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS analyses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT, ticker TEXT, score REAL,
                    price REAL, action TEXT, reason TEXT, raw_json TEXT
                )"""
            )
            conn.execute(
                "INSERT INTO analyses (date, ticker, score, price, action, reason, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    data.get("date"), data.get("ticker"), data.get("score"),
                    data.get("price"), data.get("action"), data.get("reason"),
                    json.dumps(data, ensure_ascii=False),
                ),
            )
            conn.commit()
        finally:
            conn.close()

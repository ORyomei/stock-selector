"""JSON ファイルによる PortfolioRepository 実装。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from interfaces.repositories.portfolio import PortfolioRepository


class JsonPortfolioRepository(PortfolioRepository):
    """portfolio.json でポートフォリオ状態を永続化する。"""

    def __init__(
        self,
        portfolio_path: Path,
        risk_limits_path: Path,
    ) -> None:
        self._portfolio_path = portfolio_path
        self._risk_limits_path = risk_limits_path

    def load(self) -> dict[str, Any] | None:
        if self._portfolio_path.exists():
            with open(self._portfolio_path) as f:
                return json.load(f)
        return None

    def save(self, data: dict[str, Any]) -> None:
        with open(self._portfolio_path, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def get_held_tickers(self) -> set[str]:
        pf = self.load()
        return {p["ticker"] for p in pf.get("positions", [])} if pf else set()

    def get_held_positions(self) -> list[dict[str, Any]]:
        pf = self.load()
        return pf.get("positions", []) if pf else []

    def count_positions(self) -> int:
        pf = self.load()
        return len(pf.get("positions", [])) if pf else 0

    def get_max_positions(self) -> int:
        if self._risk_limits_path.exists():
            with open(self._risk_limits_path) as f:
                return json.load(f).get("max_concurrent_positions", 5)
        return 5

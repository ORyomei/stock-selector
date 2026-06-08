"""JSON ファイルによる PortfolioRepository 実装。"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
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
        if not self._portfolio_path.exists():
            return None
        try:
            with open(self._portfolio_path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            # 破損時は直近のバックアップにフォールバック
            backup = self._portfolio_path.with_name(self._portfolio_path.name + ".bak")
            if backup.exists():
                with open(backup) as f:
                    return json.load(f)
            raise

    def save(self, data: dict[str, Any]) -> None:
        # アトミック書き込み: temp に書いてから os.replace で原子的に差し替える。
        # 途中クラッシュでも portfolio.json が壊れた JSON になることはない。
        path = self._portfolio_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        # 既存ファイルを .bak にコピー退避 (best-effort)。コピーなので path は常に有効なまま。
        if path.exists():
            with contextlib.suppress(OSError):
                shutil.copy2(path, path.with_name(path.name + ".bak"))
        # 原子的に差し替え: 読み手は常に旧 or 新の完全なファイルを見る
        os.replace(tmp, path)

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

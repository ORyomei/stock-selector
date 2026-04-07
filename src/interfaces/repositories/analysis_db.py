"""Analysis database repository interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class AnalysisDBRepository(ABC):
    """過去の分析結果データベースを抽象化するリポジトリ。"""

    @abstractmethod
    def get_past_analyses(
        self,
        *,
        min_score: int | None = None,
        ticker: str | None = None,
    ) -> list[dict[str, Any]]:
        """過去の分析データを取得する。"""

    @abstractmethod
    def save_analysis(self, data: dict[str, Any]) -> None:
        """分析結果を保存する。"""

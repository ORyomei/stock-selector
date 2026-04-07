"""Portfolio repository interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class PortfolioRepository(ABC):
    """ポートフォリオ状態の永続化を抽象化するリポジトリ。"""

    @abstractmethod
    def load(self) -> dict[str, Any] | None:
        """ポートフォリオ全体を読み込む。未作成なら None。"""

    @abstractmethod
    def save(self, data: dict[str, Any]) -> None:
        """ポートフォリオ全体を保存する。"""

    @abstractmethod
    def get_held_tickers(self) -> set[str]:
        """保有中のティッカー一覧を返す。"""

    @abstractmethod
    def get_held_positions(self) -> list[dict[str, Any]]:
        """保有中のポジション一覧を返す。"""

    @abstractmethod
    def count_positions(self) -> int:
        """保有ポジション数を返す。"""

    @abstractmethod
    def get_max_positions(self) -> int:
        """同時保有上限を返す。"""

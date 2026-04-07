"""Config repository interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ConfigRepository(ABC):
    """設定ファイル読み込みを抽象化するリポジトリ。"""

    @abstractmethod
    def load_trading_config(self) -> dict[str, Any]:
        """trading_config.json を読み込む。"""

    @abstractmethod
    def load_risk_limits(self) -> dict[str, Any]:
        """risk_limits.json を読み込む。"""

    @abstractmethod
    def load_watchlist(self) -> list[dict[str, Any]]:
        """watchlist.json の watchlist 配列を返す。"""

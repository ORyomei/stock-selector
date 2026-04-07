"""Market data repository interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import pandas as pd


class MarketDataRepository(ABC):
    """株価・銘柄情報の取得を抽象化するリポジトリ。"""

    @abstractmethod
    def get_price_history(
        self,
        ticker: str,
        period: str = "3mo",
        interval: str = "1d",
    ) -> pd.DataFrame:
        """OHLCV 履歴を取得する。"""

    @abstractmethod
    def get_ticker_info(self, ticker: str) -> dict[str, Any]:
        """銘柄の基本情報辞書を取得する。"""

    @abstractmethod
    def get_current_price(self, ticker: str) -> float | None:
        """直近の終値を返す。取得できなければ None。"""

    @abstractmethod
    def get_earnings_dates(self, ticker: str) -> pd.DataFrame | None:
        """決算日程を取得する。データが無ければ None。"""

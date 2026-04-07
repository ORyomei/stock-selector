"""yfinance を使った MarketDataRepository 実装。"""

from __future__ import annotations

import math
import sys
import time
from typing import Any

import pandas as pd
import yfinance as yf

from interfaces.repositories.market_data import MarketDataRepository


class YFinanceMarketDataRepository(MarketDataRepository):
    """yfinance 経由で株価・銘柄情報を取得する。"""

    def __init__(self, retries: int = 2) -> None:
        self._retries = retries

    def get_price_history(
        self,
        ticker: str,
        period: str = "3mo",
        interval: str = "1d",
    ) -> pd.DataFrame:
        max_attempts = self._retries + 1
        for attempt in range(max_attempts):
            try:
                t = yf.Ticker(ticker)
                hist = t.history(period=period, interval=interval)
                if hist is not None and not hist.empty:
                    return hist
            except Exception as e:
                print(
                    f"WARN: {ticker} 履歴取得失敗 (attempt {attempt + 1}): {e}",
                    file=sys.stderr,
                )
            if attempt < max_attempts - 1:
                time.sleep(1.0 * (attempt + 1))
        return pd.DataFrame()

    def get_ticker_info(self, ticker: str) -> dict[str, Any]:
        max_attempts = self._retries + 1
        for attempt in range(max_attempts):
            try:
                t = yf.Ticker(ticker)
                info = t.info
                if info:
                    return info
            except Exception as e:
                print(
                    f"WARN: {ticker} info取得失敗 (attempt {attempt + 1}): {e}",
                    file=sys.stderr,
                )
            if attempt < max_attempts - 1:
                time.sleep(1.0 * (attempt + 1))
        return {}

    def get_current_price(self, ticker: str) -> float | None:
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="5d", interval="1d")
            if hist is not None and not hist.empty:
                price = float(hist["Close"].iloc[-1])
                if math.isfinite(price):
                    return price
        except Exception:
            pass
        return None

    def get_earnings_dates(self, ticker: str) -> pd.DataFrame | None:
        try:
            t = yf.Ticker(ticker)
            ed = t.earnings_dates
            if ed is not None and not ed.empty:
                return ed
        except Exception:
            pass
        return None

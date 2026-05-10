#!/usr/bin/env python3
"""株価データ取得スクリプト

Usage: python scripts/fetch_prices.py <ticker> [--period 3mo] [--interval 1d]

:
  python scripts/fetch_prices.py 7203.T
  python scripts/fetch_prices.py AAPL --period 1mo --interval 1h
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = SRC_DIR.parent
sys.path.insert(0, str(SRC_DIR))

from infra.container import get_container


def fetch(ticker: str, period: str = "3mo", interval: str = "1d"):
    container = get_container()
    market_data = container.market_data()

    info = market_data.get_ticker_info(ticker)
    hist = market_data.get_price_history(ticker, period=period, interval=interval)

    if hist.empty:
        print(f"ERROR: {ticker} のデータが取得できませんでした", file=sys.stderr)
        sys.exit(1)

    latest = hist.iloc[-1]

    summary = {
        "ticker": ticker,
        "name": info.get("shortName", ""),
        "currency": info.get("currency", ""),
        "current_price": round(float(latest["Close"]), 2),
        "open": round(float(latest["Open"]), 2),
        "high": round(float(latest["High"]), 2),
        "low": round(float(latest["Low"]), 2),
        "volume": int(latest["Volume"]),
        "market_cap": info.get("marketCap"),
        "pe_ratio": info.get("trailingPE"),
        "pb_ratio": info.get("priceToBook"),
        "dividend_yield": info.get("dividendYield"),
        "52w_high": info.get("fiftyTwoWeekHigh"),
        "52w_low": info.get("fiftyTwoWeekLow"),
    }

    print(json.dumps(summary, ensure_ascii=False, indent=2))

    print("\n--- 直近10日の値動き ---")
    recent = hist.tail(10)[["Open", "High", "Low", "Close", "Volume"]]
    recent = recent.round(2)
    print(recent.to_string())
    return summary

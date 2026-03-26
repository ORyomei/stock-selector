#!/usr/bin/env python3
"""株価データ取得スクリプト

Usage: python scripts/fetch_prices.py <ticker> [--period 3mo] [--interval 1d]

例:
  python scripts/fetch_prices.py 7203.T
  python scripts/fetch_prices.py AAPL --period 1mo --interval 1h
"""
import argparse
import json
import sys

import yfinance as yf


def fetch(ticker: str, period: str = "3mo", interval: str = "1d"):
    t = yf.Ticker(ticker)
    info = t.info
    hist = t.history(period=period, interval=interval)

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


def main():
    parser = argparse.ArgumentParser(description="株価データ取得")
    parser.add_argument("ticker", help="ティッカーシンボル (例: 7203.T, AAPL)")
    parser.add_argument("--period", default="3mo", help="取得期間 (1d,5d,1mo,3mo,6mo,1y,2y,5y,10y,ytd,max)")
    parser.add_argument("--interval", default="1d", help="間隔 (1m,2m,5m,15m,30m,60m,90m,1h,1d,5d,1wk,1mo,3mo)")
    args = parser.parse_args()
    fetch(args.ticker, args.period, args.interval)


if __name__ == "__main__":
    main()

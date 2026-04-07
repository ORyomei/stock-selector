#!/usr/bin/env python3
"""テクニカル指標算出スクリプト

Usage: python scripts/technical.py <ticker> [--period 6mo]

RSI, MACD, ボリンジャーバンド, SMA/EMA 等を計算して出力する。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import ta

SRC_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = SRC_DIR.parent
sys.path.insert(0, str(SRC_DIR))

from infra.container import get_container


def analyze(ticker: str, period: str = "6mo"):
    container = get_container()
    hist = container.market_data().get_price_history(ticker, period=period, interval="1d")

    if hist.empty or len(hist) < 30:
        print(f"ERROR: {ticker} のデータ不足（最低30日分が必要）", file=sys.stderr)
        sys.exit(1)

    close = hist["Close"]
    hist["High"]
    hist["Low"]
    hist["Volume"]

    # RSI (14日)
    rsi_indicator = ta.momentum.RSIIndicator(close, window=14)
    rsi = rsi_indicator.rsi().iloc[-1]

    # MACD
    macd_indicator = ta.trend.MACD(close)
    macd_line = macd_indicator.macd().iloc[-1]
    macd_signal = macd_indicator.macd_signal().iloc[-1]
    macd_hist = macd_indicator.macd_diff().iloc[-1]

    # ボリンジャーバンド (20日, 2σ)
    bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)
    bb_upper = bb.bollinger_hband().iloc[-1]
    bb_middle = bb.bollinger_mavg().iloc[-1]
    bb_lower = bb.bollinger_lband().iloc[-1]

    # 移動平均
    sma_5 = close.rolling(5).mean().iloc[-1]
    sma_25 = close.rolling(25).mean().iloc[-1]
    sma_75 = close.rolling(75).mean().iloc[-1] if len(close) >= 75 else None
    ema_12 = close.ewm(span=12).mean().iloc[-1]
    ema_26 = close.ewm(span=26).mean().iloc[-1]

    current = float(close.iloc[-1])

    # シグナル判定
    signals = []
    if rsi < 30:
        signals.append("RSI 売られすぎ → 買いシグナル")
    elif rsi > 70:
        signals.append("RSI 買われすぎ → 売りシグナル")

    if macd_hist > 0 and macd_line > macd_signal:
        signals.append("MACD ゴールデンクロス → 買いシグナル")
    elif macd_hist < 0 and macd_line < macd_signal:
        signals.append("MACD デッドクロス → 売りシグナル")

    if current < bb_lower:
        signals.append("ボリンジャーバンド下限割れ → 反発期待")
    elif current > bb_upper:
        signals.append("ボリンジャーバンド上限突破 → 過熱感")

    if sma_5 > sma_25:
        signals.append("短期SMA > 中期SMA → 上昇トレンド")
    else:
        signals.append("短期SMA < 中期SMA → 下降トレンド")

    result = {
        "ticker": ticker,
        "current_price": round(current, 2),
        "rsi_14": round(float(rsi), 2),
        "macd": {
            "line": round(float(macd_line), 4),
            "signal": round(float(macd_signal), 4),
            "histogram": round(float(macd_hist), 4),
        },
        "bollinger_bands": {
            "upper": round(float(bb_upper), 2),
            "middle": round(float(bb_middle), 2),
            "lower": round(float(bb_lower), 2),
        },
        "moving_averages": {
            "sma_5": round(float(sma_5), 2),
            "sma_25": round(float(sma_25), 2),
            "sma_75": round(float(sma_75), 2) if sma_75 is not None else None,
            "ema_12": round(float(ema_12), 2),
            "ema_26": round(float(ema_26), 2),
        },
        "signals": signals,
    }

    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="テクニカル指標算出")
    parser.add_argument("ticker", help="ティッカーシンボル (例: 7203.T, AAPL)")
    parser.add_argument("--period", default="6mo", help="分析期間")
    args = parser.parse_args()
    analyze(args.ticker, args.period)


if __name__ == "__main__":
    main()

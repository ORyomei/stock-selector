#!/usr/bin/env python3
"""マクロ経済指標スクリプト

Usage: python scripts/macro.py [--period 3mo]

VIX, 米10年金利, ドル円, 原油先物, 金先物, 主要指数を取得し、
市場環境スコア（リスクオン/オフ）を算出する。
"""

from __future__ import annotations

import argparse
import json
import math

import yfinance as yf

# ---- 監視対象シンボル ----
MACRO_SYMBOLS = {
    "VIX": {"symbol": "^VIX", "label": "恐怖指数(VIX)"},
    "US10Y": {"symbol": "^TNX", "label": "米10年金利"},
    "USDJPY": {"symbol": "USDJPY=X", "label": "ドル円"},
    "OIL": {"symbol": "CL=F", "label": "原油先物(WTI)"},
    "GOLD": {"symbol": "GC=F", "label": "金先物"},
    "SP500": {"symbol": "^GSPC", "label": "S&P 500"},
    "NASDAQ": {"symbol": "^IXIC", "label": "NASDAQ"},
    "NIKKEI": {"symbol": "^N225", "label": "日経平均"},
    "DXY": {"symbol": "DX-Y.NYB", "label": "ドルインデックス"},
}


def fetch_macro(period: str = "3mo"):
    data = {}

    for key, meta in MACRO_SYMBOLS.items():
        try:
            t = yf.Ticker(meta["symbol"])
            hist = t.history(period=period, interval="1d")
            if hist.empty or len(hist) < 2:
                continue

            close = hist["Close"]
            current = float(close.iloc[-1])
            prev = float(close.iloc[-2])
            ret_1d = (current / prev - 1) * 100

            # 5日・20日リターン
            ret_5d = float((close.iloc[-1] / close.iloc[-6] - 1) * 100) if len(close) >= 6 else None
            ret_20d = (
                float((close.iloc[-1] / close.iloc[-21] - 1) * 100) if len(close) >= 21 else None
            )

            # 20日ボラティリティ
            daily_ret = close.pct_change().dropna()
            vol = (
                float(daily_ret.tail(20).std() * math.sqrt(252) * 100)
                if len(daily_ret) >= 20
                else None
            )

            # 位置（直近高安に対する位置）
            h20 = float(close.tail(20).max())
            l20 = float(close.tail(20).min())
            position = round((current - l20) / (h20 - l20) * 100, 1) if h20 != l20 else 50.0

            data[key] = {
                "label": meta["label"],
                "current": round(current, 2),
                "change_1d": f"{ret_1d:+.2f}%",
                "change_5d": f"{ret_5d:+.2f}%" if ret_5d is not None else None,
                "change_20d": f"{ret_20d:+.2f}%" if ret_20d is not None else None,
                "volatility": f"{vol:.1f}%" if vol is not None else None,
                "position_20d": f"{position}%",
            }
        except Exception:
            continue

    # ---- 市場環境スコア算出 ----
    env_score = 0
    env_signals = []

    # VIX 評価
    vix = data.get("VIX", {}).get("current")
    if vix is not None:
        if vix < 15:
            env_score += 20
            env_signals.append(f"VIX低い({vix:.1f}) → リスクオン")
        elif vix < 20:
            env_score += 10
            env_signals.append(f"VIX通常({vix:.1f})")
        elif vix < 30:
            env_score -= 10
            env_signals.append(f"VIXやや高い({vix:.1f}) → 警戒")
        else:
            env_score -= 20
            env_signals.append(f"VIX高い({vix:.1f}) → リスクオフ")

    # 金利動向
    us10y = data.get("US10Y", {}).get("current")
    if us10y is not None:
        us10y_5d = data.get("US10Y", {}).get("change_5d")
        if us10y_5d:
            change = float(us10y_5d.strip("%"))
            if change > 5:
                env_score -= 10
                env_signals.append(f"金利急上昇({us10y_5d}) → 株式に逆風")
            elif change < -5:
                env_score += 10
                env_signals.append(f"金利低下({us10y_5d}) → 株式に追い風")

    # S&P500 トレンド
    sp_change_20d = data.get("SP500", {}).get("change_20d")
    if sp_change_20d:
        change = float(sp_change_20d.strip("%"))
        if change > 5:
            env_score += 10
            env_signals.append(f"S&P500上昇トレンド({sp_change_20d})")
        elif change < -5:
            env_score -= 10
            env_signals.append(f"S&P500下落トレンド({sp_change_20d})")

    # 原油動向
    oil_change_20d = data.get("OIL", {}).get("change_20d")
    if oil_change_20d:
        change = float(oil_change_20d.strip("%"))
        if change > 10:
            env_signals.append(f"原油急騰({oil_change_20d}) → インフレ懸念")
        elif change < -10:
            env_signals.append(f"原油急落({oil_change_20d}) → デフレ/景気懸念")

    # ドル円
    usdjpy_change_20d = data.get("USDJPY", {}).get("change_20d")
    if usdjpy_change_20d:
        change = float(usdjpy_change_20d.strip("%"))
        if change > 3:
            env_signals.append(f"円安進行({usdjpy_change_20d}) → 輸出企業に追い風")
        elif change < -3:
            env_signals.append(f"円高進行({usdjpy_change_20d}) → 輸出企業に逆風")

    # 環境判定
    if env_score >= 20:
        environment = "強気（リスクオン）"
    elif env_score >= 5:
        environment = "やや強気"
    elif env_score >= -5:
        environment = "中立"
    elif env_score >= -20:
        environment = "やや弱気"
    else:
        environment = "弱気（リスクオフ）"

    result = {
        "indicators": data,
        "market_environment": {
            "score": env_score,
            "assessment": environment,
            "signals": env_signals,
        },
    }

    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="マクロ経済指標")
    parser.add_argument("--period", default="3mo", help="取得期間")
    args = parser.parse_args()
    fetch_macro(args.period)


if __name__ == "__main__":
    main()

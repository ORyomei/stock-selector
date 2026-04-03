#!/usr/bin/env python3
"""アラート・監視スクリプト

Usage:
  python scripts/alert.py                        # ウォッチリスト全銘柄チェック
  python scripts/alert.py --ticker SLB            # 特定銘柄のみ
  python scripts/alert.py --check-portfolio       # ポートフォリオの損切り/利確チェック

ウォッチリスト/ポートフォリオの銘柄を監視し、
価格急変・損切りライン到達・テクニカルシグナル発生を検知する。
"""
import argparse
import json
import math
import sys
from pathlib import Path

import yfinance as yf
import ta

CONFIG_DIR = Path(__file__).parent.parent / "config"
PORTFOLIO_PATH = Path(__file__).parent.parent / "portfolio.json"


def load_watchlist() -> list[dict]:
    """ウォッチリスト読み込み"""
    wl_path = CONFIG_DIR / "watchlist.json"
    if not wl_path.exists():
        return []
    with open(wl_path) as f:
        data = json.load(f)
    return data.get("watchlist", [])


def load_portfolio() -> dict:
    """ポートフォリオ読み込み"""
    if not PORTFOLIO_PATH.exists():
        return {"holdings": []}
    with open(PORTFOLIO_PATH) as f:
        return json.load(f)


def check_ticker(ticker: str) -> dict | None:
    """1銘柄のアラートチェック"""
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="3mo", interval="1d")
        if hist.empty or len(hist) < 20:
            return None

        info = t.info
        close = hist["Close"]
        high = hist["High"]
        low = hist["Low"]
        volume = hist["Volume"]
        current = float(close.iloc[-1])

        # 日次リターン
        daily_ret = close.pct_change().dropna()
        ret_1d = float(daily_ret.iloc[-1]) * 100
        ret_5d = float((close.iloc[-1] / close.iloc[-6] - 1) * 100) if len(close) >= 6 else 0

        # RSI
        rsi = float(ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1])

        # ボリンジャーバンド
        bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)
        bb_upper = float(bb.bollinger_hband().iloc[-1])
        bb_lower = float(bb.bollinger_lband().iloc[-1])

        # 出来高
        vol_sma = float(volume.rolling(20).mean().iloc[-1])
        vol_ratio = float(volume.iloc[-1]) / vol_sma if vol_sma > 0 else 1.0

        # MACD
        macd_ind = ta.trend.MACD(close)
        macd_hist = float(macd_ind.macd_diff().iloc[-1])
        macd_hist_prev = float(macd_ind.macd_diff().iloc[-2])
        macd_gc = macd_hist > 0 and macd_hist_prev <= 0
        macd_dc = macd_hist < 0 and macd_hist_prev >= 0

        # ATR
        atr = float(ta.volatility.AverageTrueRange(high, low, close, window=14)
                     .average_true_range().iloc[-1])

        # 52週高安
        high_52w = info.get("fiftyTwoWeekHigh")
        low_52w = info.get("fiftyTwoWeekLow")

        # ---- アラート判定 ----
        alerts = []

        # 価格急変
        if abs(ret_1d) > 5:
            alerts.append({
                "type": "急変",
                "severity": "高",
                "message": f"1日で{ret_1d:+.1f}%の{'急騰' if ret_1d > 0 else '急落'}",
            })
        elif abs(ret_1d) > 3:
            alerts.append({
                "type": "変動",
                "severity": "中",
                "message": f"1日で{ret_1d:+.1f}%の{'上昇' if ret_1d > 0 else '下落'}",
            })

        # 出来高異常
        if vol_ratio > 3.0:
            alerts.append({
                "type": "出来高異常",
                "severity": "高",
                "message": f"出来高が20日平均の{vol_ratio:.1f}倍",
            })
        elif vol_ratio > 2.0:
            alerts.append({
                "type": "出来高増加",
                "severity": "中",
                "message": f"出来高が20日平均の{vol_ratio:.1f}倍",
            })

        # テクニカルシグナル
        if rsi < 25:
            alerts.append({
                "type": "RSI極端",
                "severity": "高",
                "message": f"RSI={rsi:.1f} — 極端に売られすぎ（反発候補）",
            })
        elif rsi > 80:
            alerts.append({
                "type": "RSI極端",
                "severity": "高",
                "message": f"RSI={rsi:.1f} — 極端に買われすぎ（天井候補）",
            })

        if macd_gc:
            alerts.append({
                "type": "MACDゴールデンクロス",
                "severity": "中",
                "message": "MACD GC発生 → 買いシグナル",
            })
        elif macd_dc:
            alerts.append({
                "type": "MACDデッドクロス",
                "severity": "中",
                "message": "MACD DC発生 → 売りシグナル",
            })

        if current < bb_lower:
            alerts.append({
                "type": "BB下限割れ",
                "severity": "中",
                "message": f"ボリンジャーバンド下限割れ（下限: {bb_lower:.2f}）",
            })
        elif current > bb_upper:
            alerts.append({
                "type": "BB上限突破",
                "severity": "中",
                "message": f"ボリンジャーバンド上限突破（上限: {bb_upper:.2f}）",
            })

        # 52週高安接近
        if high_52w and current >= high_52w * 0.98:
            alerts.append({
                "type": "52週高値接近",
                "severity": "中",
                "message": f"52週高値に接近（高値: {high_52w:.2f}）",
            })
        if low_52w and current <= low_52w * 1.02:
            alerts.append({
                "type": "52週安値接近",
                "severity": "高",
                "message": f"52週安値に接近（安値: {low_52w:.2f}）",
            })

        return {
            "ticker": ticker,
            "name": info.get("shortName", ticker),
            "current_price": round(current, 2),
            "currency": info.get("currency", ""),
            "return_1d": f"{ret_1d:+.2f}%",
            "return_5d": f"{ret_5d:+.2f}%",
            "rsi": round(rsi, 1),
            "volume_ratio": round(vol_ratio, 2),
            "atr": round(atr, 2),
            "alerts": alerts,
            "alert_count": len(alerts),
        }
    except Exception as e:
        print(f"WARN: {ticker} のチェックに失敗: {e}", file=sys.stderr)
        return None


def check_portfolio_stops(portfolio: dict) -> list[dict]:
    """ポートフォリオの損切り/利確チェック"""
    alerts = []
    for h in portfolio.get("holdings", []):
        ticker = h.get("ticker")
        entry_price = h.get("entry_price")
        stop_loss = h.get("stop_loss")
        take_profit = h.get("take_profit")

        if not ticker or not entry_price:
            continue

        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="5d", interval="1d")
            if hist.empty:
                continue
            current = float(hist["Close"].iloc[-1])
            pnl_pct = (current / entry_price - 1) * 100

            holding_alert = {
                "ticker": ticker,
                "entry_price": entry_price,
                "current_price": round(current, 2),
                "pnl_pct": f"{pnl_pct:+.2f}%",
                "alerts": [],
            }

            if stop_loss and current <= stop_loss:
                holding_alert["alerts"].append({
                    "type": "損切り到達",
                    "severity": "高",
                    "message": f"損切りライン{stop_loss}に到達（現在値: {current:.2f}）",
                })

            if take_profit and current >= take_profit:
                holding_alert["alerts"].append({
                    "type": "利確到達",
                    "severity": "中",
                    "message": f"利確目標{take_profit}に到達（現在値: {current:.2f}）",
                })

            if holding_alert["alerts"]:
                alerts.append(holding_alert)

        except Exception:
            continue

    return alerts


def main():
    parser = argparse.ArgumentParser(description="アラート・監視")
    parser.add_argument("--ticker", type=str, default=None, help="特定銘柄のみチェック")
    parser.add_argument("--check-portfolio", action="store_true",
                        help="ポートフォリオの損切り/利確チェック")
    args = parser.parse_args()

    results = []

    if args.check_portfolio:
        portfolio = load_portfolio()
        portfolio_alerts = check_portfolio_stops(portfolio)
        if portfolio_alerts:
            print(json.dumps({"portfolio_alerts": portfolio_alerts}, ensure_ascii=False, indent=2))
        else:
            print(json.dumps({"portfolio_alerts": [], "message": "アラートなし"},
                             ensure_ascii=False, indent=2))
        return

    if args.ticker:
        tickers = [args.ticker]
    else:
        watchlist = load_watchlist()
        tickers = [w["ticker"] for w in watchlist]
        if not tickers:
            print("ウォッチリストが空です", file=sys.stderr)
            sys.exit(1)

    print(f"チェック中... {len(tickers)} 銘柄", file=sys.stderr)

    for ticker in tickers:
        r = check_ticker(ticker)
        if r:
            results.append(r)

    # アラートがあるものを上に
    results.sort(key=lambda x: x["alert_count"], reverse=True)

    total_alerts = sum(r["alert_count"] for r in results)
    output = {
        "total_tickers": len(results),
        "total_alerts": total_alerts,
        "results": results,
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

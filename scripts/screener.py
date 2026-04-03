#!/usr/bin/env python3
"""株式スクリーナー — 市場全体から有望銘柄を自動発見

Usage:
  python scripts/screener.py                    # デフォルト（米国+日本、全戦略）
  python scripts/screener.py --market us         # 米国株のみ
  python scripts/screener.py --market jp         # 日本株のみ
  python scripts/screener.py --strategy oversold  # 売られすぎ銘柄のみ
  python scripts/screener.py --top 10            # 上位10銘柄を表示
  python scripts/screener.py --universe expanded  # 拡張ユニバース

戦略:
  oversold    — RSI低 + BB下限接近（逆張り買い候補）
  momentum    — MACD GC + 出来高急増（順張り買い候補）
  breakout    — 直近高値ブレイク + 出来高増（ブレイクアウト）
  value       — PER/PBR割安 + 配当利回り高
  all         — 上記すべてを実行（デフォルト）
"""
import argparse
import json
import sys
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import yfinance as yf
import ta
import pandas as pd


# ---- スキャン対象ユニバース ----
# 主要指数の構成銘柄（代表的なもの）

US_UNIVERSE = [
    # Mega Cap Tech
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AVGO", "ORCL", "CRM",
    # Semiconductors
    "AMD", "INTC", "QCOM", "TXN", "MU", "MRVL", "KLAC", "LRCX", "AMAT", "ON",
    # AI / Cloud
    "PLTR", "SNOW", "NET", "DDOG", "ZS", "CRWD", "MDB", "PANW",
    # Finance
    "JPM", "BAC", "GS", "MS", "V", "MA", "AXP", "BLK", "SCHW", "C",
    # Healthcare
    "UNH", "JNJ", "LLY", "PFE", "ABBV", "MRK", "TMO", "ABT", "BMY", "AMGN",
    # Consumer
    "WMT", "COST", "HD", "MCD", "NKE", "SBUX", "TGT", "LOW", "DIS", "NFLX",
    # Energy
    "XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX", "VLO", "OXY", "HAL",
    # Industrial
    "CAT", "DE", "BA", "GE", "HON", "UPS", "RTX", "LMT", "MMM", "FDX",
    # Other
    "BRK-B", "COIN", "SQ", "SHOP", "UBER", "ABNB", "RIVN", "LCID",
]

JP_UNIVERSE = [
    # 日経225 主要銘柄
    "7203.T", "6758.T", "9984.T", "8306.T", "6861.T", "9432.T", "9433.T",
    "7267.T", "6501.T", "6902.T", "4063.T", "8035.T", "6367.T", "7974.T",
    "4502.T", "4503.T", "4568.T", "6098.T", "3382.T", "9983.T",
    "2802.T", "7751.T", "6954.T", "8001.T", "8058.T", "8031.T",
    "5401.T", "3407.T", "6503.T", "7011.T", "2914.T", "4661.T",
    "6273.T", "7741.T", "6857.T", "6971.T", "4519.T", "6702.T",
    "9613.T", "6326.T",
]

# ---- 拡張ユニバース ----
# S&P 500 の追加銘柄 (主要分以外)
US_EXPANDED = [
    # Additional S&P 500 components (Finance)
    "WFC", "USB", "PNC", "TFC", "COF", "AIG", "MET", "PRU", "ALL", "CB",
    # Healthcare expanded
    "GILD", "REGN", "VRTX", "ISRG", "DXCM", "ZTS", "CI", "HUM", "ELV", "MCK",
    # Consumer expanded
    "PG", "KO", "PEP", "PM", "MO", "CL", "EL", "MDLZ", "KHC", "GIS",
    # Tech expanded
    "ADBE", "INTU", "NOW", "WDAY", "TEAM", "DOCU", "FTNT", "CDNS", "SNPS", "ANSS",
    # Real Estate / Utilities
    "AMT", "PLD", "CCI", "EQIX", "SPG", "NEE", "DUK", "SO", "AEP", "D",
    # Industrials expanded
    "WM", "RSG", "EMR", "ITW", "ROK", "SWK", "IR", "GD", "NOC", "TDG",
    # Materials
    "LIN", "APD", "ECL", "SHW", "NEM", "FCX", "GOLD", "NUE", "CLF", "STLD",
]

# 日経225 追加銘柄
JP_EXPANDED = [
    "8316.T", "8411.T", "8604.T", "8766.T", "8725.T",  # 金融追加
    "6301.T", "6305.T", "7012.T", "7013.T", "7201.T",  # 機械・自動車
    "4901.T", "4911.T", "2801.T", "2269.T", "7269.T",  # 消費財
    "3086.T", "8267.T", "3099.T", "9843.T", "2413.T",  # 小売・サービス
    "9020.T", "9021.T", "9022.T", "9001.T", "9005.T",  # 運輸
    "1925.T", "1928.T", "1878.T", "5108.T", "5802.T",  # 建設・素材
    "4507.T", "4523.T", "4578.T", "2432.T", "4689.T",  # 製薬・IT
]


def analyze_single(ticker: str, retries: int = 2) -> dict | None:
    """1銘柄のテクニカル指標を取得して辞書で返す。失敗時はリトライ後 None。"""
    for attempt in range(retries + 1):
        try:
            return _analyze_impl(ticker)
        except Exception:
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
            continue
    return None


def _analyze_impl(ticker: str) -> dict | None:
    """analyze_single の実装本体"""
    t = yf.Ticker(ticker)
    hist = t.history(period="6mo", interval="1d")
    if hist.empty or len(hist) < 30:
        return None

    info = t.info
    close = hist["Close"]
    high = hist["High"]
    low = hist["Low"]
    volume = hist["Volume"]
    current = float(close.iloc[-1])

    # RSI
    rsi = float(ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1])

    # MACD
    macd_ind = ta.trend.MACD(close)
    macd_hist = float(macd_ind.macd_diff().iloc[-1])
    macd_hist_prev = float(macd_ind.macd_diff().iloc[-2])

    # ボリンジャーバンド
    bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)
    bb_upper = float(bb.bollinger_hband().iloc[-1])
    bb_lower = float(bb.bollinger_lband().iloc[-1])
    bb_pct = (current - bb_lower) / (bb_upper - bb_lower) if (bb_upper - bb_lower) > 0 else 0.5

    # 出来高
    vol_sma_20 = float(volume.rolling(20).mean().iloc[-1])
    vol_ratio = float(volume.iloc[-1]) / vol_sma_20 if vol_sma_20 > 0 else 1.0

    # ATR
    atr = float(ta.volatility.AverageTrueRange(high, low, close, window=14).average_true_range().iloc[-1])

    # リターン
    daily_returns = close.pct_change().dropna()
    ret_1d = float(daily_returns.iloc[-1])
    ret_5d = float(close.iloc[-1] / close.iloc[-6] - 1) if len(close) >= 6 else 0
    ret_20d = float(close.iloc[-1] / close.iloc[-21] - 1) if len(close) >= 21 else 0

    # ボラティリティ
    vol_20d = float(daily_returns.tail(20).std())
    ann_vol = vol_20d * math.sqrt(252)

    # SMA
    sma_5 = float(close.rolling(5).mean().iloc[-1])
    sma_25 = float(close.rolling(25).mean().iloc[-1])
    sma_75 = float(close.rolling(75).mean().iloc[-1]) if len(close) >= 75 else None

    # 直近高値・安値
    high_20d = float(high.tail(20).max())
    low_20d = float(low.tail(20).min())
    high_60d = float(high.tail(60).max()) if len(high) >= 60 else high_20d

    # ファンダメンタル
    pe = info.get("trailingPE")
    pb = info.get("priceToBook")
    div_yield = info.get("dividendYield")
    market_cap = info.get("marketCap")

    return {
        "ticker": ticker,
        "name": info.get("shortName", ticker),
        "currency": info.get("currency", ""),
        "current_price": round(current, 2),
        "rsi": round(rsi, 2),
        "macd_hist": round(macd_hist, 4),
        "macd_hist_prev": round(macd_hist_prev, 4),
        "macd_gc": macd_hist > 0 and macd_hist_prev <= 0,
        "macd_dc": macd_hist < 0 and macd_hist_prev >= 0,
        "bb_pct": round(bb_pct, 4),
        "bb_lower": round(bb_lower, 2),
        "bb_upper": round(bb_upper, 2),
        "vol_ratio": round(vol_ratio, 2),
        "atr": round(atr, 2),
        "ret_1d": round(ret_1d * 100, 2),
        "ret_5d": round(ret_5d * 100, 2),
        "ret_20d": round(ret_20d * 100, 2),
        "ann_vol": round(ann_vol * 100, 1),
        "sma_5": round(sma_5, 2),
        "sma_25": round(sma_25, 2),
        "sma_75": round(sma_75, 2) if sma_75 else None,
        "high_20d": round(high_20d, 2),
        "low_20d": round(low_20d, 2),
        "high_60d": round(high_60d, 2),
        "pe": round(pe, 2) if pe else None,
        "pb": round(pb, 2) if pb else None,
        "div_yield": round(div_yield * 100, 2) if div_yield else None,
        "market_cap": market_cap,
    }


def screen_oversold(data: list[dict]) -> list[dict]:
    """売られすぎ銘柄: RSI < 35 かつ BB下位20%"""
    results = []
    for d in data:
        score = 0
        reasons = []
        if d["rsi"] < 25:
            score += 40
            reasons.append(f"RSI極端に低い ({d['rsi']})")
        elif d["rsi"] < 30:
            score += 30
            reasons.append(f"RSI売られすぎ ({d['rsi']})")
        elif d["rsi"] < 35:
            score += 15
            reasons.append(f"RSI低め ({d['rsi']})")
        else:
            continue

        if d["bb_pct"] < 0:
            score += 30
            reasons.append(f"BB下限割れ (位置: {d['bb_pct']:.1%})")
        elif d["bb_pct"] < 0.15:
            score += 20
            reasons.append(f"BB下限近辺 (位置: {d['bb_pct']:.1%})")
        elif d["bb_pct"] < 0.25:
            score += 10
            reasons.append(f"BB下方 (位置: {d['bb_pct']:.1%})")

        if d["vol_ratio"] > 1.5:
            score += 10
            reasons.append(f"出来高急増 ({d['vol_ratio']}倍)")

        if score >= 25:
            results.append({**d, "strategy": "oversold", "score": score, "reasons": reasons})

    return sorted(results, key=lambda x: x["score"], reverse=True)


def screen_momentum(data: list[dict]) -> list[dict]:
    """モメンタム銘柄: MACDゴールデンクロス + 出来高増"""
    results = []
    for d in data:
        score = 0
        reasons = []

        if d["macd_gc"]:
            score += 35
            reasons.append("MACDゴールデンクロス発生")
        elif d["macd_hist"] > 0 and d["macd_hist"] > d["macd_hist_prev"]:
            score += 20
            reasons.append(f"MACDヒストグラム拡大中 ({d['macd_hist']:.4f})")
        else:
            continue

        if d["vol_ratio"] > 2.0:
            score += 25
            reasons.append(f"出来高急増 ({d['vol_ratio']}倍)")
        elif d["vol_ratio"] > 1.3:
            score += 15
            reasons.append(f"出来高増加 ({d['vol_ratio']}倍)")

        if d["ret_1d"] > 2:
            score += 15
            reasons.append(f"直近1日+{d['ret_1d']}%の急騰")
        elif d["ret_1d"] > 0:
            score += 5
            reasons.append(f"直近1日+{d['ret_1d']}%")

        if d["sma_5"] > d["sma_25"]:
            score += 10
            reasons.append("短期SMA > 中期SMA（上昇トレンド）")

        if score >= 30:
            results.append({**d, "strategy": "momentum", "score": score, "reasons": reasons})

    return sorted(results, key=lambda x: x["score"], reverse=True)


def screen_breakout(data: list[dict]) -> list[dict]:
    """ブレイクアウト銘柄: 60日高値更新 + 出来高増"""
    results = []
    for d in data:
        score = 0
        reasons = []

        if d["current_price"] >= d["high_60d"] * 0.99:
            score += 30
            reasons.append(f"60日高値圏 (高値: {d['high_60d']})")
        elif d["current_price"] >= d["high_20d"] * 0.99:
            score += 20
            reasons.append(f"20日高値圏 (高値: {d['high_20d']})")
        else:
            continue

        if d["vol_ratio"] > 2.0:
            score += 25
            reasons.append(f"出来高急増 ({d['vol_ratio']}倍)")
        elif d["vol_ratio"] > 1.3:
            score += 15
            reasons.append(f"出来高増加 ({d['vol_ratio']}倍)")

        if d["rsi"] > 50 and d["rsi"] < 75:
            score += 10
            reasons.append(f"RSIが適度な強さ ({d['rsi']})")

        if d["ret_5d"] > 3:
            score += 10
            reasons.append(f"5日間で+{d['ret_5d']}%上昇")

        if score >= 30:
            results.append({**d, "strategy": "breakout", "score": score, "reasons": reasons})

    return sorted(results, key=lambda x: x["score"], reverse=True)


def screen_value(data: list[dict]) -> list[dict]:
    """バリュー銘柄: PER低 + PBR低 + 配当利回り高"""
    results = []
    for d in data:
        score = 0
        reasons = []

        if d["pe"] is None and d["pb"] is None:
            continue

        if d["pe"] is not None:
            if 0 < d["pe"] < 8:
                score += 25
                reasons.append(f"PER非常に割安 ({d['pe']})")
            elif 0 < d["pe"] < 12:
                score += 15
                reasons.append(f"PER割安 ({d['pe']})")
            elif 0 < d["pe"] < 15:
                score += 5
                reasons.append(f"PERやや割安 ({d['pe']})")

        if d["pb"] is not None:
            if 0 < d["pb"] < 0.8:
                score += 20
                reasons.append(f"PBR非常に割安 ({d['pb']})")
            elif 0 < d["pb"] < 1.0:
                score += 10
                reasons.append(f"PBR割安 ({d['pb']})")

        if d["div_yield"] is not None:
            if d["div_yield"] > 4:
                score += 20
                reasons.append(f"高配当 ({d['div_yield']}%)")
            elif d["div_yield"] > 3:
                score += 10
                reasons.append(f"配当利回り良好 ({d['div_yield']}%)")

        # RSI が中立〜やや低めだとさらに加点
        if d["rsi"] < 45:
            score += 5
            reasons.append(f"RSIも低め ({d['rsi']})")

        if score >= 20:
            results.append({**d, "strategy": "value", "score": score, "reasons": reasons})

    return sorted(results, key=lambda x: x["score"], reverse=True)


def format_result(item: dict) -> dict:
    """出力用に整形"""
    return {
        "ticker": item["ticker"],
        "name": item["name"],
        "strategy": item["strategy"],
        "score": item["score"],
        "current_price": item["current_price"],
        "currency": item["currency"],
        "reasons": item["reasons"],
        "key_metrics": {
            "RSI": item["rsi"],
            "MACD_hist": item["macd_hist"],
            "BB位置": f"{item['bb_pct']:.1%}",
            "出来高倍率": item["vol_ratio"],
            "1日リターン": f"{item['ret_1d']}%",
            "5日リターン": f"{item['ret_5d']}%",
            "20日リターン": f"{item['ret_20d']}%",
            "年率ボラ": f"{item['ann_vol']}%",
            "PER": item["pe"],
            "PBR": item["pb"],
            "配当利回り": f"{item['div_yield']}%" if item["div_yield"] else None,
        },
    }


def main():
    parser = argparse.ArgumentParser(description="株式スクリーナー")
    parser.add_argument("--market", choices=["us", "jp", "all"], default="all",
                        help="スキャン対象市場")
    parser.add_argument("--strategy", choices=["oversold", "momentum", "breakout", "value", "all"],
                        default="all", help="スクリーニング戦略")
    parser.add_argument("--top", type=int, default=5, help="各戦略の上位N件を表示")
    parser.add_argument("--universe", choices=["default", "expanded"], default="default",
                        help="ユニバースサイズ (expanded: S&P500+日経225全銘柄)")
    args = parser.parse_args()

    # ユニバース選択
    universe = []
    if args.market in ("us", "all"):
        universe += US_UNIVERSE
        if args.universe == "expanded":
            universe += US_EXPANDED
    if args.market in ("jp", "all"):
        universe += JP_UNIVERSE
        if args.universe == "expanded":
            universe += JP_EXPANDED
    # 重複除去
    universe = list(dict.fromkeys(universe))

    print(f"スキャン中... {len(universe)} 銘柄を分析しています", file=sys.stderr)

    # 並列でデータ取得
    results = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(analyze_single, t): t for t in universe}
        done = 0
        for future in as_completed(futures):
            done += 1
            if done % 20 == 0:
                print(f"  進捗: {done}/{len(universe)}", file=sys.stderr)
            result = future.result()
            if result:
                results.append(result)

    print(f"データ取得完了: {len(results)}/{len(universe)} 銘柄", file=sys.stderr)

    # スクリーニング実行
    strategies = {
        "oversold": screen_oversold,
        "momentum": screen_momentum,
        "breakout": screen_breakout,
        "value": screen_value,
    }

    output = {}
    if args.strategy == "all":
        for name, func in strategies.items():
            hits = func(results)
            output[name] = [format_result(h) for h in hits[:args.top]]
    else:
        hits = strategies[args.strategy](results)
        output[args.strategy] = [format_result(h) for h in hits[:args.top]]

    # サマリー
    total_hits = sum(len(v) for v in output.values())
    summary = {
        "scan_universe": len(universe),
        "data_obtained": len(results),
        "total_hits": total_hits,
        "strategies": {},
    }
    for name, hits in output.items():
        summary["strategies"][name] = {
            "count": len(hits),
            "top_picks": [h["ticker"] for h in hits],
        }

    final = {
        "summary": summary,
        "results": output,
    }

    print(json.dumps(final, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""ファンダメンタル分析スクリプト

Usage: python scripts/fundamentals.py <ticker>

yfinance から決算データ・財務諸表を取得し、成長性・収益性・財務健全性を評価する。
"""
import argparse
import json
import sys

import yfinance as yf


def analyze_fundamentals(ticker: str):
    t = yf.Ticker(ticker)
    info = t.info

    result = {
        "ticker": ticker,
        "name": info.get("shortName", ""),
        "currency": info.get("currency", ""),
        "sector": info.get("sector", ""),
        "industry": info.get("industry", ""),
    }

    # ---- バリュエーション ----
    valuation = {
        "PER": info.get("trailingPE"),
        "予想PER": info.get("forwardPE"),
        "PBR": info.get("priceToBook"),
        "PSR": info.get("priceToSalesTrailing12Months"),
        "EV/EBITDA": info.get("enterpriseToEbitda"),
        "時価総額": info.get("marketCap"),
        "企業価値(EV)": info.get("enterpriseValue"),
    }
    result["valuation"] = {k: round(v, 2) if isinstance(v, float) else v
                           for k, v in valuation.items() if v is not None}

    # ---- 収益性 ----
    profitability = {
        "粗利益率": info.get("grossMargins"),
        "営業利益率": info.get("operatingMargins"),
        "純利益率": info.get("profitMargins"),
        "ROE": info.get("returnOnEquity"),
        "ROA": info.get("returnOnAssets"),
    }
    result["profitability"] = {k: f"{round(v * 100, 1)}%" for k, v in profitability.items()
                               if v is not None}

    # ---- 成長性 ----
    growth = {
        "売上成長率": info.get("revenueGrowth"),
        "利益成長率": info.get("earningsGrowth"),
        "売上(TTM)": info.get("totalRevenue"),
        "EBITDA": info.get("ebitda"),
    }
    result["growth"] = {}
    for k, v in growth.items():
        if v is None:
            continue
        if isinstance(v, float) and abs(v) < 10:
            result["growth"][k] = f"{round(v * 100, 1)}%"
        else:
            result["growth"][k] = v

    # ---- 財務健全性 ----
    health = {
        "総負債": info.get("totalDebt"),
        "総現金": info.get("totalCash"),
        "負債/資本比率": info.get("debtToEquity"),
        "流動比率": info.get("currentRatio"),
        "フリーCF": info.get("freeCashflow"),
        "営業CF": info.get("operatingCashflow"),
    }
    result["financial_health"] = {k: round(v, 2) if isinstance(v, float) else v
                                  for k, v in health.items() if v is not None}

    # ---- 配当 ----
    dividend = {
        "配当利回り": info.get("dividendYield"),
        "配当性向": info.get("payoutRatio"),
        "年間配当": info.get("dividendRate"),
    }
    result["dividend"] = {}
    for k, v in dividend.items():
        if v is None:
            continue
        if k in ("配当利回り", "配当性向"):
            result["dividend"][k] = f"{round(v * 100, 1)}%"
        else:
            result["dividend"][k] = round(v, 2)

    # ---- アナリスト予想 ----
    analyst = {
        "目標株価(平均)": info.get("targetMeanPrice"),
        "目標株価(高)": info.get("targetHighPrice"),
        "目標株価(低)": info.get("targetLowPrice"),
        "推奨": info.get("recommendationKey"),
        "推奨スコア": info.get("recommendationMean"),
        "アナリスト数": info.get("numberOfAnalystOpinions"),
    }
    result["analyst"] = {k: v for k, v in analyst.items() if v is not None}

    # ---- 決算サプライズ（直近4四半期）----
    try:
        earnings_dates = t.earnings_dates
        if earnings_dates is not None and not earnings_dates.empty:
            surprises = []
            for idx, row in earnings_dates.head(4).iterrows():
                eps_est = row.get("EPS Estimate")
                eps_act = row.get("Reported EPS")
                if eps_est is not None and eps_act is not None:
                    surprise_pct = ((eps_act - eps_est) / abs(eps_est) * 100
                                    if eps_est != 0 else 0)
                    surprises.append({
                        "date": str(idx.date()) if hasattr(idx, 'date') else str(idx),
                        "EPS予想": round(float(eps_est), 3),
                        "EPS実績": round(float(eps_act), 3),
                        "サプライズ": f"{round(surprise_pct, 1)}%",
                    })
            if surprises:
                result["earnings_surprise"] = surprises
    except Exception:
        pass

    # ---- ファンダメンタルスコア ----
    score = 0
    reasons = []

    # PER 評価
    pe = info.get("trailingPE")
    if pe is not None and pe > 0:
        if pe < 10:
            score += 15
            reasons.append(f"PER割安({pe:.1f})")
        elif pe < 15:
            score += 10
            reasons.append(f"PER適正〜割安({pe:.1f})")
        elif pe < 25:
            score += 0
        elif pe < 40:
            score -= 5
            reasons.append(f"PERやや割高({pe:.1f})")
        else:
            score -= 10
            reasons.append(f"PER割高({pe:.1f})")

    # ROE 評価
    roe = info.get("returnOnEquity")
    if roe is not None:
        if roe > 0.20:
            score += 15
            reasons.append(f"ROE高い({roe:.0%})")
        elif roe > 0.10:
            score += 5
            reasons.append(f"ROE良好({roe:.0%})")
        elif roe < 0:
            score -= 10
            reasons.append(f"ROEマイナス({roe:.0%})")

    # 売上成長率
    rev_growth = info.get("revenueGrowth")
    if rev_growth is not None:
        if rev_growth > 0.20:
            score += 15
            reasons.append(f"売上高成長率({rev_growth:.0%})")
        elif rev_growth > 0.05:
            score += 5
        elif rev_growth < 0:
            score -= 5
            reasons.append(f"売上減少({rev_growth:.0%})")

    # 負債/資本比率
    de_ratio = info.get("debtToEquity")
    if de_ratio is not None:
        if de_ratio < 50:
            score += 5
            reasons.append("低負債")
        elif de_ratio > 200:
            score -= 10
            reasons.append(f"高負債(D/E={de_ratio:.0f})")

    # フリーCF
    fcf = info.get("freeCashflow")
    if fcf is not None:
        if fcf > 0:
            score += 5
            reasons.append("FCFプラス")
        else:
            score -= 5
            reasons.append("FCFマイナス")

    # 配当利回り
    div_y = info.get("dividendYield")
    if div_y is not None and div_y > 0.03:
        score += 5
        reasons.append(f"高配当({div_y:.1%})")

    # アナリスト推奨
    rec = info.get("recommendationMean")
    if rec is not None:
        if rec <= 2.0:
            score += 10
            reasons.append(f"アナリストBuy推奨({rec:.1f})")
        elif rec <= 2.5:
            score += 5
        elif rec >= 3.5:
            score -= 5
            reasons.append(f"アナリストSell寄り({rec:.1f})")

    result["fundamental_score"] = {
        "score": score,
        "max_score": 70,
        "reasons": reasons,
    }

    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="ファンダメンタル分析")
    parser.add_argument("ticker", help="ティッカーシンボル (例: 7203.T, AAPL)")
    args = parser.parse_args()
    analyze_fundamentals(args.ticker)


if __name__ == "__main__":
    main()

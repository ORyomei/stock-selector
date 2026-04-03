#!/usr/bin/env python3
"""総合スコアリング＆売買判断スクリプト

Usage: python scripts/scorer.py <ticker> [--period 6mo]

テクニカル指標・価格動向・ボラティリティから総合スコアを算出し、
具体的なタイムスパン・目標価格・確率を出力する。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time

import numpy as np
import ta
import yfinance as yf


def compute_score(ticker: str, period: str = "6mo"):
    hist = None
    info = {}
    for attempt in range(3):
        try:
            t = yf.Ticker(ticker)
            info = t.info
            hist = t.history(period=period, interval="1d")
            if hist is not None and not hist.empty and len(hist) >= 30:
                break
        except Exception as e:
            print(f"WARN: {ticker} 取得失敗 (attempt {attempt + 1}): {e}", file=sys.stderr)
        if attempt < 2:
            time.sleep(1.0 * (attempt + 1))

    if hist is None or hist.empty or len(hist) < 30:
        print(f"ERROR: {ticker} のデータ不足（最低30日分が必要）", file=sys.stderr)
        sys.exit(1)

    close = hist["Close"]
    high = hist["High"]
    low = hist["Low"]
    volume = hist["Volume"]
    current = float(close.iloc[-1])

    # ---- テクニカル指標 ----
    rsi = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
    macd_ind = ta.trend.MACD(close)
    macd_line = macd_ind.macd().iloc[-1]
    macd_ind.macd_signal().iloc[-1]
    macd_hist = macd_ind.macd_diff().iloc[-1]

    bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)
    bb_upper = bb.bollinger_hband().iloc[-1]
    bb_lower = bb.bollinger_lband().iloc[-1]
    bb.bollinger_mavg().iloc[-1]
    bb_pct = (current - bb_lower) / (bb_upper - bb_lower) if (bb_upper - bb_lower) > 0 else 0.5

    sma_5 = close.rolling(5).mean().iloc[-1]
    sma_25 = close.rolling(25).mean().iloc[-1]
    sma_75 = close.rolling(75).mean().iloc[-1] if len(close) >= 75 else sma_25
    close.ewm(span=12).mean().iloc[-1]
    close.ewm(span=26).mean().iloc[-1]

    # ---- ボラティリティ (20日) ----
    daily_returns = close.pct_change().dropna()
    vol_20d = float(daily_returns.tail(20).std())
    annualized_vol = vol_20d * math.sqrt(252)

    # ---- ATR (14日) ----
    atr_ind = ta.volatility.AverageTrueRange(high, low, close, window=14)
    atr = float(atr_ind.average_true_range().iloc[-1])

    # ---- 出来高トレンド ----
    vol_sma_20 = float(volume.rolling(20).mean().iloc[-1])
    vol_ratio = float(volume.iloc[-1]) / vol_sma_20 if vol_sma_20 > 0 else 1.0

    # ---- リターン計算 ----
    ret_1d = float(daily_returns.iloc[-1]) if len(daily_returns) >= 1 else 0
    ret_5d = float(close.iloc[-1] / close.iloc[-6] - 1) if len(close) >= 6 else 0
    ret_20d = float(close.iloc[-1] / close.iloc[-21] - 1) if len(close) >= 21 else 0

    # ============================================
    # スコアリング (-100 〜 +100)
    # 正 = 買いシグナル、負 = 売りシグナル
    # ============================================
    scores = {}

    # 1) RSI スコア (-30 〜 +30)
    if rsi <= 20:
        scores["rsi"] = 30
    elif rsi <= 30:
        scores["rsi"] = 20
    elif rsi <= 40:
        scores["rsi"] = 10
    elif rsi <= 60:
        scores["rsi"] = 0
    elif rsi <= 70:
        scores["rsi"] = -10
    elif rsi <= 80:
        scores["rsi"] = -20
    else:
        scores["rsi"] = -30

    # 2) MACD スコア (-25 〜 +25)
    if macd_hist > 0:
        if macd_line > 0:
            scores["macd"] = 25  # 強い買い
        else:
            scores["macd"] = 15  # ゴールデンクロスだが水面下
    else:
        if macd_line < 0:
            scores["macd"] = -25  # 強い売り
        else:
            scores["macd"] = -15  # デッドクロスだが水面上

    # 3) ボリンジャーバンド位置 (-15 〜 +15)
    if bb_pct <= 0:
        scores["bb"] = 15  # 下限割れ = 反発期待
    elif bb_pct <= 0.2:
        scores["bb"] = 10
    elif bb_pct <= 0.4:
        scores["bb"] = 5
    elif bb_pct <= 0.6:
        scores["bb"] = 0
    elif bb_pct <= 0.8:
        scores["bb"] = -5
    elif bb_pct <= 1.0:
        scores["bb"] = -10
    else:
        scores["bb"] = -15  # 上限突破 = 過熱

    # 4) 移動平均トレンド (-20 〜 +20)
    trend_score = 0
    if sma_5 > sma_25:
        trend_score += 10
    else:
        trend_score -= 10
    if current > sma_75:
        trend_score += 10
    else:
        trend_score -= 10
    scores["trend"] = trend_score

    # 5) 出来高 (-10 〜 +10)
    # 下落中の出来高増 = 売り加速、上昇中の出来高増 = 買い加速
    if vol_ratio > 1.5:
        scores["volume"] = 10 if ret_1d > 0 else -10
    elif vol_ratio > 1.0:
        scores["volume"] = 5 if ret_1d > 0 else -5
    else:
        scores["volume"] = 0

    total_score = sum(scores.values())

    # ============================================
    # 確率推定（過去データの統計的アプローチ）
    # ============================================
    # 過去のリターン分布から将来のリターン確率を推定
    returns_series = daily_returns.values

    def estimate_probability(days: int, threshold_pct: float) -> float:
        """days日後にthreshold_pct%以上上昇している確率を推定（モンテカルロ風）"""
        if len(returns_series) < 20:
            return 50.0
        mu = float(np.mean(returns_series)) * days
        sigma = float(np.std(returns_series)) * math.sqrt(days)
        if sigma == 0:
            return 50.0
        # 正規分布近似で確率を計算
        z = (threshold_pct / 100 - mu) / sigma
        # 標準正規分布の上側確率
        prob_above = 1 - _norm_cdf(z)
        return round(prob_above * 100, 1)

    def _norm_cdf(x: float) -> float:
        """標準正規分布の累積分布関数（近似）"""
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))

    # 具体的な予測
    prob_up_5d_3pct = estimate_probability(5, 3.0)
    prob_up_5d_0pct = estimate_probability(5, 0.0)
    prob_up_20d_5pct = estimate_probability(20, 5.0)
    prob_up_20d_0pct = estimate_probability(20, 0.0)
    prob_up_60d_10pct = estimate_probability(60, 10.0)
    prob_up_60d_0pct = estimate_probability(60, 0.0)

    prob_down_5d_3pct = round(100 - estimate_probability(5, -3.0), 1)
    prob_down_20d_5pct = round(100 - estimate_probability(20, -5.0), 1)

    # ============================================
    # 目標価格の算出
    # ============================================
    # ATRベースの目標価格帯
    target_upper_short = round(current + atr * 2, 2)  # 短期上値目標
    target_lower_short = round(current - atr * 2, 2)  # 短期下値目標
    target_upper_mid = round(current + atr * 4, 2)  # 中期上値目標
    target_lower_mid = round(current - atr * 4, 2)  # 中期下値目標

    # サポート/レジスタンス（直近の高値安値）
    recent_high = float(high.tail(20).max())
    recent_low = float(low.tail(20).min())
    support_1 = round(recent_low, 2)
    resistance_1 = round(recent_high, 2)

    # ============================================
    # 推奨アクション & タイムスパン
    # ============================================
    if total_score >= 40:
        action = "強い買い"
        confidence = "高"
    elif total_score >= 20:
        action = "買い"
        confidence = "中〜高"
    elif total_score >= 5:
        action = "やや買い"
        confidence = "中"
    elif total_score >= -5:
        action = "様子見"
        confidence = "低"
    elif total_score >= -20:
        action = "やや売り"
        confidence = "中"
    elif total_score >= -40:
        action = "売り"
        confidence = "中〜高"
    else:
        action = "強い売り"
        confidence = "高"

    # タイムスパン推奨
    timeframes = []
    if abs(total_score) >= 30 and vol_ratio > 1.2:
        timeframes.append(
            {
                "span": "短期（1-5営業日）",
                "reason": "強いシグナル＋出来高増加で短期的な動きが期待される",
            }
        )
    if abs(total_score) >= 15:
        timeframes.append(
            {
                "span": "スイング（1-3週間）",
                "reason": "テクニカル指標の方向性が明確",
            }
        )
    if current < sma_75 * 0.9 or current > sma_75 * 1.1:
        timeframes.append(
            {
                "span": "中期（1-3ヶ月）",
                "reason": f"75日SMAとの乖離が大きい（乖離率: {round((current / sma_75 - 1) * 100, 1)}%）",
            }
        )
    if not timeframes:
        timeframes.append(
            {
                "span": "様子見（明確なエントリーポイントを待つ）",
                "reason": "シグナルが弱く、方向性が不明確",
            }
        )

    # エントリーポイント
    entry_points = []
    if total_score > 0:
        entry_points.append(
            {
                "type": "指値買い",
                "price": round(max(support_1, current - atr), 2),
                "reason": "直近サポートまたはATR1本分の押し目",
            }
        )
        entry_points.append(
            {
                "type": "逆指値買い（ブレイクアウト）",
                "price": round(resistance_1 * 1.005, 2),
                "reason": "直近高値超えで上昇モメンタム確認後エントリー",
            }
        )
    elif total_score < 0:
        entry_points.append(
            {
                "type": "指値売り",
                "price": round(min(resistance_1, current + atr), 2),
                "reason": "直近レジスタンスまたはATR1本分の戻り",
            }
        )

    # 損切り・利確
    if total_score > 0:
        stop_loss = round(current - atr * 2.5, 2)
        take_profit_1 = round(current + atr * 2, 2)
        take_profit_2 = round(current + atr * 4, 2)
    else:
        stop_loss = round(current + atr * 2.5, 2)
        take_profit_1 = round(current - atr * 2, 2)
        take_profit_2 = round(current - atr * 4, 2)

    result = {
        "ticker": ticker,
        "name": info.get("shortName", ""),
        "currency": info.get("currency", ""),
        "current_price": current,
        "analysis_summary": {
            "total_score": total_score,
            "score_breakdown": scores,
            "action": action,
            "confidence": confidence,
        },
        "probability": {
            "5日後に上昇": f"{prob_up_5d_0pct}%",
            "5日後に+3%以上": f"{prob_up_5d_3pct}%",
            "5日後に-3%以下": f"{prob_down_5d_3pct}%",
            "20日後に上昇": f"{prob_up_20d_0pct}%",
            "20日後に+5%以上": f"{prob_up_20d_5pct}%",
            "20日後に-5%以下": f"{prob_down_20d_5pct}%",
            "60日後に上昇": f"{prob_up_60d_0pct}%",
            "60日後に+10%以上": f"{prob_up_60d_10pct}%",
        },
        "price_targets": {
            "短期上値目標（ATR×2）": target_upper_short,
            "短期下値目標（ATR×2）": target_lower_short,
            "中期上値目標（ATR×4）": target_upper_mid,
            "中期下値目標（ATR×4）": target_lower_mid,
            "直近サポート（20日安値）": support_1,
            "直近レジスタンス（20日高値）": resistance_1,
        },
        "recommended_timeframes": timeframes,
        "entry_points": entry_points,
        "risk_management": {
            "損切りライン": stop_loss,
            "利確目標1（ATR×2）": take_profit_1,
            "利確目標2（ATR×4）": take_profit_2,
            "リスクリワード比": round(abs(take_profit_1 - current) / abs(current - stop_loss), 2)
            if abs(current - stop_loss) > 0
            else 0,
        },
        "volatility": {
            "20日ボラティリティ（日次）": f"{round(vol_20d * 100, 2)}%",
            "年率換算ボラティリティ": f"{round(annualized_vol * 100, 1)}%",
            "ATR（14日）": round(atr, 2),
            "出来高比率（vs 20日平均）": round(vol_ratio, 2),
        },
        "returns": {
            "1日リターン": f"{round(ret_1d * 100, 2)}%",
            "5日リターン": f"{round(ret_5d * 100, 2)}%",
            "20日リターン": f"{round(ret_20d * 100, 2)}%",
        },
    }

    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="総合スコアリング＆売買判断")
    parser.add_argument("ticker", help="ティッカーシンボル (例: 7203.T, AAPL)")
    parser.add_argument("--period", default="6mo", help="分析期間")
    args = parser.parse_args()
    compute_score(args.ticker, args.period)


if __name__ == "__main__":
    main()

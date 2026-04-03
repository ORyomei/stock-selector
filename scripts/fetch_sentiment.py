#!/usr/bin/env python3
"""センチメント分析スクリプト（高度版）

Usage: python scripts/fetch_sentiment.py <query> [--limit 20]

重み付き辞書＋否定表現検出＋多言語対応で精度の高いセンチメント分析を行う。
Google News の日本語・英語両方のヘッドラインを統合して分析。
"""

import argparse
import json
import re
import sys
from urllib.parse import quote

import feedparser

# ---- 重み付きセンチメント辞書 ----
POSITIVE_DICT = {
    # 日本語 (重み: 1=弱, 2=中, 3=強)
    "急騰": 3,
    "暴騰": 3,
    "ストップ高": 3,
    "上方修正": 3,
    "過去最高": 3,
    "上昇": 2,
    "好調": 2,
    "増収": 2,
    "増益": 2,
    "回復": 2,
    "成長": 2,
    "買い": 2,
    "強気": 2,
    "好材料": 2,
    "最高値": 2,
    "反発": 2,
    "堅調": 1,
    "底堅い": 1,
    "出遅れ": 1,
    "割安": 1,
    "高配当": 1,
    "目標株価引き上げ": 3,
    "格上げ": 3,
    "増配": 2,
    # 英語
    "surge": 3,
    "soar": 3,
    "skyrocket": 3,
    "upgrade": 3,
    "rally": 2,
    "gain": 2,
    "rise": 2,
    "bullish": 2,
    "growth": 2,
    "profit": 2,
    "beat": 2,
    "outperform": 2,
    "buy": 2,
    "recover": 1,
    "rebound": 1,
    "steady": 1,
    "upside": 2,
}

NEGATIVE_DICT = {
    # 日本語
    "急落": 3,
    "暴落": 3,
    "ストップ安": 3,
    "下方修正": 3,
    "債務超過": 3,
    "下落": 2,
    "低迷": 2,
    "減収": 2,
    "減益": 2,
    "懸念": 2,
    "リスク": 1,
    "売り": 2,
    "弱気": 2,
    "悪材料": 2,
    "赤字": 2,
    "損失": 2,
    "不振": 2,
    "警戒": 1,
    "調整": 1,
    "目標株価引き下げ": 3,
    "格下げ": 3,
    "減配": 2,
    "無配": 2,
    # 英語
    "crash": 3,
    "plunge": 3,
    "plummet": 3,
    "downgrade": 3,
    "fall": 2,
    "drop": 2,
    "decline": 2,
    "bearish": 2,
    "loss": 2,
    "miss": 2,
    "underperform": 2,
    "sell": 2,
    "fear": 2,
    "risk": 1,
    "concern": 1,
    "weak": 1,
    "slump": 2,
    "recession": 2,
}

# 否定表現: これらの直後のセンチメントを反転させる
NEGATION_PATTERNS_JA = ["ない", "ず", "止まる", "止まった", "鈍化", "頭打ち", "一服"]
NEGATION_PATTERNS_EN = [
    r"\bnot\b",
    r"\bno\b",
    r"\bn't\b",
    r"\bfails?\b",
    r"\bstops?\b",
    r"\bdespite\b",
    r"\bhalts?\b",
    r"\bslows?\b",
]


def _score_text(text: str) -> tuple[float, str]:
    """テキストのセンチメントスコアを算出。
    戻り値: (スコア(-1.0〜+1.0), ラベル)
    """
    text_lower = text.lower()

    pos_score = 0
    neg_score = 0

    for word, weight in POSITIVE_DICT.items():
        if word in text_lower:
            pos_score += weight
    for word, weight in NEGATIVE_DICT.items():
        if word in text_lower:
            neg_score += weight

    # 否定表現チェック（ポジティブ語の近くに否定があればスコアを反転）
    has_negation = False
    for neg in NEGATION_PATTERNS_JA:
        if neg in text_lower:
            has_negation = True
            break
    if not has_negation:
        for neg_pat in NEGATION_PATTERNS_EN:
            if re.search(neg_pat, text_lower):
                has_negation = True
                break

    if has_negation and pos_score > neg_score:
        # 「上昇が止まった」→ ポジティブを減らしネガティブ寄りに
        pos_score, neg_score = neg_score, pos_score

    total = pos_score + neg_score
    if total == 0:
        return 0.0, "neutral"

    score = (pos_score - neg_score) / total  # -1.0 〜 +1.0

    if score > 0.2:
        label = "positive"
    elif score < -0.2:
        label = "negative"
    else:
        label = "neutral"

    return round(score, 3), label


def analyze_sentiment(texts: list[str]) -> dict:
    """重み付きセンチメント分析"""
    positive = 0
    negative = 0
    neutral = 0
    scores = []
    details = []

    for text in texts:
        score, label = _score_text(text)
        scores.append(score)

        if label == "positive":
            positive += 1
        elif label == "negative":
            negative += 1
        else:
            neutral += 1

        details.append(
            {
                "text": text[:120],
                "sentiment": label,
                "score": score,
            }
        )

    total = len(texts) if texts else 1
    avg_score = round(sum(scores) / total, 3) if scores else 0.0

    return {
        "total": len(texts),
        "positive_pct": round(positive / total * 100, 1),
        "negative_pct": round(negative / total * 100, 1),
        "neutral_pct": round(neutral / total * 100, 1),
        "avg_score": avg_score,
        "details": details[:10],
    }


def fetch_from_news(query: str, limit: int) -> list[str]:
    """Google News RSS から日英両方のヘッドラインを取得"""
    texts = []
    for hl, gl, ceid in [("ja", "JP", "JP:ja"), ("en", "US", "US:en")]:
        encoded = quote(query)
        url = f"https://news.google.com/rss/search?q={encoded}&hl={hl}&gl={gl}&ceid={ceid}"
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:limit]:
                title = entry.get("title", "")
                if title:
                    texts.append(title)
        except Exception:
            continue
    return texts


def main():
    parser = argparse.ArgumentParser(description="センチメント分析")
    parser.add_argument("query", help="検索クエリ")
    parser.add_argument("--limit", type=int, default=20, help="各言語の取得件数")
    args = parser.parse_args()

    texts = fetch_from_news(args.query, args.limit)

    if not texts:
        print(f"テキストが取得できませんでした: {args.query}", file=sys.stderr)
        sys.exit(1)

    result = analyze_sentiment(texts)
    result["query"] = args.query
    result["source"] = "google_news_headlines"

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""SNS センチメント分析スクリプト

Usage: python scripts/fetch_sentiment.py <query> [--limit 20]

注意: X (Twitter) API キーが .env に設定されていない場合は、
Google News のヘッドラインからセンチメント推定を行うフォールバックモードで動作する。
"""
import argparse
import json
import os
import sys
from urllib.parse import quote

import feedparser
from dotenv import load_dotenv

load_dotenv()


def analyze_sentiment_basic(texts: list[str]) -> dict:
    """キーワードベースの簡易センチメント分析"""
    positive_words = [
        "上昇", "好調", "増収", "増益", "最高", "急騰", "回復", "成長",
        "rise", "surge", "gain", "rally", "bullish", "growth", "profit",
        "買い", "強気", "好材料", "上方修正",
    ]
    negative_words = [
        "下落", "低迷", "減収", "減益", "急落", "暴落", "懸念", "リスク",
        "fall", "drop", "decline", "bearish", "loss", "crash", "fear",
        "売り", "弱気", "悪材料", "下方修正",
    ]

    positive = 0
    negative = 0
    neutral = 0

    details = []
    for text in texts:
        text_lower = text.lower()
        pos = sum(1 for w in positive_words if w in text_lower)
        neg = sum(1 for w in negative_words if w in text_lower)

        if pos > neg:
            sentiment = "positive"
            positive += 1
        elif neg > pos:
            sentiment = "negative"
            negative += 1
        else:
            sentiment = "neutral"
            neutral += 1

        details.append({"text": text[:100], "sentiment": sentiment})

    total = len(texts) if texts else 1
    return {
        "total": len(texts),
        "positive_pct": round(positive / total * 100, 1),
        "negative_pct": round(negative / total * 100, 1),
        "neutral_pct": round(neutral / total * 100, 1),
        "details": details[:10],
    }


def fetch_from_news(query: str, limit: int) -> list[str]:
    """Google News RSS からヘッドラインを取得してテキストとして返す"""
    encoded = quote(query)
    url = f"https://news.google.com/rss/search?q={encoded}&hl=ja&gl=JP&ceid=JP:ja"
    feed = feedparser.parse(url)
    return [entry.get("title", "") for entry in feed.entries[:limit]]


def main():
    parser = argparse.ArgumentParser(description="センチメント分析")
    parser.add_argument("query", help="検索クエリ")
    parser.add_argument("--limit", type=int, default=20, help="取得件数")
    args = parser.parse_args()

    # X API が使えない場合はニュースヘッドラインで代替
    texts = fetch_from_news(args.query, args.limit)

    if not texts:
        print(f"テキストが取得できませんでした: {args.query}", file=sys.stderr)
        sys.exit(1)

    result = analyze_sentiment_basic(texts)
    result["query"] = args.query
    result["source"] = "google_news_headlines"

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

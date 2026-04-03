#!/usr/bin/env python3
"""ニュース取得スクリプト

Usage: python scripts/fetch_news.py <query> [--lang ja] [--limit 10]

Google News RSS を使ってニュースを取得する。
"""
import argparse
import json
import sys
from urllib.parse import quote

import time
import feedparser


def fetch_news(query: str, lang: str = "ja", limit: int = 10, retries: int = 2):
    encoded = quote(query)
    url = f"https://news.google.com/rss/search?q={encoded}&hl={lang}&gl=JP&ceid=JP:{lang}"
    if lang == "en":
        url = f"https://news.google.com/rss/search?q={encoded}&hl=en&gl=US&ceid=US:en"

    feed = None
    for attempt in range(retries + 1):
        try:
            feed = feedparser.parse(url)
            if feed.entries:
                break
        except Exception as e:
            print(f"WARN: RSS取得失敗 (attempt {attempt + 1}): {e}", file=sys.stderr)
        if attempt < retries:
            time.sleep(1.0 * (attempt + 1))

    if not feed or not feed.entries:
        print(f"ニュースが見つかりませんでした: {query}", file=sys.stderr)
        sys.exit(1)

    articles = []
    for entry in feed.entries[:limit]:
        articles.append({
            "title": entry.get("title", ""),
            "link": entry.get("link", ""),
            "published": entry.get("published", ""),
            "source": entry.get("source", {}).get("title", "") if hasattr(entry, "source") else "",
        })

    result = {
        "query": query,
        "count": len(articles),
        "articles": articles,
    }

    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="ニュース取得")
    parser.add_argument("query", help="検索クエリ")
    parser.add_argument("--lang", default="ja", help="言語 (ja/en)")
    parser.add_argument("--limit", type=int, default=10, help="取得件数")
    args = parser.parse_args()
    fetch_news(args.query, args.lang, args.limit)


if __name__ == "__main__":
    main()

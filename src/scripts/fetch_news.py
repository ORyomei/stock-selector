#!/usr/bin/env python3
"""ニュース取得スクリプト

Usage: python scripts/fetch_news.py <query> [--lang ja] [--limit 10]

Google News RSS を使ってニュースを取得する。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = SRC_DIR.parent
sys.path.insert(0, str(SRC_DIR))

from infra.container import get_container


def fetch_news(query: str, lang: str = "ja", limit: int = 10):
    container = get_container()
    news_repo = container.news()

    articles = news_repo.fetch_headlines(query, lang=lang, limit=limit)

    if not articles:
        print(f"ニュースが見つかりませんでした: {query}", file=sys.stderr)
        sys.exit(1)

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

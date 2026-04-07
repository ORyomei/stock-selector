"""Google News RSS を使った NewsRepository 実装。"""

from __future__ import annotations

import sys
import time
from urllib.parse import quote

import feedparser

from interfaces.repositories.news import NewsRepository


class GoogleNewsRepository(NewsRepository):
    """Google News RSS フィードからヘッドラインを取得する。"""

    def __init__(self, retries: int = 2) -> None:
        self._retries = retries

    def fetch_headlines(
        self,
        query: str,
        lang: str = "ja",
        limit: int = 10,
    ) -> list[dict]:
        encoded = quote(query)
        if lang == "en":
            url = f"https://news.google.com/rss/search?q={encoded}&hl=en&gl=US&ceid=US:en"
        else:
            url = f"https://news.google.com/rss/search?q={encoded}&hl={lang}&gl=JP&ceid=JP:{lang}"

        feed = None
        max_attempts = self._retries + 1
        for attempt in range(max_attempts):
            try:
                feed = feedparser.parse(url)
                if feed.entries:
                    break
            except Exception as e:
                print(
                    f"WARN: RSS取得失敗 (attempt {attempt + 1}): {e}",
                    file=sys.stderr,
                )
            if attempt < max_attempts - 1:
                time.sleep(1.0 * (attempt + 1))

        if not feed or not feed.entries:
            return []

        articles: list[dict] = []
        for entry in feed.entries[:limit]:
            articles.append(
                {
                    "title": entry.get("title", ""),
                    "link": entry.get("link", ""),
                    "published": entry.get("published", ""),
                    "source": entry.get("source", {}).get("title", "")
                    if hasattr(entry, "source")
                    else "",
                }
            )
        return articles

"""News repository interface."""

from __future__ import annotations

from abc import ABC, abstractmethod


class NewsRepository(ABC):
    """ニュースヘッドライン取得を抽象化するリポジトリ。"""

    @abstractmethod
    def fetch_headlines(
        self,
        query: str,
        lang: str = "ja",
        limit: int = 10,
    ) -> list[dict]:
        """ニュースヘッドラインを取得する。

        Returns:
            各要素は {"title", "link", "published", "source"} を持つ dict。
        """

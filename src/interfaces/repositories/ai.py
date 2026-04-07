"""AI repository interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class AIRepository(ABC):
    """LLM 呼び出しを抽象化するリポジトリ。"""

    @abstractmethod
    def completion(
        self,
        prompt: str,
        *,
        system_msg: str = "株式売買判断AI。JSON形式で回答。",
    ) -> str | None:
        """プロンプトを送信しテキスト応答を返す。"""

    @abstractmethod
    def completion_json(
        self,
        prompt: str,
        *,
        system_msg: str = "株式売買判断AI。JSON形式で回答。",
    ) -> dict[str, Any] | None:
        """プロンプトを送信し、応答を JSON としてパースして返す。"""

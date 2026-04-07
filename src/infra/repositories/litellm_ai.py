"""LiteLLM を使った AIRepository 実装。"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import litellm

from interfaces.repositories.ai import AIRepository

litellm.suppress_debug_info = True
litellm.set_verbose = False

AI_PROVIDERS: dict[str, dict[str, Any]] = {
    "copilot": {
        "model": "github_copilot/claude-sonnet-4",
        "token_env": None,
    },
    "github": {
        "model": "github_copilot/gpt-4o",
        "token_env": None,
    },
    "openai": {
        "model": "gpt-4o",
        "token_env": "OPENAI_API_KEY",
    },
    "anthropic": {
        "model": "anthropic/claude-sonnet-4-20250514",
        "token_env": "ANTHROPIC_API_KEY",
    },
}

PROVIDER_NAMES = list(AI_PROVIDERS.keys())


class LiteLLMAIRepository(AIRepository):
    """LiteLLM 経由で LLM API を呼び出す。"""

    def __init__(
        self,
        provider: str = "copilot",
        model: str | None = None,
    ) -> None:
        cfg = AI_PROVIDERS.get(provider, AI_PROVIDERS["copilot"])
        self._model = model or cfg["model"]
        self._token_env = cfg.get("token_env")

    def completion(
        self,
        prompt: str,
        *,
        system_msg: str = "株式売買判断AI。JSON形式で回答。",
    ) -> str | None:
        if self._token_env and not os.environ.get(self._token_env):
            print(f"[error] {self._token_env} not set", file=sys.stderr)
            return None

        try:
            resp = litellm.completion(
                model=self._model,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_tokens=4000,
                timeout=120,
            )
            content = resp.choices[0].message.content
            return content if content else None
        except Exception as e:
            print(
                f"[error] LiteLLM call failed ({self._model}): {e}",
                file=sys.stderr,
            )
            return None

    def completion_json(
        self,
        prompt: str,
        *,
        system_msg: str = "株式売買判断AI。JSON形式で回答。",
    ) -> dict[str, Any] | None:
        text = self.completion(prompt, system_msg=system_msg)
        return parse_ai_json(text)


def parse_ai_json(text: str | None) -> dict[str, Any] | None:
    """AI 応答テキストから JSON を抽出する。"""
    if not text:
        return None
    t = text.strip()

    if "```json" in t:
        t = t.split("```json", 1)[1]
        if "```" in t:
            t = t.split("```", 1)[0]
    elif "```" in t:
        parts = t.split("```")
        if len(parts) >= 3:
            t = parts[1]

    t = t.strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(t[start:end])
            except json.JSONDecodeError:
                pass
    return None

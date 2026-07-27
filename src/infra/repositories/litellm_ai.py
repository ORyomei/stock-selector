"""LiteLLM を使った AIRepository 実装。"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import litellm

from interfaces.repositories.ai import AIRepository

litellm.suppress_debug_info = True

AI_PROVIDERS: dict[str, dict[str, Any]] = {
    "claude_code": {
        # Claude サブスク (Max) の headless モード — 追加クレジット不要・クォータの
        # 崖なし。litellm ではなく claude_code_ai.ClaudeCodeAIRepository が処理する
        "model": "sonnet",
        "token_env": None,
    },
    "anthropic": {
        # Anthropic API 直 (従量課金)。ANTHROPIC_API_KEY + クレジットチャージで有効化
        "model": "anthropic/claude-sonnet-5",
        "token_env": "ANTHROPIC_API_KEY",
    },
    "copilot": {
        # Copilot 定額枠 (フォールバック)。sonnet-4.5 は 1x レートでクォータを
        # 9日で使い切った実績があるため、常用するなら haiku-4.5 (0.33x) を推奨
        "model": "github_copilot/claude-sonnet-4.5",
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
}

PROVIDER_NAMES = list(AI_PROVIDERS.keys())

# temperature 等の sampling パラメータを受け付けないモデル (送ると 400)。
# Claude Sonnet 5 / Opus 4.7+ / Fable 5 は temperature/top_p/top_k が廃止された
_NO_SAMPLING_MODELS = ("claude-sonnet-5", "claude-opus-4-7", "claude-opus-4-8", "claude-fable-5")


def supports_sampling_params(model: str) -> bool:
    """このモデルに temperature 等の sampling パラメータを送ってよいか。"""
    return not any(m in model for m in _NO_SAMPLING_MODELS)


class LiteLLMAIRepository(AIRepository):
    """LiteLLM 経由で LLM API を呼び出す。"""

    def __init__(
        self,
        provider: str = "copilot",  # litellm 系のみ。claude_code は claude_code_ai が担当
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
            call_kwargs: dict[str, Any] = {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 4000,
                "timeout": 120,
            }
            if supports_sampling_params(self._model):
                call_kwargs["temperature"] = 0.2
            resp = litellm.completion(**call_kwargs)
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

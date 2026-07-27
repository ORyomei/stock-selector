"""AI provider abstraction — backward-compat wrapper over container."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure project root is on path so infra can be imported
SRC_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(SRC_DIR))

from infra.repositories.claude_code_ai import create_ai_repository
from infra.repositories.litellm_ai import (
    AI_PROVIDERS,
    PROVIDER_NAMES,
    parse_ai_json,
)


def call_ai(
    prompt: str,
    provider: str = "claude_code",
    model: str | None = None,
    *,
    system_msg: str = "株式売買判断AI。JSON形式で回答。",
) -> str | None:
    """Unified AI API caller — provider に応じた AIRepository に委譲する。"""
    repo = create_ai_repository(provider, model)
    return repo.completion(prompt, system_msg=system_msg)


__all__ = ["AI_PROVIDERS", "PROVIDER_NAMES", "call_ai", "parse_ai_json"]

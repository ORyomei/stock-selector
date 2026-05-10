"""AI provider abstraction — backward-compat wrapper over container."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Ensure project root is on path so infra can be imported
SRC_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(SRC_DIR))

from infra.repositories.litellm_ai import (
    AI_PROVIDERS,
    PROVIDER_NAMES,
    LiteLLMAIRepository,
    parse_ai_json,
)


def call_ai(
    prompt: str,
    provider: str = "copilot",
    model: str | None = None,
    *,
    system_msg: str = "株式売買判断AI。JSON形式で回答。",
) -> str | None:
    """Unified AI API caller — delegates to LiteLLMAIRepository."""
    repo = LiteLLMAIRepository(provider=provider, model=model)
    return repo.completion(prompt, system_msg=system_msg)


__all__ = ["AI_PROVIDERS", "PROVIDER_NAMES", "call_ai", "parse_ai_json"]

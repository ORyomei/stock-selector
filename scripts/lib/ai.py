"""AI provider abstraction for calling LLM APIs."""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent

AI_PROVIDERS: dict[str, dict[str, Any]] = {
    "copilot": {
        "endpoint": None,
        "model": "claude-sonnet-4.6",
        "token_env": None,
    },
    "github": {
        "endpoint": "https://models.inference.ai.azure.com/chat/completions",
        "model": "openai/gpt-4o",
        "token_env": None,
    },
    "openai": {
        "endpoint": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o",
        "token_env": "OPENAI_API_KEY",
    },
    "anthropic": {
        "endpoint": "https://api.anthropic.com/v1/messages",
        "model": "claude-sonnet-4-20250514",
        "token_env": "ANTHROPIC_API_KEY",
    },
}

PROVIDER_NAMES = list(AI_PROVIDERS.keys())


def get_ai_token(provider: str) -> str | None:
    """Resolve an API token for the given provider."""
    cfg = AI_PROVIDERS.get(provider, {})
    env_key = cfg.get("token_env")
    if env_key:
        return os.environ.get(env_key)
    if provider == "github":
        try:
            r = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if r.returncode == 0:
                return r.stdout.strip()
        except Exception:
            pass
        return os.environ.get("GITHUB_TOKEN")
    return None


def call_copilot(prompt: str) -> str | None:
    """Invoke ``gh copilot`` CLI and return the response text."""
    prompt_file: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            f.write(prompt)
            prompt_file = f.name

        flags = (
            '--excluded-tools="shell,read,write,list,search,glob,stat,create,edit"'
            " --no-custom-instructions -s"
        )
        if len(prompt) < 10_000:
            cmd = f"gh copilot -- -p {shlex.quote(prompt)} {flags}"
        else:
            cmd = f"cat {shlex.quote(prompt_file)} | gh copilot -- -p - {flags}"

        result = subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(PROJECT_DIR),
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None
    except Exception:
        return None
    finally:
        if prompt_file:
            with contextlib.suppress(OSError):
                os.unlink(prompt_file)


def call_ai(
    prompt: str,
    provider: str = "copilot",
    model: str | None = None,
    *,
    system_msg: str = "株式売買判断AI。JSON形式で回答。",
) -> str | None:
    """Unified AI API caller.  Dispatches to copilot CLI or REST endpoint."""
    if provider == "copilot":
        return call_copilot(prompt)

    try:
        import requests
    except ImportError:
        print("[error] requests package required for non-copilot providers", file=sys.stderr)
        return None

    token = get_ai_token(provider)
    if not token:
        return None

    cfg = AI_PROVIDERS.get(provider, AI_PROVIDERS["github"])
    use_model = model or cfg["model"]

    try:
        if provider == "anthropic":
            resp = requests.post(
                cfg["endpoint"],
                headers={
                    "x-api-key": token,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json={
                    "model": use_model,
                    "max_tokens": 4000,
                    "temperature": 0.2,
                    "system": system_msg,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=120,
            )
            if resp.status_code != 200:
                return None
            return resp.json()["content"][0]["text"]
        else:
            resp = requests.post(
                cfg["endpoint"],
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": use_model,
                    "temperature": 0.2,
                    "max_tokens": 4000,
                    "messages": [
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=120,
            )
            if resp.status_code != 200:
                return None
            return resp.json()["choices"][0]["message"]["content"]
    except Exception:
        return None


def parse_ai_json(text: str | None) -> dict[str, Any] | None:
    """Extract a JSON object from an AI response that may include markdown fences."""
    if not text:
        return None
    t = text.strip()

    # Strip ```json ... ``` fences
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
        # Fall back: find outermost { ... }
        start, end = t.find("{"), t.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(t[start:end])
            except json.JSONDecodeError:
                pass
    return None

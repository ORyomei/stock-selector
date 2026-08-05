"""ClaudeCodeAIRepository (Claude Agent SDK headless) のテスト。

背景: Copilot 月次クォータ枯渇で AI が8日停止した事故を受け、個人 Claude
サブスク (Max) の headless モードを既定プロバイダにした。当初は `claude -p` の
サブプロセス直叩きだったが、メインエージェントと同じ Claude Agent SDK に統一
した。SDK 経由の呼び出し組み立てとフェイルセーフを検証する。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from infra.repositories.claude_code_ai import (  # noqa: E402
    ClaudeCodeAIRepository,
    create_ai_repository,
)
from infra.repositories.litellm_ai import LiteLLMAIRepository  # noqa: E402


class TextBlock:
    def __init__(self, text: str):
        self.text = text


class AssistantMessage:
    def __init__(self, *texts: str):
        self.content = [TextBlock(t) for t in texts]


class ResultMessage:
    def __init__(self, is_error: bool = False, errors: list[str] | None = None):
        self.subtype = "error" if is_error else "success"
        self.is_error = is_error
        self.errors = errors
        self.total_cost_usd = 0.01
        self.usage = {"input_tokens": 10, "output_tokens": 5}
        self.duration_ms = 1234


def _fake_query(messages: list, captured: dict | None = None):
    """claude_agent_sdk.query を差し替えるフェイク (async ジェネレータ)。"""

    async def _gen(*, prompt, options):
        if captured is not None:
            captured["prompt"] = prompt
            captured["options"] = options
            captured["env_key"] = "ANTHROPIC_API_KEY" in __import__("os").environ
        for m in messages:
            yield m

    return _gen


def _patch_sdk(messages: list, captured: dict | None = None):
    """SDK の遅延 import (`from claude_agent_sdk import ...`) を差し替える。"""
    import claude_agent_sdk

    return patch.object(claude_agent_sdk, "query", _fake_query(messages, captured))


def test_completion_passes_options_and_strips_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-removed")
    captured: dict = {}
    messages = [AssistantMessage("ok"), ResultMessage()]

    with _patch_sdk(messages, captured):
        out = ClaudeCodeAIRepository(model="sonnet").completion("hello", system_msg="SYS")

    assert out == "ok"
    assert captured["prompt"] == "hello"
    opts = captured["options"]
    assert opts.model == "sonnet"
    assert opts.allowed_tools == []  # ツール全面禁止
    assert opts.disallowed_tools  # ビルトインも明示的に禁止
    assert opts.max_turns == 1  # 単発補完
    assert "SYS" in opts.system_prompt
    # サブスク認証を強制: 呼び出し中は API キーが環境から外れている
    assert captured["env_key"] is False
    # 呼び出し後は元に戻る
    import os

    assert os.environ.get("ANTHROPIC_API_KEY") == "sk-should-be-removed"


def test_completion_records_usage():
    from infra.repositories.claude_code_ai import LAST_USAGE

    with _patch_sdk([AssistantMessage("hi"), ResultMessage()]):
        ClaudeCodeAIRepository().completion("x")

    assert LAST_USAGE["cost_usd"] == 0.01
    assert LAST_USAGE["usage"]["output_tokens"] == 5


def test_completion_error_result_returns_none():
    with _patch_sdk([ResultMessage(is_error=True, errors=["rate_limited"])]):
        assert ClaudeCodeAIRepository().completion("x") is None


def test_completion_exception_returns_none():
    import claude_agent_sdk

    def _boom(*, prompt, options):
        raise RuntimeError("transport died")

    with patch.object(claude_agent_sdk, "query", _boom):
        assert ClaudeCodeAIRepository().completion("x") is None


def test_completion_json_parses_fenced_json():
    with _patch_sdk([AssistantMessage('```json\n{"a": 1}\n```'), ResultMessage()]):
        assert ClaudeCodeAIRepository().completion_json("x") == {"a": 1}


def test_factory_dispatch():
    assert isinstance(create_ai_repository("claude_code"), ClaudeCodeAIRepository)
    assert isinstance(create_ai_repository("copilot"), LiteLLMAIRepository)
    assert isinstance(create_ai_repository("anthropic"), LiteLLMAIRepository)

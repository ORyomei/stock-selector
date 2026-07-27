"""ClaudeCodeAIRepository (claude -p headless) のテスト。

背景: Copilot 月次クォータ枯渇で AI が8日停止した事故を受け、個人 Claude
サブスク (Max) の headless モードを既定プロバイダにした。サブプロセス呼び出しの
組み立てとフェイルセーフを検証する。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from infra.repositories.claude_code_ai import (  # noqa: E402
    ClaudeCodeAIRepository,
    create_ai_repository,
)
from infra.repositories.litellm_ai import LiteLLMAIRepository  # noqa: E402


class _FakeResult:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def test_completion_builds_correct_command(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-removed")
    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return _FakeResult(stdout="ok\n")

    with patch("infra.repositories.claude_code_ai.subprocess.run", side_effect=_fake_run):
        out = ClaudeCodeAIRepository(model="sonnet").completion("hello", system_msg="SYS")

    assert out == "ok"
    cmd = captured["cmd"]
    assert cmd[:3] == ["claude", "-p", "hello"]
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "sonnet"
    assert "--disallowedTools" in cmd  # ツール全面禁止
    # サブスク認証を強制: API キーはサブプロセス環境から除外される
    assert "ANTHROPIC_API_KEY" not in captured["env"]


def test_completion_failure_returns_none():
    with patch(
        "infra.repositories.claude_code_ai.subprocess.run",
        return_value=_FakeResult(returncode=1, stderr="boom"),
    ):
        assert ClaudeCodeAIRepository().completion("x") is None


def test_completion_timeout_returns_none():
    with patch(
        "infra.repositories.claude_code_ai.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=180),
    ):
        assert ClaudeCodeAIRepository().completion("x") is None


def test_completion_json_parses_fenced_json():
    with patch(
        "infra.repositories.claude_code_ai.subprocess.run",
        return_value=_FakeResult(stdout='```json\n{"a": 1}\n```'),
    ):
        assert ClaudeCodeAIRepository().completion_json("x") == {"a": 1}


def test_factory_dispatch():
    assert isinstance(create_ai_repository("claude_code"), ClaudeCodeAIRepository)
    assert isinstance(create_ai_repository("copilot"), LiteLLMAIRepository)
    assert isinstance(create_ai_repository("anthropic"), LiteLLMAIRepository)

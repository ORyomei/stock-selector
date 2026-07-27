"""LLM プロバイダ設定と sampling パラメータ制御のテスト。

背景: Claude Sonnet 5 / Opus 4.7+ / Fable 5 は temperature/top_p/top_k が
廃止され、送ると 400 になる。プロバイダ切替 (Copilot→Anthropic API 直) に
伴い、モデルに応じて temperature を落とす制御を検証する。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from infra.repositories.litellm_ai import (  # noqa: E402
    AI_PROVIDERS,
    supports_sampling_params,
)


def test_sonnet5_and_opus_reject_sampling():
    assert not supports_sampling_params("anthropic/claude-sonnet-5")
    assert not supports_sampling_params("anthropic/claude-opus-4-8")
    assert not supports_sampling_params("anthropic/claude-opus-4-7")
    assert not supports_sampling_params("anthropic/claude-fable-5")


def test_older_models_accept_sampling():
    assert supports_sampling_params("github_copilot/claude-sonnet-4.5")
    assert supports_sampling_params("github_copilot/claude-haiku-4.5")
    assert supports_sampling_params("anthropic/claude-sonnet-4-6")
    assert supports_sampling_params("gpt-4o")


def test_anthropic_provider_uses_current_model():
    """anthropic プロバイダのモデル ID が現行のもの (引退済み ID でない)。"""
    model = AI_PROVIDERS["anthropic"]["model"]
    assert model == "anthropic/claude-sonnet-5"
    assert AI_PROVIDERS["anthropic"]["token_env"] == "ANTHROPIC_API_KEY"


def test_react_chat_omits_temperature_for_sonnet5():
    """LiteLLMChat が Sonnet 5 のとき temperature を送らない。"""
    from unittest.mock import patch

    from agents.llm import LiteLLMChat

    chat = LiteLLMChat(model_name="anthropic/claude-sonnet-5")
    captured: dict = {}

    class _FakeMsg:
        content = "ok"
        tool_calls = None

    class _FakeChoice:
        message = _FakeMsg()

    class _FakeResp:
        choices = [_FakeChoice()]

    def _fake_completion(**kwargs):
        captured.update(kwargs)
        return _FakeResp()

    with patch("agents.llm.litellm.completion", side_effect=_fake_completion):
        chat.invoke("hello")

    assert "temperature" not in captured
    assert captured["model"] == "anthropic/claude-sonnet-5"


def test_react_chat_keeps_temperature_for_copilot():
    from unittest.mock import patch

    from agents.llm import LiteLLMChat

    chat = LiteLLMChat(model_name="github_copilot/claude-haiku-4.5")
    captured: dict = {}

    class _FakeMsg:
        content = "ok"
        tool_calls = None

    class _FakeChoice:
        message = _FakeMsg()

    class _FakeResp:
        choices = [_FakeChoice()]

    with patch("agents.llm.litellm.completion", side_effect=lambda **kw: (captured.update(kw), _FakeResp())[1]):
        chat.invoke("hello")

    assert captured.get("temperature") == 0.2

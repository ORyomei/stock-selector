"""LiteLLM ↔ LangChain ChatModel bridge.

Provides a thin ``BaseChatModel`` wrapper so that LangGraph's
``create_react_agent`` can use our existing LiteLLM backend without
pulling in ``langchain-community`` or any other heavyweight adapter.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

SRC_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(SRC_DIR))

import litellm
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from infra.repositories.litellm_ai import AI_PROVIDERS

litellm.suppress_debug_info = True


class LiteLLMChat(BaseChatModel):
    """LangChain ``BaseChatModel`` backed by LiteLLM."""

    model_name: str = AI_PROVIDERS["copilot"]["model"]
    temperature: float = 0.2
    max_tokens: int = 16000
    timeout: int = 180
    bound_tools: list[dict[str, Any]] = []
    bound_tool_choice: Any = None

    @property
    def _llm_type(self) -> str:
        return "litellm-chat"

    def bind_tools(
        self,
        tools: list[Any],
        *,
        tool_choice: Any = None,
        **kwargs: Any,
    ) -> LiteLLMChat:
        """Return a new instance with tools bound for every call."""
        from langchain_core.utils.function_calling import convert_to_openai_tool

        openai_tools = [convert_to_openai_tool(t) for t in tools]
        return self.model_copy(
            update={
                "bound_tools": openai_tools,
                "bound_tool_choice": tool_choice,
            }
        )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        litellm_messages = _to_litellm_messages(messages)

        call_kwargs: dict[str, Any] = {
            "model": self.model_name,
            "messages": litellm_messages,
            "max_tokens": self.max_tokens,
            "timeout": self.timeout,
        }
        # Claude Sonnet 5 / Opus 4.7+ 等は temperature を送ると 400 になる
        from infra.repositories.litellm_ai import supports_sampling_params

        if supports_sampling_params(self.model_name):
            call_kwargs["temperature"] = self.temperature
        if stop:
            call_kwargs["stop"] = stop

        # Use bound tools, or per-call tools from kwargs
        tools = kwargs.get("tools") or self.bound_tools
        if tools:
            call_kwargs["tools"] = tools
        tool_choice = kwargs.get("tool_choice") or self.bound_tool_choice
        if tool_choice:
            call_kwargs["tool_choice"] = tool_choice

        resp = litellm.completion(**call_kwargs)

        # GitHub Copilot API may split content and tool_calls across multiple
        # choices (e.g. choices[0] has text, choices[1] has tool_calls).
        # Merge them into a single AIMessage.
        content_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []

        for c in resp.choices:
            msg = c.message
            if msg.content:
                content_parts.append(msg.content)
            msg_tool_calls = getattr(msg, "tool_calls", None) or []
            if msg_tool_calls:
                import json as _json
                for tc in msg_tool_calls:
                    args = tc.function.arguments
                    if isinstance(args, str):
                        args = _json.loads(args)
                    tool_calls.append({
                        "name": tc.function.name,
                        "args": args,
                        "id": tc.id,
                        "type": "tool_call",
                    })

        ai_msg = AIMessage(
            content="\n".join(content_parts),
            tool_calls=tool_calls,
        )
        return ChatResult(generations=[ChatGeneration(message=ai_msg)])


def _to_litellm_messages(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    """Convert LangChain messages to LiteLLM (OpenAI-style) dicts."""
    import json as _json

    from langchain_core.messages import (
        AIMessage,
        HumanMessage,
        SystemMessage,
        ToolMessage,
    )

    result: list[dict[str, Any]] = []
    for msg in messages:
        if isinstance(msg, SystemMessage):
            result.append({"role": "system", "content": msg.content})
        elif isinstance(msg, HumanMessage):
            result.append({"role": "user", "content": msg.content})
        elif isinstance(msg, AIMessage):
            d: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                d["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": _json.dumps(tc["args"], ensure_ascii=False),
                        },
                    }
                    for tc in msg.tool_calls
                ]
            result.append(d)
        elif isinstance(msg, ToolMessage):
            content = msg.content
            if not isinstance(content, str):
                content = _json.dumps(content, ensure_ascii=False)
            result.append({
                "role": "tool",
                "tool_call_id": msg.tool_call_id,
                "content": content,
            })
        else:
            result.append({"role": "user", "content": str(msg.content)})
    return result


def get_chat_model(
    provider: str = "claude_code",
    model: str | None = None,
    **kwargs: Any,
) -> LiteLLMChat:
    """Factory helper to create a ``LiteLLMChat`` instance."""
    cfg = AI_PROVIDERS.get(provider, AI_PROVIDERS["copilot"])
    model_name = model or cfg["model"]
    return LiteLLMChat(model_name=model_name, **kwargs)

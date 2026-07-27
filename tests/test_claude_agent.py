"""Claude Agent SDK 移植 (agents/claude_agent.py) のテスト。

カバー範囲:
- LangChain ツール → SDK ツール変換 (スキーマ抽出・実行・エラー差し戻し)
- submit_signals の pydantic 検証が変換後も効くこと
- SDK メッセージ列 → 可視化トレースの直列化
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agents.claude_agent import (  # noqa: E402
    final_text_of,
    lc_tool_to_sdk,
    serialize_sdk_trace,
)

# ── LangChain ツール変換 ─────────────────────────────────────────


def _make_lc_tool():
    from langchain_core.tools import tool

    @tool
    def add_numbers(a: int, b: int) -> dict:
        """2つの数を足す。"""
        return {"total": a + b}

    return add_numbers


def test_sdk_tool_conversion_and_execution():
    sdk_t = lc_tool_to_sdk(_make_lc_tool())
    assert sdk_t.name == "add_numbers"
    assert "a" in sdk_t.input_schema["properties"]
    out = asyncio.run(sdk_t.handler({"a": 2, "b": 3}))
    assert out["content"][0]["text"] == '{"total": 5}'
    assert "is_error" not in out


def test_sdk_tool_validation_error_is_returned_not_raised():
    """不正な引数は is_error で返る (モデルに差し戻して再試行させる)。"""
    sdk_t = lc_tool_to_sdk(_make_lc_tool())
    out = asyncio.run(sdk_t.handler({"a": "not-a-number-at-all", "b": []}))
    assert out.get("is_error") is True
    assert "ERROR" in out["content"][0]["text"]


def test_submit_signals_pydantic_enforced_through_sdk():
    """submit_signals 変換後もスキーマ検証が効き、captured に格納される。"""
    from agents.graph_trade import _make_submit_signals_tool

    captured: dict = {}
    sdk_t = lc_tool_to_sdk(_make_submit_signals_tool(captured))

    # 必須フィールド (ticker) 欠落 → is_error (差し戻し)。
    # ticker 以外はスキーマ側にデフォルトがあり補完される (LangGraph 時代と同じ)
    bad = asyncio.run(sdk_t.handler({"signals": [{}]}))
    assert bad.get("is_error") is True

    # 正しいシグナル → 受理され captured に入る
    good_signal = {
        "ticker": "7203.T", "action": "buy", "score": 30, "confidence": 0.7,
        "reason": "テスト", "entry_price": 0, "target_price": 2500.0,
        "stop_loss_price": 2400.0, "take_profit_price": 2700.0,
        "timespan": "swing", "fail_conditions": ["x"],
        "invalidation_conditions": ["y"], "exit_plan": "z",
    }
    ok = asyncio.run(sdk_t.handler({"signals": [good_signal], "market_comment": "c"}))
    assert "is_error" not in ok
    assert captured["signals"][0]["ticker"] == "7203.T"


# ── トレース直列化 ───────────────────────────────────────────────


class TextBlock:
    def __init__(self, text):
        self.text = text


class ToolUseBlock:
    def __init__(self, id, name, input):
        self.id, self.name, self.input = id, name, input


class ToolResultBlock:
    def __init__(self, tool_use_id, content):
        self.tool_use_id, self.content = tool_use_id, content


class AssistantMessage:
    def __init__(self, content):
        self.content = content


class UserMessage:
    def __init__(self, content):
        self.content = content


class ResultMessage:
    def __init__(self, result):
        self.result = result


def test_serialize_sdk_trace_schema():
    messages = [
        AssistantMessage([
            TextBlock("スクリーニングします"),
            ToolUseBlock("t1", "mcp__stock__screen_stocks", {"market": "jp"}),
        ]),
        UserMessage([ToolResultBlock("t1", [{"type": "text", "text": "候補: 7203.T"}])]),
        AssistantMessage([ToolUseBlock("t2", "mcp__stock__submit_signals", {"signals": []})]),
        UserMessage([ToolResultBlock("t2", "0 件のシグナルを受理しました")]),
        ResultMessage("本日は見送り。"),
    ]
    steps = serialize_sdk_trace(messages, user_msg="市場を分析して")

    types = [s["type"] for s in steps]
    assert types == ["user", "reasoning", "tool_call", "tool_result", "tool_call", "tool_result", "final"]
    assert steps[2] == {"type": "tool_call", "tool": "screen_stocks", "args": {"market": "jp"}}
    assert steps[3]["summary"] == "候補: 7203.T"
    assert steps[5]["tool"] == "submit_signals"
    assert steps[6]["text"] == "本日は見送り。"


def test_final_text_prefers_result_message():
    msgs = [AssistantMessage([TextBlock("途中経過")]), ResultMessage("最終回答")]
    assert final_text_of(msgs) == "最終回答"
    assert final_text_of([AssistantMessage([TextBlock("途中経過")])]) == "途中経過"
    assert final_text_of([]) == ""

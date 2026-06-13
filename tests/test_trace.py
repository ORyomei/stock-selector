"""LangGraph 思考トレースのシリアライズ (純関数) テスト。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agents.graph_trade import _serialize_trace  # noqa: E402


class _Human:
    type = "human"

    def __init__(self, content):
        self.content = content


class _AI:
    type = "ai"

    def __init__(self, content="", tool_calls=None):
        self.content = content
        if tool_calls is not None:
            self.tool_calls = tool_calls


class _Tool:
    type = "tool"

    def __init__(self, name, content):
        self.name = name
        self.content = content
        self.tool_call_id = "tc1"


def test_serialize_react_trace():
    messages = [
        _Human("市場を分析して"),
        _AI(content="まずマクロを確認", tool_calls=[{"name": "check_macro", "args": {}}]),
        _Tool("check_macro", "VIX 21.5 / やや弱気"),
        _AI(tool_calls=[{"name": "score_stock", "args": {"ticker": "7203.T"}}]),
        _Tool("score_stock", "score=45 強い買い"),
        _AI(tool_calls=[{"name": "submit_signals", "args": {"signals": [{"ticker": "7203.T"}]}}]),
        _Tool("submit_signals", "1件受理"),
        _AI(content="トヨタを買い推奨"),
    ]
    steps = _serialize_trace(messages)
    types = [s["type"] for s in steps]
    assert types == [
        "user", "reasoning", "tool_call", "tool_result",
        "tool_call", "tool_result", "tool_call", "tool_result", "final",
    ]
    # ツール呼び出しの順序と引数が取れている
    calls = [(s["tool"], s["args"]) for s in steps if s["type"] == "tool_call"]
    assert calls[0] == ("check_macro", {})
    assert calls[1] == ("score_stock", {"ticker": "7203.T"})
    assert calls[2][0] == "submit_signals"
    assert steps[-1] == {"type": "final", "text": "トヨタを買い推奨"}


def test_serialize_empty():
    assert _serialize_trace([]) == []


def test_tool_result_truncated():
    big = "x" * 1000
    steps = _serialize_trace([_Tool("get_prices", big)])
    assert steps[0]["type"] == "tool_result"
    assert len(steps[0]["summary"]) == 400

"""Claude Agent SDK でメイン取引エージェントを実行する (サブスク経路)。

provider="claude_code" のとき、LangGraph ReAct + litellm の代わりに公式の
Claude Agent SDK (headless Claude Code) でメインエージェントを回す。
個人 Claude サブスク (Max) の認証をそのまま使うため API クレジット不要。

設計:
- 既存の LangChain ツール (agents/tools.py, submit_signals) を SDK の
  in-process MCP ツールへ機械変換する (ツール実装・pydantic 検証は共有、
  ハーネスだけ差し替え)
- ビルトインツール (Bash/Read/Web...) は全禁止 — 取引判断は登録ツールのみで行う
- トレースは既存の可視化スキーマ (user/reasoning/tool_call/tool_result/final)
  に直列化し、ダッシュボードの表示をそのまま使えるようにする
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

SRC_DIR = Path(__file__).resolve().parent.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

_MCP_SERVER = "stock"

# 純粋に登録ツールだけで判断させる (ファイル/シェル/Web への迂回を防ぐ)
_DISALLOWED_BUILTINS = [
    "Bash", "Read", "Write", "Edit", "Glob", "Grep",
    "WebFetch", "WebSearch", "Task", "NotebookEdit", "TodoWrite",
]


def _work_dir() -> str:
    """空の作業ディレクトリ (プロジェクトの CLAUDE.md 等を読み込ませない)。"""
    d = Path(tempfile.gettempdir()) / "stock-selector-claude-agent"
    d.mkdir(exist_ok=True)
    return str(d)


def _tool_result_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(result)


def lc_tool_to_sdk(lc_tool: Any) -> Any:
    """LangChain StructuredTool → Claude Agent SDK ツールへ変換する。

    lc_tool.invoke() を経由するため、pydantic スキーマ検証 (submit_signals の
    シグナル形式強制など) はそのまま効く。検証エラーは is_error でモデルに
    差し戻され、再試行を促す (LangGraph 時代と同じ回復セマンティクス)。
    """
    from claude_agent_sdk import tool as sdk_tool

    schema: dict[str, Any] = {"type": "object", "properties": {}}
    args_schema = getattr(lc_tool, "args_schema", None)
    if args_schema is not None:
        s = args_schema.model_json_schema()
        schema = {
            "type": "object",
            "properties": s.get("properties", {}),
            "required": s.get("required", []),
        }
        if "$defs" in s:
            schema["$defs"] = s["$defs"]

    @sdk_tool(lc_tool.name, lc_tool.description or lc_tool.name, schema)
    async def handler(args: dict[str, Any], _t: Any = lc_tool) -> dict[str, Any]:
        try:
            result = await asyncio.to_thread(_t.invoke, args or {})
        except Exception as e:  # pydantic 検証エラー含む → モデルに差し戻し
            return {
                "content": [{"type": "text", "text": f"ERROR: {e}"}],
                "is_error": True,
            }
        return {"content": [{"type": "text", "text": _tool_result_text(result)[:20000]}]}

    return handler


def serialize_sdk_trace(messages: list[Any], user_msg: str = "") -> list[dict[str, Any]]:
    """SDK メッセージ列を可視化用ステップ列へ変換する (純関数)。

    既存の _serialize_trace と同じスキーマ:
    user / reasoning / tool_call(tool,args) / tool_result(tool,summary) / final
    """
    steps: list[dict[str, Any]] = []
    if user_msg:
        steps.append({"type": "user", "text": user_msg[:300]})
    tool_names: dict[str, str] = {}
    prefix = f"mcp__{_MCP_SERVER}__"

    for m in messages:
        cls = type(m).__name__
        if cls == "AssistantMessage":
            for b in getattr(m, "content", None) or []:
                bcls = type(b).__name__
                if bcls == "ThinkingBlock":
                    text = str(getattr(b, "thinking", "")).strip()
                    if text:
                        steps.append({"type": "reasoning", "text": text[:600]})
                elif bcls == "TextBlock":
                    text = str(getattr(b, "text", "")).strip()
                    if text:
                        steps.append({"type": "reasoning", "text": text[:600]})
                elif bcls == "ToolUseBlock":
                    name = str(getattr(b, "name", "?")).removeprefix(prefix)
                    tool_names[getattr(b, "id", "")] = name
                    steps.append({
                        "type": "tool_call",
                        "tool": name,
                        "args": getattr(b, "input", {}) or {},
                    })
        elif cls == "UserMessage":
            content = getattr(m, "content", None)
            if not isinstance(content, list):
                continue
            for b in content:
                if type(b).__name__ != "ToolResultBlock":
                    continue
                raw = getattr(b, "content", "")
                if isinstance(raw, list):  # [{"type":"text","text":...}, ...]
                    raw = " ".join(
                        str(p.get("text", "")) for p in raw if isinstance(p, dict)
                    )
                steps.append({
                    "type": "tool_result",
                    "tool": tool_names.get(getattr(b, "tool_use_id", ""), "?"),
                    "summary": str(raw)[:400],
                })
        elif cls == "ResultMessage":
            text = str(getattr(m, "result", "") or "").strip()
            if text:
                steps.append({"type": "final", "text": text[:2000]})
    return steps


def final_text_of(messages: list[Any]) -> str:
    """最終テキスト (ResultMessage.result、なければ最後の TextBlock)。"""
    for m in reversed(messages):
        if type(m).__name__ == "ResultMessage":
            text = str(getattr(m, "result", "") or "").strip()
            if text:
                return text
    for m in reversed(messages):
        if type(m).__name__ == "AssistantMessage":
            for b in reversed(getattr(m, "content", None) or []):
                if type(b).__name__ == "TextBlock" and str(getattr(b, "text", "")).strip():
                    return str(b.text).strip()
    return ""


def run_trade_agent_via_sdk(
    system_prompt: str,
    user_msg: str,
    lc_tools: list[Any],
    *,
    model: str = "sonnet",
    max_turns: int = 24,
    timeout_sec: int = 420,
    log: Any = lambda *_: None,
) -> tuple[str, list[dict[str, Any]]]:
    """メインエージェントを Claude Agent SDK で1サイクル実行する。

    Returns:
        (final_text, trace_steps)。シグナル自体は submit_signals ツールの
        closure (captured dict) 経由で呼び出し側に渡る。

    Raises:
        TimeoutError: timeout_sec 超過時 (呼び出し側でシグナルなし続行)。
    """
    sdk_tools = [lc_tool_to_sdk(t) for t in lc_tools]
    allowed = [f"mcp__{_MCP_SERVER}__{t.name}" for t in lc_tools]

    async def _run() -> list[Any]:
        from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server, query

        server = create_sdk_mcp_server(name=_MCP_SERVER, version="1.0.0", tools=sdk_tools)
        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            model=model,
            mcp_servers={_MCP_SERVER: server},
            allowed_tools=allowed,
            disallowed_tools=_DISALLOWED_BUILTINS,
            max_turns=max_turns,
            cwd=_work_dir(),
        )
        msgs: list[Any] = []
        async for m in query(prompt=user_msg, options=options):
            msgs.append(m)
        return msgs

    # サブスク認証を強制 (API キーがあると Claude Code が従量課金に切り替わるため、
    # 実行中だけ環境から外す)
    saved_key = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        messages = asyncio.run(asyncio.wait_for(_run(), timeout=timeout_sec))
    finally:
        if saved_key is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved_key

    n_tools = sum(
        1
        for m in messages
        for b in (getattr(m, "content", None) or [])
        if type(b).__name__ == "ToolUseBlock"
    ) if messages else 0
    log(f"  -> Agent SDK 完了 (メッセージ {len(messages)} 件 / ツール呼び出し {n_tools} 回)")
    return final_text_of(messages), serialize_sdk_trace(messages, user_msg)

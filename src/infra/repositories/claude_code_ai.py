"""Claude Code (headless) を使った AIRepository 実装。

Claude Agent SDK 経由で、個人 Claude サブスク (Max) の認証をそのまま使う公式の
headless モード。API クレジット不要で、Copilot のような月次クォータの崖もない
(サブスクの5時間窓レート制限のみ)。

メインエージェント (agents/claude_agent.py) と同じ SDK に統一している。SDK の
トランスポートは claude CLI のサブプロセス1種類だけなので、CLI 起動自体は
どのみち避けられない。手書きの subprocess 実行と stdout パースをやめて SDK に
任せることで、usage/cost が構造化データで取れ、エラーも型で上がる。

注意:
- ANTHROPIC_API_KEY を環境から除外して起動する (キーが設定されていると
  Claude Code がサブスクではなく API 課金にフォールバックするため)
- cwd を空の一時ディレクトリにする (プロジェクトの CLAUDE.md 等を
  読み込ませない = トークン浪費と文脈汚染の防止)
- ツールは全て禁止 (純粋なテキスト補完としてのみ使う)
- completion() は同期契約 (AIRepository) なので asyncio.run() で包む。
  呼び出し元がイベントループ上の場合は動かないが、ツール経由の呼び出しは
  claude_agent.lc_tool_to_sdk が asyncio.to_thread でワーカースレッドに
  逃がすため、そのスレッドにループは無く安全
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from interfaces.repositories.ai import AIRepository

# 純テキスト補完にツールは不要。明示的に全部禁止する
_DISALLOWED_TOOLS = [
    "Bash", "Read", "Write", "Edit", "Glob", "Grep",
    "WebFetch", "WebSearch", "Task", "NotebookEdit", "TodoWrite",
]


def _work_dir() -> str:
    """headless 実行の作業ディレクトリ (空ディレクトリを用意して文脈読込を防ぐ)。"""
    d = Path(tempfile.gettempdir()) / "stock-selector-claude-headless"
    d.mkdir(exist_ok=True)
    return str(d)


# 直近の呼び出しの usage/cost。レート枠の消費を可視化するために保持する
LAST_USAGE: dict[str, Any] = {}


class ClaudeCodeAIRepository(AIRepository):
    """Claude Agent SDK (headless Claude Code) 経由で LLM を呼び出す。"""

    def __init__(self, model: str = "sonnet", timeout_sec: int = 180) -> None:
        self._model = model
        self._timeout = timeout_sec

    async def _query(self, prompt: str, system_msg: str) -> str:
        from claude_agent_sdk import ClaudeAgentOptions, query

        options = ClaudeAgentOptions(
            system_prompt=f"{system_msg} ツールは使わず、直接テキストで回答すること。",
            model=self._model,
            allowed_tools=[],  # 純テキスト補完 — ツールは一切使わせない
            disallowed_tools=_DISALLOWED_TOOLS,
            max_turns=1,
            cwd=_work_dir(),
        )
        texts: list[str] = []
        async for m in query(prompt=prompt, options=options):
            cls = type(m).__name__
            if cls == "AssistantMessage":
                for b in getattr(m, "content", None) or []:
                    if type(b).__name__ == "TextBlock":
                        texts.append(str(getattr(b, "text", "")))
            elif cls == "ResultMessage":
                LAST_USAGE.clear()
                LAST_USAGE.update({
                    "model": self._model,
                    "cost_usd": getattr(m, "total_cost_usd", None),
                    "usage": getattr(m, "usage", None),
                    "duration_ms": getattr(m, "duration_ms", None),
                    "is_error": getattr(m, "is_error", False),
                })
                if getattr(m, "is_error", False):
                    errs = getattr(m, "errors", None) or [getattr(m, "subtype", "unknown")]
                    raise RuntimeError(f"claude headless failed: {errs}")
        return "\n".join(t for t in texts if t).strip()

    def completion(
        self,
        prompt: str,
        *,
        system_msg: str = "株式売買判断AI。JSON形式で回答。",
    ) -> str | None:
        # サブスク認証を強制 (API キーがあると従量課金にフォールバックするため)
        saved_key = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            out = asyncio.run(
                asyncio.wait_for(self._query(prompt, system_msg), timeout=self._timeout)
            )
        except TimeoutError:
            print(f"[error] claude headless timed out ({self._timeout}s)", file=sys.stderr)
            return None
        except Exception as e:
            print(f"[error] claude headless failed: {type(e).__name__}: {e}", file=sys.stderr)
            return None
        finally:
            if saved_key is not None:
                os.environ["ANTHROPIC_API_KEY"] = saved_key
        return out or None

    def completion_json(
        self,
        prompt: str,
        *,
        system_msg: str = "株式売買判断AI。JSON形式で回答。",
    ) -> dict[str, Any] | None:
        from infra.repositories.litellm_ai import parse_ai_json

        return parse_ai_json(self.completion(prompt, system_msg=system_msg))


def create_ai_repository(provider: str, model: str | None = None) -> AIRepository:
    """プロバイダ名から AIRepository 実装を選ぶファクトリ。

    - "claude_code": Claude サブスク経由 (headless、追加クレジット不要)
    - その他: LiteLLM 経由 (copilot / anthropic / openai ...)
    """
    if provider == "claude_code":
        from infra.repositories.litellm_ai import AI_PROVIDERS

        default_model = AI_PROVIDERS.get("claude_code", {}).get("model", "sonnet")
        return ClaudeCodeAIRepository(model=model or default_model)
    from infra.repositories.litellm_ai import LiteLLMAIRepository

    return LiteLLMAIRepository(provider=provider, model=model)

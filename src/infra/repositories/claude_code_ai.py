"""Claude Code (headless) を使った AIRepository 実装。

`claude -p` サブプロセス経由で、個人 Claude サブスク (Max) の認証をそのまま使う
公式の headless モード。API クレジット不要で、Copilot のような月次クォータの
崖もない (サブスクの5時間窓レート制限のみ)。

注意:
- ANTHROPIC_API_KEY を環境から除外して起動する (キーが設定されていると
  Claude Code がサブスクではなく API 課金にフォールバックするため)
- cwd を空の一時ディレクトリにする (プロジェクトの CLAUDE.md 等を
  読み込ませない = トークン浪費と文脈汚染の防止)
- ツールは全て禁止 (純粋なテキスト補完としてのみ使う)
"""

from __future__ import annotations

import os
import subprocess
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
    """claude -p の作業ディレクトリ (空ディレクトリを用意して文脈読込を防ぐ)。"""
    d = Path(tempfile.gettempdir()) / "stock-selector-claude-headless"
    d.mkdir(exist_ok=True)
    return str(d)


class ClaudeCodeAIRepository(AIRepository):
    """`claude -p` (headless Claude Code) 経由で LLM を呼び出す。"""

    def __init__(self, model: str = "sonnet", timeout_sec: int = 180) -> None:
        self._model = model
        self._timeout = timeout_sec

    def completion(
        self,
        prompt: str,
        *,
        system_msg: str = "株式売買判断AI。JSON形式で回答。",
    ) -> str | None:
        env = os.environ.copy()
        # サブスク認証を強制 (API キーがあると従量課金にフォールバックするため)
        env.pop("ANTHROPIC_API_KEY", None)
        cmd = [
            "claude", "-p", prompt,
            "--model", self._model,
            "--append-system-prompt",
            f"{system_msg} ツールは使わず、直接テキストで回答すること。",
            "--disallowedTools", *_DISALLOWED_TOOLS,
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                cwd=_work_dir(),
                env=env,
            )
        except subprocess.TimeoutExpired:
            print(f"[error] claude -p timed out ({self._timeout}s)", file=sys.stderr)
            return None
        except FileNotFoundError:
            print("[error] claude CLI not found (Claude Code 未インストール)", file=sys.stderr)
            return None

        if result.returncode != 0:
            print(
                f"[error] claude -p failed (rc={result.returncode}): {result.stderr[:300]}",
                file=sys.stderr,
            )
            return None
        out = result.stdout.strip()
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

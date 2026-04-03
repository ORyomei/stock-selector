"""Subprocess runner for invoking sibling scripts and returning parsed JSON."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = SCRIPT_DIR.parent


def run_script(
    script_name: str,
    args: list[str] | None = None,
    *,
    timeout: int = 120,
) -> dict[str, Any] | None:
    """Execute a sibling Python script and return its JSON output.

    Returns ``None`` on timeout, non-zero exit, or JSON parse failure.
    """
    cmd = [sys.executable, str(SCRIPT_DIR / script_name)]
    if args:
        cmd.extend(args)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(PROJECT_DIR),
        )
    except subprocess.TimeoutExpired:
        print(f"  [warn] {script_name}: timeout ({timeout}s)", file=sys.stderr)
        return None

    if result.returncode != 0:
        print(
            f"  [warn] {script_name}: exit {result.returncode}: {result.stderr[:300]}",
            file=sys.stderr,
        )
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"  [warn] {script_name}: invalid JSON output", file=sys.stderr)
        return None


def run_trade_cmd(
    args_list: list[str],
    *,
    timeout: int = 60,
) -> tuple[str, int]:
    """Execute ``trade.py`` with the given arguments.

    Returns ``(stdout, return_code)``.
    """
    cmd = [sys.executable, str(SCRIPT_DIR / "trade.py")] + args_list
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(PROJECT_DIR),
        )
        return result.stdout, result.returncode
    except subprocess.TimeoutExpired:
        return "", 1

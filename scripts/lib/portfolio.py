"""Portfolio state helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = PROJECT_DIR / "config"
PORTFOLIO_FILE = PROJECT_DIR / "portfolio.json"


def load_portfolio() -> dict[str, Any] | None:
    """Load portfolio.json, returning None if absent."""
    if PORTFOLIO_FILE.exists():
        with open(PORTFOLIO_FILE) as f:
            return json.load(f)
    return None


def get_held_tickers() -> set[str]:
    pf = load_portfolio()
    return {p["ticker"] for p in pf.get("positions", [])} if pf else set()


def get_held_positions() -> list[dict[str, Any]]:
    pf = load_portfolio()
    return pf.get("positions", []) if pf else []


def count_positions() -> int:
    pf = load_portfolio()
    return len(pf.get("positions", [])) if pf else 0


def get_max_positions() -> int:
    limits_path = CONFIG_DIR / "risk_limits.json"
    if limits_path.exists():
        with open(limits_path) as f:
            return json.load(f).get("max_concurrent_positions", 5)
    return 5


def confidence_to_float(label: str) -> float:
    """Convert Japanese confidence label to a numeric value."""
    return {
        "高": 0.90,
        "中〜高": 0.80,
        "中": 0.70,
        "低〜中": 0.60,
        "低": 0.50,
    }.get(label, 0.70)

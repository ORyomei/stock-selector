"""Portfolio state helpers — backward-compat wrapper over container."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

SRC_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(SRC_DIR))

from infra.container import get_container


def load_portfolio() -> dict[str, Any] | None:
    """Load portfolio.json, returning None if absent."""
    return get_container().portfolio().load()


def get_held_tickers() -> set[str]:
    return get_container().portfolio().get_held_tickers()


def get_held_positions() -> list[dict[str, Any]]:
    return get_container().portfolio().get_held_positions()


def count_positions() -> int:
    return get_container().portfolio().count_positions()


def get_max_positions() -> int:
    return get_container().portfolio().get_max_positions()


def confidence_to_float(label: str) -> float:
    """Convert Japanese confidence label to a numeric value."""
    return {
        "高": 0.90,
        "中〜高": 0.80,
        "中": 0.70,
        "低〜中": 0.60,
        "低": 0.50,
    }.get(label, 0.70)

"""売買単位 (単元株数 / ETF の口数単位) の管理。

kabu の銘柄情報 (/symbol の TradingUnit) から取得して config に保持し、
ポジションサイズの丸めや資金チェックで参照する。ETF は 1 口単位のことが
多く、個別株 (100 株) 前提では正しく扱えないため。

このモジュールは leaf (json/os/pathlib のみ依存) とし、trading 層を import
しない (risk_manager から lazy import されても循環しないように)。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "trading_units.json"


def _load() -> dict[str, int]:
    try:
        with open(_CONFIG_PATH) as f:
            data = json.load(f)
        return {str(k): int(v) for k, v in data.items() if isinstance(v, int | float)}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def default_unit(ticker: str) -> int:
    """config に無い場合のフォールバック: 日本株は単元100、その他は1。"""
    return 100 if ticker.endswith(".T") else 1


def get_trading_unit(ticker: str) -> int:
    """銘柄の売買単位を返す。config 優先、無ければ通貨ベースのデフォルト。"""
    unit = _load().get(ticker)
    if unit and unit > 0:
        return int(unit)
    return default_unit(ticker)


def refresh_trading_units(broker: Any, tickers: list[str]) -> dict[str, int]:
    """ブローカーから売買単位を取得し config/trading_units.json に永続化する。

    Args:
        broker: BrokerInterface 実装 (get_trading_unit を持つ)
        tickers: 取得対象のティッカー一覧

    Returns:
        更新後の {ticker: unit} 全体
    """
    units = _load()
    for t in tickers:
        try:
            u = int(broker.get_trading_unit(t))
        except Exception:
            continue
        if u > 0:
            units[t] = u

    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CONFIG_PATH.with_name(_CONFIG_PATH.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(dict(sorted(units.items())), f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _CONFIG_PATH)
    return units

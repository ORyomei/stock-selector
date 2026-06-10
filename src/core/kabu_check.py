#!/usr/bin/env python3
"""kabuステーション API 接続テスト"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.brokers import KabuStationBroker
from infra.brokers.kabu_station import KabuStationError
from infra.container import get_container


def check_connection(
    *, show_positions: bool = False, show_orders: bool = False
) -> dict[str, Any]:
    """kabu API に接続し、残高・ポジション・注文を返す。"""
    config = get_container().config_repo().load_trading_config()
    kabu_config = config.get("kabu")
    if not kabu_config:
        return {"ok": False, "error": "trading_config.json に kabu セクションがありません"}

    try:
        broker = KabuStationBroker(kabu_config)
        result = broker.ping()
        out: dict[str, Any] = {
            "ok": True,
            "host": f"{kabu_config.get('host')}:{kabu_config.get('port')}",
            "sandbox": kabu_config.get("sandbox"),
            "token_prefix": result["token_prefix"],
            "cash_jpy": result["balance"]["cash_jpy"],
        }
        if show_positions:
            positions = broker.get_positions()
            out["positions"] = [p.to_dict() for p in positions]
        if show_orders:
            orders = broker.get_orders()
            out["orders"] = [o.to_dict() for o in orders]
        return out
    except KabuStationError as exc:
        return {"ok": False, "error": str(exc)}


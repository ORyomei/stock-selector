#!/usr/bin/env python3
"""セクター騰落率 (相対強度)。

TOPIX-17 ETF (1617〜1633.T) と米セクター ETF の騰落率を横並びにする。
個別銘柄のスコアからセクターの強弱を推測させるのではなく、直接データで渡す。
決定的 (AI なし)。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

SRC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC_DIR))

from infra.container import get_container

# TOPIX-17 シリーズ (NEXT FUNDS)
JP_SECTOR_ETFS = {
    "1617.T": "食品",
    "1618.T": "エネルギー資源",
    "1619.T": "建設・資材",
    "1620.T": "素材・化学",
    "1621.T": "医薬品",
    "1622.T": "自動車・輸送機",
    "1623.T": "鉄鋼・非鉄",
    "1624.T": "機械",
    "1625.T": "電機・精密",
    "1626.T": "情報通信・サービス",
    "1627.T": "電力・ガス",
    "1628.T": "運輸・物流",
    "1629.T": "商社・卸売",
    "1630.T": "小売",
    "1631.T": "銀行",
    "1632.T": "金融(除く銀行)",
    "1633.T": "不動産",
}

US_SECTOR_ETFS = {
    "XLK": "テクノロジー",
    "SOXX": "半導体",
    "XLF": "金融",
    "XLV": "ヘルスケア",
    "XLI": "資本財",
    "XLY": "一般消費財",
    "XLP": "生活必需品",
    "XLE": "エネルギー",
    "XLB": "素材",
    "XLU": "公益",
    "XLC": "通信",
    "XLRE": "不動産",
}


def _changes(closes: list[float]) -> dict[str, float | None]:
    """終値列 (古い順) から 1d/5d/20d 騰落率% を出す。"""
    out: dict[str, float | None] = {"change_1d": None, "change_5d": None, "change_20d": None}
    last = closes[-1]
    for key, n in (("change_1d", 1), ("change_5d", 5), ("change_20d", 20)):
        if len(closes) > n and closes[-1 - n]:
            out[key] = round((last / closes[-1 - n] - 1) * 100, 2)
    return out


def run_sector_strength(market: str = "jp", period: str = "3mo") -> dict[str, Any]:
    """セクター ETF の騰落率を 5日騰落の降順で返す。

    Args:
        market: 'jp' (TOPIX-17) / 'us' (米セクターETF) / 'all'
    """
    etfs: dict[str, str] = {}
    if market in ("jp", "all"):
        etfs.update(JP_SECTOR_ETFS)
    if market in ("us", "all"):
        etfs.update(US_SECTOR_ETFS)

    md = get_container().market_data()
    rows: list[dict[str, Any]] = []
    errors = 0
    for ticker, label in etfs.items():
        try:
            hist = md.get_price_history(ticker, period=period, interval="1d")
            closes = [float(v) for v in hist["Close"].dropna().tolist()]
            if len(closes) < 2:
                errors += 1
                continue
        except Exception:
            errors += 1
            continue
        rows.append({"ticker": ticker, "sector": label, **_changes(closes)})

    rows.sort(key=lambda r: (r["change_5d"] is None, -(r["change_5d"] or 0)))
    return {
        "market": market,
        "sectors": rows,
        "errors": errors,
        "note": "TOPIX-17 ETF / 米セクターETF の騰落率。change_5d 降順 (上=強い)。",
    }


if __name__ == "__main__":
    import json

    market = sys.argv[1] if len(sys.argv) > 1 else "jp"
    print(json.dumps(run_sector_strength(market), ensure_ascii=False, indent=2))

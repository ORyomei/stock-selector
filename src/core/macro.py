#!/usr/bin/env python3
"""マクロ経済指標スクリプト

Usage: python scripts/macro.py [--period 3mo]

VIX, 米10年金利, ドル円, 原油先物, 金先物, 主要指数を取得し、
市場環境スコア（リスクオン/オフ）を算出する。
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = SRC_DIR.parent
sys.path.insert(0, str(SRC_DIR))

from infra.container import get_container

# ---- 監視対象シンボル ----
MACRO_SYMBOLS = {
    "VIX": {"symbol": "^VIX", "label": "恐怖指数(VIX)"},
    "US10Y": {"symbol": "^TNX", "label": "米10年金利"},
    "USDJPY": {"symbol": "USDJPY=X", "label": "ドル円"},
    "OIL": {"symbol": "CL=F", "label": "原油先物(WTI)"},
    "GOLD": {"symbol": "GC=F", "label": "金先物"},
    "SP500": {"symbol": "^GSPC", "label": "S&P 500"},
    "NASDAQ": {"symbol": "^IXIC", "label": "NASDAQ"},
    "NIKKEI": {"symbol": "^N225", "label": "日経平均"},
    "DXY": {"symbol": "DX-Y.NYB", "label": "ドルインデックス"},
}


def compute_env_score(data: dict) -> tuple[int, list[str]]:
    """マクロ指標から市場環境スコアとシグナル列を算出する (純関数)。

    従来は VIX/米金利/S&P500 のみで、取引対象である日本市場を見ておらず
    「TOPIX が3日続落しても neutral 固定」という欠陥があった。
    日経平均のモメンタム (5日/20日) を最重要入力として追加している。
    """
    env_score = 0
    env_signals: list[str] = []

    def _pct(key: str, field: str) -> float | None:
        raw = data.get(key, {}).get(field)
        if not raw:
            return None
        try:
            return float(str(raw).strip("%"))
        except ValueError:
            return None

    # 日経平均モメンタム (取引対象市場 — 最重要)
    nikkei_5d = _pct("NIKKEI", "change_5d")
    if nikkei_5d is not None:
        if nikkei_5d < -3:
            env_score -= 20
            env_signals.append(f"日経急落(5日{nikkei_5d:+.1f}%) → リスクオフ")
        elif nikkei_5d < -1.5:
            env_score -= 10
            env_signals.append(f"日経下落(5日{nikkei_5d:+.1f}%) → 警戒")
        elif nikkei_5d > 3:
            env_score += 10
            env_signals.append(f"日経上昇(5日{nikkei_5d:+.1f}%)")
    nikkei_20d = _pct("NIKKEI", "change_20d")
    if nikkei_20d is not None:
        if nikkei_20d < -5:
            env_score -= 10
            env_signals.append(f"日経下落トレンド(20日{nikkei_20d:+.1f}%)")
        elif nikkei_20d > 5:
            env_score += 10
            env_signals.append(f"日経上昇トレンド(20日{nikkei_20d:+.1f}%)")

    # VIX 評価
    vix = data.get("VIX", {}).get("current")
    if vix is not None:
        if vix < 15:
            env_score += 20
            env_signals.append(f"VIX低い({vix:.1f}) → リスクオン")
        elif vix < 20:
            env_score += 10
            env_signals.append(f"VIX通常({vix:.1f})")
        elif vix < 30:
            env_score -= 10
            env_signals.append(f"VIXやや高い({vix:.1f}) → 警戒")
        else:
            env_score -= 20
            env_signals.append(f"VIX高い({vix:.1f}) → リスクオフ")

    # 金利動向
    us10y = data.get("US10Y", {}).get("current")
    if us10y is not None:
        us10y_5d = _pct("US10Y", "change_5d")
        if us10y_5d is not None:
            if us10y_5d > 5:
                env_score -= 10
                env_signals.append(f"金利急上昇({us10y_5d:+.2f}%) → 株式に逆風")
            elif us10y_5d < -5:
                env_score += 10
                env_signals.append(f"金利低下({us10y_5d:+.2f}%) → 株式に追い風")

    # S&P500 トレンド
    sp_change_20d = _pct("SP500", "change_20d")
    if sp_change_20d is not None:
        if sp_change_20d > 5:
            env_score += 10
            env_signals.append(f"S&P500上昇トレンド({sp_change_20d:+.2f}%)")
        elif sp_change_20d < -5:
            env_score -= 10
            env_signals.append(f"S&P500下落トレンド({sp_change_20d:+.2f}%)")

    # 原油動向 (シグナルのみ)
    oil_change_20d = _pct("OIL", "change_20d")
    if oil_change_20d is not None:
        if oil_change_20d > 10:
            env_signals.append(f"原油急騰({oil_change_20d:+.2f}%) → インフレ懸念")
        elif oil_change_20d < -10:
            env_signals.append(f"原油急落({oil_change_20d:+.2f}%) → デフレ/景気懸念")

    # ドル円 (シグナルのみ)
    usdjpy_change_20d = _pct("USDJPY", "change_20d")
    if usdjpy_change_20d is not None:
        if usdjpy_change_20d > 3:
            env_signals.append(f"円安進行({usdjpy_change_20d:+.2f}%) → 輸出企業に追い風")
        elif usdjpy_change_20d < -3:
            env_signals.append(f"円高進行({usdjpy_change_20d:+.2f}%) → 輸出企業に逆風")

    return env_score, env_signals


def fetch_macro(period: str = "3mo"):
    data = {}

    market_data = get_container().market_data()
    for key, meta in MACRO_SYMBOLS.items():
        try:
            hist = market_data.get_price_history(meta["symbol"], period=period, interval="1d")
            if hist.empty or len(hist) < 2:
                continue

            close = hist["Close"]
            current = float(close.iloc[-1])
            prev = float(close.iloc[-2])
            ret_1d = (current / prev - 1) * 100

            # 5日・20日リターン
            ret_5d = float((close.iloc[-1] / close.iloc[-6] - 1) * 100) if len(close) >= 6 else None
            ret_20d = (
                float((close.iloc[-1] / close.iloc[-21] - 1) * 100) if len(close) >= 21 else None
            )

            # 20日ボラティリティ
            daily_ret = close.pct_change().dropna()
            vol = (
                float(daily_ret.tail(20).std() * math.sqrt(252) * 100)
                if len(daily_ret) >= 20
                else None
            )

            # 位置（直近高安に対する位置）
            h20 = float(close.tail(20).max())
            l20 = float(close.tail(20).min())
            position = round((current - l20) / (h20 - l20) * 100, 1) if h20 != l20 else 50.0

            data[key] = {
                "label": meta["label"],
                "current": round(current, 2),
                "change_1d": f"{ret_1d:+.2f}%",
                "change_5d": f"{ret_5d:+.2f}%" if ret_5d is not None else None,
                "change_20d": f"{ret_20d:+.2f}%" if ret_20d is not None else None,
                "volatility": f"{vol:.1f}%" if vol is not None else None,
                "position_20d": f"{position}%",
            }
        except Exception:
            continue

    # ---- 市場環境スコア算出 ----
    env_score, env_signals = compute_env_score(data)

    # 環境判定
    if env_score >= 20:
        environment = "強気（リスクオン）"
    elif env_score >= 5:
        environment = "やや強気"
    elif env_score >= -5:
        environment = "中立"
    elif env_score >= -20:
        environment = "やや弱気"
    else:
        environment = "弱気（リスクオフ）"

    result = {
        "indicators": data,
        "market_environment": {
            "score": env_score,
            "assessment": environment,
            "signals": env_signals,
        },
    }

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result

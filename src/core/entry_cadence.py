#!/usr/bin/env python3
"""新規エントリー判断の頻度制御 (issue #22)。

30 分ごとに白紙からメインエージェントを起動すると、score_stock の日次変動 (日立 35 → 0、
アステラス 40 → 5) で swap が駆動し、同日内の翻意 (ソニー・富士通・1545・ホンダ) が常態化した。
保有 0〜4 営業日の 24 建玉は勝率 29% -179,783 円、5 営業日以上の 17 建玉は 65% +151,813 円。

新規エントリー判断 (Step 2 メインエージェント) は **1 日 1 回**、既定 10:30 JST 以降の最初の
サイクルだけ走らせる。他のサイクルは機械ストップ (Step 1) と AI 手仕舞い (Step 1.7) のみ。
判断済みかどうかは logs/entry_decision_state.json に JST 日付で記録する (デーモン再起動を
またいでも二重に走らない)。レジーム不成立で Step 2 が走らなかった日は「未判断」のままにし、
同日中にレジームが成立すれば判断できる。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
STATE_PATH = PROJECT_DIR / "logs" / "entry_decision_state.json"
JST = timezone(timedelta(hours=9))

DEFAULT_CONFIG: dict[str, Any] = {"enabled": True, "time_jst": "10:30"}


def load_cadence_config() -> dict[str, Any]:
    """config/trading_config.json の ``entry_decision`` (無ければ既定値)。"""
    cfg = dict(DEFAULT_CONFIG)
    try:
        from core.trade import load_config

        user = load_config().get("entry_decision", {})
        if isinstance(user, dict):
            cfg.update(user)
    except Exception:
        pass
    return cfg


def parse_hhmm(value: str) -> tuple[int, int]:
    h, m = str(value).strip().split(":")
    hh, mm = int(h), int(m)
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise ValueError(f"invalid time: {value}")
    return hh, mm


def load_state(path: Path = STATE_PATH) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def mark_decided(now: datetime | None = None, path: Path = STATE_PATH) -> dict[str, Any]:
    """このサイクルで新規エントリー判断を実施したことを記録する。"""
    now_jst = (now or datetime.now(JST)).astimezone(JST)
    state = {"last_date": now_jst.date().isoformat(), "last_time": now_jst.strftime("%H:%M")}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        pass
    return state


def should_decide(
    now: datetime, cfg: dict[str, Any] | None = None, state: dict[str, Any] | None = None
) -> tuple[bool, str]:
    """このサイクルで新規エントリー判断を走らせるか。(decide, 理由) を返す純関数。"""
    cfg = {**DEFAULT_CONFIG, **(cfg or {})}
    state = state or {}
    if not cfg.get("enabled", True):
        return True, "判断頻度の制限なし (entry_decision 無効)"
    now_jst = now.astimezone(JST)
    try:
        hh, mm = parse_hhmm(cfg.get("time_jst", DEFAULT_CONFIG["time_jst"]))
    except ValueError:
        hh, mm = parse_hhmm(DEFAULT_CONFIG["time_jst"])
    today = now_jst.date().isoformat()
    if state.get("last_date") == today:
        return False, f"本日の新規エントリー判断は {state.get('last_time', '?')} に実施済み"
    if (now_jst.hour, now_jst.minute) < (hh, mm):
        return False, f"本日の新規エントリー判断は {hh:02d}:{mm:02d} JST 以降の最初のサイクルで実施"
    return True, f"本日の新規エントリー判断 ({hh:02d}:{mm:02d} 以降の最初のサイクル)"


def check_now(now: datetime | None = None) -> tuple[bool, str]:
    """設定と状態ファイルを読んで should_decide を評価する。"""
    return should_decide(now or datetime.now(JST), load_cadence_config(), load_state())

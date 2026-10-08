#!/usr/bin/env python3
"""通過した重要イベントの事後評価 (issue #24 (3))。

market_calendar は「これから」のイベントしか見ず、通過後の結果を誰も評価していなかった
(回顧録 §2-3 (6): FOMC 0/14・短観 0/13・雇用統計 0/12・議事録 0/14)。カレンダー取得時に
イベントを履歴 (data/market_calendar_events.jsonl) に残し、直近に通過した importance=high の
イベントについて「指数がどう反応したか」を 1 行にしてプロンプトへ入れる。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
HISTORY_PATH = PROJECT_DIR / "data" / "market_calendar_events.jsonl"
JST = timezone(timedelta(hours=9))
_RANK = {"high": 2, "medium": 1, "low": 0}


def _parse_dt(value: Any) -> datetime | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M").replace(tzinfo=JST)
    except (TypeError, ValueError):
        return None


def record_events(events: Sequence[dict[str, Any]], path: Path = HISTORY_PATH) -> int:
    """カレンダー取得結果を履歴に追記 (datetime_jst + name で重複排除)。追加件数を返す。"""
    known = {(e.get("datetime_jst"), e.get("name")) for e in load_history(path)}
    added = 0
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for e in events:
                if not isinstance(e, dict) or _parse_dt(e.get("datetime_jst")) is None:
                    continue
                key = (e.get("datetime_jst"), e.get("name"))
                if key in known:
                    continue
                known.add(key)
                f.write(json.dumps({
                    "datetime_jst": e.get("datetime_jst"),
                    "name": str(e.get("name", ""))[:80],
                    "country": e.get("country"),
                    "importance": str(e.get("importance", "low")).lower(),
                }, ensure_ascii=False) + "\n")
                added += 1
    except OSError as e:
        print(f"⚠️  event history write failed: {e}", file=sys.stderr)
    return added


def load_history(path: Path = HISTORY_PATH) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return out


def passed_events(
    events: Sequence[dict[str, Any]], now: datetime, days: int = 3, min_importance: str = "high"
) -> list[dict[str, Any]]:
    """now から過去 days 日以内に通過した、importance が min_importance 以上のイベント (純関数)。"""
    now = now.astimezone(JST)
    lo = now - timedelta(days=days)
    thr = _RANK.get(min_importance, 2)
    out: list[dict[str, Any]] = []
    for e in events:
        dt = _parse_dt(e.get("datetime_jst"))
        if dt is None or not (lo <= dt <= now):
            continue
        if _RANK.get(str(e.get("importance", "low")).lower(), 0) < thr:
            continue
        out.append({**e, "_dt": dt})
    out.sort(key=lambda e: e["_dt"])
    return out


def index_reaction(event_dt: datetime, closes: Sequence[tuple[date, float]]) -> float | None:
    """イベント通過後の指数の反応 % (純関数)。

    基準 = イベント前の最後の終値 (場中・場前のイベントは前日終値、15:00 以降なら当日終値)。
    比較 = 最新終値。基準が無い / 最新がイベント前なら None。
    """
    if not closes:
        return None
    ev_date = event_dt.astimezone(JST).date()
    after_close = event_dt.astimezone(JST).hour >= 15
    base = None
    for d, c in closes:
        if d < ev_date or (after_close and d == ev_date):
            base = c
    latest_d, latest = closes[-1]
    if base is None or base <= 0 or latest_d < ev_date:
        return None
    return (latest / base - 1) * 100


def _index_closes(ticker: str) -> list[tuple[date, float]]:
    """直近 3 か月の日足終値 (中央値×3 の外れ値除外)。失敗は空。"""
    try:
        from core.regime import filter_outliers
        from infra.container import get_container

        hist = get_container().market_data().get_price_history(ticker, period="3mo", interval="1d")
        if hist is None or hist.empty:
            return []
        s = hist["Close"].dropna()
        vals = filter_outliers(list(s.values))
        keep = set(vals)
        return [(idx.date(), float(v)) for idx, v in s.items() if float(v) in keep]
    except Exception as e:
        print(f"⚠️  event digest: {ticker} 取得失敗: {e}", file=sys.stderr)
        return []


def digest_lines(now: datetime | None = None, days: int = 3) -> list[str]:
    """プロンプト用の 1 行要約 (通過した high イベント × 指数反応)。無ければ空。"""
    now = now or datetime.now(JST)
    events = passed_events(load_history(), now, days=days)
    if not events:
        return []
    topix = _index_closes("1306.T")
    nikkei = _index_closes("^N225")
    lines: list[str] = []
    for e in events:
        parts: list[str] = []
        for label, closes in (("TOPIX", topix), ("日経", nikkei)):
            r = index_reaction(e["_dt"], closes)
            if r is not None:
                parts.append(f"{label} {r:+.1f}%")
        reaction = " / ".join(parts) if parts else "指数反応 未取得"
        lines.append(f"{e.get('name', '?')} ({e['_dt']:%m/%d %H:%M}, {e.get('country', '')}) → 通過後 {reaction}")
    return lines


def prompt_block(now: datetime | None = None) -> str:
    lines = digest_lines(now)
    if not lines:
        return ""
    return (
        "\n## 📰 通過した重要イベントの事後評価（直近 3 日）\n"
        + "\n".join(f"- {line}" for line in lines)
        + "\n（事前の警戒と事後の実際の反応を照らし、過剰反応・無反応のどちらだったかを判断に織り込むこと）\n"
    )

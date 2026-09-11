#!/usr/bin/env python3
"""回転抑制ガード (issue #11)。

観測された粗い回転 (90分〜4時間の往復・同日3回売買、7例中5例が損失) を
機械的に止める2つのガードの判定ロジック。純関数 + diary 読み取りのみ。

- 売り側: 保有 min_hold_business_days 未満のポジションの AI 判断による
  全量売り (swap) を拒否。機械ストップ・ai_trim (部分利確) は対象外
- 買い側: 直近 reentry_cooldown_business_days 以内に CLOSE した銘柄の
  再取得を拒否 (反証ゲートから利用)

営業日は「土日を除く暦日」の近似 (祝日は考慮しない — 保守的側に倒れるだけ)。
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SRC_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = SRC_DIR.parent
sys.path.insert(0, str(SRC_DIR))

_JST = timezone(timedelta(hours=9))
_TRADES_DIR = PROJECT_DIR / "diary" / "trades"

DEFAULT_MIN_HOLD_BDAYS = 2
DEFAULT_REENTRY_COOLDOWN_BDAYS = 2


def _load_limits() -> dict[str, Any]:
    try:
        from core.trade import load_risk_limits

        return load_risk_limits()
    except Exception:
        return {}


def business_days_between(start: datetime, end: datetime) -> int:
    """start→end の営業日数 (JST 暦日ベース、土日除外、同日=0)。"""
    s = start.astimezone(_JST).date()
    e = end.astimezone(_JST).date()
    if e <= s:
        return 0
    days = 0
    d = s
    while d < e:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


def check_min_hold(
    entry_time: str | datetime | None,
    now: datetime | None = None,
    min_bdays: int | None = None,
) -> tuple[bool, str]:
    """AI 判断の全量売りを許可してよいか。(ok, 理由) を返す。

    entry_time が無い/壊れている場合は許可 (ガードで正当な手仕舞いを
    塞ぐ方が危険なため、安全側 = 許可に倒す)。
    """
    if min_bdays is None:
        min_bdays = int(_load_limits().get("min_hold_business_days", DEFAULT_MIN_HOLD_BDAYS))
    if not entry_time:
        return True, "entry_time 不明のため許可"
    try:
        dt = entry_time if isinstance(entry_time, datetime) else datetime.fromisoformat(str(entry_time))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return True, "entry_time パース不能のため許可"

    held = business_days_between(dt, now or datetime.now(UTC))
    if held < min_bdays:
        return False, (
            f"保有{held}営業日 < 最低{min_bdays}営業日 (取得 {dt.astimezone(_JST):%m/%d %H:%M})。"
            "回転抑制ガード: 機械ストップと fail_conditions 発動時の exit は対象外"
        )
    return True, f"保有{held}営業日"


def recent_closes(days: int = 7) -> dict[str, datetime]:
    """直近 days 日の CLOSE 約定 {ticker: 最新のクローズ時刻} (diary/trades から)。"""
    out: dict[str, datetime] = {}
    cutoff = datetime.now(UTC) - timedelta(days=days)
    try:
        paths = sorted(_TRADES_DIR.glob("*_trade.json"))[-200:]
    except OSError:
        return out
    for p in paths:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if str(d.get("action", "")).upper() != "CLOSE":
                continue
            if str(d.get("status", "")).upper() != "FILLED":
                continue
            ts = datetime.fromisoformat(str(d["timestamp"]))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if ts < cutoff:
                continue
            t = str(d.get("ticker", ""))
            if t and (t not in out or ts > out[t]):
                out[t] = ts
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            continue
    return out


def same_day_ai_sold_qty(ticker: str, now: datetime | None = None) -> int:
    """当日 (JST) に AI 判断経路 (ai_trim / ai_exit / swap) で売却済みの株数合計。

    機械ストップ (mech:*) は数えない — 安全装置の執行を抑制の分母にしないため。
    """
    now = now or datetime.now(UTC)
    today = now.astimezone(_JST).date()
    total = 0
    try:
        paths = sorted(_TRADES_DIR.glob("*_trade.json"))[-60:]
    except OSError:
        return 0
    for p in paths:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if str(d.get("ticker", "")) != ticker:
                continue
            if str(d.get("action", "")).upper() != "CLOSE":
                continue
            if str(d.get("status", "")).upper() != "FILLED":
                continue
            src = str(d.get("source", ""))
            if src.startswith("mech:"):
                continue
            ts = datetime.fromisoformat(str(d["timestamp"]))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if ts.astimezone(_JST).date() == today:
                total += int(d.get("quantity", 0) or 0)
        except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
            continue
    return total


def check_ai_sell(
    entry_time: str | datetime | None,
    ticker: str,
    sell_qty: int,
    held_qty: int,
    now: datetime | None = None,
    min_bdays: int | None = None,
    max_same_day_frac: float = 0.5,
) -> tuple[bool, str]:
    """AI 判断の売り (trim / exit / swap) を許可してよいか。(ok, 理由) を返す。

    trim 連射による最低保有ガードの迂回 (issue #13) を塞ぐ:
    最低保有期間内のポジションは、当日の AI 売り合算が建玉の
    max_same_day_frac (既定50%) を超える分を拒否する。
    - 全量 exit / swap 売りは合算に関係なく check_min_hold と同じ判定
    - 機械ストップは呼び出し側でこのガードを通さないこと (無条件即時)
    """
    ok_hold, hold_msg = check_min_hold(entry_time, now=now, min_bdays=min_bdays)
    if ok_hold:
        return True, hold_msg  # 最低保有を満たしていれば trim も exit も自由

    # 最低保有期間内: 当日合算で 50% までの部分縮小のみ許す
    base = held_qty + same_day_ai_sold_qty(ticker, now=now)  # 当日開始時点の建玉を復元
    already = same_day_ai_sold_qty(ticker, now=now)
    allowed = int(base * max_same_day_frac)
    if already + sell_qty > allowed:
        return False, (
            f"{hold_msg}。当日のAI売り合算 {already + sell_qty}/{base}株 が "
            f"上限{int(max_same_day_frac * 100)}% ({allowed}株) を超過 — trim 連射による"
            "最低保有ガードの迂回は不可 (issue #13)。機械ストップは対象外"
        )
    return True, f"{hold_msg} (部分縮小 {already + sell_qty}/{base}株 は50%以内で許可)"


def check_reentry(
    ticker: str,
    now: datetime | None = None,
    cooldown_bdays: int | None = None,
    closes: dict[str, datetime] | None = None,
) -> tuple[bool, str]:
    """この銘柄を新規取得してよいか。(ok, 理由) を返す。"""
    if cooldown_bdays is None:
        cooldown_bdays = int(
            _load_limits().get("reentry_cooldown_business_days", DEFAULT_REENTRY_COOLDOWN_BDAYS)
        )
    if closes is None:
        closes = recent_closes()
    last = closes.get(ticker)
    if last is None:
        return True, "直近の売却なし"
    elapsed = business_days_between(last, now or datetime.now(UTC))
    if elapsed < cooldown_bdays:
        return False, (
            f"再入場クールダウン中: {last.astimezone(_JST):%m/%d %H:%M} に売却したばかり "
            f"(経過{elapsed}営業日 < {cooldown_bdays}営業日)"
        )
    return True, f"売却から{elapsed}営業日経過"

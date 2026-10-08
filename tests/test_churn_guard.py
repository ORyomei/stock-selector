"""回転抑制ガード (issue #11) のテスト。

背景: 90分〜4時間の往復・同日3回売買が7例発生 (うち5例が損失)。
保有2営業日未満の swap 売りと、売却2営業日未満の再入場を機械的に止める。
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.churn_guard import (  # noqa: E402
    business_days_between,
    check_min_hold,
    check_reentry,
)

# 2026-08-28 は金曜 (JST)
_FRI = datetime(2026, 8, 28, 6, 0, tzinfo=UTC)  # 15:00 JST 金曜


def test_business_days_same_day_is_zero():
    start = datetime(2026, 8, 28, 0, 30, tzinfo=UTC)  # 同日朝 (9:30 JST)
    assert business_days_between(start, _FRI) == 0


def test_business_days_skips_weekend():
    fri = datetime(2026, 8, 21, 6, 0, tzinfo=UTC)  # 前週金曜
    mon = datetime(2026, 8, 24, 1, 0, tzinfo=UTC)  # 月曜
    assert business_days_between(fri, mon) == 1  # 土日を挟んでも1営業日


def test_min_hold_blocks_same_day_swap():
    """ソニー36分往復 (8/26) の再現: 同日取得の swap 売りは拒否。"""
    entry = datetime(2026, 8, 28, 5, 24, tzinfo=UTC)  # 36分前に取得
    ok, msg = check_min_hold(entry, now=_FRI, min_bdays=2)
    assert ok is False
    assert "保有0営業日" in msg


def test_min_hold_allows_after_two_business_days():
    entry = datetime(2026, 8, 26, 1, 0, tzinfo=UTC)  # 水曜取得 → 金曜で2営業日
    ok, _ = check_min_hold(entry, now=_FRI, min_bdays=2)
    assert ok is True


def test_min_hold_fails_open_on_missing_entry_time():
    """entry_time 不明はガードで塞がず許可 (正当な手仕舞いを止める方が危険)。"""
    ok, msg = check_min_hold(None, now=_FRI)
    assert ok is True
    assert "不明" in msg


def test_reentry_blocks_recent_close():
    """キヤノン翌日再入場 (8/26売り→8/27買い) の再現: 拒否。"""
    closes = {"7751.T": datetime(2026, 8, 27, 5, 17, tzinfo=UTC)}  # 木曜に売却
    ok, msg = check_reentry("7751.T", now=_FRI, cooldown_bdays=2, closes=closes)
    assert ok is False
    assert "クールダウン" in msg


def test_reentry_allows_after_cooldown():
    closes = {"7751.T": datetime(2026, 8, 24, 5, 0, tzinfo=UTC)}  # 月曜売却 → 金曜=4営業日
    ok, _ = check_reentry("7751.T", now=_FRI, cooldown_bdays=2, closes=closes)
    assert ok is True


def test_reentry_allows_unknown_ticker():
    ok, _ = check_reentry("9999.T", now=_FRI, cooldown_bdays=2, closes={})
    assert ok is True


# ── issue #19: 再エントリー禁止 20 営業日 ──────────────────────────────
# 回顧録 (2026-10-08): 再エントリー 14 建玉は勝率 21%・-71,919 円。2 営業日では
# 1〜2 週間後の買い直しを止められなかった。

from core.churn_guard import add_business_days, reentry_banned  # noqa: E402


def test_add_business_days_skips_weekend():
    from datetime import date

    assert add_business_days(date(2026, 8, 28), 1) == date(2026, 8, 31)  # 金 → 月
    assert add_business_days(date(2026, 8, 28), 20) == date(2026, 9, 25)
    assert add_business_days(date(2026, 8, 28), 0) == date(2026, 8, 28)


def test_reentry_blocks_two_weeks_after_close_with_20bday_cooldown():
    """8/14 (金) 売却 → 8/28 (金) は 10 営業日: 20 営業日ルールでは拒否。"""
    closes = {"7751.T": datetime(2026, 8, 14, 5, 0, tzinfo=UTC)}
    ok, msg = check_reentry("7751.T", now=_FRI, cooldown_bdays=20, closes=closes)
    assert ok is False
    assert "経過10営業日 < 20営業日" in msg


def test_reentry_allows_after_20_business_days():
    closes = {"7751.T": datetime(2026, 7, 30, 5, 0, tzinfo=UTC)}  # 木 → 8/28 で 21 営業日
    ok, _ = check_reentry("7751.T", now=_FRI, cooldown_bdays=20, closes=closes)
    assert ok is True


def test_reentry_banned_lists_release_dates_in_order():
    from datetime import date

    closes = {
        "7751.T": datetime(2026, 8, 14, 5, 0, tzinfo=UTC),  # 解除 9/11
        "6758.T": datetime(2026, 8, 27, 5, 0, tzinfo=UTC),  # 解除 9/24
        "9984.T": datetime(2026, 7, 30, 5, 0, tzinfo=UTC),  # 21 営業日経過 → 対象外
    }
    banned = reentry_banned(now=_FRI, cooldown_bdays=20, closes=closes)
    assert list(banned.items()) == [
        ("7751.T", date(2026, 9, 11)),
        ("6758.T", date(2026, 9, 24)),
    ]
    # 解除日当日は check_reentry も許可する (両者の整合)
    release = datetime(2026, 9, 11, 1, 0, tzinfo=UTC)
    ok, _ = check_reentry("7751.T", now=release, cooldown_bdays=20, closes=closes)
    assert ok is True

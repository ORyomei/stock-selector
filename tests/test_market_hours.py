"""取引時間・休場日判定 (issue #18: 祝日にサイクルが空転していた)。"""
from datetime import date, datetime, timedelta, timezone

import pytest

from agents.auto_trade import _is_jp_market_holiday, _should_skip_cycle, _skip_reason

JST = timezone(timedelta(hours=9))


def _at(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=JST)


# ── 東証 (--market jp) ──────────────────────────────────────────────────────

def test_jp_weekday_in_hours_is_active():
    assert _skip_reason("jp", _at(2026, 9, 15, 10)) is None      # 火 10:00
    assert _should_skip_cycle("jp", _at(2026, 9, 15, 8, 30)) is False
    assert _should_skip_cycle("jp", _at(2026, 9, 15, 16, 0)) is False


def test_jp_outside_hours_and_weekend():
    assert _skip_reason("jp", _at(2026, 9, 15, 7, 59)) == "取引時間外"
    assert _skip_reason("jp", _at(2026, 9, 15, 16, 1)) == "取引時間外"
    assert _skip_reason("jp", _at(2026, 9, 19, 10)) == "取引時間外"  # 土


@pytest.mark.parametrize(
    "ymd, name",
    [
        ((2026, 8, 11), "山の日"),        # 実測: 14 サイクル空転した日
        ((2026, 9, 21), "敬老の日"),
        ((2026, 9, 22), "国民の休日"),    # 祝日に挟まれた平日
        ((2026, 9, 23), "秋分の日"),
        ((2026, 5, 6), "憲法記念日 振替休日"),  # 5/3 が日曜 → 5/6 に振替
    ],
)
def test_jp_holiday_in_hours_is_closed(ymd, name):
    reason = _skip_reason("jp", _at(*ymd, 10))
    assert reason == f"休場日 ({name})"
    assert _should_skip_cycle("jp", _at(*ymd, 10)) is True


def test_jp_year_end_new_year_closed():
    assert _skip_reason("jp", _at(2026, 12, 31, 10)) == "休場日 (年末休場)"
    assert _skip_reason("jp", _at(2027, 1, 1, 10)) == "休場日 (年始休場)"
    assert _skip_reason("jp", _at(2027, 1, 4, 10)) is None            # 大発会 (月)


def test_jp_holiday_outside_hours_reports_hours_not_holiday():
    # 休場日でも時間外なら「取引時間外」(理由は場中判定にだけ意味がある)
    assert _skip_reason("jp", _at(2026, 9, 21, 7)) == "取引時間外"


def test_is_jp_market_holiday_helper():
    assert _is_jp_market_holiday(date(2026, 9, 21)) == "敬老の日"
    assert _is_jp_market_holiday(date(2026, 12, 31)) == "年末休場"
    assert _is_jp_market_holiday(date(2026, 1, 2)) == "年始休場"
    assert _is_jp_market_holiday(date(2026, 9, 24)) is None


# ── 米国 / all は東証の祝日に影響されない ───────────────────────────────────

def test_us_market_ignores_jp_holiday():
    assert _skip_reason("us", _at(2026, 9, 21, 23)) is None          # 月 23:00 JST
    assert _skip_reason("us", _at(2026, 9, 21, 10)) == "取引時間外"
    assert _skip_reason("us", _at(2026, 9, 21, 3)) == "取引時間外"   # 月早朝 = 米日曜


def test_all_market_on_jp_holiday():
    assert _skip_reason("all", _at(2026, 9, 21, 10)) == "休場日 (敬老の日)"
    assert _skip_reason("all", _at(2026, 9, 21, 23)) is None
    assert _skip_reason("all", _at(2026, 9, 15, 10)) is None


def test_default_now_is_used_when_not_injected():
    # 例外なく bool を返すことだけ確認 (現在時刻依存)
    assert isinstance(_should_skip_cycle("jp"), bool)

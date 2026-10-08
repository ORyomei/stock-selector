"""通過イベントの事後評価 (issue #24 (3)) のテスト。"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.event_digest import (  # noqa: E402
    index_reaction,
    load_history,
    passed_events,
    record_events,
)

JST = timezone(timedelta(hours=9))
_NOW = datetime(2026, 10, 9, 10, 30, tzinfo=JST)
_EVENTS = [
    {"datetime_jst": "2026-10-07 03:00", "name": "FOMC 議事録", "importance": "high", "country": "アメリカ"},
    {"datetime_jst": "2026-10-08 21:30", "name": "新規失業保険", "importance": "medium", "country": "アメリカ"},
    {"datetime_jst": "2026-10-10 21:30", "name": "米雇用統計", "importance": "high", "country": "アメリカ"},  # 未来
    {"datetime_jst": "2026-10-01 10:00", "name": "日銀短観", "importance": "high", "country": "日本"},  # 3 日超
    {"datetime_jst": "bad", "name": "壊れた", "importance": "high"},
]


def test_passed_events_filters_window_and_importance():
    out = passed_events(_EVENTS, _NOW, days=3)
    assert [e["name"] for e in out] == ["FOMC 議事録"]
    assert [e["name"] for e in passed_events(_EVENTS, _NOW, days=3, min_importance="medium")] == [
        "FOMC 議事録", "新規失業保険",
    ]


_CLOSES = [
    (date(2026, 10, 6), 430.0),
    (date(2026, 10, 7), 437.0),
    (date(2026, 10, 8), 430.6),
]


def test_index_reaction_uses_prior_close_for_intraday_event():
    ev = datetime(2026, 10, 7, 3, 0, tzinfo=JST)  # 10/7 未明 → 基準は 10/6 終値 430
    assert round(index_reaction(ev, _CLOSES), 2) == round((430.6 / 430.0 - 1) * 100, 2)


def test_index_reaction_uses_same_day_close_for_after_hours_event():
    ev = datetime(2026, 10, 7, 21, 30, tzinfo=JST)  # 大引け後 → 基準は 10/7 終値 437
    assert round(index_reaction(ev, _CLOSES), 2) == round((430.6 / 437.0 - 1) * 100, 2)


def test_index_reaction_none_without_data_after_event():
    ev = datetime(2026, 10, 9, 21, 30, tzinfo=JST)
    assert index_reaction(ev, _CLOSES) is None
    assert index_reaction(ev, []) is None


def test_record_events_dedupes_and_skips_bad_dates(tmp_path):
    path = tmp_path / "events.jsonl"
    assert record_events(_EVENTS, path=path) == 4  # 壊れた日付は捨てる
    assert record_events(_EVENTS, path=path) == 0  # 重複なし
    assert record_events([{"datetime_jst": "2026-10-12 09:00", "name": "新規", "importance": "low"}], path=path) == 1
    names = [e["name"] for e in load_history(path)]
    assert names == ["FOMC 議事録", "新規失業保険", "米雇用統計", "日銀短観", "新規"]

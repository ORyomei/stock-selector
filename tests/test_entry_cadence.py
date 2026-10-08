"""新規エントリー判断の頻度制御 (issue #22) のテスト。

1 日 1 回 (既定 10:30 JST 以降の最初のサイクル) だけメインエージェントを走らせる。
保有 0〜4 営業日 24 建玉 29% -179,783 円 / 5 営業日以上 17 建玉 65% +151,813 円。
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.entry_cadence import load_state, mark_decided, should_decide  # noqa: E402

_CFG = {"enabled": True, "time_jst": "10:30"}


def _jst(h: int, m: int, day: int = 9) -> datetime:
    return datetime(2026, 10, day, h - 9, m, tzinfo=UTC)  # UTC = JST - 9h


def test_before_decision_time_waits():
    ok, msg = should_decide(_jst(9, 0), _CFG, {})
    assert ok is False and "10:30" in msg


def test_first_cycle_after_time_decides():
    ok, msg = should_decide(_jst(10, 31), _CFG, {})
    assert ok is True and "最初のサイクル" in msg


def test_exact_time_decides():
    ok, _ = should_decide(_jst(10, 30), _CFG, {})
    assert ok is True


def test_already_decided_today_skips():
    state = {"last_date": "2026-10-09", "last_time": "10:31"}
    ok, msg = should_decide(_jst(13, 0), _CFG, state)
    assert ok is False and "10:31 に実施済み" in msg


def test_previous_day_state_does_not_block():
    state = {"last_date": "2026-10-08", "last_time": "10:31"}
    ok, _ = should_decide(_jst(10, 45), _CFG, state)
    assert ok is True


def test_missed_slot_runs_later_same_day():
    """10:30 のサイクルをデーモン停止で逃しても、同日の次サイクルで判断する。"""
    ok, _ = should_decide(_jst(14, 5), _CFG, {"last_date": "2026-10-08"})
    assert ok is True


def test_disabled_always_decides():
    ok, msg = should_decide(_jst(9, 0), {"enabled": False}, {"last_date": "2026-10-09"})
    assert ok is True and "無効" in msg


def test_bad_time_falls_back_to_default():
    ok, _ = should_decide(_jst(10, 0), {"time_jst": "nonsense"}, {})
    assert ok is False  # 既定 10:30 より前


def test_state_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    st = mark_decided(_jst(10, 31), path=path)
    assert st == {"last_date": "2026-10-09", "last_time": "10:31"}
    assert load_state(path) == st
    assert should_decide(_jst(15, 0), _CFG, load_state(path))[0] is False
    assert load_state(tmp_path / "missing.json") == {}

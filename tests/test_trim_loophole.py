"""trim 連射による最低保有ガード迂回の封鎖 (issue #13) のテスト。

背景: swap ガード (#11) 導入後、保有1〜2営業日の全量売却が ai_trim の連射に
分解されて素通りした (9/8 の 5401.T: SWAP_BLOCKED 直後に trim×5+exit×1 で
-27,306。同型5件で計 -78,170)。最低保有期間内は当日 AI 売り合算 50% までとする。
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import core.churn_guard as cg  # noqa: E402

_FRI = datetime(2026, 9, 11, 5, 0, tzinfo=UTC)  # 14:00 JST 金曜


def _no_prior_sales(monkeypatch):
    monkeypatch.setattr(cg, "same_day_ai_sold_qty", lambda *a, **k: 0)


def test_first_half_trim_allowed_within_min_hold(monkeypatch):
    """day-0 でも 50% までの trim は許可 (部分利確・部分損切りは正当な機能)。"""
    _no_prior_sales(monkeypatch)
    entry = datetime(2026, 9, 11, 0, 30, tzinfo=UTC)  # 当日朝取得
    ok, msg = cg.check_ai_sell(entry, "5401.T", sell_qty=600, held_qty=1300, now=_FRI, min_bdays=2)
    assert ok is True
    assert "50%以内" in msg


def test_serial_trim_blocked_within_min_hold(monkeypatch):
    """5401.T の実パターン再現: 既に 600/1300 売却済み → 追加 300 は 50% 超で拒否。"""
    monkeypatch.setattr(cg, "same_day_ai_sold_qty", lambda *a, **k: 600)
    entry = datetime(2026, 9, 11, 0, 30, tzinfo=UTC)
    ok, msg = cg.check_ai_sell(entry, "5401.T", sell_qty=300, held_qty=700, now=_FRI, min_bdays=2)
    assert ok is False
    assert "迂回は不可" in msg


def test_full_exit_blocked_within_min_hold(monkeypatch):
    """day-0 の全量 ai_exit は拒否 (50% 上限に必ず抵触)。"""
    _no_prior_sales(monkeypatch)
    entry = datetime(2026, 9, 11, 0, 30, tzinfo=UTC)
    ok, _ = cg.check_ai_sell(entry, "4661.T", sell_qty=300, held_qty=300, now=_FRI, min_bdays=2)
    assert ok is False


def test_unrestricted_after_min_hold(monkeypatch):
    """最低保有を満たせば全量 exit も自由。"""
    monkeypatch.setattr(cg, "same_day_ai_sold_qty", lambda *a, **k: 600)
    entry = datetime(2026, 9, 8, 0, 30, tzinfo=UTC)  # 火曜取得 → 金曜で3営業日
    ok, _ = cg.check_ai_sell(entry, "9433.T", sell_qty=1000, held_qty=1000, now=_FRI, min_bdays=2)
    assert ok is True


def test_missing_entry_time_fails_open(monkeypatch):
    _no_prior_sales(monkeypatch)
    ok, _ = cg.check_ai_sell(None, "9433.T", sell_qty=100, held_qty=100, now=_FRI)
    assert ok is True

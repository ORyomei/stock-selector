"""AI手仕舞い最小保有期間 + 期待値バケット集計のテスト。"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agents.portfolio_helpers import held_calendar_days  # noqa: E402
from web import dashboard_data as dd  # noqa: E402

# ── held_calendar_days (JST 暦日) ────────────────────────────────


def test_same_jst_day_is_zero():
    entry = datetime(2026, 7, 3, 0, 30, tzinfo=UTC)  # 09:30 JST
    now = datetime(2026, 7, 3, 6, 0, tzinfo=UTC)  # 15:00 JST 同日
    assert held_calendar_days(entry, now) == 0


def test_next_jst_day_is_one_even_within_24h():
    entry = datetime(2026, 7, 3, 6, 0, tzinfo=UTC)  # 7/3 15:00 JST
    now = datetime(2026, 7, 3, 16, 0, tzinfo=UTC)  # 7/4 01:00 JST (19時間後だが翌暦日)
    assert held_calendar_days(entry, now) == 1


def test_naive_entry_treated_as_utc():
    entry = datetime(2026, 7, 3, 6, 0)  # naive → UTC とみなす
    now = datetime(2026, 7, 4, 6, 0, tzinfo=UTC)
    assert held_calendar_days(entry, now) == 1


def test_unknown_entry_is_always_eligible():
    assert held_calendar_days(None) == 9999


# ── 期待値バケット (純関数) ──────────────────────────────────────


def test_hold_bucket():
    assert dd._hold_bucket({"hold_days": 0}) == "当日"
    assert dd._hold_bucket({"hold_days": 2}) == "1-3日"
    assert dd._hold_bucket({"hold_days": 7}) == "4-10日"
    assert dd._hold_bucket({"hold_days": 15}) == "11日+"
    assert dd._hold_bucket({}) == "不明"  # legacy レコード


def test_score_bucket():
    assert dd._score_bucket({"entry_score": 20}) == "score<25"
    assert dd._score_bucket({"entry_score": 30}) == "score25-40"
    assert dd._score_bucket({"entry_score": 45}) == "score>40"
    assert dd._score_bucket({}) == "不明"


def test_join_entry_meta_picks_latest_prior_entry():
    closes = [{"ticker": "A.T", "timestamp": "2026-07-03T05:00", "pnl": 100}]
    entries = [
        {"ticker": "A.T", "timestamp": "2026-07-01T01:00", "score": 20, "confidence": 0.6},
        {"ticker": "A.T", "timestamp": "2026-07-02T01:00", "score": 30, "confidence": 0.7},
        {"ticker": "A.T", "timestamp": "2026-07-04T01:00", "score": 50, "confidence": 0.9},  # 後 → 対象外
        {"ticker": "B.T", "timestamp": "2026-07-02T02:00", "score": 40, "confidence": 0.8},  # 別銘柄
    ]
    joined = dd._join_entry_meta(closes, entries)
    assert joined[0]["entry_score"] == 30  # 直近の先行エントリー
    assert joined[0]["entry_confidence"] == 0.7


def test_join_entry_meta_no_match_leaves_unknown():
    joined = dd._join_entry_meta([{"ticker": "C.T", "timestamp": "2026-07-03", "pnl": 1}], [])
    assert "entry_score" not in joined[0]


def test_bucket_stats():
    items = [
        {"hold_days": 0, "pnl": 100},
        {"hold_days": 0, "pnl": -50},
        {"hold_days": 5, "pnl": 200},
        {"hold_days": 5, "pnl": "bad"},  # 非数値は無視
    ]
    stats = dd._bucket_stats(items, dd._hold_bucket)
    assert stats["当日"] == {"count": 2, "win_rate": 50.0, "total_pnl": 50, "avg_pnl": 25}
    assert stats["4-10日"]["count"] == 1


# ── equity_history (JSONL 読み込み) ──────────────────────────────


def test_equity_history_parses_and_skips_broken_lines(monkeypatch, tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "equity_history.jsonl").write_text(
        '{"ts": "2026-07-04T01:00:00+00:00", "equity_jpy": 3000000}\n'
        "{ broken\n"
        "\n"
        '{"ts": "2026-07-04T02:00:00+00:00", "equity_jpy": 3010000}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(dd, "PROJECT_DIR", tmp_path)
    hist = dd.equity_history()
    assert len(hist) == 2
    assert hist[0]["equity_jpy"] == 3000000
    assert hist[0]["x"] == "2026-07-04 10:00:00"  # JST 変換

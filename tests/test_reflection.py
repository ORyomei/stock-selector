"""振り返り学習ループの集計/フィルタのテスト (純関数部分)。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agents import reflection as rf  # noqa: E402


class _FakeDiary:
    def __init__(self, trades):
        self._trades = trades

    def load_recent_trades(self, days=30):
        return self._trades


class _FakeContainer:
    def __init__(self, diary):
        self._diary = diary

    def diary(self):
        return self._diary


def _patch_trades(monkeypatch, trades):
    monkeypatch.setattr(rf, "get_container", lambda: _FakeContainer(_FakeDiary(trades)))


def test_only_closed_trades_with_pnl(monkeypatch):
    trades = [
        {"action": "BUY", "ticker": "X.T", "pnl": -50, "timestamp": "2026-06-10T00:00:00"},  # 除外(BUY)
        {"action": "CLOSE", "ticker": "A.T", "pnl": 1000, "reason": "take_profit", "timestamp": "2026-06-11T00:00:00"},
        {"action": "SELL", "ticker": "B.T", "pnl": -300, "reason": "stop_loss", "timestamp": "2026-06-09T00:00:00"},
        {"action": "CLOSE", "ticker": "C.T", "pnl": None, "timestamp": "2026-06-08T00:00:00"},  # 除外(pnl無)
    ]
    _patch_trades(monkeypatch, trades)
    closed = rf._recent_closed_trades()
    assert [c["ticker"] for c in closed] == ["A.T", "B.T"]  # 新しい順、BUY/pnl無は除外


def test_aggregate_win_rate_and_pnl():
    closed = [
        {"ticker": "A", "pnl": 1000, "reason": "", "date": "2026-06-11"},
        {"ticker": "B", "pnl": -300, "reason": "", "date": "2026-06-10"},
        {"ticker": "C", "pnl": 500, "reason": "", "date": "2026-06-09"},
        {"ticker": "D", "pnl": -200, "reason": "", "date": "2026-06-08"},
    ]
    s = rf._aggregate(closed)
    assert s["count"] == 4
    assert s["wins"] == 2 and s["losses"] == 2
    assert s["win_rate_pct"] == 50.0
    assert s["total_pnl"] == 1000  # 1000-300+500-200
    assert s["avg_win"] == 750 and s["avg_loss"] == -250


def test_aggregate_empty():
    assert rf._aggregate([])["count"] == 0


def test_reflect_skips_when_too_few(monkeypatch):
    """クローズ3件未満なら LLM を呼ばず空文字。"""
    _patch_trades(monkeypatch, [
        {"action": "CLOSE", "ticker": "A.T", "pnl": 100, "timestamp": "2026-06-11T00:00:00"},
    ])
    called = []
    monkeypatch.setattr(rf, "call_ai", lambda *a, **k: called.append(1) or "x")
    assert rf.reflect_on_history() == ""
    assert not called  # LLM 未呼び出し


def test_reflect_caches_when_no_new_close(monkeypatch):
    """同じクローズ集合なら2回目は LLM を呼ばずキャッシュ再利用。"""
    rf._cache["key"] = ""
    rf._cache["lessons"] = ""
    trades = [
        {"action": "CLOSE", "ticker": "A.T", "pnl": 100, "reason": "tp", "timestamp": "2026-06-11T00:00:00"},
        {"action": "CLOSE", "ticker": "B.T", "pnl": -50, "reason": "sl", "timestamp": "2026-06-10T00:00:00"},
        {"action": "CLOSE", "ticker": "C.T", "pnl": 30, "reason": "tp", "timestamp": "2026-06-09T00:00:00"},
    ]
    _patch_trades(monkeypatch, trades)
    calls = []
    monkeypatch.setattr(rf, "call_ai", lambda *a, **k: calls.append(1) or "教訓: テスト")
    first = rf.reflect_on_history()
    second = rf.reflect_on_history()
    assert first == second == "教訓: テスト"
    assert len(calls) == 1  # 2回目はキャッシュ

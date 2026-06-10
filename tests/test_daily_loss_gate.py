"""日次損失サーキットブレーカー / 総資産算出 / 集中超過警告のテスト。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from datetime import UTC, datetime  # noqa: E402

from agents import portfolio_helpers as gt  # noqa: E402
from domain.models import Position  # noqa: E402


class _FakeDiary:
    def __init__(self, trades):
        self._trades = trades

    def load_recent_trades(self, days=30):
        return self._trades


class _FakeBroker:
    """総資産・集中度の読み取り元 (状態の所有者)。"""

    def __init__(self, positions, cash_jpy):
        self._positions = [
            Position(
                ticker=p["ticker"],
                quantity=p["quantity"],
                entry_price=p.get("entry_price", p["current_price"]),
                current_price=p["current_price"],
                entry_time=datetime.now(UTC),
            )
            for p in positions
        ]
        self._cash_jpy = cash_jpy

    def get_balance(self):
        return {"cash_jpy": self._cash_jpy, "cash_usd": 0.0, "timestamp": datetime.now(UTC)}

    def get_positions(self):
        return self._positions


class _FakeContainer:
    def __init__(self, broker, diary):
        self._broker = broker
        self._diary = diary

    def broker(self):
        return self._broker

    def diary(self):
        return self._diary


def _patch(monkeypatch, *, positions, cash_jpy, trades, max_daily=2.0, max_pos=30.0):
    broker = _FakeBroker(positions, cash_jpy)
    monkeypatch.setattr(gt, "get_container", lambda: _FakeContainer(broker, _FakeDiary(trades)))

    import core.trade as ct

    monkeypatch.setattr(
        ct, "load_risk_limits",
        lambda: {"max_daily_loss_pct": max_daily, "max_position_size_pct": max_pos},
    )


def _today_iso():
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def test_total_equity_jpy(monkeypatch):
    _patch(
        monkeypatch,
        positions=[{"ticker": "8306.T", "quantity": 500, "current_price": 3000}],
        cash_jpy=1_000_000,
        trades=[],
    )
    # 現金100万 + 8306.T 500*3000=150万 = 250万
    assert gt.total_equity_jpy() == 2_500_000


def test_daily_loss_triggers_when_exceeded(monkeypatch):
    # 総資産 100万、本日実現損 -30,000 = -3% > 上限2% → True
    _patch(
        monkeypatch,
        positions=[],
        cash_jpy=1_000_000,
        trades=[{"timestamp": _today_iso(), "pnl": -30_000}],
        max_daily=2.0,
    )
    logged = []
    assert gt.daily_loss_exceeded(logged.append) is True
    assert logged  # 理由がログされる


def test_daily_loss_ok_within_limit(monkeypatch):
    # -15,000 = -1.5% < 2% → False
    _patch(
        monkeypatch,
        positions=[],
        cash_jpy=1_000_000,
        trades=[{"timestamp": _today_iso(), "pnl": -15_000}],
        max_daily=2.0,
    )
    assert gt.daily_loss_exceeded(lambda *_: None) is False


def test_daily_loss_ignores_profit_and_old_trades(monkeypatch):
    # 本日は利益、損失は過去日付 → ブレーカー作動しない
    _patch(
        monkeypatch,
        positions=[],
        cash_jpy=1_000_000,
        trades=[
            {"timestamp": _today_iso(), "pnl": 50_000},
            {"timestamp": "2020-01-01T00:00:00+00:00", "pnl": -500_000},
        ],
        max_daily=2.0,
    )
    assert gt.daily_loss_exceeded(lambda *_: None) is False


def test_overweight_warning_fires(monkeypatch):
    # 8306.T が総資産の ~60% → 警告
    _patch(
        monkeypatch,
        positions=[{"ticker": "8306.T", "quantity": 500, "current_price": 3000}],
        cash_jpy=1_000_000,
        trades=[],
        max_pos=30.0,
    )
    logged = []
    gt.warn_overweight_positions(logged.append)
    assert any("集中超過" in m and "8306.T" in m for m in logged)

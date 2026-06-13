"""ダッシュボード状態アセンブラの純関数テスト。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from web import dashboard_data as dd  # noqa: E402


def test_value_jpy_currency_conversion():
    assert dd._value_jpy("7203.T", 1000.0, 100) == 100_000.0  # 日本株はそのまま
    assert dd._value_jpy("AAPL", 100.0, 10) == 100.0 * 10 * dd.USD_JPY  # 米国株は換算


def test_close_reason_bucket():
    assert dd._close_reason_bucket("Auto-close: stop_loss") == "stop_loss"
    assert dd._close_reason_bucket("利確到達") == "take_profit"
    assert dd._close_reason_bucket("trailing_stop") == "trailing"
    assert dd._close_reason_bucket("hold_timeout_30d") == "max_hold"
    assert dd._close_reason_bucket("Manual close: 100 shares") == "manual/ai_exit"
    assert dd._close_reason_bucket("Order filled: 100 @ 50") == "other"


def test_portfolio_overview_shape(monkeypatch):
    """ブローカーを触らず portfolio.json (repo) から組み立てる。"""
    fake_pf = {
        "balance": {"cash_jpy": 500_000, "cash_usd": 0},
        "positions": [
            {"ticker": "8306.T", "quantity": 500, "entry_price": 3000,
             "current_price": 3100, "stop_loss": 2900, "take_profit": 3500},
        ],
    }

    class _FakeRepo:
        def load(self):
            return fake_pf

    class _FakeContainer:
        def portfolio(self):
            return _FakeRepo()

    monkeypatch.setattr(dd, "get_container", lambda: _FakeContainer())
    import core.trade as ct
    monkeypatch.setattr(ct, "load_risk_limits", lambda: {"max_position_size_pct": 30})

    ov = dd.portfolio_overview()
    # equity = 50万現金 + 8306.T 500*3100=155万 = 205万
    assert ov["equity_jpy"] == 2_050_000
    h = ov["holdings"][0]
    assert h["ticker"] == "8306.T"
    assert h["pnl"] == 50_000  # (3100-3000)*500
    assert h["pnl_pct"] == 3.33
    assert h["concentration_pct"] == round(1_550_000 / 2_050_000 * 100, 1)
    assert h["dist_to_stop_pct"] is not None  # 損切りまでの距離

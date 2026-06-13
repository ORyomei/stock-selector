"""ダッシュボード状態アセンブラの純関数テスト。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from web import dashboard_data as dd  # noqa: E402


def test_value_jpy_currency_conversion():
    assert dd._value_jpy("7203.T", 1000.0, 100) == 100_000.0  # 日本株はそのまま
    assert dd._value_jpy("AAPL", 100.0, 10) == 100.0 * 10 * dd.USD_JPY  # 米国株は換算


def test_etf_name_override():
    """既知ETFは yfinance を呼ばず固定のファンド名を返す。"""
    assert dd._resolve_name("2559.T") == "MAXIS 全世界株式 (オルカン/ACWI)"
    assert dd._resolve_name("1306.T") == "NEXT FUNDS TOPIX"


def test_name_fallback_to_ticker(monkeypatch):
    """名前解決に失敗したらティッカーにフォールバック。"""
    dd._NAME_CACHE.clear()
    import core.portfolio_ops as po
    monkeypatch.setattr(po, "get_ticker_name", lambda t: (_ for _ in ()).throw(RuntimeError("net")))
    assert dd._resolve_name("ZZZZ.T") == "ZZZZ.T"


def test_close_reason_bucket():
    assert dd._close_reason_bucket("Auto-close: stop_loss") == "stop_loss"
    assert dd._close_reason_bucket("利確到達") == "take_profit"
    assert dd._close_reason_bucket("trailing_stop") == "trailing"
    assert dd._close_reason_bucket("hold_timeout_30d") == "max_hold"
    assert dd._close_reason_bucket("Manual close: 100 shares") == "manual/ai_exit"
    assert dd._close_reason_bucket("Order filled: 100 @ 50") == "other"


def test_trace_to_dot():
    """ツール呼び出し列が開始→各ツール→最終判断の有向グラフ DOT になる。"""
    steps = [
        {"type": "user", "text": "分析して"},
        {"type": "tool_call", "tool": "check_macro", "args": {}},
        {"type": "tool_result", "tool": "check_macro", "summary": "..."},
        {"type": "tool_call", "tool": "score_stock", "args": {"ticker": "7203.T"}},
        {"type": "tool_call", "tool": "submit_signals", "args": {"signals": []}},
        {"type": "final", "text": "買い"},
    ]
    dot = dd.trace_to_dot(steps)
    assert "digraph" in dot
    assert "check_macro" in dot
    assert "score_stock\\n7203.T" in dot  # 主要引数がラベルに出る
    assert "submit_signals" in dot
    assert "開始" in dot and "最終判断" in dot
    assert dot.count("->") == 4  # 5ノード(開始+3ツール+最終判断)= 4エッジ


def test_flag_on_at():
    """クローズ時刻時点のフラグ状態を正しく引く (記録前は None)。"""
    log = [
        {"ts": "2026-06-01T00:00:00+00:00", "flags": {"ai_exit_advisor": True}},
        {"ts": "2026-06-10T00:00:00+00:00", "flags": {"ai_exit_advisor": False}},
    ]
    assert dd._flag_on_at(log, "ai_exit_advisor", "2026-05-30T00:00:00+00:00") is None  # 記録前
    assert dd._flag_on_at(log, "ai_exit_advisor", "2026-06-05T00:00:00+00:00") is True   # ON期間
    assert dd._flag_on_at(log, "ai_exit_advisor", "2026-06-15T00:00:00+00:00") is False  # OFF期間


def test_performance_by_period(monkeypatch):
    flag_log = [
        {"ts": "2026-06-01T00:00:00+00:00", "flags": {"ai_exit_advisor": True}},
        {"ts": "2026-06-10T00:00:00+00:00", "flags": {"ai_exit_advisor": False}},
    ]
    trades = [
        {"action": "CLOSE", "ticker": "A.T", "pnl": 500, "timestamp": "2026-06-05T00:00:00+00:00"},  # ON
        {"action": "CLOSE", "ticker": "B.T", "pnl": -200, "timestamp": "2026-06-12T00:00:00+00:00"},  # OFF
        {"action": "CLOSE", "ticker": "C.T", "pnl": 99, "timestamp": "2026-05-01T00:00:00+00:00"},  # unknown
    ]
    monkeypatch.setattr(dd, "_load_flag_log", lambda: flag_log)

    class _D:
        def load_recent_trades(self, days=30):
            return trades

    monkeypatch.setattr(dd, "get_container", lambda: type("C", (), {"diary": lambda self=None: _D()})())
    out = dd.performance_by_period()
    f = out["by_flag"]["ai_exit_advisor"]
    assert f["ON"]["count"] == 1 and f["ON"]["total_pnl"] == 500
    assert f["OFF"]["count"] == 1 and f["OFF"]["total_pnl"] == -200
    assert f["unknown"] == 1


def test_source_category():
    assert dd._source_category("ai_exit") == "AI手仕舞い"
    assert dd._source_category("ai_trim") == "AI手仕舞い"
    assert dd._source_category("mech:stop_loss") == "機械ストップ"
    assert dd._source_category("swap") == "スワップ"
    assert dd._source_category("manual") == "手動"
    assert dd._source_category("legacy") == "legacy(タグ付け前)"


def test_performance_by_source(monkeypatch):
    trades = [
        {"action": "BUY", "ticker": "X.T", "pnl": 0, "source": "ai_entry"},  # 除外(BUY)
        {"action": "CLOSE", "ticker": "A.T", "pnl": 1000, "source": "ai_exit"},
        {"action": "CLOSE", "ticker": "B.T", "pnl": -400, "source": "ai_exit"},
        {"action": "CLOSE", "ticker": "C.T", "pnl": -300, "source": "mech:stop_loss"},
        {"action": "CLOSE", "ticker": "D.T", "pnl": 200, "source": None},  # legacy
    ]

    class _D:
        def load_recent_trades(self, days=30):
            return trades

    class _C:
        def diary(self):
            return _D()

    monkeypatch.setattr(dd, "get_container", lambda: _C())
    out = dd.performance_by_source()
    assert out["total_closed"] == 4  # BUY 除外
    ai = out["by_category"]["AI手仕舞い"]
    assert ai["count"] == 2 and ai["wins"] == 1 and ai["win_rate"] == 50.0
    assert ai["total_pnl"] == 600  # 1000-400
    assert out["by_category"]["機械ストップ"]["count"] == 1
    assert out["by_category"]["legacy(タグ付け前)"]["count"] == 1


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

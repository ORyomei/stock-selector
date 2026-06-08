"""reconcile の通貨保護テスト。

kabu (JPY のみ管理) で --apply したとき、ローカルの USD 現金・米国株建玉を
消さないことを検証する (旧実装は cash_usd を 0 上書き・米国株を全 REMOVE していた)。
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core import reconcile as rec  # noqa: E402
from trading.broker_interface import Position  # noqa: E402


class _FakeJPBroker:
    """日本株のみ管理する擬似ブローカー (kabu 相当)。"""

    def __init__(self, positions, cash_jpy):
        self._positions = positions
        self._cash_jpy = cash_jpy

    def sync_from_broker(self):
        pass

    def get_positions(self):
        return self._positions

    def get_balance(self):
        return {"cash_jpy": self._cash_jpy, "cash_usd": 0.0, "timestamp": datetime.now(UTC)}

    def managed_currencies(self):
        return {"JPY"}


def _pos(ticker, qty, price):
    return Position(
        ticker=ticker, quantity=qty, entry_price=price, current_price=price,
        entry_time=datetime.now(UTC),
    )


def test_apply_preserves_usd_cash_and_us_positions(tmp_path, monkeypatch):
    from infra.repositories.json_portfolio import JsonPortfolioRepository

    repo = JsonPortfolioRepository(tmp_path / "portfolio.json", tmp_path / "risk.json")
    repo.save({
        "balance": {"cash_jpy": 500_000, "cash_usd": 12_345.0},
        "positions": [
            {"ticker": "8306.T", "quantity": 500, "entry_price": 3180.0,
             "current_price": 3180.0, "entry_time": "2026-06-04T00:00:00+00:00"},
            {"ticker": "AAPL", "quantity": 10, "entry_price": 200.0,
             "current_price": 200.0, "entry_time": "2026-06-04T00:00:00+00:00"},
        ],
    })

    # container.portfolio() がこの repo を返すよう差し替え
    monkeypatch.setattr(rec, "get_container", lambda: type("C", (), {"portfolio": lambda self=None: repo})())

    # ブローカー側: 日本株は 600 株に増えている。USD/米国株は管理外
    broker = _FakeJPBroker(positions=[_pos("8306.T", 600, 3165.0)], cash_jpy=480_000)

    result = rec.reconcile(broker, apply=True, verbose=False)
    assert result.synced

    saved = repo.load()
    tickers = {p["ticker"]: p for p in saved["positions"]}
    # 米国株は温存
    assert "AAPL" in tickers and tickers["AAPL"]["quantity"] == 10
    # 日本株はブローカー実態に同期
    assert tickers["8306.T"]["quantity"] == 600
    # USD 現金は温存、JPY のみ同期
    assert saved["balance"]["cash_usd"] == 12_345.0
    assert saved["balance"]["cash_jpy"] == 480_000

    # AAPL は照合対象外 (REMOVE diff を出さない)
    assert all(not (d.ticker == "AAPL" and d.action == "REMOVE") for d in result.diffs)

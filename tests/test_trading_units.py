"""売買単位 (trading_units) の取得・永続化・サイジング連携のテスト。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core import trading_units as tu  # noqa: E402


def test_default_unit():
    assert tu.default_unit("7203.T") == 100  # 日本株は単元100
    assert tu.default_unit("AAPL") == 1  # 米国株は1


def test_config_seed_etf():
    """config に保持された ETF(2559.T) は 1 口単位で返る。"""
    assert tu.get_trading_unit("2559.T") == 1


def test_get_trading_unit_falls_back_to_default():
    """config に無い銘柄はデフォルト (.T=100 / 他=1)。"""
    assert tu.get_trading_unit("7203.T") == 100
    assert tu.get_trading_unit("NVDA") == 1


class _FakeBroker:
    """get_trading_unit を返す擬似ブローカー (kabu 相当)。"""

    UNITS = {"2559.T": 1, "7203.T": 100, "1306.T": 10}

    def get_trading_unit(self, ticker: str) -> int:
        return self.UNITS.get(ticker, 100 if ticker.endswith(".T") else 1)


def test_refresh_persists_and_reads(tmp_path, monkeypatch):
    cfg = tmp_path / "trading_units.json"
    monkeypatch.setattr(tu, "_CONFIG_PATH", cfg)
    units = tu.refresh_trading_units(_FakeBroker(), ["2559.T", "1306.T", "7203.T"])
    assert units["2559.T"] == 1
    assert units["1306.T"] == 10
    assert cfg.exists()
    # 再読込でも反映される
    assert tu.get_trading_unit("1306.T") == 10


def test_order_cost_uses_unit():
    """_order_cost が売買単位を反映する (ETF=1口, 株=100株)。"""
    from agents.auto_trade import _order_cost

    ccy, unit, cost = _order_cost("2559.T", 2997.0)
    assert (ccy, unit, cost) == ("JPY", 1, 2997.0)  # ETF: 1口 = ¥2,997

    ccy2, unit2, cost2 = _order_cost("7203.T", 1000.0)
    assert (ccy2, unit2, cost2) == ("JPY", 100, 100_000.0)  # 株: 100株 = ¥100,000

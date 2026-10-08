"""サイズと現金の機械ルール (issue #21) のテスト。

回顧録 §2-3 (3): 1 銘柄 25〜30%・露出 97〜99%、SBG (年率ボラ 88%) も JT も同じ比率、
現金が無く swap が片肺になり翌日 12 サイクル空転。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.sizing import (  # noqa: E402
    cash_floor_check,
    sector_cap_check,
    swap_feasibility,
    vol_size_factor,
)


@pytest.mark.parametrize(
    "vol,factor",
    [(None, 1.0), (30.0, 1.0), (60.0, 1.0), (60.1, 0.5), (79.9, 0.5), (88.0, 1 / 3), (float("nan"), 1.0)],
)
def test_vol_size_factor(vol, factor):
    """SBG の年率ボラ 88% は 1/3、境界 60% ちょうどは等倍。"""
    assert vol_size_factor(vol) == pytest.approx(factor)


def test_cash_floor_blocks_when_buy_breaches_floor():
    ok, msg = cash_floor_check(cash_jpy=500_000, buy_cost_jpy=500_000, equity_jpy=3_000_000, min_cash_pct=20)
    assert ok is False and "0%" in msg and "下限 20%" in msg


def test_cash_floor_allows_when_enough_cash_remains():
    ok, _ = cash_floor_check(cash_jpy=1_200_000, buy_cost_jpy=500_000, equity_jpy=3_000_000, min_cash_pct=20)
    assert ok is True


def test_cash_floor_disabled_when_zero():
    ok, _ = cash_floor_check(cash_jpy=0, buy_cost_jpy=500_000, equity_jpy=3_000_000, min_cash_pct=0)
    assert ok is True


def test_swap_infeasible_when_buy_leg_exceeds_proceeds_plus_cash():
    """片肺 swap の再現: 現金 5 万 + 売却代金 40 万 - 現金下限 60 万 < 買い足 50 万。"""
    ok, msg = swap_feasibility(
        cash_jpy=50_000, sell_value_jpy=400_000, buy_unit_cost_jpy=500_000,
        equity_jpy=3_000_000, min_cash_pct=20,
    )
    assert ok is False and "片肺" in msg


def test_swap_feasible_when_proceeds_cover_buy_leg():
    ok, _ = swap_feasibility(
        cash_jpy=700_000, sell_value_jpy=400_000, buy_unit_cost_jpy=500_000,
        equity_jpy=3_000_000, min_cash_pct=20,
    )
    assert ok is True


_HOLD = [("6501.T", "Industrials", 600_000.0), ("7751.T", "Technology", 450_000.0), ("1545.T", "分散/ETF", 300_000.0)]


def test_sector_cap_blocks_concentration():
    """Industrials 20% + 見込み 20% = 40% > 35%。"""
    ok, msg = sector_cap_check(_HOLD, "7011.T", "Industrials", 600_000.0, 3_000_000.0, 35)
    assert ok is False and "20% → 40%" in msg


def test_sector_cap_allows_other_sector():
    ok, _ = sector_cap_check(_HOLD, "8306.T", "Financial Services", 600_000.0, 3_000_000.0, 35)
    assert ok is True


def test_sector_cap_excludes_swap_sell_leg():
    """6501 を売って 7011 を買う swap: 売り足を除けば Industrials 0% → 20%。"""
    ok, _ = sector_cap_check(_HOLD, "7011.T", "Industrials", 600_000.0, 3_000_000.0, 35, exclude_ticker="6501.T")
    assert ok is True


def test_sector_cap_passes_unknown_sector():
    ok, msg = sector_cap_check(_HOLD, "9999.T", "不明", 600_000.0, 3_000_000.0, 35)
    assert ok is True and "不明" in msg

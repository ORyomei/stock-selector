"""注文実行の機械ガード (issue #21): 片肺 swap 防止・現金下限・セクター上限・ボラ連動サイズ。

_execute_signals をブローカー・AI 無しで駆動し、売り足が出る前に止まることを検証する。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import agents.auto_trade as at  # noqa: E402
import agents.graph_trade as gt  # noqa: E402
import agents.portfolio_helpers as ph  # noqa: E402

_LIMITS = {
    "max_position_size_pct": 20.0, "min_cash_pct": 20.0, "max_sector_pct": 35.0,
    "vol_half_size_above_pct": 60.0, "vol_third_size_above_pct": 80.0,
}


@pytest.fixture
def harness(monkeypatch):
    """ブローカー・diary・設定をスタブし、run_trade_cmd の呼び出しと保存シグナルを記録する。"""
    state = {"cash": 50_000.0, "equity": 3_000_000.0, "positions": [], "sectors": {}, "calls": [], "saved": []}

    class _Broker:
        def get_balance(self):
            return {"cash_jpy": state["cash"], "cash_usd": 0}

    class _Diary:
        def save_signal(self, name, sig):
            state["saved"].append(sig)
            return f"/tmp/{name}"

    class _Container:
        def broker(self):
            return _Broker()

        def diary(self):
            return _Diary()

    def _cmd(args):
        state["calls"].append(args)
        return '{"status": "FILLED"}', 0

    monkeypatch.setattr(gt, "get_container", lambda: _Container())
    monkeypatch.setattr(gt, "run_trade_cmd", _cmd)
    monkeypatch.setattr(gt, "get_held_positions", lambda: list(state["positions"]))
    monkeypatch.setattr(gt, "_sizing_limits", lambda: dict(_LIMITS))
    monkeypatch.setattr(gt, "_sector_of", lambda t: state["sectors"].get(t, "不明"))
    monkeypatch.setattr(at, "_order_cost", lambda t, p: ("JPY", 100, p * 100))
    monkeypatch.setattr(ph, "total_equity_jpy", lambda: state["equity"])
    return state


def _pos(ticker, qty, price, entry="2026-09-01T00:00:00+00:00"):
    return {"ticker": ticker, "quantity": qty, "current_price": price, "entry_price": price, "entry_time": entry}


def test_swap_blocked_before_selling_when_buy_leg_unaffordable(harness):
    """片肺 swap の再現 (現金 5 万、1545 売却 40 万、買い足 50 万): 売り足も出さない。"""
    harness["positions"] = [_pos("1545.T", 100, 4000.0)]
    sig = {"ticker": "6501.T", "sell_ticker": "1545.T", "target_price": 5000.0, "score": 45, "reason": "x"}
    out = gt._execute_signals([sig], dry_run=False, log=lambda m: None)
    assert out == [{"ticker": "6501.T", "status": "SWAP_BLOCKED_FUNDS", "score": 45}]
    assert harness["calls"] == []


def test_swap_executes_when_feasible_and_carries_size_factor(harness):
    harness.update(cash=700_000.0, positions=[_pos("1545.T", 100, 4000.0)], sectors={"6501.T": "Industrials"})
    sig = {"ticker": "6501.T", "sell_ticker": "1545.T", "target_price": 5000.0, "score": 45, "reason": "x",
           "_entry_measured": {"annualized_vol_pct": 88.0}}
    out = gt._execute_signals([sig], dry_run=False, log=lambda m: None)
    assert [e["status"] for e in out] == ["SOLD", "FILLED"]
    assert harness["calls"][0][:3] == ["--close", "1545.T", "100"]
    saved = harness["saved"][0]
    assert saved["size_factor"] == pytest.approx(1 / 3)  # ボラ 88% → 1/3
    assert "sell_ticker" not in saved and not any(k.startswith("_") for k in saved)


def test_buy_skipped_when_cash_floor_breached(harness):
    """現金 50 万 / 総資産 300 万: 1 単元 50 万を買うと現金 0% < 下限 20%。"""
    harness.update(cash=500_000.0)
    sig = {"ticker": "6501.T", "target_price": 5000.0, "score": 45}
    out = gt._execute_signals([sig], dry_run=False, log=lambda m: None)
    assert out[0]["status"] == "SKIPPED_CASH_FLOOR"
    assert harness["calls"] == []


def test_buy_skipped_when_sector_cap_exceeded(harness):
    """Industrials を既に 20% 保有 → 見込み +20% = 40% > 35%。"""
    harness.update(
        cash=1_500_000.0,
        positions=[_pos("7011.T", 200, 3000.0)],  # 60 万 = 20%
        sectors={"7011.T": "Industrials", "6501.T": "Industrials"},
    )
    sig = {"ticker": "6501.T", "target_price": 5000.0, "score": 45}
    out = gt._execute_signals([sig], dry_run=False, log=lambda m: None)
    assert out[0]["status"] == "SKIPPED_SECTOR_CAP"
    assert harness["calls"] == []


def test_buy_passes_all_guards_with_full_size(harness):
    harness.update(cash=1_500_000.0, positions=[_pos("7011.T", 200, 3000.0)],
                   sectors={"7011.T": "Industrials", "8306.T": "Financial Services"})
    sig = {"ticker": "8306.T", "target_price": 2000.0, "score": 45, "_entry_measured": {"annualized_vol_pct": 25.0}}
    out = gt._execute_signals([sig], dry_run=False, log=lambda m: None)
    assert out[0]["status"] == "FILLED" and out[0]["measured"] == {"annualized_vol_pct": 25.0}
    assert harness["saved"][0]["size_factor"] == 1.0

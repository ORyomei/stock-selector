"""並走ベンチマーク「ルールのみ」(issue #24 (4)) のテスト。純関数部分 + run_cycle のスタブ駆動。"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import core.rulebook as rb  # noqa: E402
from core.rulebook import (  # noqa: E402
    Candidate,
    apply_entries,
    apply_exits,
    candidate_from_score,
    equity_of,
    load_config,
    new_state,
    select_entries,
)

_NOW = datetime(2026, 10, 9, 1, 30, tzinfo=UTC)  # 10:30 JST
_CFG = load_config(Path("/nonexistent"))  # DEFAULTS


def _cand(t, score=45, rsi=55.0, bb=60.0, vol=30.0, price=5000.0, sector="Industrials"):
    return Candidate(t, score, rsi, bb, vol, price, sector)


def test_select_entries_applies_gate_and_orders_by_score():
    st = new_state(_CFG, _NOW)
    cands = [
        _cand("A.T", score=50), _cand("B.T", score=60, bb=90.0),  # BB 超
        _cand("C.T", score=55, rsi=70.0),  # RSI 超
        _cand("D.T", score=30),  # score 不足
        _cand("E.T", score=48, sector="Technology"),
    ]
    chosen = select_entries(st, _NOW, cands, _CFG, equity=3_000_000)
    assert [c.ticker for c, _ in chosen] == ["A.T", "E.T"]
    assert all(q == 100 for _, q in chosen)  # 300 万 × 20% = 60 万 → 5000 円 × 100 株


def test_select_entries_respects_cash_floor_and_sector_cap():
    st = new_state(_CFG, _NOW)
    st["cash"] = 800_000.0  # 総資産 300 万のうち現金 80 万 → 使えるのは 20 万
    st["positions"]["X.T"] = {"qty": 100, "entry_price": 10000.0, "last_price": 10000.0, "sector": "Industrials",
                              "entry_time": _NOW.isoformat()}
    cands = [_cand("A.T", score=50, price=5000.0, sector="Technology"),
             _cand("B.T", score=45, price=1500.0, sector="Technology")]
    chosen = select_entries(st, _NOW, cands, _CFG, equity=3_000_000)
    # A: 20 万では 5000 円 × 100 株 (50 万) は買えない → 0 株でスキップ。B: 1500 × 100 = 15 万 → 可
    assert [(c.ticker, q) for c, q in chosen] == [("B.T", 100)]


def test_select_entries_sector_cap_blocks_concentration():
    st = new_state(_CFG, _NOW)
    st["positions"]["X.T"] = {"qty": 100, "entry_price": 6000.0, "last_price": 6000.0, "sector": "Industrials",
                              "entry_time": _NOW.isoformat()}  # 60 万 = 20%
    st["cash"] = 2_400_000.0
    cands = [_cand("A.T", score=50, price=6000.0, sector="Industrials"), _cand("B.T", score=45, sector="Technology")]
    chosen = select_entries(st, _NOW, cands, _CFG, equity=3_000_000)
    assert [c.ticker for c, _ in chosen] == ["B.T"]  # Industrials 20% + 20% = 40% > 35%


def test_select_entries_skips_recent_close_and_held():
    st = new_state(_CFG, _NOW)
    st["closes"]["A.T"] = "2026-10-01"  # 6 営業日前 < 20
    st["positions"]["B.T"] = {"qty": 100, "entry_price": 1.0, "last_price": 1.0, "sector": "x", "entry_time": _NOW.isoformat()}
    chosen = select_entries(st, _NOW, [_cand("A.T"), _cand("B.T"), _cand("C.T")], _CFG, equity=3_000_000)
    assert [c.ticker for c, _ in chosen] == ["C.T"]


def test_vol_scaled_size():
    st = new_state(_CFG, _NOW)
    chosen = select_entries(st, _NOW, [_cand("A.T", vol=88.0, price=1000.0)], _CFG, equity=3_000_000)
    assert chosen[0][1] == 200  # 60 万 × 1/3 = 20 万 → 1000 円 × 200 株


def test_apply_entries_and_exits_roundtrip():
    st = new_state(_CFG, _NOW)
    trades = apply_entries(st, [(_cand("A.T", price=1000.0), 100), (_cand("B.T", price=2000.0), 100)], _NOW, _CFG)
    assert [t["side"] for t in trades] == ["BUY", "BUY"]
    assert st["cash"] < 3_000_000 - 300_000  # スプレッド込み
    later = datetime(2026, 10, 15, 6, 0, tzinfo=UTC)
    closed = apply_exits(st, later, {"A.T": 940.0, "B.T": 2200.0}, {}, _CFG)  # A -6% / B +10%
    assert {c["ticker"]: c["reason"] for c in closed} == {"A.T": "stop_loss", "B.T": "take_profit"}
    assert st["positions"] == {} and st["closes"] == {"A.T": "2026-10-15", "B.T": "2026-10-15"}
    assert closed[0]["pnl"] < 0 < closed[1]["pnl"]


def test_apply_exits_rule_and_timeout():
    st = new_state(_CFG, _NOW)
    apply_entries(st, [(_cand("A.T", price=1000.0), 100), (_cand("B.T", price=1000.0), 100)], _NOW, _CFG)
    later = datetime(2026, 10, 14, 6, 0, tzinfo=UTC)
    closed = apply_exits(st, later, {"A.T": 1010.0, "B.T": 1010.0},
                         {"A.T": {"close_vs_sma25_pct": -0.5}, "B.T": {"close_vs_sma25_pct": 1.0}}, _CFG)
    assert [(c["ticker"], c["reason"]) for c in closed] == [("A.T", "rule:close_vs_sma25_pct")]
    far = datetime(2026, 11, 20, 6, 0, tzinfo=UTC)  # 21 営業日超
    closed = apply_exits(st, far, {"B.T": 1010.0}, {}, _CFG)
    assert [(c["ticker"], c["reason"]) for c in closed] == [("B.T", "hold_timeout")]


def test_equity_of_uses_latest_price():
    st = new_state(_CFG, _NOW)
    apply_entries(st, [(_cand("A.T", price=1000.0), 100)], _NOW, _CFG)
    assert round(equity_of(st, {"A.T": 1100.0})) == round(st["cash"] + 110_000)


def test_candidate_from_score_maps_indicators():
    c = candidate_from_score("7203.T", {"current_price": 3100.0, "indicators": {"rsi_14": 58.0, "bb_position_pct": 70.0,
                                        "annualized_vol_pct": 25.0}, "analysis_summary": {"total_score": 45}}, "Auto")
    assert (c.ticker, c.score, c.rsi, c.bb_position_pct, c.annualized_vol_pct, c.price, c.sector) == (
        "7203.T", 45.0, 58.0, 70.0, 25.0, 3100.0, "Auto")
    assert candidate_from_score("X", {"analysis_summary": {}}, "s") is None


def test_run_cycle_decides_once_per_day_and_records(tmp_path, monkeypatch):
    monkeypatch.setattr(rb, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(rb, "TRADES_PATH", tmp_path / "trades.jsonl")
    monkeypatch.setattr(rb, "EQUITY_PATH", tmp_path / "equity.jsonl")
    monkeypatch.setattr(rb, "CONFIG_PATH", tmp_path / "missing.json")
    scores = {
        "A.T": {"current_price": 1000.0, "indicators": {"rsi_14": 50.0, "bb_position_pct": 50.0, "annualized_vol_pct": 20.0,
                                                        "sma_25": 950.0}, "analysis_summary": {"total_score": 50}},
        "B.T": {"current_price": 2000.0, "indicators": {"rsi_14": 72.0, "bb_position_pct": 50.0}, "analysis_summary": {"total_score": 50}},
    }
    monkeypatch.setattr(rb, "_score_safe", lambda t: scores.get(t))
    monkeypatch.setattr(rb, "_sector_safe", lambda t: "Industrials")
    monkeypatch.setattr("core.trading_units.get_trading_unit", lambda t: 100)

    class _Regime:
        ok = True
        decided = True
        sma_gap_pct = 1.0
        lookback_ret_pct = 0.5

    logs: list[str] = []
    out = rb.run_cycle(_NOW, regime=_Regime(), log=logs.append, universe=["A.T", "B.T"])
    assert out["decided"] is True and list(out["positions"]) == ["A.T"]  # B は RSI 72 で不可
    assert out["positions"]["A.T"]["qty"] == 600  # 60 万 / 1000 円
    # 同日 2 回目は判断しない、資産は記録される
    out2 = rb.run_cycle(datetime(2026, 10, 9, 5, 0, tzinfo=UTC), regime=_Regime(), log=logs.append, universe=["A.T", "B.T"])
    assert out2["decided"] is False and list(out2["positions"]) == ["A.T"]
    assert len((tmp_path / "equity.jsonl").read_text().splitlines()) == 2
    assert (tmp_path / "trades.jsonl").read_text().count('"BUY"') == 1


def test_run_cycle_regime_off_keeps_decision_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(rb, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(rb, "TRADES_PATH", tmp_path / "trades.jsonl")
    monkeypatch.setattr(rb, "EQUITY_PATH", tmp_path / "equity.jsonl")
    monkeypatch.setattr(rb, "CONFIG_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(rb, "_score_safe", lambda t: None)

    class _Off:
        ok = False
        decided = True

    out = rb.run_cycle(_NOW, regime=_Off(), universe=["A.T"])
    assert out["decided"] is False and out["positions"] == {}
    assert rb.load_state(tmp_path / "state.json")["last_decision_date"] is None

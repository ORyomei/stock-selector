"""入口の測定値ゲート (issue #20) のテスト。

自己申告 (confidence) や文章ではなく、score_stock の実測値 (BB 位置 / RSI / score) で
buy / swap を機械検査する。41 建玉の実測: BB>85% は 10 件 30% -103,959 円、RSI>65 は
3 件全敗、score≥40 は 9 件 56% +94,407 円。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.entry_gate import (  # noqa: E402
    EntryMeasure,
    check_entry,
    evaluate_entry,
    measure_from_score_result,
)

_CFG = {"enabled": True, "max_bb_position_pct": 85, "max_rsi": 65, "min_score": 40}


def _m(**kw) -> EntryMeasure:
    base = dict(ticker="7203.T", score=45.0, rsi=55.0, bb_position_pct=60.0)
    base.update(kw)
    return EntryMeasure(**base)


def test_passes_when_all_thresholds_met():
    ok, msg = check_entry(_m(), _CFG)
    assert ok is True
    assert "通過" in msg and "BB 60%" in msg and "RSI 55" in msg and "score 45" in msg


def test_bb_over_85_rejected():
    """BB 位置 90% (5401.T 9/7 型: BB 106% で買って翌日反落 -27,306 円)。"""
    ok, msg = check_entry(_m(bb_position_pct=90.0), _CFG)
    assert ok is False
    assert "BB位置 90% > 85%" in msg


def test_rsi_over_65_rejected():
    ok, msg = check_entry(_m(rsi=70.0), _CFG)
    assert ok is False
    assert "RSI 70 > 65" in msg


def test_score_under_40_rejected():
    ok, msg = check_entry(_m(score=30.0), _CFG)
    assert ok is False
    assert "score 30 < 40" in msg


def test_multiple_failures_listed_without_keyword_hints():
    """却下文面は数字と閾値のみ (語句の手がかりを渡さない: #17 の教訓)。"""
    ok, msg = check_entry(_m(bb_position_pct=95.0, rsi=72.0, score=20.0), _CFG)
    assert ok is False
    assert "BB位置 95% > 85%" in msg and "RSI 72 > 65" in msg and "score 20 < 40" in msg
    for hint in ("過熱", "出来高", "confidence"):
        assert hint not in msg


def test_boundary_values_pass():
    """閾値ちょうど (85 / 65 / 40) は通す。"""
    ok, _ = check_entry(_m(bb_position_pct=85.0, rsi=65.0, score=40.0), _CFG)
    assert ok is True


def test_missing_measurement_fails_open():
    ok, msg = check_entry(None, _CFG)
    assert ok is True and "フェイルオープン" in msg


def test_partial_measurement_checks_available_fields_only():
    ok, msg = check_entry(_m(rsi=None, bb_position_pct=None, score=30.0), _CFG)
    assert ok is False and "score 30 < 40" in msg


def test_disabled_gate_allows_everything():
    ok, msg = check_entry(_m(bb_position_pct=120.0, rsi=90.0, score=-50.0), {**_CFG, "enabled": False})
    assert ok is True and "無効" in msg


def test_measure_from_score_result_maps_indicators():
    result = {
        "current_price": 3100.0,
        "indicators": {"rsi_14": 58.3, "bb_position_pct": 72.5, "annualized_vol_pct": 31.2,
                       "volume_ratio_20d": 1.4},
        "analysis_summary": {"total_score": 45},
    }
    m = measure_from_score_result("7203.T", result)
    assert (m.score, m.rsi, m.bb_position_pct) == (45.0, 58.3, 72.5)
    assert m.annualized_vol_pct == 31.2 and m.volume_ratio_20d == 1.4 and m.current_price == 3100.0


def test_measure_from_legacy_result_without_indicators_is_partial():
    """indicators ブロックが無い古い形式でも落ちない (score だけで判定)。"""
    m = measure_from_score_result("7203.T", {"analysis_summary": {"total_score": 10}})
    assert m.score == 10.0 and m.rsi is None and m.bb_position_pct is None


def test_evaluate_entry_uses_cached_scorer(monkeypatch):
    calls: list[str] = []

    def fake_cached(ticker, period="6mo"):
        calls.append(ticker)
        return {"indicators": {"rsi_14": 71.0, "bb_position_pct": 92.0},
                "analysis_summary": {"total_score": 25}}

    monkeypatch.setattr("core.scorer.compute_score_cached", fake_cached)
    ok, msg, measured = evaluate_entry("6702.T", _CFG)
    assert ok is False
    assert calls == ["6702.T"]
    assert measured["bb_position_pct"] == 92.0 and measured["rsi"] == 71.0


def test_evaluate_entry_fails_open_when_scorer_exits(monkeypatch):
    """compute_score はデータ不足で sys.exit する — 捕まえて通す。"""
    def boom(ticker, period="6mo"):
        raise SystemExit(1)

    monkeypatch.setattr("core.scorer.compute_score_cached", boom)
    ok, msg, measured = evaluate_entry("9999.T", _CFG)
    assert ok is True and measured is None and "フェイルオープン" in msg


def test_submit_tool_rejects_and_reports_in_same_turn(monkeypatch):
    """submit_signals は却下をツール応答で返し、受理分だけを captured に残す。"""
    from agents import graph_trade as gt

    verdicts = {
        "7203.T": (True, "入口ゲート通過 (BB 60%, RSI 55, score 45)", {"bb_position_pct": 60.0}),
        "6702.T": (False, "入口ゲート却下 (BB位置 92% > 85%, RSI 71 > 65)", {"bb_position_pct": 92.0}),
    }
    monkeypatch.setattr(gt, "_evaluate_entry_safe", lambda t: verdicts[t])
    captured: dict = {}
    tool = gt._make_submit_signals_tool(captured)
    base = dict(action="buy", score=30, confidence=0.6, target_price=100.0,
                stop_loss_price=95.0, take_profit_price=110.0)
    out = tool.invoke({"signals": [{"ticker": "7203.T", **base}, {"ticker": "6702.T", **base}],
                       "market_comment": "x"})
    assert "1 件受理、1 件を入口ゲートで却下" in out and "6702.T" in out
    assert [s["ticker"] for s in captured["signals"]] == ["7203.T"]
    assert captured["signals"][0]["_entry_measured"] == {"bb_position_pct": 60.0}
    assert captured["entry_gate_rejected"] == [("6702.T", verdicts["6702.T"][1])]

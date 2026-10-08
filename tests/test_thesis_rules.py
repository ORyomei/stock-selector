"""テーゼをコードにする (issue #23) のテスト。

数値 invalidation ルールを submit 時に検証し、毎サイクル決定論的に評価して成立したら全株
クローズする。1 単元保有の trim は exit に昇格 (#15 吸収)。
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.thesis_rules import (  # noqa: E402
    ALLOWED_METRICS,
    compute_metrics,
    evaluate_rules,
    format_rule,
    load_rules_for,
    validate_rule,
)


def test_validate_rule_normalizes():
    assert validate_rule({"metric": " rsi_14 ", "op": "<", "value": "50"}) == {
        "metric": "rsi_14", "op": "<", "value": 50.0,
    }


@pytest.mark.parametrize(
    "rule,msg",
    [
        ({"metric": "vibes", "op": "<", "value": 1}, "使えない"),
        ({"metric": "rsi_14", "op": "==", "value": 1}, "op"),
        ({"metric": "rsi_14", "op": "<", "value": "abc"}, "数値"),
        ("rsi_14 < 50", "オブジェクト"),
    ],
)
def test_validate_rule_rejects(rule, msg):
    with pytest.raises(ValueError, match=msg):
        validate_rule(rule)


def test_evaluate_rules_fires_only_when_condition_holds():
    rules = [
        {"metric": "close_vs_sma25_pct", "op": "<", "value": 0},
        {"metric": "drawdown_from_peak_pct", "op": "<", "value": -4},
        {"metric": "topix_5d_pct", "op": "<", "value": -2},  # metric 欠損 → 不成立
    ]
    metrics = {"close_vs_sma25_pct": -1.2, "drawdown_from_peak_pct": -2.0, "topix_5d_pct": None}
    fired = evaluate_rules(rules, metrics)
    assert [f["metric"] for f in fired] == ["close_vs_sma25_pct"]
    assert fired[0]["actual"] == -1.2
    assert format_rule(fired[0]) == "close_vs_sma25_pct < 0"


def test_evaluate_rules_boundary_ops():
    m = {"days_held": 10.0}
    assert evaluate_rules([{"metric": "days_held", "op": ">=", "value": 10}], m)
    assert not evaluate_rules([{"metric": "days_held", "op": ">", "value": 10}], m)
    assert evaluate_rules([{"metric": "days_held", "op": "<=", "value": 10}], m)


def test_evaluate_rules_ignores_invalid_rules():
    assert evaluate_rules([{"metric": "nope", "op": "<", "value": 1}], {"nope": 0}) == []


class _Regime:
    decided = True
    sma_gap_pct = -1.5
    lookback_ret_pct = -2.3


def test_compute_metrics_from_position_score_and_regime():
    pos = {
        "ticker": "9984.T", "entry_price": 10000.0, "current_price": 9500.0, "peak_price": 10200.0,
        "entry_time": "2026-09-03T01:00:00+00:00", "quantity": 100,
    }
    score = {
        "current_price": 9500.0,
        "indicators": {"rsi_14": 42.0, "bb_position_pct": 18.0, "sma_5": 9600.0, "sma_25": 9800.0,
                       "sma_75": 9000.0, "macd_hist": -12.5, "volume_ratio_20d": 1.3,
                       "ret_5d_pct": -4.1, "ret_20d_pct": 2.2},
    }
    m = compute_metrics(pos, score, _Regime(), now=datetime(2026, 9, 12, 1, 0, tzinfo=UTC))
    assert m["pnl_pct"] == pytest.approx(-5.0)
    assert m["drawdown_from_peak_pct"] == pytest.approx(-6.8627, rel=1e-3)
    assert m["days_held"] == 6.0  # 9/3 (木) → 9/12 (土): 9/4, 9/7〜9/11 の 6 営業日
    assert m["close_vs_sma25_pct"] == pytest.approx(-3.0612, rel=1e-3)
    assert m["close_vs_sma75_pct"] == pytest.approx(5.5556, rel=1e-3)
    assert m["topix_vs_sma20_pct"] == -1.5 and m["topix_5d_pct"] == -2.3
    assert set(m) == set(ALLOWED_METRICS)


def test_compute_metrics_tolerates_missing_data():
    m = compute_metrics({"ticker": "X", "entry_price": None, "current_price": 100}, None, None)
    assert m["pnl_pct"] is None and m["rsi_14"] is None and m["topix_5d_pct"] is None
    assert evaluate_rules([{"metric": "pnl_pct", "op": "<", "value": -3}], m) == []


def test_load_rules_for_reads_signal_file_and_skips_invalid(tmp_path, monkeypatch):
    import core.thesis_rules as tr

    (tmp_path / "7203T_auto.json").write_text(
        '{"ticker": "7203.T", "invalidation_rules": [{"metric": "rsi_14", "op": "<", "value": 45},'
        ' {"metric": "bogus", "op": "<", "value": 1}]}', encoding="utf-8",
    )
    monkeypatch.setattr(tr, "signal_path", lambda t: tmp_path / f"{t.replace('.', '')}_auto.json")
    assert load_rules_for("7203.T") == [{"metric": "rsi_14", "op": "<", "value": 45.0}]
    assert load_rules_for("9999.T") == []


# ── submit_signals スキーマ ─────────────────────────────────────


def _sig(**kw):
    from agents.graph_trade import TradeSignalArg

    base = dict(ticker="7203.T", action="buy", target_price=3100.0, stop_loss_price=2950.0,
                take_profit_price=3250.0)
    base.update(kw)
    return TradeSignalArg(**base)


def test_schema_requires_at_least_one_rule():
    with pytest.raises(ValidationError, match="invalidation_rules は 1 件以上"):
        _sig()


def test_schema_rejects_unknown_metric_with_allowed_list():
    with pytest.raises(ValidationError, match="使える metric"):
        _sig(invalidation_rules=[{"metric": "feelings", "op": "<", "value": 0}])


def test_schema_accepts_valid_rules_and_dumps_them():
    s = _sig(invalidation_rules=[{"metric": "close_vs_sma25_pct", "op": "<", "value": 0},
                                 {"metric": "drawdown_from_peak_pct", "op": "<", "value": -4}])
    dumped = s.model_dump()["invalidation_rules"]
    assert dumped[1] == {"metric": "drawdown_from_peak_pct", "op": "<", "value": -4.0}


# ── サイクル内の適用 ────────────────────────────────────────────


def test_apply_thesis_rules_closes_full_position(monkeypatch):
    import agents.graph_trade as gt
    import core.thesis_rules as tr

    calls: list[list[str]] = []
    monkeypatch.setattr(gt, "run_trade_cmd", lambda args: (calls.append(args) or ('{"status": "FILLED"}', 0)))
    monkeypatch.setattr(gt, "get_held_positions", lambda: [
        {"ticker": "9984.T", "quantity": 100, "entry_price": 10000.0, "current_price": 9500.0},
        {"ticker": "7203.T", "quantity": 200, "entry_price": 3000.0, "current_price": 3100.0},
    ])
    monkeypatch.setattr(tr, "load_rules_for", lambda t: {
        "9984.T": [{"metric": "pnl_pct", "op": "<", "value": -3}],
        "7203.T": [{"metric": "pnl_pct", "op": "<", "value": -3}],
    }[t])
    monkeypatch.setattr(gt, "_thesis_metrics_safe", lambda p, r: {"pnl_pct": (p["current_price"] / p["entry_price"] - 1) * 100})
    logs: list[str] = []
    out = gt._apply_thesis_rules(None, False, logs.append)
    assert calls == [["--close", "9984.T", "100", "--source", "rule:pnl_pct"]]
    assert out[0]["ticker"] == "9984.T" and out[0]["status"] == "RULE_EXIT"
    assert any("7203.T" in line and "不成立" in line for line in logs)


def test_apply_thesis_rules_dry_run_does_not_trade(monkeypatch):
    import agents.graph_trade as gt
    import core.thesis_rules as tr

    monkeypatch.setattr(gt, "run_trade_cmd", lambda args: pytest.fail("must not trade in dry run"))
    monkeypatch.setattr(gt, "get_held_positions", lambda: [{"ticker": "9984.T", "quantity": 100}])
    monkeypatch.setattr(tr, "load_rules_for", lambda t: [{"metric": "rsi_14", "op": "<", "value": 50}])
    monkeypatch.setattr(gt, "_thesis_metrics_safe", lambda p, r: {"rsi_14": 40.0})
    assert gt._apply_thesis_rules(None, True, lambda m: None) == []


def test_single_unit_trim_is_promoted_to_exit(monkeypatch):
    """#15: 1 単元保有の trim は数量 0 で空振りしていた → exit に昇格して全株クローズ。"""
    import agents.exit_advisor as ea
    import agents.graph_trade as gt
    import core.churn_guard as cg
    import core.trading_units as tu
    from domain.models import Position

    pos = Position(ticker="9984.T", quantity=100, entry_price=10000.0, current_price=9500.0,
                   entry_time=datetime(2026, 9, 3, 1, 0, tzinfo=UTC))

    class _Broker:
        def get_positions(self):
            return [pos]

    class _C:
        def broker(self):
            return _Broker()

    calls: list[list[str]] = []
    monkeypatch.setattr(gt, "get_container", lambda: _C())
    monkeypatch.setattr(gt, "_ai_exit_enabled", lambda: True)
    monkeypatch.setattr(gt, "run_trade_cmd", lambda args: (calls.append(args) or ('{"status": "FILLED"}', 0)))
    monkeypatch.setattr(ea, "advise_exits", lambda *a, **k: [{"ticker": "9984.T", "action": "trim", "reason": "invalidation"}])
    monkeypatch.setattr(tu, "get_trading_unit", lambda t: 100)
    monkeypatch.setattr(cg, "check_ai_sell", lambda *a, **k: (True, "ok"))
    logs: list[str] = []
    gt._run_ai_exit_advisor("neutral", "claude_code", None, False, logs.append)
    assert calls == [["--close", "9984.T", "100", "--source", "ai_exit"]]
    assert any("exit に昇格" in line for line in logs)

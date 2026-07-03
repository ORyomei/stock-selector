"""シグナル仮想追跡 (P5) と事前フィルタ (P4) のテスト。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from infra.repositories.file_diary import FileDiaryRepository  # noqa: E402
from web import dashboard_data as dd  # noqa: E402

# ── _forward_returns (純関数) ────────────────────────────────────

_CLOSES = [
    ("2026-07-01", 100.0),
    ("2026-07-02", 102.0),
    ("2026-07-03", 101.0),
    ("2026-07-06", 105.0),
    ("2026-07-07", 99.0),
]


def test_forward_returns_from_signal_date():
    fwd = dd._forward_returns(_CLOSES, "2026-07-01", (1, 3))
    assert fwd[1] == 2.0  # 100 → 102
    assert fwd[3] == 5.0  # 100 → 105


def test_forward_returns_uses_next_trading_day_when_signal_on_holiday():
    # 7/4-7/5 は休場 → 基準は 7/6 の 105
    fwd = dd._forward_returns(_CLOSES, "2026-07-04", (1,))
    assert fwd[1] == round((99.0 / 105.0 - 1) * 100, 2)


def test_forward_returns_omits_unavailable_horizons():
    fwd = dd._forward_returns(_CLOSES, "2026-07-07", (1, 5))
    assert fwd == {}  # 先のデータがない
    assert dd._forward_returns(_CLOSES, "2026-08-01", (1,)) == {}  # 基準日なし


def test_signal_group():
    assert dd._signal_group("FILLED") == "採用(約定)"
    assert dd._signal_group("REJECTED:gate") == "却下(ゲート/上限)"
    assert dd._signal_group("REJECTED:unaffordable_unit_cost") == "却下(ゲート/上限)"
    assert dd._signal_group("SKIPPED_FUNDS") == "スキップ/失敗"
    assert dd._signal_group("FAILED") == "スキップ/失敗"


# ── signals_log.jsonl (追記と読み込み) ──────────────────────────


def test_signal_log_append_and_load(tmp_path):
    repo = FileDiaryRepository(tmp_path)
    repo.append_signal_log({"ts": "2026-07-04T01:00:00+00:00", "ticker": "7203.T", "status": "FILLED"})
    repo.append_signal_log({"ts": "2020-01-01T00:00:00+00:00", "ticker": "OLD.T", "status": "FILLED"})
    # 壊れた行が混ざっても他は読める
    (tmp_path / "signals_log.jsonl").open("a", encoding="utf-8").write("{ broken\n")

    recs = repo.load_signal_log(days=3650)
    assert {r["ticker"] for r in recs} == {"7203.T", "OLD.T"}
    recent = repo.load_signal_log(days=365)
    assert {r["ticker"] for r in recent} == {"7203.T"}  # 古い行はカットオフ


def test_signal_log_missing_file_is_empty(tmp_path):
    assert FileDiaryRepository(tmp_path).load_signal_log() == []


# ── 執行可能性の事前フィルタ (P4) ────────────────────────────────


def test_filter_affordable_signals(monkeypatch):
    import agents.auto_trade as at
    import agents.graph_trade as gt

    monkeypatch.setattr(gt, "_max_unit_cost_jpy_safe", lambda: 900_000.0)
    monkeypatch.setattr(
        at, "_order_cost",
        lambda ticker, price: ("JPY", 100, price * 100),  # 1単元 = 100株
    )
    signals = [
        {"ticker": "7203.T", "target_price": 2500.0, "score": 30},  # ¥250k → 通る
        {"ticker": "6367.T", "target_price": 26000.0, "score": 45},  # ¥2.6M → 落ちる
        {"ticker": "9999.T", "target_price": 0, "score": 10},  # 価格不明 → 通す
        {"ticker": "8058.T", "target_price": 30000.0, "sell_ticker": "7203.T"},  # swap → 通す
    ]
    logs: list[str] = []
    kept, dropped = gt._filter_affordable_signals(signals, logs.append)

    assert [s["ticker"] for s in kept] == ["7203.T", "9999.T", "8058.T"]
    assert len(dropped) == 1
    assert dropped[0]["ticker"] == "6367.T"
    assert dropped[0]["_gate_rejection_reason"] == "unaffordable_unit_cost"
    assert any("6367.T" in m for m in logs)


def test_filter_affordable_passes_all_when_cap_unknown(monkeypatch):
    import agents.graph_trade as gt

    monkeypatch.setattr(gt, "_max_unit_cost_jpy_safe", lambda: None)
    signals = [{"ticker": "6367.T", "target_price": 26000.0}]
    kept, dropped = gt._filter_affordable_signals(signals, lambda *_: None)
    assert kept == signals and dropped == []

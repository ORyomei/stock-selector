"""反証ゲート (gates.py) とシグナル正規化 (_normalize_signals) のテスト。

ゲートの内容検証: 旧実装は項目数しか見ず "exit_plan": "N/A" や空文字の
fail_conditions でも通過していた。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agents.gates import CounterargumentGate, validate_signals_batch  # noqa: E402
from agents.graph_trade import _normalize_signals  # noqa: E402


def _signal(**over):
    base = {
        "ticker": "7203.T",
        "action": "buy",
        "fail_conditions": ["RSI逆張り失敗 (現在RSI=75, 高値圏での反落リスク)"],
        "invalidation_conditions": ["VIX>25 に上昇した場合は買い控え"],
        "exit_plan": "逆指値 1700.0 で損切り、+5% で利確",
    }
    base.update(over)
    return base


# ── ゲート: 実 config (validation_rules.json) で検証 ─────────────


def test_rules_actually_load():
    """config/validation_rules.json が読めている (パス階層ずれの再発防止)。"""
    g = CounterargumentGate()
    assert g.rules, f"ルールが空: {g.rules_path} が読めていない"
    assert "scoring_thresholds" in g.rules


def test_valid_signal_passes_neutral():
    ok, _, missing = CounterargumentGate().validate_signal(_signal(), "neutral")
    assert ok and not missing


def test_empty_exit_plan_rejected():
    ok, _, missing = CounterargumentGate().validate_signal(_signal(exit_plan=""), "neutral")
    assert not ok and "exit_plan" in missing


def test_na_exit_plan_rejected():
    """'N/A' 等の無実質文字列は内容検証で却下 (旧実装は通過していた)。"""
    ok, _, missing = CounterargumentGate().validate_signal(_signal(exit_plan="N/A"), "neutral")
    assert not ok and "exit_plan" in missing


def test_exit_plan_without_number_rejected():
    """価格・割合の数値を含まない撤退計画は却下。"""
    ok, _, missing = CounterargumentGate().validate_signal(
        _signal(exit_plan="状況を見て適切に撤退する予定です"), "neutral"
    )
    assert not ok and "exit_plan" in missing


def test_blank_fail_condition_items_dont_count():
    """空文字・極端に短い項目は有効項目として数えない (旧実装は数えていた)。"""
    ok, _, missing = CounterargumentGate().validate_signal(
        _signal(fail_conditions=["", "リスク"]), "neutral"
    )
    assert not ok and "fail_conditions" in missing


def test_risk_off_requires_three_fail_conditions():
    """risk_off (strict_mode) では fail_conditions 3 項目必須。"""
    sig = _signal(
        invalidation_conditions=[
            "VIX>30 継続なら無効化する",
            "日経が25日線を割れたら無効化する",
        ]
    )
    ok, summary, _ = CounterargumentGate().validate_signal(sig, "risk_off")
    assert not ok  # 1項目しかない
    sig["fail_conditions"] = [
        "RSI逆張り失敗 (現在RSI=75, 反落リスク)",
        "出来高不足でのスリップ (平均出来高比30%以下)",
        "決算前のボラティリティ急騰リスク",
    ]
    ok2, _, _ = CounterargumentGate().validate_signal(sig, "risk_off")
    assert ok2


def test_batch_separates_valid_and_invalid():
    valid, invalid, details = validate_signals_batch(
        [_signal(), _signal(ticker="9433.T", exit_plan="N/A")], "neutral"
    )
    assert len(valid) == 1 and len(invalid) == 1
    assert invalid[0]["ticker"] == "9433.T"
    assert "_gate_rejection_reason" in invalid[0] or "_missing_fields" in invalid[0]


# ── _normalize_signals (ツール/テキスト共通の正規化) ─────────────


def test_normalize_filters_universe_and_caps():
    raw = [
        {"ticker": "7203.T", "action": "buy"},
        {"ticker": "FAKE999", "action": "buy"},  # ユニバース外 → 除外
        {"ticker": "8306.T", "action": "sell"},  # sell → 除外
        {"ticker": "9433.T", "action": "buy"},
        {"ticker": "6902.T", "action": "buy"},  # max_signals=2 で切り捨て対象
    ]
    out = _normalize_signals(raw, max_signals=2)
    # max_signals は先頭から適用されるため 7203.T のみ通過 (FAKE999 は枠内だが除外)
    assert [s["ticker"] for s in out] == ["7203.T"]


def test_normalize_swap_keeps_sell_ticker():
    out = _normalize_signals(
        [{"ticker": "7203.T", "action": "swap", "sell_ticker": "8306.T"}], max_signals=2
    )
    assert out[0]["sell_ticker"] == "8306.T"

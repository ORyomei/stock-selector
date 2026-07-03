"""トレーリングストップ活性化ゲートのテスト。

背景: trailing_stop_pct=2% は日本株の日中ボラで即発動し、勝ちを +1〜2% で
刈り取っていた (利小損大)。trailing_activation_pct を導入し、高値が
取得価格 +activation% に達するまでトレーリングを武装しない。
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from domain.models import Position  # noqa: E402
from trading.risk_manager import RiskManager  # noqa: E402


def _pos(entry: float, current: float, peak: float) -> Position:
    return Position(
        ticker="7203.T",
        quantity=100,
        entry_price=entry,
        current_price=current,
        entry_time=datetime.now(UTC),
        peak_price=peak,
    )


def _rm(**cfg) -> RiskManager:
    return RiskManager({"trailing_stop_pct": 3, "trailing_activation_pct": 5, **cfg})


def test_not_armed_below_activation():
    """高値が +5% 未満ならトレーリングは発動しない (旧仕様なら発動していたケース)。"""
    # 高値 +4%、そこから押して +0.5%。旧仕様 (trailing 2-3%) なら早刈りされていた
    rm = _rm()
    should, reason = rm.should_close_position(_pos(100.0, 100.5, 104.0), 100.5)
    assert not should, f"should not close: {reason}"


def test_fires_after_activation():
    """高値が +5% 以上に達した後、3% 逆行で発動する。"""
    rm = _rm()
    # peak 106 → trailing level 102.82。current 102 は下回る → 発動
    should, reason = rm.should_close_position(_pos(100.0, 102.0, 106.0), 102.0)
    assert should and reason == "trailing_stop"


def test_no_fire_when_above_trailing_level():
    """武装後でもトレーリング水準より上なら保有継続。"""
    rm = _rm()
    # peak 106 → trailing level 102.82。current 104 は上 → 継続
    should, reason = rm.should_close_position(_pos(100.0, 104.0, 106.0), 104.0)
    assert not should, f"should not close: {reason}"


def test_activation_zero_keeps_legacy_behavior():
    """activation 未設定 (0) なら従来どおり即武装 (後方互換)。"""
    rm = RiskManager({"trailing_stop_pct": 2})  # activation なし → 0
    # peak 103 → trailing level 100.94 ≥ entry。current 100.5 ≤ → 発動 (旧挙動)
    should, reason = rm.should_close_position(_pos(100.0, 100.5, 103.0), 100.5)
    assert should and reason == "trailing_stop"


def test_locks_in_profit_after_activation():
    """武装後の発動水準は必ず取得価格越え (+5%活性化 × -3%トレールなら +1.85%)。"""
    rm = _rm()
    # ぎりぎり活性化した peak=105 のトレーリング水準は 101.85 (> entry 100)
    should, reason = rm.should_close_position(_pos(100.0, 101.0, 105.0), 101.0)
    assert should and reason == "trailing_stop"  # +1% まで押したら +1.85% 水準を割っている

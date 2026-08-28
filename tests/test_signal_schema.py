"""submit_signals スキーマの価格関係検証のテスト (issue #6)。

背景: 反証ゲート/executor での却下が累計19件、ほぼ全てが
「target_price にアナリスト目標等の上値メド (take_profit 超) を入れる」
という意味の取り違えだった。executor まで到達すると次サイクルまで執行が
遅れるため、ツール境界 (pydantic) で検証し同一ターン内の自己修正を可能にする。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agents.graph_trade import TradeSignalArg  # noqa: E402


def _sig(**kw):
    base = dict(
        ticker="7203.T", action="buy", score=30, confidence=0.6,
        target_price=3100.0, stop_loss_price=2950.0, take_profit_price=3250.0,
    )
    base.update(kw)
    return TradeSignalArg(**base)


def test_valid_buy_passes():
    s = _sig()
    assert s.ticker == "7203.T"


def test_analyst_target_above_take_profit_rejected():
    """実際の却下パターン再現: target(6025) > take(5880)。"""
    with pytest.raises(ValidationError, match="上値メド"):
        _sig(target_price=6025.0, stop_loss_price=4849.0, take_profit_price=5880.0)


def test_zero_target_rejected():
    """実際の却下パターン再現: target_price が 0。"""
    with pytest.raises(ValidationError, match="正の価格が必須"):
        _sig(target_price=0)


def test_stop_above_target_rejected():
    with pytest.raises(ValidationError, match="価格関係が不正"):
        _sig(stop_loss_price=3150.0)  # stop > target


def test_swap_requires_sell_ticker():
    with pytest.raises(ValidationError, match="sell_ticker"):
        _sig(action="swap")


def test_swap_with_sell_ticker_passes():
    s = _sig(action="swap", sell_ticker="9432.T")
    assert s.sell_ticker == "9432.T"


def test_invalid_action_rejected():
    with pytest.raises(ValidationError, match="action"):
        _sig(action="sell")

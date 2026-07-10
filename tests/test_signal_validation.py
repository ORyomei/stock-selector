"""TradingSignal.validation_error (失敗理由付き妥当性チェック) のテスト。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trading.order_manager import TradeAction, TradingSignal  # noqa: E402


def _sig(**kw) -> TradingSignal:
    base = dict(
        ticker="7203.T",
        action=TradeAction.BUY,
        confidence=0.7,
        target_price=2500.0,
        stop_loss_price=2400.0,
        take_profit_price=2700.0,
    )
    base.update(kw)
    return TradingSignal(**base)


def test_valid_buy_signal():
    assert _sig().validation_error() is None
    assert _sig().validate() is True


def test_buy_price_relation_error_names_the_rule():
    # stop > target — 8306.T で実際に起きた「validation failed」の原因調査用
    err = _sig(stop_loss_price=2600.0).validation_error()
    assert err is not None and "BUY の価格関係が不正" in err
    assert _sig(stop_loss_price=2600.0).validate() is False


def test_each_field_error_is_specific():
    assert "confidence" in _sig(confidence=1.5).validation_error()
    assert "target_price" in _sig(target_price=0).validation_error()
    assert "stop_loss_price" in _sig(stop_loss_price=-1).validation_error()
    assert "take_profit_price" in _sig(take_profit_price=0).validation_error()


def test_sell_price_relation():
    ok = _sig(action=TradeAction.SELL, take_profit_price=2300.0, stop_loss_price=2600.0)
    assert ok.validation_error() is None
    bad = _sig(action=TradeAction.SELL, take_profit_price=2600.0, stop_loss_price=2300.0)
    assert "SELL の価格関係が不正" in bad.validation_error()

"""RiskManager.calculate_position_size の評価額上限テスト。

背景: max_position_size_pct は Kelly 式の許容損失率として使われており、
損切りが浅いと現金を超える建玉を要求し、affordability クリップで
1 銘柄に全力買いになっていた（実際に 8306.T が総資産の 52% を占めた）。
建玉評価額 ≤ 総資産 × max_position_size_pct のハード上限を検証する。
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trading.broker_interface import Position  # noqa: E402
from trading.risk_manager import RiskManager  # noqa: E402

PCT = 30


def _rm() -> RiskManager:
    return RiskManager({"max_position_size_pct": PCT})


def _pos(ticker: str, qty: int, price: float) -> Position:
    return Position(
        ticker=ticker,
        quantity=qty,
        entry_price=price,
        current_price=price,
        entry_time=datetime.now(UTC),
    )


def test_no_holdings_caps_at_pct_of_cash():
    """建玉なし時、新規建玉は総資産(=現金)の 30% 以下に収まる。"""
    rm = _rm()
    cash = 1_800_000
    qty = rm.calculate_position_size(
        balance={"cash_jpy": cash, "cash_usd": 0},
        ticker="8306.T",
        entry_price=3344.0,
        stop_loss_price=2977.25,  # 損切り幅 ~11% → Kelly は現金超を要求
        confidence=0.8,
        current_positions=[],
    )
    assert qty * 3344.0 <= cash * PCT / 100 + 3344.0  # 単元丸めの 1 単元分は許容


def test_existing_same_ticker_reduces_allowance():
    """同一銘柄を既に保有していると、追加買いは残り枠までに制限される。"""
    rm = _rm()
    held = [_pos("9999.T", 100, 5000)]  # ¥500k
    qty = rm.calculate_position_size(
        balance={"cash_jpy": 1_500_000, "cash_usd": 0},  # 総資産 2.0M, 30%=600k
        ticker="9999.T",
        entry_price=5000.0,
        stop_loss_price=4700.0,
        confidence=0.9,
        current_positions=held,
    )
    # 既存 500k + 新規 ≤ 600k → 新規は 100k 以下
    assert qty * 5000.0 <= 100_000 + 5000.0


def test_already_over_cap_returns_zero():
    """既に上限超の銘柄は追加買いゼロ。"""
    rm = _rm()
    held = [_pos("8306.T", 500, 3165)]  # ¥1.58M
    qty = rm.calculate_position_size(
        balance={"cash_jpy": 1_178_332, "cash_usd": 0},
        ticker="8306.T",
        entry_price=3165.0,
        stop_loss_price=2977.0,
        confidence=0.8,
        current_positions=held,
    )
    assert qty == 0


def test_other_currency_holdings_excluded_from_base():
    """他通貨の建玉は総資産(分母)に含めない。浅い損切りでも 30% を超えない。"""
    rm = _rm()
    usd_held = [_pos("AAPL", 100, 200)]  # USD建玉 → JPY算出に無関係
    cash = 1_000_000
    qty = rm.calculate_position_size(
        balance={"cash_jpy": cash, "cash_usd": 5000},
        ticker="7203.T",
        entry_price=2000.0,
        stop_loss_price=1980.0,  # 損切り 1% → Kelly 爆発
        confidence=0.9,
        current_positions=usd_held,
    )
    assert qty * 2000.0 <= cash * PCT / 100 + 2000.0


def test_none_positions_falls_back_to_cash():
    """current_positions=None でも現金ベースで上限が効く（後方互換）。"""
    rm = _rm()
    cash = 1_000_000
    qty = rm.calculate_position_size(
        balance={"cash_jpy": cash, "cash_usd": 0},
        ticker="7203.T",
        entry_price=2000.0,
        stop_loss_price=1900.0,
        confidence=1.0,
        current_positions=None,
    )
    assert qty * 2000.0 <= cash * PCT / 100 + 2000.0


# ── トレーリングストップ (高値追従) ──────────────────────────────


def _held(entry, current, peak, stop_loss=None, take_profit=None):
    return Position(
        ticker="7203.T", quantity=100, entry_price=entry, current_price=current,
        entry_time=datetime.now(UTC), stop_loss=stop_loss, take_profit=take_profit,
        peak_price=peak,
    )


def test_trailing_stop_locks_profit_from_peak():
    """高値 1300 から 2% 逆行 (≤1274) で利益確定。旧実装(取得価×0.98=980)では発火しなかった。"""
    rm = RiskManager({"max_position_size_pct": PCT, "trailing_stop_pct": 2})
    pos = _held(entry=1000, current=1270, peak=1300, stop_loss=900, take_profit=2000)
    should, reason = rm.should_close_position(pos, 1270)
    assert should and reason == "trailing_stop"


def test_trailing_stop_does_not_fire_above_level():
    """高値近辺 (1290 > 1274) ではまだ発火しない。"""
    rm = RiskManager({"max_position_size_pct": PCT, "trailing_stop_pct": 2})
    pos = _held(entry=1000, current=1290, peak=1300, stop_loss=900, take_profit=2000)
    should, _ = rm.should_close_position(pos, 1290)
    assert not should


def test_trailing_stop_inactive_before_profit():
    """含み益が乗る前 (高値≒取得価) はトレーリングが発火せず、通常の損切りに委ねる。"""
    rm = RiskManager({"max_position_size_pct": PCT, "trailing_stop_pct": 2})
    # peak=1010, trailing_level=989.8 < entry(1000) → トレーリング発動条件を満たさない
    pos = _held(entry=1000, current=992, peak=1010, stop_loss=950, take_profit=2000)
    should, _ = rm.should_close_position(pos, 992)
    assert not should  # entry-2% 程度では即クローズしない (旧バグの挙動を防ぐ)


def test_peak_price_ratchets_up_not_down():
    """__post_init__ で高値は切り上がり、下落では下がらない。"""
    pos = Position(ticker="X", quantity=1, entry_price=1000, current_price=1000,
                   entry_time=datetime.now(UTC))
    assert pos.peak_price == 1000
    pos.current_price = 1300
    pos.__post_init__()
    assert pos.peak_price == 1300
    pos.current_price = 1100
    pos.__post_init__()
    assert pos.peak_price == 1300  # 下落しても高値は維持

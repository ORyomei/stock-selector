"""通貨別の資金チェック (_order_cost) のテスト。

旧実装は USD価格を JPY 残高と混在比較していたため、USD建て候補が
事前チェックをすり抜けて発注枠を空費し、約定段階で USD 不足で失敗していた。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agents.auto_trade import _cash_by_currency, _order_cost  # noqa: E402


def test_order_cost_jp():
    ccy, lot, cost = _order_cost("8306.T", 3000.0)
    assert ccy == "JPY"
    assert lot == 100
    assert cost == 300_000.0  # 100株単位


def test_order_cost_us():
    ccy, lot, cost = _order_cost("MRK", 80.0)
    assert ccy == "USD"
    assert lot == 1
    assert cost == 80.0


def test_us_candidate_not_affordable_with_only_jpy():
    """JPY のみ保有時、USD建て候補は USD 現金(0)で判定され買えない。"""
    cash = _cash_by_currency({"cash_jpy": 1_463_575, "cash_usd": 0})
    ccy, _lot, cost = _order_cost("MRK", 80.0)
    # 旧バグ: cost(80) を JPY残高(146万) と比較して「買える」と誤判定していた
    assert ccy == "USD"
    assert cost > cash[ccy]  # USD 0 なので買えない (正しく弾かれる)


def test_jp_candidate_affordable_with_jpy():
    """JPY 保有があれば JP 候補は JPY 現金で正しく判定され買える。"""
    cash = _cash_by_currency({"cash_jpy": 1_463_575, "cash_usd": 0})
    ccy, _lot, cost = _order_cost("7203.T", 1000.0)  # 100株 = 10万円
    assert ccy == "JPY"
    assert cost <= cash[ccy]

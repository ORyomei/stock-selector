"""AI 手仕舞い助言の応答パース/検証テスト (機械ストップは床)。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from datetime import date, timedelta  # noqa: E402

from agents.exit_advisor import _earnings_days_from, _parse_exit_response  # noqa: E402


def test_earnings_days_picks_nearest_future():
    today = date(2026, 6, 14)
    dates = [date(2026, 4, 28), date(2026, 6, 20), date(2026, 8, 6)]  # 過去1+未来2
    assert _earnings_days_from(dates, today) == 6  # 最も近い未来 6/20


def test_earnings_days_none_when_all_past():
    today = date(2026, 6, 14)
    assert _earnings_days_from([date(2026, 1, 1)], today) is None
    assert _earnings_days_from([], today) is None


def test_earnings_days_today_is_zero():
    today = date(2026, 6, 14)
    assert _earnings_days_from([today, today + timedelta(days=10)], today) == 0

HELD = {"8306.T", "7267.T", "1306.T"}


def test_exit_and_trim_accepted():
    text = """```json
    {"exits": [
        {"ticker": "8306.T", "action": "exit", "reason": "テクニカル売り転換"},
        {"ticker": "7267.T", "action": "trim", "reason": "高値から押し戻し"}
    ]}
    ```"""
    out = _parse_exit_response(text, HELD)
    assert {a["ticker"]: a["action"] for a in out} == {"8306.T": "exit", "7267.T": "trim"}


def test_hold_is_dropped():
    """hold はノーオペなので結果に含めない。"""
    text = '{"exits": [{"ticker": "8306.T", "action": "hold", "reason": "継続"}]}'
    assert _parse_exit_response(text, HELD) == []


def test_unheld_ticker_rejected():
    """保有していないティッカー (ハルシネーション) は却下。"""
    text = '{"exits": [{"ticker": "AAPL", "action": "exit", "reason": "x"}]}'
    assert _parse_exit_response(text, HELD) == []


def test_invalid_action_rejected():
    text = '{"exits": [{"ticker": "8306.T", "action": "buy", "reason": "x"}]}'
    assert _parse_exit_response(text, HELD) == []


def test_duplicate_ticker_deduped():
    text = """{"exits": [
        {"ticker": "1306.T", "action": "exit", "reason": "a"},
        {"ticker": "1306.T", "action": "trim", "reason": "b"}
    ]}"""
    out = _parse_exit_response(text, HELD)
    assert len(out) == 1 and out[0]["action"] == "exit"


def test_parse_failure_is_safe():
    """パース不能 / 空 / 不正形は空リスト (= 機械ストップのみ)。"""
    assert _parse_exit_response(None, HELD) == []
    assert _parse_exit_response("not json at all", HELD) == []
    assert _parse_exit_response('{"exits": "oops"}', HELD) == []
    assert _parse_exit_response('{"signals": []}', HELD) == []

"""ポートフォリオ単位 AI 推論の応答パース (純関数) テスト。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agents.portfolio_review import _parse_review  # noqa: E402

CANDS = {"8306.T", "7203.T", "1306.T"}


def test_drop_only():
    text = """```json
    {"decisions": [
        {"ticker": "8306.T", "action": "drop", "reason": "金融セクター既に40%"},
        {"ticker": "7203.T", "action": "keep", "reason": "OK"}
    ]}
    ```"""
    drops = _parse_review(text, CANDS)
    assert set(drops) == {"8306.T"}  # keep は無視、drop のみ
    assert "金融" in drops["8306.T"]


def test_unknown_candidate_ignored():
    text = '{"decisions": [{"ticker": "AAPL", "action": "drop", "reason": "x"}]}'
    assert _parse_review(text, CANDS) == {}


def test_parse_failure_safe():
    assert _parse_review(None, CANDS) == {}
    assert _parse_review("garbage", CANDS) == {}
    assert _parse_review('{"decisions": "oops"}', CANDS) == {}

"""run_trade_cmd 出力からの失敗理由抽出のテスト。

背景: 発注失敗時に「約定失敗」とだけログ出力し実 reason を捨てていたため、
timespan バグ (2週間トレードゼロ) の原因特定が遅れた。失敗理由を必ず
ログに残すためのヘルパー _result_reason を検証する。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agents.graph_trade import _result_reason  # noqa: E402


def test_extracts_reason_from_json_block():
    out = (
        "❌ 7267.T: 実行失敗\n\n"
        "💾 ログ保存: diary/x.json\n\n"
        "```json\n"
        + json.dumps({"success": False, "status": "ERROR", "reason": "ValueError: bad"}, ensure_ascii=False)
        + "\n```\n"
    )
    assert _result_reason(out) == "ValueError: bad"


def test_falls_back_to_status_when_no_reason():
    out = "```json\n" + json.dumps({"status": "REJECTED", "reason": ""}) + "\n```"
    assert _result_reason(out) == "REJECTED"


def test_falls_back_to_last_line_when_no_json():
    out = "何かの出力\nInsufficient funds: JPY 0"
    assert _result_reason(out) == "Insufficient funds: JPY 0"


def test_handles_empty_output():
    assert _result_reason("") == "理由不明 (出力なし)"


def test_handles_malformed_json():
    out = "```json\n{ broken json \n```"
    # JSON パース失敗 → 末尾行フォールバック (例外を投げない)
    assert isinstance(_result_reason(out), str)
    assert _result_reason(out) != ""

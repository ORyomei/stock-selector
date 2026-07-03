"""core/trade_stats (実測期待値の集計とプロンプト整形) のテスト。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core import trade_stats as ts  # noqa: E402


def test_confidence_bucket():
    assert ts.confidence_bucket({"entry_confidence": 0.5}) == "conf<0.6"
    assert ts.confidence_bucket({"entry_confidence": 0.65}) == "conf0.6-0.7"
    assert ts.confidence_bucket({"entry_confidence": 0.9}) == "conf>0.7"
    assert ts.confidence_bucket({}) == "不明"


def test_format_for_prompt_filters_noise():
    stats = {
        "n_closes": 10,
        "by_reason": {
            "stop_loss": {"count": 5, "win_rate": 0.0, "total_pnl": -30000, "avg_pnl": -6000},
            "take_profit": {"count": 2, "win_rate": 100.0, "total_pnl": 9000, "avg_pnl": 4500},  # <3件 → 省く
        },
        "by_hold": {
            "当日": {"count": 4, "win_rate": 25.0, "total_pnl": -8000, "avg_pnl": -2000},
            "不明": {"count": 30, "win_rate": 50.0, "total_pnl": 0, "avg_pnl": 0},  # 不明 → 省く
        },
        "by_score": {},
        "by_confidence": {},
    }
    text = ts.format_for_prompt(stats, min_count=3)
    assert "stop_loss: 5件 勝率0.0% 平均¥-6,000" in text
    assert "take_profit" not in text  # min_count 未満
    assert "不明" not in text
    assert "当日: 4件" in text
    assert "指針" in text


def test_format_for_prompt_empty_when_no_rows():
    stats = {"n_closes": 2, "by_reason": {}, "by_hold": {}, "by_score": {}, "by_confidence": {}}
    assert ts.format_for_prompt(stats) == ""


def test_expectancy_includes_by_confidence_key():
    """expectancy が confidence 帯を含む (校正フィードバックの土台)。"""
    # get_container 経由の実データ読み。キーの存在だけ検証 (中身は環境依存)
    result = ts.expectancy(days=5)
    assert set(result.keys()) >= {"by_reason", "by_hold", "by_score", "by_confidence", "n_closes"}

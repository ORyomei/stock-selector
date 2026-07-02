"""シグナルの timespan 正規化のリグレッションテスト。

カバー範囲:
- JSON から読んだ timespan は文字列だが TradingSignal.timespan は TimeSpan enum。
  正規化しないと execute_signal の `signal.timespan.value` で
  'str' object has no attribute 'value' となり AI 発注が全滅する。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

from core.trade import _normalize_timespan  # noqa: E402
from trading.order_manager import TimeSpan  # noqa: E402


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("swing", TimeSpan.SWING),
        ("SHORT", TimeSpan.SHORT),  # 大文字
        (" Medium ", TimeSpan.MEDIUM),  # 前後空白
        ("long", TimeSpan.LONG),
        (TimeSpan.SHORT, TimeSpan.SHORT),  # 既に enum
        ("unknown", TimeSpan.SWING),  # 未知 → デフォルト SWING
        (None, TimeSpan.SWING),  # None → デフォルト
    ],
)
def test_normalize_timespan(raw, expected):
    assert _normalize_timespan(raw) == expected


def test_normalized_timespan_has_value_attr():
    """正規化後は .value にアクセスできる (execute_signal が呼ぶ経路)。"""
    ts = _normalize_timespan("swing")
    assert ts.value == "swing"  # 文字列のままだと AttributeError になっていた

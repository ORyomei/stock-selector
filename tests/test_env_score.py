"""市場環境スコア (compute_env_score) のテスト。

背景: 従来スコアは VIX/米金利/S&P500 のみで日本市場を見ておらず、
TOPIX が3日続落しても neutral (+10) 固定だった。日経モメンタムの
追加で下落局面に risk_off へ遷移できることを検証する。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.macro import compute_env_score  # noqa: E402


def _data(**kw) -> dict:
    """指標データの雛形。kw で NIKKEI_5d 等を指定。"""
    d: dict = {}
    if "vix" in kw:
        d["VIX"] = {"current": kw["vix"]}
    if "nikkei_5d" in kw or "nikkei_20d" in kw:
        d["NIKKEI"] = {
            "change_5d": f"{kw.get('nikkei_5d', 0):+.2f}%" if "nikkei_5d" in kw else None,
            "change_20d": f"{kw.get('nikkei_20d', 0):+.2f}%" if "nikkei_20d" in kw else None,
        }
    if "sp500_20d" in kw:
        d["SP500"] = {"change_20d": f"{kw['sp500_20d']:+.2f}%"}
    return d


def test_nikkei_crash_turns_risk_off_despite_calm_vix():
    """今週の実例: VIX 平常 (+10) でも日経が急落していれば risk_off 圏 (≤-20)。"""
    score, signals = compute_env_score(_data(vix=15.8, nikkei_5d=-3.5, nikkei_20d=-5.5))
    assert score == 10 - 20 - 10  # VIX通常 +10, 日経5日急落 -20, 20日下落 -10
    assert score <= -20  # prompt_scenarios の risk_off 閾値
    assert any("日経急落" in s for s in signals)


def test_vix_only_baseline_unchanged():
    """日経データなし時は従来どおり VIX ベース (後方互換)。"""
    score, signals = compute_env_score(_data(vix=15.8))
    assert score == 10
    assert any("VIX通常" in s for s in signals)


def test_mild_nikkei_dip_is_caution_not_risk_off():
    score, _ = compute_env_score(_data(vix=15.8, nikkei_5d=-2.0))
    assert score == 0  # +10 (VIX) -10 (日経警戒) → neutral 圏で慎重


def test_nikkei_rally_adds_risk_on():
    score, _ = compute_env_score(_data(vix=14.0, nikkei_5d=3.5, nikkei_20d=6.0))
    assert score == 20 + 10 + 10  # risk_on 圏


def test_handles_garbage_and_missing_fields():
    score, _ = compute_env_score({"NIKKEI": {"change_5d": "garbage"}, "VIX": {}})
    assert score == 0  # 例外を出さず 0

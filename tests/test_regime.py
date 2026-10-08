"""レジームゲート (issue #19) のテスト。

TOPIX (1306.T) 終値 > SMA20 かつ 5 日騰落 > 0 のときだけ新規エントリーを許可する。
41 建玉の実測: 成立局面 +92,790 円 (勝率 52%) / 不成立局面 -120,761 円 (35%)。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.regime import evaluate_regime, fetch_regime, filter_outliers  # noqa: E402


def _ramp(start: float, step: float, n: int) -> list[float]:
    return [start + step * i for i in range(n)]


def test_uptrend_passes():
    """右肩上がり: SMA20 の上、5 日騰落プラス → 成立。"""
    r = evaluate_regime(_ramp(400, 1.0, 40), as_of="2026-10-08")
    assert r.ok is True
    assert r.decided is True
    assert r.sma_gap_pct > 0 and r.lookback_ret_pct > 0
    assert "成立" in r.summary() and "2026-10-08" in r.summary()


def test_below_sma_blocks():
    """直近で急落し SMA20 を割った → 不成立。"""
    closes = _ramp(400, 1.0, 35) + [420, 410, 400, 395, 390]
    r = evaluate_regime(closes)
    assert r.ok is False
    assert "SMA20 割れ" in r.reason


def test_above_sma_but_five_day_down_blocks():
    """SMA20 の上だが 5 日間は下げている (上昇後の反落初期) → 不成立。

    回顧録 W37 (9/7〜) の型: TOPIX は高値圏だが 5 日騰落マイナスの週に 13 建玉 0 勝。
    """
    closes = _ramp(400, 2.0, 35) + [467, 466, 465, 464, 463]
    r = evaluate_regime(closes)
    assert r.close > r.sma
    assert r.lookback_ret_pct < 0
    assert r.ok is False
    assert "5日騰落マイナス" in r.reason


def test_insufficient_data_fails_open():
    """データ不足は判定不能 = フェイルオープン (ゲートの不具合で売買を止めない)。"""
    r = evaluate_regime([400, 401, 402])
    assert r.ok is True
    assert r.decided is False
    assert "フェイルオープン" in r.summary()


def test_disabled_gate_always_allows():
    r = evaluate_regime([1.0] * 5, enabled=False)
    assert r.ok is True and r.enabled is False
    assert r.summary() == "ゲート無効"


def test_outlier_filter_drops_yfinance_spike():
    """中央値の 3 倍超 (過去の +1051% 事故) と NaN・非正値を除外する。"""
    vals = [400.0, 402.0, 4600.0, float("nan"), 0.0, 398.0, 401.0]
    assert filter_outliers(vals) == [400.0, 402.0, 398.0, 401.0]


def test_spike_does_not_flip_verdict():
    """末尾に異常値が混じっても、除外後の系列で判定する。"""
    closes = _ramp(400, 1.0, 40) + [4000.0]
    r = evaluate_regime(closes)
    assert r.close == 439.0  # 異常値 4000 は無視される
    assert r.ok is True


def test_fetch_regime_fail_open_on_fetch_error(monkeypatch):
    """市場データ取得が例外を投げても ok=True で理由を残す。"""
    import core.regime as regime_mod

    class _Boom:
        def get_price_history(self, *a, **k):
            raise RuntimeError("yfinance down")

    class _Container:
        def market_data(self):
            return _Boom()

    monkeypatch.setattr("infra.container.get_container", lambda: _Container())
    r = regime_mod.fetch_regime({"enabled": True, "ticker": "1306.T"})
    assert r.ok is True and r.decided is False
    assert "取得失敗" in r.reason


def test_fetch_regime_respects_config_disable():
    r = fetch_regime({"enabled": False})
    assert r.ok is True and r.enabled is False

#!/usr/bin/env python3
"""入口の測定値ゲート (issue #20)。

submit_signals で提出された buy / swap を、自己申告 (confidence) や文章 (fail_conditions の
語句) ではなく score_stock の**実測値**で機械検査する。#14 の「キーワード照合 ∧ confidence
≥ 0.6」は表記ゆれと 0.55 申告で素通りし (#17)、confidence は相場レジームの写しで予測力が
無かった (8 月 >0.62 で 82% 勝ち、9 月以降 0 勝 3 敗)。

41 建玉の実測 (diary/retro/2026-10-08_retrospective.md §2-1):

    BB 位置 > 85%   10 建玉  勝率 30%  -103,959 円   (70〜85%: 4 建玉 75% +30,130 円)
    RSI > 65         3 建玉  勝率  0%   -41,203 円
    score >= 40      9 建玉  勝率 56%   +94,407 円   (25〜39: 29 建玉 45% -67,233 円)

- 閾値は config/risk_limits.json の ``entry_gate`` (プロンプトにも同じ数字を出す)
- 測定できない場合はフェイルオープン (警告を残して通す)。ゲートの不具合で売買を止めない
  方針は他のゲートと同じ
- 差し戻し文面は実測値と閾値だけを書き、語句の手がかりを渡さない
"""

from __future__ import annotations

import contextlib
import io
import sys
from dataclasses import asdict, dataclass
from typing import Any

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "max_bb_position_pct": 85.0,
    "max_rsi": 65.0,
    "min_score": 40.0,
}


@dataclass
class EntryMeasure:
    ticker: str
    score: float | None
    rsi: float | None
    bb_position_pct: float | None
    annualized_vol_pct: float | None = None
    volume_ratio_20d: float | None = None
    current_price: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_entry_gate_config() -> dict[str, Any]:
    """既定値に config/risk_limits.json の ``entry_gate`` を重ねて返す。"""
    cfg = dict(DEFAULTS)
    try:
        from core.trade import load_risk_limits

        user = load_risk_limits().get("entry_gate", {})
        if isinstance(user, dict):
            cfg.update(user)
    except Exception:
        pass
    return cfg


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN は None


def measure_from_score_result(ticker: str, result: dict[str, Any]) -> EntryMeasure:
    """score_stock (compute_score) の戻り値から判定に使う測定値を取り出す。"""
    ind = result.get("indicators") or {}
    summary = result.get("analysis_summary") or {}
    return EntryMeasure(
        ticker=ticker,
        score=_num(summary.get("total_score")),
        rsi=_num(ind.get("rsi_14")),
        bb_position_pct=_num(ind.get("bb_position_pct")),
        annualized_vol_pct=_num(ind.get("annualized_vol_pct")),
        volume_ratio_20d=_num(ind.get("volume_ratio_20d")),
        current_price=_num(result.get("current_price")),
    )


def measure_entry(ticker: str) -> EntryMeasure | None:
    """score_stock を (20 分キャッシュ経由で) 再計測する。取得不能なら None。"""
    try:
        from core.scorer import compute_score_cached

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):  # compute_score は JSON を stdout に出す
            result = compute_score_cached(ticker)
        if not isinstance(result, dict) or "error" in result:
            return None
        return measure_from_score_result(ticker, result)
    except (SystemExit, Exception) as e:  # compute_score はデータ不足で sys.exit する
        print(f"⚠️  entry gate: {ticker} の測定に失敗 (フェイルオープン): {e}", file=sys.stderr)
        return None


def check_entry(m: EntryMeasure | None, cfg: dict[str, Any] | None = None) -> tuple[bool, str]:
    """測定値を閾値と照合する純関数。(ok, 理由) を返す。"""
    cfg = {**DEFAULTS, **(cfg or {})}
    if not cfg.get("enabled", True):
        return True, "入口ゲート無効"
    if m is None:
        return True, "測定不能のため通過 (フェイルオープン)"
    max_bb = float(cfg["max_bb_position_pct"])
    max_rsi = float(cfg["max_rsi"])
    min_score = float(cfg["min_score"])
    fails: list[str] = []
    if m.bb_position_pct is not None and m.bb_position_pct > max_bb:
        fails.append(f"BB位置 {m.bb_position_pct:.0f}% > {max_bb:.0f}%")
    if m.rsi is not None and m.rsi > max_rsi:
        fails.append(f"RSI {m.rsi:.0f} > {max_rsi:.0f}")
    if m.score is not None and m.score < min_score:
        fails.append(f"score {m.score:.0f} < {min_score:.0f}")
    if fails:
        return False, "入口ゲート却下 (" + ", ".join(fails) + ")"
    parts: list[str] = []
    if m.bb_position_pct is not None:
        parts.append(f"BB {m.bb_position_pct:.0f}%")
    if m.rsi is not None:
        parts.append(f"RSI {m.rsi:.0f}")
    if m.score is not None:
        parts.append(f"score {m.score:.0f}")
    return True, "入口ゲート通過 (" + ", ".join(parts) + ")"


def evaluate_entry(
    ticker: str, cfg: dict[str, Any] | None = None
) -> tuple[bool, str, dict[str, Any] | None]:
    """測定 + 判定。(ok, 理由, 測定値 dict or None) を返す。"""
    cfg = load_entry_gate_config() if cfg is None else {**DEFAULTS, **cfg}
    if not cfg.get("enabled", True):
        return True, "入口ゲート無効", None
    m = measure_entry(ticker)
    ok, msg = check_entry(m, cfg)
    return ok, msg, (m.to_dict() if m is not None else None)

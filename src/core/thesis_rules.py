#!/usr/bin/env python3
"""テーゼをコードにする (issue #23)。

エントリー時に fail / invalidation 条件を**数値ルール**で提出させ、毎サイクル決定論的に
評価して成立したら全株クローズする。自由文の fail_conditions は補助 AI の読み直しに依存し、
1 単元保有では trim が no-op (#15)、語句の言い換えで回避 (#17) されていた。

ルール: ``{"metric": <ALLOWED_METRICS のキー>, "op": "<" | "<=" | ">" | ">=", "value": 数値}``
評価: metric の現在値 ``op`` value が真なら成立。metric が計算できない場合は不成立
(安全側 = 保有継続。下方リスクは機械ストップが守る)。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

ALLOWED_METRICS: dict[str, str] = {
    "pnl_pct": "建玉の含み損益 % (取得価格比)",
    "drawdown_from_peak_pct": "保有中の高値からの下落 % (0 以下。例: < -4)",
    "days_held": "保有営業日数",
    "rsi_14": "RSI (14 日)",
    "bb_position_pct": "ボリンジャーバンド内の位置 % (0=下限, 100=上限)",
    "close_vs_sma5_pct": "終値の 5 日線乖離 %",
    "close_vs_sma25_pct": "終値の 25 日線乖離 %",
    "close_vs_sma75_pct": "終値の 75 日線乖離 %",
    "macd_hist": "MACD ヒストグラム",
    "volume_ratio_20d": "出来高 / 20 日平均出来高",
    "ret_5d_pct": "銘柄の 5 日騰落 %",
    "ret_20d_pct": "銘柄の 20 日騰落 %",
    "topix_vs_sma20_pct": "TOPIX (1306.T) の 20 日線乖離 %",
    "topix_5d_pct": "TOPIX (1306.T) の 5 日騰落 %",
}
ALLOWED_OPS: tuple[str, ...] = ("<", "<=", ">", ">=")


def validate_rule(rule: Any) -> dict[str, Any]:
    """ルール 1 件を検証して正規化する。不正なら ValueError (文面はエージェントに返る)。"""
    if not isinstance(rule, dict):
        raise ValueError("invalidation_rules の各要素は {metric, op, value} のオブジェクト")
    metric = str(rule.get("metric", "")).strip()
    op = str(rule.get("op", "")).strip()
    if metric not in ALLOWED_METRICS:
        raise ValueError(
            f"metric '{metric}' は使えない。使える metric: {', '.join(ALLOWED_METRICS)}"
        )
    if op not in ALLOWED_OPS:
        raise ValueError(f"op '{op}' は使えない。使える op: {' '.join(ALLOWED_OPS)}")
    try:
        value = float(rule.get("value"))
    except (TypeError, ValueError) as e:
        raise ValueError(f"value は数値 ({rule.get('value')!r})") from e
    if value != value:
        raise ValueError("value に NaN は不可")
    return {"metric": metric, "op": op, "value": value}


def format_rule(rule: dict[str, Any]) -> str:
    return f"{rule['metric']} {rule['op']} {rule['value']:g}"


def _holds(op: str, actual: float, value: float) -> bool:
    if op == "<":
        return actual < value
    if op == "<=":
        return actual <= value
    if op == ">":
        return actual > value
    if op == ">=":
        return actual >= value
    return False


def evaluate_rules(
    rules: Iterable[dict[str, Any]], metrics: dict[str, float | None]
) -> list[dict[str, Any]]:
    """成立したルールを [{metric, op, value, actual}] で返す (純関数)。metric 欠損は不成立。"""
    fired: list[dict[str, Any]] = []
    for r in rules:
        try:
            rule = validate_rule(r)
        except ValueError:
            continue
        actual = metrics.get(rule["metric"])
        if actual is None:
            continue
        try:
            a = float(actual)
        except (TypeError, ValueError):
            continue
        if a != a:
            continue
        if _holds(rule["op"], a, rule["value"]):
            fired.append({**rule, "actual": a})
    return fired


def _pct(num: float | None, den: float | None) -> float | None:
    try:
        if num is None or den is None or float(den) == 0:
            return None
        return (float(num) / float(den) - 1) * 100
    except (TypeError, ValueError):
        return None


def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def compute_metrics(
    position: dict[str, Any],
    score_result: dict[str, Any] | None,
    regime: Any = None,
    now: datetime | None = None,
) -> dict[str, float | None]:
    """建玉・score_stock・レジーム判定から ALLOWED_METRICS の現在値を組み立てる。"""
    entry = _f(position.get("entry_price"))
    current = _f(position.get("current_price"))
    peak = _f(position.get("peak_price")) or entry
    m: dict[str, float | None] = {
        "pnl_pct": _pct(current, entry),
        "drawdown_from_peak_pct": _pct(current, peak),
        "days_held": None,
    }
    et = position.get("entry_time")
    try:
        from core.churn_guard import business_days_between

        dt = et if isinstance(et, datetime) else datetime.fromisoformat(str(et))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        m["days_held"] = float(business_days_between(dt, now or datetime.now(UTC)))
    except (TypeError, ValueError):
        pass

    ind = (score_result or {}).get("indicators") or {}
    price = _f((score_result or {}).get("current_price")) or current
    m.update({
        "rsi_14": _f(ind.get("rsi_14")),
        "bb_position_pct": _f(ind.get("bb_position_pct")),
        "close_vs_sma5_pct": _pct(price, _f(ind.get("sma_5"))),
        "close_vs_sma25_pct": _pct(price, _f(ind.get("sma_25"))),
        "close_vs_sma75_pct": _pct(price, _f(ind.get("sma_75"))),
        "macd_hist": _f(ind.get("macd_hist")),
        "volume_ratio_20d": _f(ind.get("volume_ratio_20d")),
        "ret_5d_pct": _f(ind.get("ret_5d_pct")),
        "ret_20d_pct": _f(ind.get("ret_20d_pct")),
    })
    if regime is not None and getattr(regime, "decided", False):
        m["topix_vs_sma20_pct"] = _f(getattr(regime, "sma_gap_pct", None))
        m["topix_5d_pct"] = _f(getattr(regime, "lookback_ret_pct", None))
    else:
        m["topix_vs_sma20_pct"] = None
        m["topix_5d_pct"] = None
    return m


def signal_path(ticker: str):
    from infra.container import DIARY_DIR

    return DIARY_DIR / "signals" / f"{ticker.replace('.', '')}_auto.json"


def load_rules_for(ticker: str) -> list[dict[str, Any]]:
    """エントリー時のシグナルファイルから有効なルールだけを読む (無ければ空)。"""
    try:
        data = json.loads(signal_path(ticker).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    out: list[dict[str, Any]] = []
    for r in data.get("invalidation_rules") or []:
        try:
            out.append(validate_rule(r))
        except ValueError as e:
            print(f"⚠️  thesis rule skipped ({ticker}): {e}", file=sys.stderr)
    return out


def rules_prompt_block() -> str:
    """システムプロンプト用: 使える metric と op の一覧 (ALLOWED_METRICS が唯一の真実)。"""
    lines = [f"  - `{k}`: {v}" for k, v in ALLOWED_METRICS.items()]
    return "\n".join(lines) + f"\n  op: {' '.join(ALLOWED_OPS)}"

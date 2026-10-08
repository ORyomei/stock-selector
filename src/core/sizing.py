#!/usr/bin/env python3
"""サイズと現金の機械ルール (issue #21)。純関数のみ (I/O は呼び出し側)。

回顧録 §2-3 (3): 1 銘柄 25〜30%・露出 97〜99% が常態で、SBG (年率ボラ 88%) も JT も同じ
比率で持ち、単発 -83,848 円 = 資本の 2.8%。現金が無く swap が片肺 (売り成功 → 買い足が
資金不足) になり翌日 12 サイクル空転した。

- vol_size_factor:   年率ボラでサイズを割る (> 60% で 1/2、> 80% で 1/3)
- cash_floor_check:  買い後の現金比率が下限 (20%) を割らないか
- swap_feasibility:  売る前に買い足 1 単元が賄えるか (片肺防止)
- sector_cap_check:  同一セクター比率が上限 (35%) を超えないか
"""

from __future__ import annotations

from collections.abc import Sequence

DEFAULT_MIN_CASH_PCT = 20.0
DEFAULT_MAX_SECTOR_PCT = 35.0
DEFAULT_VOL_HALF_SIZE_ABOVE_PCT = 60.0
DEFAULT_VOL_THIRD_SIZE_ABOVE_PCT = 80.0
UNKNOWN_SECTORS = {"", "不明", "None"}


def vol_size_factor(
    annualized_vol_pct: float | None,
    half_above_pct: float = DEFAULT_VOL_HALF_SIZE_ABOVE_PCT,
    third_above_pct: float = DEFAULT_VOL_THIRD_SIZE_ABOVE_PCT,
) -> float:
    """年率ボラ (%) からサイズ係数を返す。> third → 1/3、> half → 1/2、それ以外 1.0。不明は 1.0。"""
    if annualized_vol_pct is None:
        return 1.0
    try:
        v = float(annualized_vol_pct)
    except (TypeError, ValueError):
        return 1.0
    if v != v:  # NaN
        return 1.0
    if v > third_above_pct:
        return 1.0 / 3.0
    if v > half_above_pct:
        return 0.5
    return 1.0


def cash_floor_check(
    cash_jpy: float, buy_cost_jpy: float, equity_jpy: float, min_cash_pct: float
) -> tuple[bool, str]:
    """買い後の現金比率が下限を割らないか。(ok, 理由)。"""
    if equity_jpy <= 0 or min_cash_pct <= 0:
        return True, "現金下限なし"
    after = cash_jpy - buy_cost_jpy
    after_pct = after / equity_jpy * 100
    if after_pct < min_cash_pct:
        return False, (
            f"買い後の現金比率 {after_pct:.0f}% が下限 {min_cash_pct:.0f}% を下回る "
            f"(現金 ¥{cash_jpy:,.0f} - ¥{buy_cost_jpy:,.0f})"
        )
    return True, f"買い後の現金比率 {after_pct:.0f}% ≥ 下限 {min_cash_pct:.0f}%"


def swap_feasibility(
    cash_jpy: float,
    sell_value_jpy: float,
    buy_unit_cost_jpy: float,
    equity_jpy: float,
    min_cash_pct: float,
) -> tuple[bool, str]:
    """売り足を出す前に、買い足 1 単元が (売却代金 + 現金 - 現金下限) で賄えるか。片肺防止。"""
    floor = equity_jpy * min_cash_pct / 100 if equity_jpy > 0 and min_cash_pct > 0 else 0.0
    spendable = cash_jpy + sell_value_jpy - floor
    if buy_unit_cost_jpy > spendable:
        return False, (
            f"買い足 1 単元 ¥{buy_unit_cost_jpy:,.0f} > 売却代金 ¥{sell_value_jpy:,.0f} + 現金 "
            f"¥{cash_jpy:,.0f} - 現金下限 ¥{floor:,.0f} = ¥{spendable:,.0f} — 片肺 swap になるため売らない"
        )
    return True, f"買い足 1 単元 ¥{buy_unit_cost_jpy:,.0f} ≤ 使用可能 ¥{spendable:,.0f}"


def sector_cap_check(
    holdings: Sequence[tuple[str, str, float]],
    candidate_ticker: str,
    candidate_sector: str,
    prospective_value_jpy: float,
    equity_jpy: float,
    max_sector_pct: float,
    exclude_ticker: str | None = None,
) -> tuple[bool, str]:
    """同一セクター比率が上限を超えないか。holdings = [(ticker, sector, 評価額 JPY)]。

    swap では売り足 (exclude_ticker) を保有から除いて判定する。セクター不明は通す
    (ゲートの都合で売買を止めない)。
    """
    if equity_jpy <= 0 or max_sector_pct <= 0:
        return True, "セクター上限なし"
    if str(candidate_sector) in UNKNOWN_SECTORS:
        return True, "セクター不明のため通過"
    cur = sum(
        v for t, s, v in holdings
        if s == candidate_sector and t != exclude_ticker and t != candidate_ticker
    )
    cur_pct = cur / equity_jpy * 100
    after_pct = (cur + prospective_value_jpy) / equity_jpy * 100
    if after_pct > max_sector_pct:
        return False, (
            f"セクター '{candidate_sector}' が {cur_pct:.0f}% → {after_pct:.0f}% で"
            f"上限 {max_sector_pct:.0f}% 超"
        )
    return True, f"セクター '{candidate_sector}' {cur_pct:.0f}% → {after_pct:.0f}% (上限 {max_sector_pct:.0f}%)"

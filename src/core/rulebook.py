#!/usr/bin/env python3
"""並走ベンチマーク「ルールのみ」(issue #24 (4))。

LLM を使わず、#19〜#23 で入れたルールだけで同じユニバースを売買する紙上の帳簿:
レジームゲート (TOPIX > SMA20 かつ 5 日騰落 > 0) → 測定値ゲート (BB ≤ 85 / RSI ≤ 65 /
score ≥ 40) で score 上位から埋める → 1 銘柄 20% / 現金下限 20% / セクター 35% / ボラ連動サイズ
→ 出口は機械 (損切り -5% / 利確 +8% / 21 営業日) + 固定 invalidation ルール (close < SMA25)。
新規判断は 1 日 1 回 (10:30 JST 以降の最初のサイクル)。

LLM 版 (本番帳簿) が 2 か月でこれを上回らなければ、LLM の役割を候補選定と材料精査に限定する。
状態: logs/rulebook_state.json / 約定: logs/rulebook_trades.jsonl / 資産: logs/equity_history_rules.jsonl
判定ロジックは純関数 (select_entries / apply_exits)、I/O は run_cycle に集約。
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = PROJECT_DIR / "config" / "rulebook.json"
STATE_PATH = PROJECT_DIR / "logs" / "rulebook_state.json"
TRADES_PATH = PROJECT_DIR / "logs" / "rulebook_trades.jsonl"
EQUITY_PATH = PROJECT_DIR / "logs" / "equity_history_rules.jsonl"
JST = timezone(timedelta(hours=9))

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "initial_capital_jpy": 3_000_000,
    "max_positions": 5,
    "max_position_pct": 20.0,
    "min_cash_pct": 20.0,
    "max_sector_pct": 35.0,
    "vol_half_size_above_pct": 60.0,
    "vol_third_size_above_pct": 80.0,
    "stop_loss_pct": 5.0,
    "take_profit_pct": 8.0,
    "max_hold_bdays": 21,
    "reentry_cooldown_bdays": 20,
    "entry_gate": {"max_bb_position_pct": 85.0, "max_rsi": 65.0, "min_score": 40.0},
    "exit_rules": [{"metric": "close_vs_sma25_pct", "op": "<", "value": 0}],
    "spread_pct": 0.02,
    "decision_time_jst": "10:30",
}


@dataclass
class Candidate:
    ticker: str
    score: float
    rsi: float | None
    bb_position_pct: float | None
    annualized_vol_pct: float | None
    price: float
    sector: str = "不明"


# ── config / state ───────────────────────────────────────────────


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        user = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(user, dict):
            for k, v in user.items():
                if k == "entry_gate" and isinstance(v, dict):
                    cfg["entry_gate"].update(v)
                else:
                    cfg[k] = v
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def new_state(cfg: dict[str, Any], now: datetime) -> dict[str, Any]:
    return {
        "started": now.astimezone(JST).date().isoformat(),
        "cash": float(cfg["initial_capital_jpy"]),
        "positions": {},  # ticker -> {qty, entry_price, entry_time, peak, sector}
        "closes": {},  # ticker -> 最終売却日 (再エントリー禁止)
        "last_decision_date": None,
        "last_decision_time": None,
    }


def load_state(path: Path = STATE_PATH) -> dict[str, Any] | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and "cash" in d else None
    except (OSError, json.JSONDecodeError):
        return None


def save_state(state: dict[str, Any], path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _append_jsonl(path: Path, rec: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"⚠️  rulebook: {path.name} 追記失敗: {e}", file=sys.stderr)


# ── pure logic ───────────────────────────────────────────────────


def equity_of(state: dict[str, Any], prices: dict[str, float]) -> float:
    eq = float(state["cash"])
    for t, p in state["positions"].items():
        px = prices.get(t) or p.get("last_price") or p["entry_price"]
        eq += float(px) * int(p["qty"])
    return eq


def apply_exits(
    state: dict[str, Any],
    now: datetime,
    prices: dict[str, float],
    metrics: dict[str, dict[str, float | None]],
    cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    """機械出口 + 固定ルールを評価し、成立した建玉を全株クローズ (state を更新、約定を返す)。"""
    from core.churn_guard import business_days_between
    from core.thesis_rules import evaluate_rules

    closed: list[dict[str, Any]] = []
    spread = float(cfg["spread_pct"]) / 100
    for t in list(state["positions"]):
        p = state["positions"][t]
        px = prices.get(t)
        if px is None or px <= 0:
            continue
        p["peak"] = max(float(p.get("peak") or p["entry_price"]), px)
        p["last_price"] = px
        pnl_pct = (px / float(p["entry_price"]) - 1) * 100
        try:
            et = datetime.fromisoformat(str(p["entry_time"]))
            held = business_days_between(et, now)
        except (TypeError, ValueError):
            held = 0
        reason = None
        if pnl_pct <= -float(cfg["stop_loss_pct"]):
            reason = "stop_loss"
        elif pnl_pct >= float(cfg["take_profit_pct"]):
            reason = "take_profit"
        elif held >= int(cfg["max_hold_bdays"]):
            reason = "hold_timeout"
        else:
            fired = evaluate_rules(cfg.get("exit_rules") or [], metrics.get(t) or {})
            if fired:
                reason = f"rule:{fired[0]['metric']}"
        if reason is None:
            continue
        qty = int(p["qty"])
        fill = px * (1 - spread)
        pnl = (fill - float(p["entry_price"])) * qty
        state["cash"] += fill * qty
        state["closes"][t] = now.astimezone(JST).date().isoformat()
        del state["positions"][t]
        closed.append({
            "ts": now.astimezone(UTC).isoformat(), "ticker": t, "side": "SELL", "qty": qty,
            "price": round(fill, 2), "reason": reason, "pnl": round(pnl, 0),
            "pnl_pct": round(pnl_pct, 2), "held_bdays": held,
        })
    return closed


def select_entries(
    state: dict[str, Any],
    now: datetime,
    candidates: list[Candidate],
    cfg: dict[str, Any],
    equity: float,
    unit_fn: Callable[[str], int] = lambda _t: 100,
) -> list[tuple[Candidate, int]]:
    """測定値ゲートを通った候補を score 順に、サイズ・現金・セクター上限の範囲で選ぶ (純関数)。"""
    from core.churn_guard import business_days_between
    from core.sizing import vol_size_factor

    gate = cfg["entry_gate"]
    held = set(state["positions"])
    slots = int(cfg["max_positions"]) - len(held)
    if slots <= 0 or equity <= 0:
        return []
    cash = float(state["cash"])
    floor = equity * float(cfg["min_cash_pct"]) / 100
    sector_value: dict[str, float] = {}
    for p in state["positions"].values():
        px = float(p.get("last_price") or p["entry_price"])
        sector_value[p.get("sector", "不明")] = sector_value.get(p.get("sector", "不明"), 0.0) + px * int(p["qty"])

    def banned(t: str) -> bool:
        d = state["closes"].get(t)
        if not d:
            return False
        try:
            last = datetime.fromisoformat(d).replace(tzinfo=JST)
        except ValueError:
            return False
        return business_days_between(last, now) < int(cfg["reentry_cooldown_bdays"])

    eligible = [
        c for c in candidates
        if c.ticker not in held and not banned(c.ticker) and c.price > 0
        and c.score >= float(gate["min_score"])
        and (c.bb_position_pct is None or c.bb_position_pct <= float(gate["max_bb_position_pct"]))
        and (c.rsi is None or c.rsi <= float(gate["max_rsi"]))
    ]
    eligible.sort(key=lambda c: (-c.score, c.ticker))

    chosen: list[tuple[Candidate, int]] = []
    for c in eligible:
        if slots <= 0:
            break
        factor = vol_size_factor(
            c.annualized_vol_pct, float(cfg["vol_half_size_above_pct"]), float(cfg["vol_third_size_above_pct"])
        )
        target_value = equity * float(cfg["max_position_pct"]) / 100 * factor
        spendable = cash - floor
        value = min(target_value, spendable)
        unit = max(1, int(unit_fn(c.ticker)))
        qty = int(value / c.price) // unit * unit
        if qty <= 0:
            continue
        cost = qty * c.price
        sec = c.sector or "不明"
        if sec not in ("不明", ""):
            after = (sector_value.get(sec, 0.0) + cost) / equity * 100
            if after > float(cfg["max_sector_pct"]):
                continue
            sector_value[sec] = sector_value.get(sec, 0.0) + cost
        chosen.append((c, qty))
        cash -= cost
        slots -= 1
    return chosen


def apply_entries(
    state: dict[str, Any], chosen: list[tuple[Candidate, int]], now: datetime, cfg: dict[str, Any]
) -> list[dict[str, Any]]:
    spread = float(cfg["spread_pct"]) / 100
    trades: list[dict[str, Any]] = []
    for c, qty in chosen:
        fill = c.price * (1 + spread)
        state["cash"] -= fill * qty
        state["positions"][c.ticker] = {
            "qty": qty, "entry_price": round(fill, 4), "entry_time": now.astimezone(UTC).isoformat(),
            "peak": round(fill, 4), "last_price": c.price, "sector": c.sector,
            "score": c.score, "rsi": c.rsi, "bb_position_pct": c.bb_position_pct,
            "annualized_vol_pct": c.annualized_vol_pct,
        }
        trades.append({
            "ts": now.astimezone(UTC).isoformat(), "ticker": c.ticker, "side": "BUY", "qty": qty,
            "price": round(fill, 2), "reason": f"score {c.score:g}", "pnl": None,
        })
    return trades


# ── I/O ──────────────────────────────────────────────────────────


def _score_safe(ticker: str) -> dict[str, Any] | None:
    try:
        from core.scorer import compute_score_cached

        with contextlib.redirect_stdout(io.StringIO()):
            r = compute_score_cached(ticker)
        return r if isinstance(r, dict) and "error" not in r else None
    except (SystemExit, Exception):
        return None


def _sector_safe(ticker: str) -> str:
    try:
        from agents.portfolio_review import _sector

        return _sector(ticker)
    except Exception:
        return "不明"


def candidate_from_score(ticker: str, score: dict[str, Any], sector: str) -> Candidate | None:
    ind = score.get("indicators") or {}
    summary = score.get("analysis_summary") or {}
    try:
        price = float(score.get("current_price") or 0)
        total = float(summary.get("total_score"))
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None

    def _f(v: Any) -> float | None:
        try:
            x = float(v)
            return x if x == x else None
        except (TypeError, ValueError):
            return None

    return Candidate(
        ticker=ticker, score=total, rsi=_f(ind.get("rsi_14")), bb_position_pct=_f(ind.get("bb_position_pct")),
        annualized_vol_pct=_f(ind.get("annualized_vol_pct")), price=price, sector=sector,
    )


def run_cycle(
    now: datetime | None = None,
    *,
    regime: Any = None,
    log: Callable[[str], None] = lambda *_: None,
    universe: list[str] | None = None,
) -> dict[str, Any]:
    """1 サイクル分: 保有の出口判定 → (判断時刻なら) 新規選定 → 資産記録。失敗はログに残して続行。"""
    from core.entry_cadence import should_decide
    from core.thesis_rules import compute_metrics

    now = now or datetime.now(JST)
    cfg = load_config(CONFIG_PATH)  # モジュール変数を呼び出し時に参照 (テストで差し替え可能)
    if not cfg.get("enabled", True):
        return {"enabled": False}
    state = load_state(STATE_PATH) or new_state(cfg, now)

    # 保有の現在値と指標
    prices: dict[str, float] = {}
    metrics: dict[str, dict[str, float | None]] = {}
    for t, p in state["positions"].items():
        sc = _score_safe(t)
        if sc is None:
            continue
        try:
            prices[t] = float(sc.get("current_price") or 0)
        except (TypeError, ValueError):
            continue
        pos = {"entry_price": p["entry_price"], "current_price": prices[t], "peak_price": p.get("peak"),
               "entry_time": p["entry_time"]}
        metrics[t] = compute_metrics(pos, sc, regime, now)
    closed = apply_exits(state, now, prices, metrics, cfg)
    for c in closed:
        _append_jsonl(TRADES_PATH, c)
        log(f"  📘 ルール帳簿 SELL {c['ticker']} {c['qty']}株 @{c['price']:,.0f} ({c['reason']}, {c['pnl_pct']:+.1f}%)")

    # 新規判断 (1 日 1 回、レジーム成立時のみ)
    decided_now = False
    decide, why = should_decide(
        now, {"enabled": True, "time_jst": cfg["decision_time_jst"]}, {"last_date": state.get("last_decision_date"), "last_time": state.get("last_decision_time")}
    )
    regime_ok = regime is None or bool(getattr(regime, "ok", True))
    if decide and regime_ok:
        equity = equity_of(state, prices)
        if universe is None:
            from core.screener import JP_UNIVERSE

            universe = list(JP_UNIVERSE)
        cands: list[Candidate] = []
        for t in universe:
            if t in state["positions"]:
                continue
            sc = _score_safe(t)
            if sc is None:
                continue
            c = candidate_from_score(t, sc, _sector_safe(t))
            if c is not None:
                cands.append(c)
        from core.trading_units import get_trading_unit

        chosen = select_entries(state, now, cands, cfg, equity, unit_fn=get_trading_unit)
        for tr in apply_entries(state, chosen, now, cfg):
            _append_jsonl(TRADES_PATH, tr)
            log(f"  📘 ルール帳簿 BUY {tr['ticker']} {tr['qty']}株 @{tr['price']:,.0f} ({tr['reason']})")
        state["last_decision_date"] = now.astimezone(JST).date().isoformat()
        state["last_decision_time"] = now.astimezone(JST).strftime("%H:%M")
        decided_now = True
        log(f"  📘 ルール帳簿: 候補 {len(cands)} → 新規 {len(chosen)} (枠 {len(state['positions'])}/{cfg['max_positions']})")
    elif decide and not regime_ok:
        log("  📘 ルール帳簿: レジーム不成立のため新規なし (判断は未消化のまま)")

    equity = equity_of(state, prices)
    _append_jsonl(EQUITY_PATH, {
        "ts": now.astimezone(UTC).isoformat(), "equity_jpy": round(equity),
        "cash_jpy": round(state["cash"]), "positions": len(state["positions"]),
    })
    save_state(state, STATE_PATH)
    return {
        "enabled": True, "equity_jpy": round(equity), "cash_jpy": round(state["cash"]),
        "positions": dict(state["positions"]), "closed": closed, "decided": decided_now,
        "cadence": why, "started": state["started"],
    }

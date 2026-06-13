"""ダッシュボード用の状態アセンブラ (読み取り専用)。

CLI と同じく共有ファイルを読むだけ。ブローカー (実価格 fetch / kabu API) は一切触らず、
portfolio.json・diary・daemon ログ・config を読んで表示用 dict を組み立てる。
各セクションは独立して try/except で堅牢化 (1つ壊れても全体は出る)。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

SRC_DIR = Path(__file__).resolve().parent.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from infra.container import DIARY_DIR, get_container

USD_JPY = 150.0  # 概算換算 (表示用)
LOCK_FILE = SRC_DIR.parent / ".auto_trade.lock"
DAEMON_LOG = SRC_DIR.parent / "logs" / "auto_trade_daemon.log"
SIGNALS_DIR = DIARY_DIR / "signals"


def _value_jpy(ticker: str, price: float, qty: int) -> float:
    val = price * qty
    return val if ticker.endswith(".T") else val * USD_JPY


def _tail(path: Path, n: int) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-n:]
    except OSError:
        return []


# ── daemon status ────────────────────────────────────────────────


def daemon_status() -> dict[str, Any]:
    out: dict[str, Any] = {"running": False, "pid": None, "broker": "?",
                           "ai_exit": False, "ai_reflection": False,
                           "jp_market_open": None, "running_since": None, "last_cycle": None}
    try:
        if LOCK_FILE.exists():
            pid = int(LOCK_FILE.read_text().strip() or 0)
            out["pid"] = pid
            try:
                os.kill(pid, 0)
                out["running"] = True
            except (ProcessLookupError, PermissionError):
                out["running"] = pid > 0 and isinstance(pid, int)  # PermissionError = alive
            out["running_since"] = LOCK_FILE.stat().st_mtime
    except (OSError, ValueError):
        pass
    try:
        from core.trade import load_config

        cfg = load_config()
        out["broker"] = cfg.get("broker", "simulator")
        out["ai_exit"] = bool(cfg.get("ai_exit_advisor", False))
        out["ai_reflection"] = bool(cfg.get("ai_reflection", False))
    except Exception:
        pass
    try:
        from agents.auto_trade import _should_skip_cycle

        out["jp_market_open"] = not _should_skip_cycle("jp")
    except Exception:
        pass
    # 最新サイクル行
    for line in reversed(_tail(DAEMON_LOG, 200)):
        if "### サイクル" in line:
            out["last_cycle"] = line.strip().lstrip("# ").rstrip(" #")
            break
    return out


# ── portfolio ────────────────────────────────────────────────────


def portfolio_overview() -> dict[str, Any]:
    pf = get_container().portfolio().load() or {}
    bal = pf.get("balance", {})
    cash_jpy = float(bal.get("cash_jpy", 0) or 0)
    cash_usd = float(bal.get("cash_usd", 0) or 0)
    equity = cash_jpy + cash_usd * USD_JPY

    holdings: list[dict[str, Any]] = []
    for p in pf.get("positions", []):
        ticker = str(p.get("ticker", ""))
        qty = int(p.get("quantity", 0) or 0)
        entry = float(p.get("entry_price", 0) or 0)
        current = float(p.get("current_price") or entry)
        mv_jpy = _value_jpy(ticker, current, qty)
        equity += mv_jpy
        pnl = (current - entry) * qty
        pnl_pct = ((current / entry - 1) * 100) if entry else 0.0
        stop = p.get("stop_loss")
        take = p.get("take_profit")
        holdings.append({
            "ticker": ticker,
            "qty": qty,
            "entry": round(entry, 2),
            "current": round(current, 2),
            "market_value": round(current * qty, 2),
            "pnl": round(pnl, 0),
            "pnl_pct": round(pnl_pct, 2),
            "stop_loss": stop,
            "take_profit": take,
            "dist_to_stop_pct": round((current / stop - 1) * 100, 1) if stop else None,
            "dist_to_take_pct": round((take / current - 1) * 100, 1) if take else None,
            "_mv_jpy": mv_jpy,
        })

    for h in holdings:
        h["concentration_pct"] = round(h.pop("_mv_jpy") / equity * 100, 1) if equity > 0 else 0.0

    try:
        from core.trade import load_risk_limits

        max_pct = float(load_risk_limits().get("max_position_size_pct", 30))
    except Exception:
        max_pct = 30.0

    return {
        "equity_jpy": round(equity, 0),
        "cash_jpy": round(cash_jpy, 0),
        "cash_usd": round(cash_usd, 2),
        "unrealized_pnl": round(sum(h["pnl"] for h in holdings), 0),
        "holdings": sorted(holdings, key=lambda h: h["concentration_pct"], reverse=True),
        "max_position_pct": max_pct,
    }


# ── performance ──────────────────────────────────────────────────


def _close_reason_bucket(reason: str) -> str:
    r = reason.lower()
    if "stop_loss" in r or "損切" in reason:
        return "stop_loss"
    if "take_profit" in r or "利確" in reason:
        return "take_profit"
    if "trailing" in r:
        return "trailing"
    if "max_hold" in r or "hold_timeout" in r:
        return "max_hold"
    if "manual" in r:
        return "manual/ai_exit"
    return "other"


def performance(days: int = 90) -> dict[str, Any]:
    from agents.reflection import _aggregate, _recent_closed_trades

    closed = _recent_closed_trades(days=days)
    stats = _aggregate(closed)

    # エクイティカーブ (実現損益の累積、古い順)
    asc = sorted(closed, key=lambda c: c["date"])
    curve, cum = [], 0.0
    for c in asc:
        cum += c["pnl"]
        curve.append({"date": c["date"], "cum_pnl": round(cum, 0)})

    # クローズ理由別
    by_reason: dict[str, dict[str, float]] = {}
    for c in closed:
        b = _close_reason_bucket(c.get("reason", ""))
        d = by_reason.setdefault(b, {"count": 0, "pnl": 0.0})
        d["count"] += 1
        d["pnl"] += c["pnl"]

    return {
        "stats": stats,
        "equity_curve": curve,
        "by_reason": {k: {"count": int(v["count"]), "pnl": round(v["pnl"], 0)} for k, v in by_reason.items()},
        "recent_closed": closed[:20],
    }


# ── signals / activity / AI insights ─────────────────────────────


def recent_signals(n: int = 8) -> list[dict[str, Any]]:
    try:
        files = sorted(SIGNALS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:n]
    except OSError:
        return []
    out = []
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            out.append({
                "ticker": d.get("ticker"),
                "action": d.get("action"),
                "score": d.get("score"),
                "confidence": d.get("confidence"),
                "reason": (d.get("reason") or "")[:120],
                "file": f.name,
            })
        except (OSError, json.JSONDecodeError):
            continue
    return out


def recent_cycle_log(n_lines: int = 60) -> str:
    return "\n".join(_tail(DAEMON_LOG, n_lines))


def ai_insights() -> dict[str, Any]:
    """daemon ログから直近の振り返り教訓・AI手仕舞い助言を抽出する。"""
    lines = _tail(DAEMON_LOG, 400)
    lessons: list[str] = []
    exits: list[str] = []
    in_lessons = False
    for line in lines:
        s = line.strip()
        if "教訓抽出" in s:
            lessons = []  # 最新ブロックで上書き
            in_lessons = True
            continue
        if in_lessons:
            if s and not s.startswith("Step ") and "###" not in s and "->" not in s:
                lessons.append(s)
            else:
                in_lessons = False
        if "🤖 AI手仕舞い助言" in s or "AIexit" in s or "AItrim" in s:
            exits.append(s)
    return {"lessons": lessons[-12:], "exit_advisories": exits[-8:]}

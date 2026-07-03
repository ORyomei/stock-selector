"""ダッシュボード用の状態アセンブラ (読み取り専用)。

CLI と同じく共有ファイルを読むだけ。ブローカー (実価格 fetch / kabu API) は一切触らず、
portfolio.json・diary・daemon ログ・config を読んで表示用 dict を組み立てる。
各セクションは独立して try/except で堅牢化 (1つ壊れても全体は出る)。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SRC_DIR = Path(__file__).resolve().parent.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from infra.container import DIARY_DIR, PROJECT_DIR, get_container

USD_JPY = 150.0  # 概算換算 (表示用)
LOCK_FILE = SRC_DIR.parent / ".auto_trade.lock"
DAEMON_LOG = SRC_DIR.parent / "logs" / "auto_trade_daemon.log"
SIGNALS_DIR = DIARY_DIR / "signals"
TRACES_DIR = DIARY_DIR / "traces"


# 既知ETFの表示名 (yfinance は運用会社名を返すため正しいファンド名で上書き)
_ETF_NAMES = {
    "2559.T": "MAXIS 全世界株式 (オルカン/ACWI)",
    "1655.T": "iシェアーズ S&P500",
    "1306.T": "NEXT FUNDS TOPIX",
    "1545.T": "NEXT FUNDS NASDAQ100",
    "1489.T": "NEXT FUNDS 日経高配当株50",
}
# 銘柄名は変わらないのでプロセス内でキャッシュ (yfinance 呼び出しを1回だけに)
_NAME_CACHE: dict[str, str] = {}


def _resolve_name(ticker: str) -> str:
    """表示用の銘柄名を返す。既知ETFは固定名、個別株は yfinance (キャッシュ)。"""
    if ticker in _ETF_NAMES:
        return _ETF_NAMES[ticker]
    if ticker in _NAME_CACHE:
        return _NAME_CACHE[ticker]
    try:
        from core.portfolio_ops import get_ticker_name

        name = get_ticker_name(ticker) or ticker
    except Exception:
        name = ticker
    _NAME_CACHE[ticker] = name
    return name


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
            "name": _resolve_name(ticker),
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

_JST = timezone(timedelta(hours=9))


def _to_jst_x(ts: str, date: str) -> str:
    """UTC ISO タイムスタンプを JST の壁時計文字列に変換する (ECharts time 軸用)。

    時刻情報が無い/パース不能なら日付の 00:00 にフォールバック。ECharts の
    ``type:"time"`` は "YYYY-MM-DD HH:MM:SS" をローカル時刻として解釈する。
    """
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(_JST).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return f"{date} 00:00:00"


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

    # エクイティカーブ (実現損益の累積、古い順)。x はフルタイムスタンプ(JST)を使い、
    # 同じ日の複数約定が 0時に重ならないようにする。
    asc = sorted(closed, key=lambda c: c.get("ts") or c["date"])
    curve, cum = [], 0.0
    for c in asc:
        cum += c["pnl"]
        curve.append({"date": c["date"], "x": _to_jst_x(c.get("ts", ""), c["date"]), "cum_pnl": round(cum, 0)})

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


# ── benchmark & expectancy (P2: 計測基盤) ────────────────────────

_bench_cache: dict[str, tuple[float, Any]] = {}


def _price_change_pct(ticker: str, days: int, ttl_sec: int = 900) -> float | None:
    """ticker の直近 days 日の騰落率% (yfinance)。TTL キャッシュ付き。"""
    import time

    key = f"{ticker}:{days}"
    hit = _bench_cache.get(key)
    if hit and time.time() - hit[0] < ttl_sec:
        return hit[1]
    import yfinance as yf

    closes = yf.Ticker(ticker).history(period=f"{days}d")["Close"].dropna()
    val = float((closes.iloc[-1] / closes.iloc[0] - 1) * 100) if len(closes) >= 2 else None
    _bench_cache[key] = (time.time(), val)
    return val


def _initial_capital_jpy() -> float:
    try:
        cfg = get_container().config_repo().load_trading_config()
        return float(cfg.get("simulator", {}).get("initial_capital_jpy", 0)) or 3_000_000
    except Exception:
        return 3_000_000


def benchmark(days: int = 90) -> dict[str, Any]:
    """TOPIX (1306.T) と実現損益ベースリターンの比較 (アルファ計測)。

    system_pct は「期間内実現損益 ÷ 初期資金」— 含み損益は含まない点に注意。
    """
    out: dict[str, Any] = {
        "window_days": days,
        "topix_pct": None,
        "system_pct": None,
        "alpha_pct": None,
        "realized_jpy": None,
    }
    try:
        from agents.reflection import _recent_closed_trades

        realized = sum(c["pnl"] for c in _recent_closed_trades(days=days))
        out["realized_jpy"] = round(realized)
        out["system_pct"] = round(realized / _initial_capital_jpy() * 100, 2)
    except Exception:
        pass
    try:
        topix = _price_change_pct("1306.T", days)
        out["topix_pct"] = round(topix, 2) if topix is not None else None
    except Exception:
        pass
    if out["topix_pct"] is not None and out["system_pct"] is not None:
        out["alpha_pct"] = round(out["system_pct"] - out["topix_pct"], 2)
    return out


def equity_history(limit: int = 2000) -> list[dict[str, Any]]:
    """デーモンが蓄積する総資産スナップショット (logs/equity_history.jsonl)。"""
    path = PROJECT_DIR / "logs" / "equity_history.jsonl"
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec.get("equity_jpy"), int | float) and rec.get("ts"):
                    records.append({
                        "x": _to_jst_x(str(rec["ts"]), str(rec["ts"])[:10]),
                        "equity_jpy": rec["equity_jpy"],
                    })
    except OSError:
        return []
    return records[-limit:]


def _hold_bucket(t: dict[str, Any]) -> str:
    hd = t.get("hold_days")
    if not isinstance(hd, int | float):
        return "不明"
    if hd <= 0:
        return "当日"
    if hd <= 3:
        return "1-3日"
    if hd <= 10:
        return "4-10日"
    return "11日+"


def _score_bucket(t: dict[str, Any]) -> str:
    s = t.get("entry_score")
    if not isinstance(s, int | float):
        return "不明"
    if s < 25:
        return "score<25"
    if s <= 40:
        return "score25-40"
    return "score>40"


def _join_entry_meta(
    closes: list[dict[str, Any]], entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """各クローズに、同一ティッカーの直近先行エントリーの score/confidence を付与 (純関数)。"""
    ents = sorted(entries, key=lambda e: str(e.get("timestamp", "")))
    joined: list[dict[str, Any]] = []
    for c in closes:
        cts = str(c.get("timestamp", ""))
        best = None
        for e in ents:
            if e.get("ticker") == c.get("ticker") and str(e.get("timestamp", "")) <= cts:
                best = e  # ents は昇順なので最後にマッチしたものが直近
        d = dict(c)
        if best is not None:
            d["entry_score"] = best.get("score")
            d["entry_confidence"] = best.get("confidence")
        joined.append(d)
    return joined


def _bucket_stats(
    items: list[dict[str, Any]], key_fn: Any
) -> dict[str, dict[str, Any]]:
    """バケット別の件数・勝率・合計/平均損益 (純関数)。"""
    buckets: dict[str, dict[str, float]] = {}
    for t in items:
        pnl = t.get("pnl")
        if not isinstance(pnl, int | float):
            continue
        b = key_fn(t)
        d = buckets.setdefault(b, {"count": 0, "wins": 0, "total": 0.0})
        d["count"] += 1
        d["wins"] += 1 if pnl > 0 else 0
        d["total"] += float(pnl)
    return {
        b: {
            "count": int(d["count"]),
            "win_rate": round(d["wins"] / d["count"] * 100, 1),
            "total_pnl": round(d["total"]),
            "avg_pnl": round(d["total"] / d["count"]),
        }
        for b, d in buckets.items()
        if d["count"]
    }


def expectancy(days: int = 120) -> dict[str, Any]:
    """保有期間別・エントリースコア別の実測期待値 (diary/trades から集計)。"""
    trades = get_container().diary().load_recent_trades(days=days)
    closes = [
        t
        for t in trades
        if str(t.get("action", "")).upper() in ("CLOSE", "SELL")
        and isinstance(t.get("pnl"), int | float)
    ]
    entries = [t for t in trades if str(t.get("action", "")).upper() == "BUY"]
    return {
        "by_hold": _bucket_stats(closes, _hold_bucket),
        "by_score": _bucket_stats(_join_entry_meta(closes, entries), _score_bucket),
        "n_closes": len(closes),
    }


def _source_category(src: str) -> str:
    """決定ソースを比較カテゴリにまとめる。"""
    if src.startswith("ai_"):
        return "AI手仕舞い"
    if src.startswith("mech:"):
        return "機械ストップ"
    if src == "swap":
        return "スワップ"
    if src == "manual":
        return "手動"
    return "legacy(タグ付け前)"


def _agg_pnl(items: list[float]) -> dict[str, Any]:
    if not items:
        return {"count": 0}
    wins = [x for x in items if x > 0]
    losses = [x for x in items if x < 0]
    total = sum(items)
    return {
        "count": len(items),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(items) * 100, 1),
        "total_pnl": round(total, 0),
        "avg_pnl": round(total / len(items), 0),
    }


def performance_by_source(days: int = 120) -> dict[str, Any]:
    """決定ソース別の実現損益を集計 (AI手仕舞い vs 機械ストップ 等の比較)。

    ※ 観測ベースの帰属であり無作為化A/Bではない (AIは弱含み銘柄を早期手仕舞い
    する傾向があるため選択バイアスがある)。因果ではなく実績の内訳として読む。
    """
    trades = get_container().diary().load_recent_trades(days=days)
    closed = [
        t for t in trades
        if str(t.get("action", "")) in {"CLOSE", "SELL", "close", "sell"}
        and isinstance(t.get("pnl"), int | float)
    ]
    by_source: dict[str, list[float]] = {}
    by_cat: dict[str, list[float]] = {}
    for t in closed:
        src = t.get("source") or "legacy"
        pnl = float(t["pnl"])
        by_source.setdefault(src, []).append(pnl)
        by_cat.setdefault(_source_category(src), []).append(pnl)
    return {
        "total_closed": len(closed),
        "by_category": {k: _agg_pnl(v) for k, v in by_cat.items()},
        "by_source": {k: _agg_pnl(v) for k, v in by_source.items()},
    }


_AI_FLAG_KEYS = ("ai_exit_advisor", "ai_reflection", "ai_portfolio_review")


def _load_flag_log() -> list[dict[str, Any]]:
    """diary/ai_flags.jsonl を時系列順 (追記順) で返す。"""
    path = DIARY_DIR / "ai_flags.jsonl"
    out: list[dict[str, Any]] = []
    try:
        for ln in path.read_text(encoding="utf-8").strip().splitlines():
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return out


def _flag_on_at(flag_log: list[dict[str, Any]], flag: str, ts: str) -> bool | None:
    """時刻 ts 時点でのフラグ状態 (記録前なら None)。ISO文字列の辞書順比較。"""
    applicable = [e for e in flag_log if str(e.get("ts", "")) <= ts]
    if not applicable:
        return None
    return bool(applicable[-1].get("flags", {}).get(flag, False))


def performance_by_period(days: int = 180) -> dict[str, Any]:
    """AIフラグ ON/OFF 期間別に実現損益を比較する (期間A/B)。

    各クローズをそのクローズ時点のフラグ状態に帰属させ、フラグごとに ON/OFF の
    実績を集計する。フラグ記録前のクローズは unknown。
    """
    flag_log = _load_flag_log()
    trades = get_container().diary().load_recent_trades(days=days)
    closed = [
        t for t in trades
        if str(t.get("action", "")) in {"CLOSE", "SELL", "close", "sell"}
        and isinstance(t.get("pnl"), int | float)
    ]
    by_flag: dict[str, Any] = {}
    for flag in _AI_FLAG_KEYS:
        on_p: list[float] = []
        off_p: list[float] = []
        unknown = 0
        for t in closed:
            state = _flag_on_at(flag_log, flag, str(t.get("timestamp", "")))
            if state is None:
                unknown += 1
            elif state:
                on_p.append(float(t["pnl"]))
            else:
                off_p.append(float(t["pnl"]))
        by_flag[flag] = {"ON": _agg_pnl(on_p), "OFF": _agg_pnl(off_p), "unknown": unknown}
    return {"flag_log": flag_log, "by_flag": by_flag, "total_closed": len(closed)}


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


# ── AI 思考トレース ──────────────────────────────────────────────


def recent_traces(n: int = 20) -> list[str]:
    """新しい順の思考トレースファイル名 (拡張子なし) 一覧。"""
    try:
        files = sorted(TRACES_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:n]
        return [f.stem for f in files]
    except OSError:
        return []


def load_trace(name: str) -> list[dict[str, Any]]:
    """指定トレースのステップ列を返す (見つからなければ空)。"""
    try:
        path = TRACES_DIR / f"{name}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        steps = data.get("steps", [])
        return steps if isinstance(steps, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def trace_to_dot(steps: list[dict[str, Any]]) -> str:
    """ツール呼び出し列を Graphviz DOT (有向フロー) に変換する。"""
    nodes: list[str] = ["開始"]
    for s in steps:
        if s.get("type") == "tool_call":
            tool = s.get("tool", "?")
            args = s.get("args", {}) or {}
            key = args.get("ticker") or args.get("query") or args.get("market") or ""
            label = f"{tool}\\n{key}" if key else tool
            nodes.append(label)
    nodes.append("最終判断")

    lines = ["digraph trace {", "  rankdir=LR;", '  node [shape=box, style=rounded, fontsize=10];']
    for i, label in enumerate(nodes):
        color = "#cce5ff" if label in ("開始", "最終判断") else (
            "#d4edda" if label.startswith("submit_signals") else "#ffffff")
        lines.append(f'  n{i} [label="{label}", style="rounded,filled", fillcolor="{color}"];')
    for i in range(len(nodes) - 1):
        lines.append(f"  n{i} -> n{i+1};")
    lines.append("}")
    return "\n".join(lines)


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

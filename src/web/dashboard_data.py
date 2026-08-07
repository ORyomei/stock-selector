"""ダッシュボード用の状態アセンブラ (読み取り専用)。

CLI と同じく共有ファイルを読むだけ。ブローカー (実価格 fetch / kabu API) は一切触らず、
portfolio.json・diary・daemon ログ・config を読んで表示用 dict を組み立てる。
各セクションは独立して try/except で堅牢化 (1つ壊れても全体は出る)。
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta, timezone
from html import escape
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


def _hold_days_jst(entry_iso: str | None) -> int | None:
    """エントリー時刻 (ISO) から JST 暦日ベースの保有日数を返す (当日=0)。"""
    if not entry_iso:
        return None
    try:
        dt = datetime.fromisoformat(str(entry_iso))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return (datetime.now(UTC).astimezone(_JST).date() - dt.astimezone(_JST).date()).days
    except (ValueError, TypeError):
        return None


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
        hold_days = _hold_days_jst(p.get("entry_time"))
        # 保有期間あたりの効率 (日次換算)。当日建ては1日とみなす
        pnl_pct_per_day = (
            round(pnl_pct / max(1, hold_days), 2) if hold_days is not None else None
        )
        holdings.append({
            "ticker": ticker,
            "name": _resolve_name(ticker),
            "qty": qty,
            "entry": round(entry, 2),
            "current": round(current, 2),
            "market_value": round(current * qty, 2),
            "pnl": round(pnl, 0),
            "pnl_pct": round(pnl_pct, 2),
            "hold_days": hold_days,
            "pnl_pct_per_day": pnl_pct_per_day,
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


from core.trade_stats import close_reason_bucket as _close_reason_bucket  # noqa: E402


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
    """ticker の直近 days 日の騰落率% (yfinance)。TTL キャッシュ付き。

    yfinance は取得窓の端で分割調整に失敗した異常値を返すことがある
    (実例: 1306.T の period='90d' 先頭2行だけ約1/10 の価格 → +1051% と算出)。
    中央値から3倍以上乖離した行はデータ不良とみなして除外する。90日で
    株価が正しく3倍/3分の1になるケースは指数ETFのベンチマーク用途では無い。
    """
    import time

    key = f"{ticker}:{days}"
    hit = _bench_cache.get(key)
    if hit and time.time() - hit[0] < ttl_sec:
        return hit[1]
    import yfinance as yf

    closes = yf.Ticker(ticker).history(period=f"{days}d")["Close"].dropna()
    if len(closes) >= 2:
        med = float(closes.median())
        closes = closes[(closes > med / 3) & (closes < med * 3)]
    val = float((closes.iloc[-1] / closes.iloc[0] - 1) * 100) if len(closes) >= 2 else None
    _bench_cache[key] = (time.time(), val)
    return val


def _initial_capital_jpy() -> float:
    try:
        cfg = get_container().config_repo().load_trading_config()
        return float(cfg.get("simulator", {}).get("initial_capital_jpy", 0)) or 3_000_000
    except Exception:
        return 3_000_000


def initial_capital_jpy() -> float:
    """元本 (config の simulator.initial_capital_jpy)。損益の基準線に使う。"""
    return _initial_capital_jpy()


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


# ── signal follow-through (P5: シグナル仮想追跡) ─────────────────


def _forward_returns(
    closes: list[tuple[str, float]], signal_date: str, horizons: tuple[int, ...]
) -> dict[int, float]:
    """signal_date 以降の最初の終値を基準に +N営業日リターン% を返す (純関数)。

    closes は (YYYY-MM-DD, close) の昇順リスト。基準日やホライズン先の
    データが無い場合はそのキーを省く。
    """
    idx = next((i for i, (d, _) in enumerate(closes) if d >= signal_date), None)
    if idx is None:
        return {}
    base = closes[idx][1]
    out: dict[int, float] = {}
    for h in horizons:
        j = idx + h
        if j < len(closes) and base:
            out[h] = round((closes[j][1] / base - 1) * 100, 2)
    return out


def _ticker_closes(ticker: str, days: int, ttl_sec: int = 900) -> list[tuple[str, float]]:
    """ticker の日次終値列 (TTL キャッシュ付き)。"""
    import time

    key = f"closes:{ticker}:{days}"
    hit = _bench_cache.get(key)
    if hit and time.time() - hit[0] < ttl_sec:
        return hit[1]
    import yfinance as yf

    hist = yf.Ticker(ticker).history(period=f"{days + 30}d")["Close"].dropna()
    closes = [(idx.strftime("%Y-%m-%d"), float(v)) for idx, v in hist.items()]
    _bench_cache[key] = (time.time(), closes)
    return closes


def _signal_group(status: str) -> str:
    if status == "FILLED":
        return "採用(約定)"
    if status.startswith("REJECTED"):
        return "却下(ゲート/上限)"
    return "スキップ/失敗"


def signal_followthrough(
    days: int = 30, horizons: tuple[int, ...] = (1, 5, 10)
) -> dict[str, Any]:
    """シグナル結果ログの仮想追跡: 採用/却下それぞれの N日後リターンを集計。

    「反証ゲートや上限で却下したシグナルはその後上がったのか」を検証し、
    ゲートが良いシグナルを殺していないかを測る。
    """
    recs = get_container().diary().load_signal_log(days=days)
    rows: list[dict[str, Any]] = []
    for r in recs:
        ticker = r.get("ticker")
        ts = str(r.get("ts", ""))
        if not ticker or len(ts) < 10:
            continue
        sig_date = _to_jst_x(ts, ts[:10])[:10]
        try:
            closes = _ticker_closes(str(ticker), days)
        except Exception:
            continue
        fwd = _forward_returns(closes, sig_date, horizons)
        rows.append({
            "group": _signal_group(str(r.get("status", ""))),
            "ticker": ticker,
            "date": sig_date,
            "status": r.get("status"),
            "score": r.get("score"),
            **{f"r{h}": fwd.get(h) for h in horizons},
        })

    # グループ×ホライズン別に平均リターン・勝率を集計
    agg: dict[str, dict[str, list[float]]] = {}
    for row in rows:
        cols = agg.setdefault(row["group"], {f"r{h}": [] for h in horizons})
        for h in horizons:
            v = row.get(f"r{h}")
            if isinstance(v, int | float):
                cols[f"r{h}"].append(float(v))
    summary: dict[str, dict[str, Any]] = {}
    for g, cols in agg.items():
        summary[g] = {}
        for h in horizons:
            vals = cols[f"r{h}"]
            if vals:
                summary[g][f"+{h}d"] = {
                    "count": len(vals),
                    "avg_pct": round(sum(vals) / len(vals), 2),
                    "hit_rate": round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1),
                }
    return {"summary": summary, "rows": rows[-50:], "n_records": len(recs)}


# バケット集計は core/trade_stats に集約 (プロンプト注入と共有)。
# 既存テスト・呼び出し互換のため旧名で re-export する (import-as だと
# ruff の未使用 import 除去に消されるため、明示的な代入にしている)。
from core import trade_stats as _trade_stats  # noqa: E402

expectancy = _trade_stats.expectancy
_bucket_stats = _trade_stats.bucket_stats
_hold_bucket = _trade_stats.hold_bucket
_join_entry_meta = _trade_stats.join_entry_meta
_score_bucket = _trade_stats.score_bucket


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


TRADES_DIR = DIARY_DIR / "trades"


def _filled_trades() -> list[dict[str, Any]]:
    """約定記録 (FILLED) を JST の時刻付きで新しい順に返す。"""
    out: list[dict[str, Any]] = []
    try:
        paths = sorted(TRADES_DIR.glob("*_trade.json"))
    except OSError:
        return out
    for p in paths:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(d.get("status", "")).upper() != "FILLED":
            continue
        try:  # timestamp は UTC の ISO。JST に直してサイクルと突き合わせる
            dt = datetime.fromisoformat(str(d["timestamp"]))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
        except (KeyError, ValueError, TypeError):
            continue
        out.append({
            "dt": dt.astimezone(_JST),
            "action": str(d.get("action", "")).upper(),
            "ticker": str(d.get("ticker", "")),
            "quantity": d.get("quantity"),
            "pnl": d.get("pnl"),
            "fill_price": d.get("fill_price"),
            "source": str(d.get("source", "")),
            "reason": str(d.get("reason", "")),
        })
    return sorted(out, key=lambda t: t["dt"], reverse=True)


def trade_markers() -> list[dict[str, Any]]:
    """約定をグラフ上に打つためのマーカー (JST の x 文字列付き、古い順)。"""
    return [
        {
            "x": t["dt"].strftime("%Y-%m-%d %H:%M:%S"),
            "action": t["action"],
            "ticker": t["ticker"],
            "quantity": t["quantity"],
            "pnl": t["pnl"],
            "source": t["source"],
        }
        for t in reversed(_filled_trades())
    ]


def _trace_signals(name: str) -> list[dict[str, Any]]:
    """そのサイクルで submit_signals に渡されたシグナル (要点のみ)。"""
    for s in load_trace(name):
        if s.get("type") != "tool_call" or s.get("tool") != "submit_signals":
            continue
        sigs = (s.get("args") or {}).get("signals") or []
        if not isinstance(sigs, list):
            return []
        return [
            {
                "ticker": str(g.get("ticker", "")),
                "action": str(g.get("action", "")).lower(),
                "score": g.get("score"),
                "confidence": g.get("confidence"),
                "sell_ticker": g.get("sell_ticker"),
                "reason": str(g.get("reason", "")),
            }
            for g in sigs
            if isinstance(g, dict)
        ]
    return []


def cycle_index(n: int = 60) -> list[dict[str, Any]]:
    """サイクル一覧 (新しい順)。時刻・シグナル件数・約定を突き合わせて返す。

    トレース名は JST ローカル時刻 (例 2026-08-05_152953_jp)、約定記録の
    timestamp は UTC なので、JST に揃えてから「そのサイクル開始〜次サイクル開始」
    の窓に入る約定を割り当てる。
    """
    names = recent_traces(n)
    entries: list[dict[str, Any]] = []
    for name in names:
        stamp, _, market = name.rpartition("_")
        try:
            dt = datetime.strptime(stamp, "%Y-%m-%d_%H%M%S").replace(tzinfo=_JST)
        except ValueError:
            continue
        entries.append({"name": name, "dt": dt, "market": market})

    entries.sort(key=lambda e: e["dt"])  # 古い順にして窓を作る
    trades = _filled_trades()
    for i, e in enumerate(entries):
        nxt = entries[i + 1]["dt"] if i + 1 < len(entries) else None
        e["trades"] = [
            t for t in trades if t["dt"] >= e["dt"] and (nxt is None or t["dt"] < nxt)
        ]
        e["signals"] = _trace_signals(e["name"])
        # 同じ窓に同じ銘柄の約定があれば「通った」シグナルとみなす
        filled_tickers = {t["ticker"] for t in e["trades"]}
        for g in e["signals"]:
            g["filled"] = g["ticker"] in filled_tickers
        unfilled = [g for g in e["signals"] if not g["filled"]]
        # 約定と未約定が同居するサイクルは mixed (両方のアイコンを出す)
        if e["trades"] and unfilled:
            e["status"] = "mixed"
        elif e["trades"]:
            e["status"] = "filled"
        elif e["signals"]:
            e["status"] = "rejected"
        else:
            e["status"] = "none"
    entries.reverse()  # 新しい順に戻す
    return entries


def load_trace(name: str) -> list[dict[str, Any]]:
    """指定トレースのステップ列を返す (見つからなければ空)。"""
    try:
        path = TRACES_DIR / f"{name}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        steps = data.get("steps", [])
        return steps if isinstance(steps, list) else []
    except (OSError, json.JSONDecodeError):
        return []


FLOW_COLS = 5  # フロー図の1行あたりノード数（超えたら折り返す）
_FLOW_WRAP = 22  # ノードラベル1行あたりの目安文字数


def _wrap_label(text: str, width: int = _FLOW_WRAP) -> str:
    """ラベルを width 文字目安で折り返す。内容は省略しない。

    長いキー (ToolSearch の select リスト等) を1行に出すとノードが極端に
    横長になるため、区切り文字の直後で改行する。区切りのない長い語は強制分割。
    """
    chunks: list[str] = []
    line = ""
    for token in re.split(r"(?<=[,、\s])", text):
        if line and len(line) + len(token) > width:
            chunks.append(line)
            line = token
        else:
            line += token
    if line:
        chunks.append(line)

    out: list[str] = []
    for chunk in chunks:
        while len(chunk) > width:
            out.append(chunk[:width])
            chunk = chunk[width:]
        if chunk:
            out.append(chunk.rstrip())
    return "\\n".join(out)


def _flow_key(args: dict[str, Any]) -> str:
    """ノードラベルに添える識別子 (ticker / query / market)。"""
    key = str(args.get("ticker") or args.get("query") or args.get("market") or "")
    return key.replace("\\", "/").replace('"', "'")


def _flow_nodes(steps: list[dict[str, Any]]) -> list[str]:
    """ツール呼び出し列をノードラベル列に変換する。

    連続する同一ツールの呼び出し (score_stock を4銘柄分など) は1ノードに畳む。
    畳んだ場合も対象は全件ラベルに載せる。
    """
    groups: list[tuple[str, list[str]]] = []
    for s in steps:
        if s.get("type") != "tool_call":
            continue
        tool = str(s.get("tool", "?"))
        key = _flow_key(s.get("args", {}) or {})
        if groups and groups[-1][0] == tool:
            groups[-1][1].append(key)
        else:
            groups.append((tool, [key]))

    nodes = ["開始"]
    for tool, keys in groups:
        shown = [k for k in keys if k]
        head = f"{tool} ×{len(keys)}" if len(keys) > 1 else tool
        nodes.append(f"{head}\\n{_wrap_label(', '.join(shown))}" if shown else head)
    nodes.append("最終判断")
    return nodes


def trace_to_dot(steps: list[dict[str, Any]], cols: int = FLOW_COLS) -> str:
    """ツール呼び出し列を Graphviz DOT (有向フロー) に変換する。

    1行 cols ノードで折り返した格子状に並べる。一直線 (rankdir=LR) だと
    ツール呼び出しが数十件になったとき横スクロールなしでは読めないため。
    行ごとに向きを反転する蛇行配置にして、行が右へ階段状にずれるのを防ぐ。
    """
    nodes = _flow_nodes(steps)
    rows = [list(range(s, min(s + cols, len(nodes)))) for s in range(0, len(nodes), cols)]

    lines = [
        "digraph trace {",
        "  rankdir=TB;",
        "  nodesep=0.25;",
        "  ranksep=0.35;",
        '  node [shape=box, style="rounded,filled", fontsize=10, margin="0.12,0.06"];',
    ]
    ai_tools = _ai_tool_names()
    for i, label in enumerate(nodes):
        tool = label.split("\\n")[0].split(" ×")[0]
        if label in ("開始", "最終判断"):
            color = "#cce5ff"
        elif tool in ai_tools:  # 内部で入れ子 LLM を呼ぶツール
            color = "#ede0f7"
        elif label.startswith("submit_signals"):
            color = "#d4edda"
        else:
            color = "#ffffff"
        lines.append(f'  n{i} [label="{label}", fillcolor="{color}"];')

    for r, row in enumerate(rows):
        visual = row if r % 2 == 0 else row[::-1]  # 奇数行は右→左
        lines.append("  {rank=same; " + " ".join(f"n{i}" for i in visual) + ";}")
        # 同一ランク内の辺は tail が左に来る。奇数行は辺を反転して dir=back で
        # 矢印の向きだけ論理どおりに戻す
        for a, b in zip(row, row[1:], strict=False):
            lines.append(f"  n{a} -> n{b};" if r % 2 == 0 else f"  n{b} -> n{a} [dir=back];")
        if r + 1 < len(rows):  # 行末から次の行頭へ真下に降ろす
            lines.append(f"  n{row[-1]} -> n{rows[r + 1][0]};")
    lines.append("}")
    return "\n".join(lines)


# ── シーケンス図 ──────────────────────────────────────────────────
# ツール定義を読み込むだけのハーネス呼び出し。分析の流れではないので図から外す
_SEQ_SKIP_TOOLS = {"ToolSearch"}
_SEQ_LANE_W = 94  # レーン間隔(px)
_SEQ_ROW_H = 26  # 1ステップの行高(px)
_SEQ_TOP = 62  # レーン見出しの高さ(px)
_SEQ_LEFT = 60  # 左端の余白(px)


def _all_tool_names() -> list[str]:
    """登録されている全ツール名 (定義順)。取得できなければ空。"""
    try:
        from agents.tools import ALL_TOOLS

        return [t.name for t in ALL_TOOLS]
    except Exception:
        return []


def _ai_tool_names() -> set[str]:
    """内部で入れ子 LLM を呼ぶツール名。定義は agents/tools.py が持つ。"""
    try:
        from agents.tools import AI_TOOLS

        return set(AI_TOOLS)
    except Exception:
        return set()


def _lane_header_lines(name: str) -> list[str]:
    """レーン見出しを2行までに折る (analyze_fundamentals 等が枠に収まらないため)。"""
    if len(name) <= 13 or "_" not in name:
        return [name]
    head, _, tail = name.partition("_")
    return [head + "_", tail]


def trace_to_sequence_svg(steps: list[dict[str, Any]]) -> tuple[str, int]:
    """ツール呼び出し列を UML シーケンス図風の SVG に変換し (HTML, 高さpx) を返す。

    レーン (Agent + 使用ツール) を横に並べ、時間は下へ流す。横幅はレーン数
    だけで決まりステップ数に依存しないため、呼び出しが数十件でも横に伸びない。
    実線 = 呼び出し、破線 = 結果の返却。引数の全文はタイムライン側で見られる。

    st.html は SVG をサニタイズで落とすため、呼び出し側は components.html
    (iframe) に渡すこと。高さを返すのは iframe が明示指定を要するため。
    """
    events: list[tuple[str, str, str]] = []  # (kind, tool, label)
    for s in steps:
        kind = s.get("type")
        tool = str(s.get("tool", ""))
        if kind not in ("tool_call", "tool_result") or tool in _SEQ_SKIP_TOOLS:
            continue
        label = _flow_key(s.get("args", {}) or {}) if kind == "tool_call" else ""
        events.append((kind, tool, label))

    if not events:
        return '<p style="color:#888">ツール呼び出しなし</p>', 60

    # 登録済みツールを常に全部レーンとして立てる (サイクル間で列位置が動かず、
    # 「何を使わなかったか」も読み取れる)。未登録のツールが出てきたら後ろに足す
    used = {tool for _, tool, _label in events}
    lanes = ["Agent"] + _all_tool_names()
    for _, tool, _label in events:
        if tool not in lanes:
            lanes.append(tool)
    x = {name: _SEQ_LEFT + i * _SEQ_LANE_W for i, name in enumerate(lanes)}
    width = _SEQ_LEFT + len(lanes) * _SEQ_LANE_W
    height = _SEQ_TOP + len(events) * _SEQ_ROW_H + 24

    out = [
        f'<div style="overflow-x:auto"><svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width}" height="{height}" font-family="sans-serif">',
        '<defs><marker id="ah" markerWidth="8" markerHeight="8" refX="7" refY="3" '
        'orient="auto"><path d="M0,0 L7,3 L0,6 z" fill="#555"/></marker></defs>',
    ]
    # レーン見出しとライフライン。未使用ツールは淡く、入れ子 AI を持つツールは
    # 紫で描いて「決定的ではない = 遅い・レート枠を食う」ことが一目で分かるようにする
    ai_tools = _ai_tool_names()
    for name in lanes:
        cx = x[name]
        on = name == "Agent" or name in used
        is_ai = name in ai_tools
        if name == "Agent":
            fill, stroke, color = "#cce5ff", "#999", "#222"
        elif is_ai:
            fill, stroke, color = (
                ("#ede0f7", "#9575cd", "#4a2c6d") if on else ("#faf6fd", "#e0d4ec", "#b9a8ca")
            )
        elif name == "submit_signals" and on:
            fill, stroke, color = "#d4edda", "#999", "#222"
        else:
            fill, stroke, color = ("#f4f4f4", "#999", "#222") if on else ("#fbfbfb", "#ddd", "#aaa")
        out.append(
            f'<rect x="{cx - 43}" y="8" width="86" height="34" rx="5" fill="{fill}" '
            f'stroke="{stroke}"{" stroke-width=\"1.6\"" if is_ai else ""}/>'
        )
        head = _lane_header_lines(name)
        if is_ai:
            head = ["🤖 " + head[0]] + head[1:]
        y0 = 24 if len(head) == 1 else 19
        for j, line in enumerate(head):
            out.append(
                f'<text x="{cx}" y="{y0 + j * 11}" font-size="9" fill="{color}" '
                f'text-anchor="middle">{escape(line)}</text>'
            )
        out.append(
            f'<line x1="{cx}" y1="42" x2="{cx}" y2="{height - 12}" '
            f'stroke="{"#bbb" if on else "#eee"}" stroke-dasharray="3,3"/>'
        )
    # ステップごとの矢印 (実線=呼び出し / 破線=結果)
    for i, (kind, tool, label) in enumerate(events):
        y = _SEQ_TOP + i * _SEQ_ROW_H + 10
        agent_x, tool_x = x["Agent"], x[tool]
        call = kind == "tool_call"
        x1, x2 = (agent_x, tool_x) if call else (tool_x, agent_x)
        dash = "" if call else ' stroke-dasharray="4,3"'
        out.append(
            f'<text x="12" y="{y + 4}" font-size="9" fill="#999">{i + 1}</text>'
            f'<line x1="{x1}" y1="{y}" x2="{x2}" y2="{y}" stroke="#555"{dash} '
            f'marker-end="url(#ah)"/>'
        )
        text = escape(label) if call else "結果"
        if text:
            color = "#333" if call else "#999"
            out.append(
                f'<text x="{(x1 + x2) / 2}" y="{y - 4}" font-size="9" fill="{color}" '
                f'text-anchor="middle">{text}</text>'
            )
    out.append("</svg></div>")
    return "".join(out), height


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

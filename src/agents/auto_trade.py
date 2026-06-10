#!/usr/bin/env python3
"""Automated trading daemon — LangGraph Agent ベースの自動売買ループ。

サイクル本体は ``agents.graph_trade.run_trade_graph`` (ReAct Agent +
反証ゲート)。このモジュールはデーモン化・排他ロック・取引時間判定・
ブローカー同期などの周辺機構を提供する。

旧 legacy 固定パイプライン (screen → score → judge → execute) は
2026-06-11 に削除済み。必要なら git 履歴 (run_cycle) を参照。
"""

from __future__ import annotations

import atexit
import contextlib
import fcntl
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# --- paths ----------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent
PROJECT_DIR = SRC_DIR.parent
DIARY_DIR = PROJECT_DIR / "diary"
JST = timezone(timedelta(hours=9))

sys.path.insert(0, str(SRC_DIR))

# ── lock file (排他制御) ──────────────────────────────────────────────────────

LOCK_FILE = PROJECT_DIR / ".auto_trade.lock"
DAEMON_LOG = PROJECT_DIR / "logs" / "auto_trade_daemon.log"
_lock_fd: int | None = None


def _daemonize() -> None:
    """Double-fork でプロセスをバックグラウンドに切り離す。"""
    # Pipe to communicate grandchild PID back to parent
    r_fd, w_fd = os.pipe()

    # First fork
    pid = os.fork()
    if pid > 0:
        # Parent: wait for grandchild PID from pipe
        os.close(w_fd)
        data = os.read(r_fd, 32)
        os.close(r_fd)
        grandchild_pid = int(data.strip())
        print(f"✅ デーモン起動 (PID={grandchild_pid})")
        print(f"   ログ: {DAEMON_LOG}")
        print(f"   停止: kill {grandchild_pid}")
        sys.stdout.flush()  # os._exit はバッファを flush しないため明示的に
        os._exit(0)

    # Child: new session
    os.close(r_fd)
    os.setsid()

    # Second fork (prevent re-acquiring terminal)
    pid2 = os.fork()
    if pid2 > 0:
        # Send grandchild PID (pid2) to original parent
        os.write(w_fd, f"{pid2}\n".encode())
        os.close(w_fd)
        os._exit(0)

    os.close(w_fd)

    # Grandchild: redirect stdio to log file
    DAEMON_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_fd = os.open(str(DAEMON_LOG), os.O_CREAT | os.O_WRONLY | os.O_APPEND)
    os.dup2(log_fd, sys.stdout.fileno())
    os.dup2(log_fd, sys.stderr.fileno())
    os.close(log_fd)
    # 非TTYだと stdout がブロックバッファになり、ログが数時間滞留するため行バッファに切替
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    sys.stderr.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    # Close stdin
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, sys.stdin.fileno())
    os.close(devnull)


def _acquire_lock() -> None:
    """ロックファイルを取得。既にデーモンが動いていたら即終了。"""
    global _lock_fd
    _lock_fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_WRONLY)
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(_lock_fd)
        _lock_fd = None
        print("❌ 別のauto-tradeプロセスが実行中です。先に停止してください。", file=sys.stderr)
        sys.exit(1)
    # PIDを書き込む
    os.ftruncate(_lock_fd, 0)
    os.write(_lock_fd, f"{os.getpid()}\n".encode())
    os.fsync(_lock_fd)


def _release_lock() -> None:
    """ロックファイルを解放。"""
    global _lock_fd
    if _lock_fd is not None:
        fcntl.flock(_lock_fd, fcntl.LOCK_UN)
        os.close(_lock_fd)
        _lock_fd = None
    with contextlib.suppress(OSError):
        LOCK_FILE.unlink(missing_ok=True)


# ── order cost helpers ────────────────────────────────────────────────────────


def _order_cost(ticker: str, price: float) -> tuple[str, int, float]:
    """ティッカーと価格から (通貨, 売買単位, 1ロットあたりコスト) を返す。

    売買単位はブローカーから取得して config に保持した値を参照する
    (ETF は 1 口単位等)。資金チェックを通貨混在せず通貨ごとに行うために使う。
    """
    from core.trading_units import get_trading_unit

    ccy = "JPY" if ticker.endswith(".T") else "USD"
    unit = get_trading_unit(ticker)
    return ccy, unit, price * unit


def _cash_by_currency(pf_balance: dict[str, Any]) -> dict[str, float]:
    return {
        "JPY": float(pf_balance.get("cash_jpy", 0) or 0),
        "USD": float(pf_balance.get("cash_usd", 0) or 0),
    }


# ── reconcile helper ──────────────────────────────────────────────────────────


def _reconcile_if_needed(log) -> None:
    """kabuブローカー使用時にサイクル先頭でポートフォリオを同期する。"""
    try:
        config_path = PROJECT_DIR / "config" / "trading_config.json"
        if not config_path.exists():
            return
        import json as _json
        with open(config_path) as f:
            config = _json.load(f)
        if config.get("broker", "simulator") == "simulator":
            return
        from core.reconcile import reconcile
        from core.trade import load_or_create_broker
        broker = load_or_create_broker(config)
        result = reconcile(broker, apply=True, verbose=False)
        if result.synced:
            log("  🔄 ブローカーとローカルの同期を実行しました")
            for d in result.diffs:
                if d.action != "MATCH":
                    log(f"     {d.action}: {d.ticker} (local={d.local_qty} -> broker={d.broker_qty})")
        else:
            log("  ✅ ブローカーと同期済み")
    except Exception as e:
        log(f"  ⚠️ ブローカー同期エラー (続行): {e}")


# ── daemon ────────────────────────────────────────────────────────────────────


def _should_skip_cycle(market: str) -> bool:
    """取引時間外ならTrue。東証 8:30-16:00 / 米国 22:00-06:00 JST (余裕込み)."""
    JST = timezone(timedelta(hours=9))
    now = datetime.now(JST)
    weekday = now.weekday()  # 0=Mon ... 6=Sun
    hour = now.hour
    minute = now.minute
    t = hour * 60 + minute

    jp_open = 8 * 60 + 30   # 08:30
    jp_close = 16 * 60       # 16:00
    us_open = 22 * 60        # 22:00 (JST)
    us_close = 6 * 60        # 06:00 (JST, 翌朝)

    # 東証: 月〜金 08:30-16:00 JST
    jp_active = weekday <= 4 and jp_open <= t <= jp_close
    # 米国: 現地月〜金 = JST 月〜金 22:00〜 / 火〜土 〜06:00 (翌朝に跨ぐ)
    # ※月曜 00:00-06:00 JST は米国日曜のため休場、土曜早朝は金曜セッション継続中
    us_active = (weekday <= 4 and t >= us_open) or (1 <= weekday <= 5 and t <= us_close)

    if market == "jp":
        return not jp_active
    elif market == "us":
        return not us_active
    else:  # "all"
        # 日本か米国どちらかが開いていればOK
        return not (jp_active or us_active)


def daemon_loop(
    market: str,
    min_score: int,
    max_signals: int,
    interval: int,
    dry_run: bool,
    ai_provider: str,
    ai_model: str | None,
    foreground: bool = False,
) -> None:
    """LangGraph Agent のサイクルを定期実行するデーモンループ。"""
    # ロック取得を先に行う（fork前）→ 2重起動を即座にブロック
    _acquire_lock()

    if not foreground:
        _daemonize()
        # fork後のgrandchildでPIDを更新
        os.lseek(_lock_fd, 0, os.SEEK_SET)  # type: ignore[arg-type]
        os.ftruncate(_lock_fd, 0)  # type: ignore[arg-type]
        os.write(_lock_fd, f"{os.getpid()}\n".encode())  # type: ignore[arg-type]
        os.fsync(_lock_fd)  # type: ignore[arg-type]

    atexit.register(_release_lock)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    print(f"デーモンモード (LangGraph Agent): {interval}s ({interval // 60}min) ごとに自動実行")
    print(f"  market={market}  min_score={min_score}  max_signals={max_signals}  dry_run={dry_run}")
    print(f"  LLM: {ai_provider} (model: {ai_model or 'default'})")
    print(f"  PID={os.getpid()}")
    if foreground:
        print("  Ctrl+C で停止\n")
    else:
        print(f"  ログ: {DAEMON_LOG}\n")

    cycle = 0
    while True:
        cycle += 1

        # 取引時間外スキップ
        if _should_skip_cycle(market):
            now_jst = datetime.now(JST)
            print(f"\n### サイクル #{cycle} [SKIP] {now_jst.strftime('%H:%M')} JST — 取引時間外 ###")
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                print("\nデーモン停止")
                break
            continue

        print(f"\n### サイクル #{cycle} ###")
        try:
            from agents.graph_trade import run_trade_graph

            run_trade_graph(
                market=market,
                min_score=min_score,
                max_signals=max_signals,
                dry_run=dry_run,
                provider=ai_provider,
                model=ai_model,
            )
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
        print(f"\n次回: {interval}s後 ({interval // 60}min後)")
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\nデーモン停止")
            break

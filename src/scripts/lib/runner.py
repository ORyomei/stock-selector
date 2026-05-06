"""Direct function calls to sibling scripts (no subprocess)."""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from typing import Any


def run_script(
    script_name: str,
    args: list[str] | None = None,
    *,
    timeout: int = 120,  # kept for API compat, not used
) -> dict[str, Any] | None:
    """Call a sibling script's main function directly and return its result dict.

    Returns ``None`` on error.
    """
    try:
        if script_name == "scorer.py":
            from scripts.scorer import compute_score
            return compute_score(args[0]) if args else None

        if script_name == "macro.py":
            from scripts.macro import fetch_macro
            return fetch_macro()

        if script_name == "screener.py":
            from scripts.screener import run_screen
            kw: dict[str, Any] = {}
            if args:
                it = iter(args)
                for a in it:
                    if a == "--market":
                        kw["market"] = next(it)
                    elif a == "--strategy":
                        kw["strategy"] = next(it)
                    elif a == "--top":
                        kw["top"] = int(next(it))
                    elif a == "--universe":
                        kw["universe_size"] = next(it)
            return run_screen(**kw)

        if script_name == "fundamentals.py":
            from scripts.fundamentals import analyze_fundamentals
            return analyze_fundamentals(args[0]) if args else None

        if script_name == "fetch_sentiment.py":
            from scripts.fetch_sentiment import run_sentiment
            return run_sentiment(args[0]) if args else None

        if script_name == "event_impact_analyzer.py":
            from scripts.event_impact_analyzer import run as run_event
            return run_event()

        if script_name == "technical.py":
            from scripts.technical import analyze as run_technical
            return run_technical(args[0]) if args else None

        if script_name == "fetch_news.py":
            from scripts.fetch_news import fetch_news
            return fetch_news(args[0]) if args else None

        if script_name == "fetch_prices.py":
            from scripts.fetch_prices import fetch as fetch_price
            return fetch_price(args[0]) if args else None

        print(f"  [warn] runner: unknown script {script_name}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  [warn] {script_name}: {e}", file=sys.stderr)
        return None


def run_trade_cmd(
    args_list: list[str],
    *,
    timeout: int = 60,  # kept for API compat, not used
) -> tuple[str, int]:
    """Call trade.py commands directly.

    Returns ``(stdout_text, return_code)``.
    """
    try:
        from scripts.trade import (
            cmd_check_and_close_positions,
            cmd_close_position,
            cmd_execute_signal,
            load_config,
            load_risk_limits,
            load_signal_from_file,
        )

        config = load_config()
        risk_limits = load_risk_limits()

        buf = io.StringIO()
        with redirect_stdout(buf):
            if "--from-signal" in args_list:
                idx = args_list.index("--from-signal")
                sig_path = args_list[idx + 1]
                signal = load_signal_from_file(sig_path)
                if signal is None:
                    return "ERROR: signal load failed", 1
                rc = cmd_execute_signal(config, risk_limits, signal)

            elif "--check-and-close" in args_list:
                rc = cmd_check_and_close_positions(config, risk_limits)

            elif "--close" in args_list:
                idx = args_list.index("--close")
                ticker = args_list[idx + 1]
                qty = int(args_list[idx + 2])
                rc = cmd_close_position(config, ticker, qty)

            else:
                return f"ERROR: unknown args {args_list}", 1

        return buf.getvalue(), rc
    except Exception as e:
        return f"ERROR: {e}", 1

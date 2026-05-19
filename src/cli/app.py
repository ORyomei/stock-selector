"""stock-selector CLI — typer で全コマンドを集約."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer

# Ensure src/ is on sys.path for editable installs / direct execution
_SRC = str(Path(__file__).resolve().parent.parent)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

app = typer.Typer(
    name="stock-selector",
    help="AI-driven stock screening, technical analysis, and simulated trading",
    no_args_is_help=True,
)


def _json_out(data: dict | list | None) -> None:
    """Pretty-print JSON to stdout."""
    if data is None:
        typer.echo("No data", err=True)
        raise typer.Exit(1)
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2))


# ── Screen ───────────────────────────────────────────────
@app.command()
def screen(
    market: Annotated[str, typer.Option(help="対象市場")] = "all",
    strategy: Annotated[str, typer.Option(help="戦略")] = "all",
    top: Annotated[int, typer.Option(help="各戦略の上位N件")] = 5,
    universe: Annotated[str, typer.Option(help="ユニバースサイズ")] = "default",
) -> None:
    """市場全体から有望銘柄をスキャン."""
    from core.screener import run_screen

    _json_out(run_screen(market=market, strategy=strategy, top=top, universe_size=universe))


# ── Score ────────────────────────────────────────────────
@app.command()
def score(
    ticker: Annotated[str, typer.Argument(help="ティッカーシンボル")],
    period: Annotated[str, typer.Option(help="分析期間")] = "6mo",
) -> None:
    """総合スコアリング＆売買判断."""
    from core.scorer import compute_score

    _json_out(compute_score(ticker, period))


# ── Technical ────────────────────────────────────────────
@app.command()
def technical(
    ticker: Annotated[str, typer.Argument(help="ティッカーシンボル")],
    period: Annotated[str, typer.Option(help="分析期間")] = "6mo",
) -> None:
    """テクニカル指標算出."""
    from core.technical import analyze

    _json_out(analyze(ticker, period))


# ── Macro ────────────────────────────────────────────────
@app.command()
def macro(
    period: Annotated[str, typer.Option(help="分析期間")] = "3mo",
) -> None:
    """マクロ経済指標（VIX・金利・為替等）."""
    from core.macro import fetch_macro

    _json_out(fetch_macro(period))


# ── Fundamentals ─────────────────────────────────────────
@app.command()
def fundamentals(
    ticker: Annotated[str, typer.Argument(help="ティッカーシンボル")],
) -> None:
    """ファンダメンタル分析."""
    from core.fundamentals import analyze_fundamentals

    _json_out(analyze_fundamentals(ticker))


# ── Prices ───────────────────────────────────────────────
@app.command()
def prices(
    ticker: Annotated[str, typer.Argument(help="ティッカーシンボル")],
    period: Annotated[str, typer.Option(help="取得期間")] = "3mo",
    interval: Annotated[str, typer.Option(help="足種")] = "1d",
) -> None:
    """株価データ取得."""
    from core.prices import fetch

    _json_out(fetch(ticker, period, interval))


# ── News ─────────────────────────────────────────────────
@app.command()
def news(
    query: Annotated[str, typer.Argument(help="検索クエリ")],
    lang: Annotated[str, typer.Option(help="言語")] = "ja",
    limit: Annotated[int, typer.Option(help="取得件数")] = 10,
) -> None:
    """ニュース取得."""
    from core.news import fetch_news

    _json_out(fetch_news(query, lang, limit))


# ── Sentiment ────────────────────────────────────────────
@app.command()
def sentiment(
    query: Annotated[str, typer.Argument(help="検索クエリ")],
    limit: Annotated[int, typer.Option(help="取得件数")] = 20,
) -> None:
    """センチメント分析."""
    from core.sentiment import run_sentiment

    _json_out(run_sentiment(query, limit))


# ── Event Impact ─────────────────────────────────────────
@app.command(name="event-impact")
def event_impact(
    query: Annotated[Optional[str], typer.Option(help="検索クエリ")] = None,
    lang: Annotated[Optional[str], typer.Option(help="言語")] = None,
    limit: Annotated[int, typer.Option(help="取得件数")] = 8,
    fmt: Annotated[str, typer.Option("--format", help="出力形式")] = "json",
) -> None:
    """イベント因果分析."""
    from core.event_impact import format_causal_summary, run

    result = run(query=query, lang=lang, limit=limit)
    if fmt == "text":
        typer.echo(format_causal_summary(result))
    else:
        _json_out(result)


# ── Backtest ─────────────────────────────────────────────
@app.command()
def backtest(
    days: Annotated[int, typer.Option(help="検証日数")] = 5,
    min_score: Annotated[int, typer.Option(help="最低スコア")] = 0,
    ticker: Annotated[Optional[str], typer.Option(help="銘柄絞り込み")] = None,
) -> None:
    """過去推奨の的中率バックテスト."""
    from core.backtest import run_backtest

    _json_out(run_backtest(days=days, min_score=min_score, ticker=ticker))


# ── Alert ────────────────────────────────────────────────
@app.command()
def alert(
    ticker: Annotated[Optional[str], typer.Option(help="個別銘柄指定")] = None,
    check_portfolio: Annotated[bool, typer.Option("--check-portfolio", help="保有銘柄チェック")] = False,
) -> None:
    """ウォッチリスト・ポートフォリオのアラート検知."""
    from core.alert import run_alert

    _json_out(run_alert(ticker=ticker, check_portfolio=check_portfolio))


# ── Portfolio ────────────────────────────────────────────
@app.command()
def portfolio(
    command: Annotated[str, typer.Argument(help="status|buy|sell|performance|reconcile")],
    ticker: Annotated[Optional[str], typer.Argument(help="銘柄")] = None,
    shares: Annotated[Optional[int], typer.Argument(help="株数")] = None,
    price: Annotated[Optional[float], typer.Argument(help="価格")] = None,
    stop_loss: Annotated[Optional[float], typer.Option(help="損切りライン")] = None,
    take_profit: Annotated[Optional[float], typer.Option(help="利確ライン")] = None,
    apply: Annotated[bool, typer.Option("--apply", help="照合結果をローカルに反映")] = False,
) -> None:
    """ポートフォリオ管理."""
    from core.portfolio_ops import (
        cmd_buy,
        cmd_performance,
        cmd_sell,
        cmd_status,
        load_portfolio,
        save_portfolio,
    )

    if command == "reconcile":
        from core.reconcile import reconcile
        from core.trade import load_or_create_broker

        import json
        config_path = Path(__file__).resolve().parent.parent.parent / "config" / "trading_config.json"
        with open(config_path) as f:
            config = json.load(f)
        broker = load_or_create_broker(config)
        result = reconcile(broker, apply=apply)
        if not apply and any(d.action != "MATCH" for d in result.diffs) or abs(result.cash_diff) > 1:
            typer.echo("\n💡 同期するには: uv run stock-selector portfolio reconcile --apply")
        return

    pf = load_portfolio()
    if command == "status":
        _json_out(cmd_status(pf))
    elif command == "performance":
        _json_out(cmd_performance(pf))
    elif command == "buy":
        if not all([ticker, shares, price]):
            typer.echo("buy には ticker, shares, price が必要です", err=True)
            raise typer.Exit(1)
        result = cmd_buy(pf, ticker, shares, price, stop_loss=stop_loss, take_profit=take_profit)
        save_portfolio(pf)
        _json_out(result)
    elif command == "sell":
        if not all([ticker, shares, price]):
            typer.echo("sell には ticker, shares, price が必要です", err=True)
            raise typer.Exit(1)
        result = cmd_sell(pf, ticker, shares, price)
        save_portfolio(pf)
        _json_out(result)
    else:
        typer.echo(f"不明なコマンド: {command}", err=True)
        raise typer.Exit(1)


# ── Trade ────────────────────────────────────────────────
@app.command()
def trade(
    from_signal: Annotated[Optional[str], typer.Option("--from-signal", help="シグナルファイル")] = None,
    check_positions: Annotated[bool, typer.Option("--check-positions", help="ポジション確認")] = False,
    check_and_close: Annotated[bool, typer.Option("--check-and-close", help="ポジション確認+クローズ")] = False,
    close_ticker: Annotated[Optional[str], typer.Option("--close", help="クローズ銘柄")] = None,
    close_qty: Annotated[Optional[int], typer.Option("--close-qty", help="クローズ数量")] = None,
) -> None:
    """トレード実行."""
    from core.trade import (
        cmd_check_and_close_positions,
        cmd_close_position,
        cmd_execute_signal,
        load_config,
        load_risk_limits,
        load_signal_from_file,
    )

    config = load_config()
    risk_limits = load_risk_limits()

    if from_signal:
        signal = load_signal_from_file(from_signal)
        if signal is None:
            typer.echo("シグナルの読み込みに失敗", err=True)
            raise typer.Exit(1)
        rc = cmd_execute_signal(config, risk_limits, signal)
    elif check_and_close:
        rc = cmd_check_and_close_positions(config, risk_limits)
    elif close_ticker and close_qty:
        rc = cmd_close_position(config, close_ticker, close_qty)
    elif check_positions:
        from core.trade import cmd_check_positions
        rc = cmd_check_positions(config, risk_limits)
    else:
        typer.echo("オプションを指定してください (--from-signal, --check-and-close, etc.)", err=True)
        raise typer.Exit(1)
    raise typer.Exit(rc)


# ── Auto Trade ───────────────────────────────────────────
@app.command(name="auto-trade")
def auto_trade(
    market: Annotated[str, typer.Option(help="対象市場")] = "all",
    min_score: Annotated[int, typer.Option(help="最低スコア")] = 10,
    max_signals: Annotated[int, typer.Option(help="最大シグナル数")] = 2,
    dry_run: Annotated[bool, typer.Option(help="ドライラン")] = False,
    ai: Annotated[bool, typer.Option("--ai", help="AI判断有効")] = False,
    ai_provider: Annotated[str, typer.Option(help="AIプロバイダー")] = "copilot",
    ai_model: Annotated[Optional[str], typer.Option(help="AIモデル")] = None,
    daemon: Annotated[bool, typer.Option(help="デーモンモード（自動バックグラウンド化）")] = False,
    foreground: Annotated[bool, typer.Option("--fg", help="デーモンをフォアグラウンドで実行")] = False,
    interval: Annotated[int, typer.Option(help="実行間隔(秒)")] = 1800,
    legacy: Annotated[bool, typer.Option(help="レガシーモード")] = False,
) -> None:
    """自動売買ループ."""
    from agents.auto_trade import daemon_loop, run_cycle, _acquire_lock, _release_lock

    if daemon:
        daemon_loop(
            market=market,
            min_score=min_score,
            max_signals=max_signals,
            dry_run=dry_run,
            interval=interval,
            use_ai=ai,
            ai_provider=ai_provider,
            ai_model=ai_model,
            foreground=foreground,
        )
    else:
        _acquire_lock()
        try:
            run_cycle(
                market=market,
                min_score=min_score,
                max_signals=max_signals,
                dry_run=dry_run,
                use_ai=ai,
                ai_provider=ai_provider,
                ai_model=ai_model,
            )
        finally:
            _release_lock()


# ── Auto Analyze ─────────────────────────────────────────
@app.command(name="auto-analyze")
def auto_analyze(
    market: Annotated[str, typer.Option(help="対象市場")] = "jp",
    span: Annotated[str, typer.Option(help="タイムスパン")] = "medium",
    depth: Annotated[str, typer.Option(help="分析深度")] = "standard",
    ai: Annotated[bool, typer.Option("--ai", help="AI分析有効")] = False,
    ai_provider: Annotated[str, typer.Option(help="AIプロバイダー")] = "copilot",
    ai_model: Annotated[Optional[str], typer.Option(help="AIモデル")] = None,
    daemon: Annotated[bool, typer.Option(help="デーモンモード")] = False,
    interval: Annotated[int, typer.Option(help="実行間隔(秒)")] = 1800,
    legacy: Annotated[bool, typer.Option(help="レガシーモード")] = False,
) -> None:
    """自動分析スクリプト."""
    from agents.auto_analyze import daemon_loop, run_analysis

    if daemon:
        daemon_loop(
            market=market,
            span=span,
            depth=depth,
            interval=interval,
            use_ai=ai,
            ai_provider=ai_provider,
            ai_model=ai_model,
        )
    else:
        run_analysis(
            market=market,
            span=span,
            depth=depth,
            use_ai=ai,
            ai_provider=ai_provider,
            ai_model=ai_model,
        )


# ── Kabu Check ───────────────────────────────────────────
@app.command(name="kabu-check")
def kabu_check(
    positions: Annotated[bool, typer.Option(help="ポジション表示")] = False,
    orders: Annotated[bool, typer.Option(help="注文表示")] = False,
) -> None:
    """kabuステーション API 接続テスト."""
    from core.kabu_check import check_connection

    result = check_connection(show_positions=positions, show_orders=orders)
    if not result.get("ok"):
        typer.echo(f"❌ {result.get('error')}", err=True)
        raise typer.Exit(1)
    _json_out(result)


def main() -> None:
    """Entry point for [project.scripts]."""
    app()


if __name__ == "__main__":
    main()

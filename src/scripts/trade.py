#!/usr/bin/env python3
"""自動取引メインスクリプト

Usage:
  python scripts/trade.py --check-positions              # ポジション確認
  python scripts/trade.py --from-signal <signal_file>   # シグナル実行
  python scripts/trade.py --close <ticker> <qty>        # ポジションクローズ

主な機能:
  - 売買判断シグナル（/analyze の結果）を読み込み
  - TradeExecutor で実行（シミュレータ or 本取引）
  - portfolio.json でポジション状態を永続化
  - 結果を diary/trades に記録
  - JSON 出力（Copilot Chat 向け）
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# 親ディレクトリから import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trading import (
    BrokerSimulator,
    OrderManager,
    RiskManager,
    TradeAction,
    TradeExecutor,
    TradingSignal,
)
from trading.broker_interface import OrderSide, OrderType

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent

from infra.container import get_container


def load_config() -> dict[str, Any]:
    """設定ファイルを読み込む"""
    return get_container().config_repo().load_trading_config()


def load_risk_limits() -> dict[str, Any]:
    """リスク管理設定を読み込む"""
    return get_container().config_repo().load_risk_limits()


def load_or_create_broker(config: dict) -> BrokerSimulator:
    """BrokerSimulator を生成・復元"""
    broker = BrokerSimulator(config["simulator"])
    portfolio_repo = get_container().portfolio()
    portfolio_data = portfolio_repo.load()
    if portfolio_data.get("holdings"):
        broker.from_dict(portfolio_data)
        print("✅ ポートフォリオ復元")
    else:
        print("⚠️  ポートフォリオファイルなし - 初期状態で開始")
    return broker


def save_broker_state(broker: BrokerSimulator) -> None:
    """BrokerSimulator の状態を portfolio.json に保存"""
    portfolio_data = broker.to_dict()
    get_container().portfolio().save(portfolio_data)
    print("💾 ポートフォリオ保存完了")


def _normalize_action(raw: str) -> TradeAction:
    """大文字小文字を問わず TradeAction に変換する。"""
    mapping = {
        "buy": TradeAction.BUY,
        "sell": TradeAction.SELL,
        "close": TradeAction.CLOSE,
    }
    key = raw.strip().lower()
    if key in mapping:
        return mapping[key]
    return TradeAction(raw)  # そのまま渡して ValueError で落とす


def load_signal_from_file(file_path: str) -> TradingSignal | None:
    """ファイルから TradingSignal を読み込む"""
    try:
        data = get_container().diary().load_signal(file_path)

        return TradingSignal(
            ticker=data["ticker"],
            action=_normalize_action(data["action"]),
            confidence=data["confidence"],
            target_price=data["target_price"],
            stop_loss_price=data["stop_loss_price"],
            take_profit_price=data["take_profit_price"],
            entry_price=data.get("entry_price", 0.0),
            timespan=data.get("timespan", "swing"),
            reason=data.get("reason", ""),
            score=data.get("score", 0),
        )
    except Exception as e:
        print(f"ERROR: Failed to load signal: {e}", file=sys.stderr)
        return None


def save_trade_result(result: dict) -> str:
    """取引結果を diary/trades に記録

    Returns:
        保存したファイルパス
    """
    return get_container().diary().save_trade(result)


def format_result_for_chat(result: dict) -> str:
    """Chat 出力用にフォーマット"""
    status_emoji = "✅" if result["success"] else "❌"

    lines = [
        f"{status_emoji} **取引実行結果**",
        f"- **銘柄**: {result['ticker']}",
        f"- **アクション**: {result['action']}",
        f"- **数量**: {result['quantity']}",
        f"- **エントリー価格**: ${result['entry_price']:,.2f}"
        if result["entry_price"] > 0
        else "- **エントリー価格**: (成行)",
        f"- **約定価格**: ${result['fill_price']:,.2f}"
        if result["fill_price"]
        else "- **約定価格**: (未約定)",
        f"- **ステータス**: {result['status']}",
        f"- **損益**: {result['pnl']}" if result["pnl"] is not None else "- **損益**: N/A",
        f"- **理由**: {result['reason']}",
    ]

    return "\n".join(lines)


def _format_num(value: float, digits: int = 2) -> str:
    """NaN/inf を安全に表示する。"""
    try:
        v = float(value)
        if math.isfinite(v):
            return f"{v:,.{digits}f}"
    except (TypeError, ValueError):
        pass
    return "N/A"


def cmd_execute_signal(config: dict, risk_limits: dict, signal: TradingSignal) -> int:
    """売買シグナルを実行"""
    # ブローカー初期化・復元
    broker = load_or_create_broker(config)

    # マネージャー初期化
    risk_manager = RiskManager(risk_limits)
    order_manager = OrderManager(risk_manager)
    executor = TradeExecutor(broker, order_manager, risk_manager)

    # 実行
    result = executor.execute_signal(signal)

    # ポートフォリオ保存
    save_broker_state(broker)

    # 結果保存
    saved_path = save_trade_result(result)

    # Chat 用出力
    print(format_result_for_chat(result))
    print(f"\n💾 **ログ保存**: {saved_path}")

    # JSON 出力（Copilot 連携用）
    print(f"\n```json\n{json.dumps(result, ensure_ascii=False, indent=2)}\n```")

    return 0


def cmd_check_positions(config: dict, risk_limits: dict) -> int:
    """ポジション確認"""
    broker = load_or_create_broker(config)
    risk_manager = RiskManager(risk_limits)
    order_manager = OrderManager(risk_manager)
    executor = TradeExecutor(broker, order_manager, risk_manager)

    summary = executor.get_portfolio_summary()

    print("## 📊 **ポートフォリオ概要**\n")
    print("**残高:**")
    print(f"- JPY: ¥{_format_num(summary['balance']['cash_jpy'], 0)}")
    print(f"- USD: ${_format_num(summary['balance']['cash_usd'])}\n")

    total_pnl = _format_num(summary["total_pnl"])
    total_pnl_pct = _format_num(summary["total_pnl_pct"])
    print(f"**総損益:** {total_pnl} ({total_pnl_pct}%)\n")

    if summary["positions"]:
        print("**保有ポジション:**")
        for pos in summary["positions"]:
            entry = _format_num(pos["entry_price"])
            pnl = _format_num(pos["pnl"])
            pnl_pct = _format_num(pos["pnl_pct"])
            print(f"- {pos['ticker']}: {pos['quantity']}株 @ ¥{entry} (損益: {pnl} / {pnl_pct}%)")
    else:
        print("**保有ポジション:** なし")

    # JSON 出力
    print(f"\n```json\n{json.dumps(summary, ensure_ascii=False, indent=2)}\n```")

    return 0


def cmd_check_and_close_positions(config: dict, risk_limits: dict) -> int:
    """損切り/利確チェックと自動クローズ"""
    broker = load_or_create_broker(config)
    risk_manager = RiskManager(risk_limits)
    order_manager = OrderManager(risk_manager)
    executor = TradeExecutor(broker, order_manager, risk_manager)

    results = executor.check_and_close_positions()

    # ポートフォリオ保存
    save_broker_state(broker)

    if results:
        print("## 🔄 **自動クローズ実行結果**\n")
        for result in results:
            status_emoji = "✅" if result["success"] else "⚠️"
            print(
                f"{status_emoji} {result['ticker']}: {result['reason']} "
                f"(数量: {result['quantity']}, PnL: {result['pnl']})"
            )

        print(f"\n```json\n{json.dumps(results, ensure_ascii=False, indent=2)}\n```")
    else:
        print("## 🔄 **自動クローズ実行結果**\n")
        print("クローズ対象なし")

    return 0


def cmd_close_position(config: dict, ticker: str, quantity: int) -> int:
    """手動でポジションをクローズする。"""
    broker = load_or_create_broker(config)
    positions = broker.get_positions()

    target = None
    for pos in positions:
        if pos.ticker == ticker:
            target = pos
            break

    if target is None:
        print(f"ERROR: Position not found for ticker: {ticker}", file=sys.stderr)
        return 1

    if quantity <= 0:
        print("ERROR: quantity must be > 0", file=sys.stderr)
        return 1

    if quantity > target.quantity:
        print(
            f"ERROR: quantity exceeds position size. requested={quantity}, held={target.quantity}",
            file=sys.stderr,
        )
        return 1

    estimated_pnl = None
    if target.entry_price is not None and target.current_price is not None:
        estimated_pnl = (target.current_price - target.entry_price) * quantity

    try:
        order = broker.place_order(
            ticker=ticker,
            side=OrderSide.SELL,
            quantity=quantity,
            order_type=OrderType.MARKET,
            entry_price=0.0,
            stop_loss=None,
            take_profit=None,
        )
    except Exception as e:
        print(f"ERROR: Failed to close position: {e}", file=sys.stderr)
        return 1

    result = {
        "success": order.status.value == "FILLED",
        "order_id": order.id,
        "ticker": ticker,
        "action": "CLOSE",
        "quantity": quantity,
        "entry_price": target.entry_price,
        "fill_price": order.fill_price,
        "status": order.status.value,
        "pnl": round(estimated_pnl, 2) if estimated_pnl is not None else None,
        "reason": f"Manual close: {quantity} shares",
        "timestamp": datetime.now(UTC).isoformat(),
    }

    save_broker_state(broker)
    saved_path = save_trade_result(result)

    print(format_result_for_chat(result))
    print(f"\n💾 **ログ保存**: {saved_path}")
    print(f"\n```json\n{json.dumps(result, ensure_ascii=False, indent=2)}\n```")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="自動取引スクリプト",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/trade.py --from-signal /path/to/signal.json
  python scripts/trade.py --check-positions
  python scripts/trade.py --check-and-close
    python scripts/trade.py --close 7203.T 100
        """,
    )

    parser.add_argument(
        "--from-signal",
        type=str,
        help="シグナルファイルパス（JSON）",
    )

    parser.add_argument(
        "--check-positions",
        action="store_true",
        help="ポジション確認のみ",
    )

    parser.add_argument(
        "--check-and-close",
        action="store_true",
        help="損切り/利確チェックと自動クローズ",
    )

    parser.add_argument(
        "--close",
        nargs=2,
        metavar=("TICKER", "QTY"),
        help="手動クローズ（例: --close 7203.T 100）",
    )

    parser.add_argument(
        "--ticker",
        type=str,
        help="テスト用：手動でティッカーを指定",
    )

    parser.add_argument(
        "--action",
        type=str,
        choices=["buy", "sell"],
        help="テスト用：アクション",
    )

    args = parser.parse_args()

    # 設定読み込み
    config = load_config()
    risk_limits = load_risk_limits()

    # コマンド分岐
    if args.from_signal:
        signal = load_signal_from_file(args.from_signal)
        if signal is None:
            return 1
        return cmd_execute_signal(config, risk_limits, signal)

    elif args.check_positions:
        return cmd_check_positions(config, risk_limits)

    elif args.check_and_close:
        return cmd_check_and_close_positions(config, risk_limits)

    elif args.close:
        ticker = args.close[0]
        try:
            quantity = int(args.close[1])
        except ValueError:
            print("ERROR: QTY must be integer", file=sys.stderr)
            return 1
        return cmd_close_position(config, ticker, quantity)

    elif args.ticker and args.action:
        # テスト用：手動シグナル生成
        signal = TradingSignal(
            ticker=args.ticker,
            action=TradeAction.BUY if args.action == "buy" else TradeAction.SELL,
            confidence=0.7,
            target_price=100.0,
            stop_loss_price=95.0,
            take_profit_price=110.0,
        )
        return cmd_execute_signal(config, risk_limits, signal)

    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())

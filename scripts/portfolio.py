#!/usr/bin/env python3
"""ポートフォリオ管理スクリプト

Usage:
  python scripts/portfolio.py status             # 現在のポートフォリオ状況
  python scripts/portfolio.py buy SLB 100 53.50   # SLB を100株 $53.50で購入
  python scripts/portfolio.py sell SLB 50 57.00   # SLB を50株 $57.00で売却
  python scripts/portfolio.py performance         # パフォーマンス統計

仮想ポートフォリオで売買をシミュレーションし、損益を追跡する。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yfinance as yf

PORTFOLIO_PATH = Path(__file__).parent.parent / "portfolio.json"


def load_portfolio() -> dict[str, Any]:
    if PORTFOLIO_PATH.exists():
        with open(PORTFOLIO_PATH) as f:
            return json.load(f)
    return {"cash_jpy": 10_000_000, "cash_usd": 50_000, "holdings": [], "history": []}


def save_portfolio(data: dict):
    with open(PORTFOLIO_PATH, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_current_price(ticker: str) -> float | None:
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="5d", interval="1d")
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception:
        return None


def get_currency(ticker: str) -> str:
    """ティッカーから通貨を推定"""
    if ticker.endswith(".T"):
        return "JPY"
    return "USD"


def cmd_buy(
    portfolio: dict,
    ticker: str,
    shares: int,
    price: float,
    stop_loss: float | None = None,
    take_profit: float | None = None,
):
    """買い注文"""
    currency = get_currency(ticker)
    total_cost = price * shares

    cash_key = "cash_jpy" if currency == "JPY" else "cash_usd"
    if portfolio[cash_key] < total_cost:
        print(
            f"ERROR: 資金不足。必要: {currency} {total_cost:,.0f}、"
            f"残高: {currency} {portfolio[cash_key]:,.0f}",
            file=sys.stderr,
        )
        sys.exit(1)

    portfolio[cash_key] -= total_cost

    # 既存保有を確認
    existing = None
    for h in portfolio["holdings"]:
        if h["ticker"] == ticker:
            existing = h
            break

    if existing:
        # 平均取得価格を再計算
        total_shares = existing["shares"] + shares
        existing["entry_price"] = round(
            (existing["entry_price"] * existing["shares"] + price * shares) / total_shares, 2
        )
        existing["shares"] = total_shares
        if stop_loss:
            existing["stop_loss"] = stop_loss
        if take_profit:
            existing["take_profit"] = take_profit
    else:
        holding = {
            "ticker": ticker,
            "shares": shares,
            "entry_price": price,
            "entry_date": datetime.now().strftime("%Y-%m-%d"),
            "currency": currency,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
        }
        portfolio["holdings"].append(holding)

    # 取引履歴
    portfolio["history"].append(
        {
            "type": "buy",
            "ticker": ticker,
            "shares": shares,
            "price": price,
            "total": round(total_cost, 2),
            "currency": currency,
            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )

    save_portfolio(portfolio)
    print(
        json.dumps(
            {
                "action": "買い",
                "ticker": ticker,
                "shares": shares,
                "price": price,
                "total_cost": round(total_cost, 2),
                "currency": currency,
                "remaining_cash": round(portfolio[cash_key], 2),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def cmd_sell(portfolio: dict, ticker: str, shares: int, price: float):
    """売り注文"""
    existing = None
    for h in portfolio["holdings"]:
        if h["ticker"] == ticker:
            existing = h
            break

    if not existing:
        print(f"ERROR: {ticker} を保有していません", file=sys.stderr)
        sys.exit(1)

    if existing["shares"] < shares:
        print(
            f"ERROR: 保有株数不足。保有: {existing['shares']}株、売却: {shares}株", file=sys.stderr
        )
        sys.exit(1)

    currency = get_currency(ticker)
    total_proceeds = price * shares
    pnl = (price - existing["entry_price"]) * shares
    pnl_pct = (price / existing["entry_price"] - 1) * 100

    cash_key = "cash_jpy" if currency == "JPY" else "cash_usd"
    portfolio[cash_key] += total_proceeds

    existing["shares"] -= shares
    if existing["shares"] == 0:
        portfolio["holdings"].remove(existing)

    portfolio["history"].append(
        {
            "type": "sell",
            "ticker": ticker,
            "shares": shares,
            "price": price,
            "total": round(total_proceeds, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": f"{pnl_pct:+.2f}%",
            "currency": currency,
            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )

    save_portfolio(portfolio)
    print(
        json.dumps(
            {
                "action": "売り",
                "ticker": ticker,
                "shares": shares,
                "price": price,
                "total_proceeds": round(total_proceeds, 2),
                "pnl": round(pnl, 2),
                "pnl_pct": f"{pnl_pct:+.2f}%",
                "currency": currency,
                "remaining_cash": round(portfolio[cash_key], 2),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def cmd_status(portfolio: dict):
    """現在のポートフォリオ状況"""
    holdings_detail = []
    total_value_jpy = portfolio["cash_jpy"]
    total_value_usd = portfolio["cash_usd"]

    for h in portfolio["holdings"]:
        ticker = h["ticker"]
        current_price = get_current_price(ticker)
        if current_price is None:
            current_price = h["entry_price"]

        market_value = current_price * h["shares"]
        pnl = (current_price - h["entry_price"]) * h["shares"]
        pnl_pct = (current_price / h["entry_price"] - 1) * 100

        if h["currency"] == "JPY":
            total_value_jpy += market_value
        else:
            total_value_usd += market_value

        holdings_detail.append(
            {
                "ticker": ticker,
                "shares": h["shares"],
                "entry_price": h["entry_price"],
                "current_price": round(current_price, 2),
                "market_value": round(market_value, 2),
                "pnl": round(pnl, 2),
                "pnl_pct": f"{pnl_pct:+.2f}%",
                "currency": h["currency"],
                "entry_date": h.get("entry_date", "N/A"),
                "stop_loss": h.get("stop_loss"),
                "take_profit": h.get("take_profit"),
            }
        )

    print(
        json.dumps(
            {
                "cash_jpy": round(portfolio["cash_jpy"], 0),
                "cash_usd": round(portfolio["cash_usd"], 2),
                "total_value_jpy": round(total_value_jpy, 0),
                "total_value_usd": round(total_value_usd, 2),
                "holdings": holdings_detail,
                "trade_count": len(portfolio.get("history", [])),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def cmd_performance(portfolio: dict):
    """パフォーマンス統計"""
    history = portfolio.get("history", [])
    sells = [h for h in history if h["type"] == "sell"]

    if not sells:
        print(json.dumps({"message": "売却履歴がありません"}, ensure_ascii=False, indent=2))
        return

    total_pnl = sum(s.get("pnl", 0) for s in sells)
    wins = [s for s in sells if s.get("pnl", 0) > 0]
    losses = [s for s in sells if s.get("pnl", 0) < 0]
    win_rate = len(wins) / len(sells) * 100 if sells else 0

    avg_win = sum(s["pnl"] for s in wins) / len(wins) if wins else 0
    avg_loss = sum(s["pnl"] for s in losses) / len(losses) if losses else 0
    profit_factor = (
        abs(sum(s["pnl"] for s in wins) / sum(s["pnl"] for s in losses)) if losses else float("inf")
    )

    print(
        json.dumps(
            {
                "total_trades": len(sells),
                "wins": len(wins),
                "losses": len(losses),
                "win_rate": f"{win_rate:.1f}%",
                "total_pnl": round(total_pnl, 2),
                "avg_win": round(avg_win, 2),
                "avg_loss": round(avg_loss, 2),
                "profit_factor": round(profit_factor, 2),
                "recent_trades": sells[-5:],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main():
    parser = argparse.ArgumentParser(description="ポートフォリオ管理")
    parser.add_argument(
        "command", choices=["status", "buy", "sell", "performance"], help="コマンド"
    )
    parser.add_argument("ticker", nargs="?", help="ティッカー（buy/sell時）")
    parser.add_argument("shares", nargs="?", type=int, help="株数（buy/sell時）")
    parser.add_argument("price", nargs="?", type=float, help="価格（buy/sell時）")
    parser.add_argument("--stop-loss", type=float, default=None, help="損切りライン")
    parser.add_argument("--take-profit", type=float, default=None, help="利確目標")
    args = parser.parse_args()

    portfolio = load_portfolio()

    if args.command == "status":
        cmd_status(portfolio)
    elif args.command == "performance":
        cmd_performance(portfolio)
    elif args.command == "buy":
        if not all([args.ticker, args.shares, args.price]):
            print("ERROR: buy には ticker, shares, price が必要です", file=sys.stderr)
            sys.exit(1)
        cmd_buy(portfolio, args.ticker, args.shares, args.price, args.stop_loss, args.take_profit)
    elif args.command == "sell":
        if not all([args.ticker, args.shares, args.price]):
            print("ERROR: sell には ticker, shares, price が必要です", file=sys.stderr)
            sys.exit(1)
        cmd_sell(portfolio, args.ticker, args.shares, args.price)


if __name__ == "__main__":
    main()

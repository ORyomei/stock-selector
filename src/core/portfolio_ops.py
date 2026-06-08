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

import copy
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SRC_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = SRC_DIR.parent
sys.path.insert(0, str(SRC_DIR))

from infra.container import get_container


def load_portfolio() -> dict[str, Any]:
    data = get_container().portfolio().load()
    if data is None:
        return {"cash_jpy": 10_000_000, "cash_usd": 50_000, "holdings": [], "history": []}
    return _normalize(data)


def _normalize(data: dict[str, Any]) -> dict[str, Any]:
    """Convert the nested portfolio.json format to the flat format used by commands.

    Actual format: {balance: {cash_jpy, cash_usd}, positions: [...], orders: {...}}
    Expected format: {cash_jpy, cash_usd, holdings: [...], history: [...]}
    """
    # Already in flat format
    if "cash_jpy" in data:
        return data

    balance = data.get("balance", {})
    result: dict[str, Any] = {
        "cash_jpy": balance.get("cash_jpy", 10_000_000),
        "cash_usd": balance.get("cash_usd", 50_000),
        "holdings": [],
        "history": data.get("history", []),
        "_raw": data,  # keep original for save
    }

    for p in data.get("positions", []):
        ticker = p["ticker"]
        entry_time = p.get("entry_time", "")
        entry_date = entry_time[:10] if entry_time else "N/A"
        result["holdings"].append({
            "ticker": ticker,
            "shares": p.get("quantity", 0),
            "entry_price": p.get("entry_price", 0),
            "currency": "JPY" if ticker.endswith(".T") else "USD",
            "entry_date": entry_date,
            "stop_loss": p.get("stop_loss"),
            "take_profit": p.get("take_profit"),
        })

    return result


def save_portfolio(data: dict):
    get_container().portfolio().save(_denormalize(data))


def _denormalize(flat: dict[str, Any]) -> dict[str, Any]:
    """フラット形式 (cash_jpy / holdings) を実ファイルのネスト形式
    (balance / positions / orders) に戻す。

    旧実装はフラット形式をそのまま書き戻していたため positions/balance キーが
    消滅し、次回 simulator.from_dict / JsonPortfolioRepository が保有ゼロと誤認して
    建玉を全消去していた。ここでネスト形式へ逆変換して往復の整合を保つ。
    """
    # 既にネスト形式 (holdings を持たない) ならそのまま
    if "holdings" not in flat:
        return flat

    raw: dict[str, Any] = copy.deepcopy(flat["_raw"]) if flat.get("_raw") else {}
    raw.setdefault("metadata", {})
    raw.setdefault("orders", {"pending": [], "filled": []})

    # 既存 positions を ticker で引けるようにし、current_price/entry_time を温存
    prior = {p.get("ticker"): p for p in raw.get("positions", [])}

    new_positions: list[dict[str, Any]] = []
    for h in flat.get("holdings", []):
        ticker = h["ticker"]
        prev = prior.get(ticker, {})
        entry_time = prev.get("entry_time")
        if not entry_time:
            ed = h.get("entry_date")
            entry_time = (
                f"{ed}T00:00:00+00:00"
                if ed and ed != "N/A"
                else datetime.now(UTC).isoformat()
            )
        entry_price = h.get("entry_price", 0)
        new_positions.append({
            "ticker": ticker,
            "quantity": h.get("shares", 0),
            "entry_price": entry_price,
            "current_price": prev.get("current_price", entry_price),
            "entry_time": entry_time,
            "stop_loss": h.get("stop_loss"),
            "take_profit": h.get("take_profit"),
            "peak_price": prev.get("peak_price", entry_price),
        })

    prev_balance = raw.get("balance", {})
    now_iso = datetime.now(UTC).isoformat()
    raw["balance"] = {
        "cash_jpy": flat.get("cash_jpy", prev_balance.get("cash_jpy", 0)),
        "cash_usd": flat.get("cash_usd", prev_balance.get("cash_usd", 0)),
        "timestamp": now_iso,
    }
    raw["positions"] = new_positions
    # portfolio_ops 独自の取引履歴は追加キーとして温存 (simulator.from_dict は無視する)
    raw["history"] = flat.get("history", [])
    raw["metadata"]["last_updated"] = now_iso
    return raw


def get_current_price(ticker: str) -> float | None:
    return get_container().market_data().get_current_price(ticker)


def get_ticker_name(ticker: str) -> str:
    """ティッカーから表示用の銘柄名を取得する。"""
    try:
        info = get_container().market_data().get_ticker_info(ticker)
        if not info:
            return ticker
        return (
            info.get("shortName")
            or info.get("longName")
            or info.get("displayName")
            or ticker
        )
    except Exception:
        return ticker


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
    name_cache: dict[str, str] = {}

    for h in portfolio["holdings"]:
        ticker = h["ticker"]
        name = name_cache.get(ticker)
        if name is None:
            name = get_ticker_name(ticker)
            name_cache[ticker] = name
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
                "name": name,
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

    return {
        "cash_jpy": round(portfolio["cash_jpy"], 0),
        "cash_usd": round(portfolio["cash_usd"], 2),
        "total_value_jpy": round(total_value_jpy, 0),
        "total_value_usd": round(total_value_usd, 2),
        "holdings": holdings_detail,
        "trade_count": len(portfolio.get("history", [])),
    }


def cmd_performance(portfolio: dict):
    """パフォーマンス統計"""
    history = portfolio.get("history", [])
    sells = [h for h in history if h["type"] == "sell"]

    if not sells:
        return {"message": "売却履歴がありません"}

    total_pnl = sum(s.get("pnl", 0) for s in sells)
    wins = [s for s in sells if s.get("pnl", 0) > 0]
    losses = [s for s in sells if s.get("pnl", 0) < 0]
    win_rate = len(wins) / len(sells) * 100 if sells else 0

    avg_win = sum(s["pnl"] for s in wins) / len(wins) if wins else 0
    avg_loss = sum(s["pnl"] for s in losses) / len(losses) if losses else 0
    profit_factor = (
        abs(sum(s["pnl"] for s in wins) / sum(s["pnl"] for s in losses)) if losses else float("inf")
    )

    return {
        "total_trades": len(sells),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": f"{win_rate:.1f}%",
        "total_pnl": round(total_pnl, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2),
        "recent_trades": sells[-5:],
    }

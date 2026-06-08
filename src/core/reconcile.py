"""ブローカーとローカル portfolio.json の照合・同期 (reconciliation)"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from infra.container import get_container
from trading.broker_interface import BrokerInterface, Position


@dataclass
class PositionDiff:
    ticker: str
    local_qty: int
    broker_qty: int
    local_entry_price: float
    broker_entry_price: float
    broker_current_price: float
    action: str  # "ADD" | "REMOVE" | "QTY_MISMATCH" | "MATCH"


@dataclass
class ReconcileResult:
    diffs: list[PositionDiff]
    local_cash: float
    broker_cash: float
    cash_diff: float
    synced: bool  # True if sync was applied


def _currency_of(ticker: str) -> str:
    """ティッカーから通貨を判定 (.T = 日本株 = JPY、その他 = USD)。"""
    return "JPY" if ticker.endswith(".T") else "USD"


def reconcile(
    broker: BrokerInterface,
    *,
    apply: bool = False,
    verbose: bool = True,
) -> ReconcileResult:
    """ブローカーの実態とローカル portfolio.json を照合する。

    Args:
        broker: BrokerInterface 実装 (KabuStationBroker 等)
        apply: True ならローカルをブローカー側に合わせて上書き
        verbose: True なら差分をprintする
    """
    repo = get_container().portfolio()
    local_data = repo.load() or {"balance": {"cash_jpy": 0}, "positions": []}

    # --- ブローカーから取得 ---
    broker.sync_from_broker()
    broker_positions: list[Position] = broker.get_positions()
    broker_balance = broker.get_balance()
    broker_cash = float(broker_balance.get("cash_jpy", 0))

    # ブローカーが管理する通貨 (None = 全通貨)。管理外の通貨の建玉・現金は照合対象外。
    managed = broker.managed_currencies()

    def _is_managed(ticker: str) -> bool:
        return managed is None or _currency_of(ticker) in managed

    # --- ローカル状態 ---
    local_positions: list[dict[str, Any]] = local_data.get("positions", [])
    local_cash = float(local_data.get("balance", {}).get("cash_jpy", 0))

    # --- 照合 (管理対象通貨のみ) ---
    local_map: dict[str, dict[str, Any]] = {p["ticker"]: p for p in local_positions}
    broker_map: dict[str, Position] = {p.ticker: p for p in broker_positions}

    all_tickers = set(local_map.keys()) | set(broker_map.keys())
    diffs: list[PositionDiff] = []

    for ticker in sorted(all_tickers):
        if not _is_managed(ticker):
            continue  # ブローカーが扱わない通貨 → 温存 (REMOVE しない)
        local_pos = local_map.get(ticker)
        broker_pos = broker_map.get(ticker)

        local_qty = int(local_pos.get("quantity", 0)) if local_pos else 0
        broker_qty = broker_pos.quantity if broker_pos else 0
        local_entry = float(local_pos.get("entry_price", 0)) if local_pos else 0.0
        broker_entry = broker_pos.entry_price if broker_pos else 0.0
        broker_current = broker_pos.current_price if broker_pos else 0.0

        if local_qty == 0 and broker_qty > 0:
            action = "ADD"
        elif local_qty > 0 and broker_qty == 0:
            action = "REMOVE"
        elif local_qty != broker_qty:
            action = "QTY_MISMATCH"
        else:
            action = "MATCH"

        diffs.append(PositionDiff(
            ticker=ticker,
            local_qty=local_qty,
            broker_qty=broker_qty,
            local_entry_price=local_entry,
            broker_entry_price=broker_entry,
            broker_current_price=broker_current,
            action=action,
        ))

    cash_diff = broker_cash - local_cash

    # --- 表示 ---
    if verbose:
        _print_report(diffs, local_cash, broker_cash, cash_diff)

    # --- 同期適用 ---
    synced = False
    if apply and _has_differences(diffs, cash_diff):
        _apply_sync(repo, local_data, broker_positions, broker_balance, managed)
        synced = True
        if verbose:
            print("\n✅ ローカル portfolio.json をブローカー側に同期しました")

    return ReconcileResult(
        diffs=diffs,
        local_cash=local_cash,
        broker_cash=broker_cash,
        cash_diff=cash_diff,
        synced=synced,
    )


def _has_differences(diffs: list[PositionDiff], cash_diff: float) -> bool:
    if abs(cash_diff) > 1.0:
        return True
    return any(d.action != "MATCH" for d in diffs)


def _print_report(
    diffs: list[PositionDiff],
    local_cash: float,
    broker_cash: float,
    cash_diff: float,
) -> None:
    print("=" * 60)
    print("📊 ポートフォリオ照合レポート")
    print("=" * 60)

    # Cash
    print("\n💰 現金残高:")
    print(f"   ローカル:  ¥{local_cash:,.0f}")
    print(f"   ブローカー: ¥{broker_cash:,.0f}")
    if abs(cash_diff) > 1.0:
        print(f"   差分:       ¥{cash_diff:+,.0f} ⚠️")
    else:
        print("   差分:       なし ✅")

    # Positions
    print("\n📋 ポジション:")
    has_diff = False
    for d in diffs:
        if d.action == "MATCH":
            print(f"   ✅ {d.ticker}: {d.broker_qty}株 (一致)")
        elif d.action == "ADD":
            print(f"   ➕ {d.ticker}: ブローカーに{d.broker_qty}株あり (ローカルになし)")
            has_diff = True
        elif d.action == "REMOVE":
            print(f"   ➖ {d.ticker}: ローカルに{d.local_qty}株あり (ブローカーになし)")
            has_diff = True
        elif d.action == "QTY_MISMATCH":
            print(f"   ⚠️  {d.ticker}: ローカル={d.local_qty}株, ブローカー={d.broker_qty}株")
            has_diff = True

    if not has_diff and abs(cash_diff) <= 1.0:
        print("\n✅ ローカルとブローカーは一致しています")


def _apply_sync(
    repo: Any,
    local_data: dict[str, Any],
    broker_positions: list[Position],
    broker_balance: dict[str, Any],
    managed: set[str] | None,
) -> None:
    """ローカル portfolio.json をブローカーの実態に合わせて上書き。

    ブローカーが管理しない通貨 (managed 外) の建玉・現金はローカル値を温存する。
    """
    now = datetime.now(UTC).isoformat()

    def _is_managed(ticker: str) -> bool:
        return managed is None or _currency_of(ticker) in managed

    # 管理対象外通貨のローカル建玉は温存し、管理対象はブローカーの実態で差し替え
    preserved = [
        p for p in local_data.get("positions", []) if not _is_managed(p["ticker"])
    ]
    new_positions = list(preserved)
    for pos in broker_positions:
        new_positions.append({
            "ticker": pos.ticker,
            "quantity": pos.quantity,
            "entry_price": pos.entry_price,
            "current_price": pos.current_price,
            "entry_time": pos.entry_time.isoformat() if pos.entry_time else now,
            "stop_loss": pos.stop_loss,
            "take_profit": pos.take_profit,
            "peak_price": pos.peak_price,
        })

    # 残高: 管理対象通貨のみブローカー値で上書き、管理外 (例: USD) はローカルを温存
    prev_balance = local_data.get("balance", {})
    new_balance = {
        "cash_jpy": float(prev_balance.get("cash_jpy", 0)),
        "cash_usd": float(prev_balance.get("cash_usd", 0)),
        "timestamp": now,
    }
    if managed is None or "JPY" in managed:
        new_balance["cash_jpy"] = float(broker_balance.get("cash_jpy", 0))
    if managed is None or "USD" in managed:
        new_balance["cash_usd"] = float(broker_balance.get("cash_usd", 0))
    local_data["balance"] = new_balance
    local_data["positions"] = new_positions
    local_data["metadata"] = local_data.get("metadata", {})
    local_data["metadata"]["last_updated"] = now
    local_data["metadata"]["last_reconcile"] = now

    repo.save(local_data)

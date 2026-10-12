"""kabu API 実接続 E2E (issue #2 (4)): 1 売買単位の BUY → 約定 → positions 反映 → CLOSE → 記録。

検証環境 (18081) は sendorder が {"Result":0,"OrderId":null} のダミー応答で約定しない
(docs/HANDOVER.md §5-11) ので、約定〜ポジション反映の確認は本番 (18080) の実注文でしか
できない。このモジュールはそれを最小リスクで一気通貫に行う。

安全策:
- 共有 config/trading_config.json は読むだけ。kabu セクションを基に env (stg/prd) で
  port / sandbox / api_password_env をプロセス内だけ上書きする (デーモンは simulator のまま)
- 数量は常に 1 売買単位 (config/trading_units.json → 無ければブローカーの /symbol)。
  概算コストが max_cost_jpy を超えたら発注しない
- 東証のザラ場 (9:00-11:20 / 12:30-15:15 JST、祝日除く) 以外は発注しない。引け後の成行は
  翌営業日に持ち越され、BUY と CLOSE が別日に分断されて保有が残るため
- 本番は real_money=True が無い限り発注直前 (stage=dry_run) で止まる
- BUY が約定しなければ取消して終了。約定したら必ず CLOSE まで進み、保有を残さない
- 記録先は diary/kabu_e2e/trades/ (既定)。diary/trades/ に混ぜるとシミュレーション成績
  (dashboard_data.performance) に実弾テストが紛れ込むため分ける。記録のキーは本番経路
  (TradeExecutor.execute_signal / cmd_close_position) に揃える
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SRC_DIR = Path(__file__).resolve().parent.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from domain.models import Order, OrderSide, OrderStatus, OrderType, Position

JST = ZoneInfo("Asia/Tokyo")
SOURCE = "kabu_e2e"

# env ごとに共有 config の kabu セクションへ上書きする値
ENV_SETTINGS: dict[str, dict[str, Any]] = {
    "stg": {"port": 18081, "sandbox": True, "api_password_env": "KABU_API_PASSWORD_STG"},
    "prd": {"port": 18080, "sandbox": False, "api_password_env": "KABU_API_PASSWORD_PRD"},
}

# 発注を許すザラ場の窓 (JST、分)。後場は CLOSE まで済ませる余裕を見て 15:15 で締める
ZARABA_WINDOWS: tuple[tuple[int, int], ...] = ((9 * 60, 11 * 60 + 20), (12 * 60 + 30, 15 * 60 + 15))


def build_kabu_config(base: dict[str, Any] | None, env: str) -> dict[str, Any]:
    """共有 config の kabu セクションに env の port / sandbox / パスワード環境変数を重ねる (純関数)。"""
    if env not in ENV_SETTINGS:
        raise ValueError(f"env は {'/'.join(ENV_SETTINGS)} のいずれか: {env!r}")
    cfg = dict(base or {})
    cfg.setdefault("host", "localhost")
    cfg.update(ENV_SETTINGS[env])
    return cfg


def _jp_market_holiday(d: date) -> str | None:
    """東証の休場名 (agents.auto_trade._is_jp_market_holiday と同じ規則。core→agents の依存を避けて再掲)。"""
    import jpholiday

    if d.month == 12 and d.day == 31:
        return "年末休場"
    if d.month == 1 and d.day <= 3:
        return "年始休場"
    return jpholiday.is_holiday_name(d)


def zaraba_reason(now: datetime | None = None) -> str | None:
    """ザラ場で発注できない理由。発注してよければ None。"""
    now = (now or datetime.now(UTC)).astimezone(JST)
    if now.weekday() > 4:
        return "週末"
    holiday = _jp_market_holiday(now.date())
    if holiday:
        return f"休場日 ({holiday})"
    t = now.hour * 60 + now.minute
    if any(start <= t <= end for start, end in ZARABA_WINDOWS):
        return None
    return f"ザラ場外 ({now.strftime('%H:%M')} JST。発注窓は 9:00-11:20 / 12:30-15:15)"


def _trading_unit(broker: Any, ticker: str) -> int:
    """売買単位: config/trading_units.json (本番 /symbol から取得済み) を優先し、無ければブローカーに聞く。"""
    from core.trading_units import get_trading_unit, stored_trading_unit

    stored = stored_trading_unit(ticker)
    if stored:
        return stored
    if hasattr(broker, "get_trading_unit"):
        return int(broker.get_trading_unit(ticker))
    return get_trading_unit(ticker)


def _position_of(broker: Any, ticker: str) -> Position | None:
    for pos in broker.get_positions():
        if pos.ticker == ticker:
            return pos
    return None


def _wait_fill(broker: Any, order: Order, timeout_sec: float, interval_sec: float) -> Order:
    """FILLED / CANCELLED になるまで refresh_order で取り直す (ブローカーが対応していなければそのまま)。"""
    refresh = getattr(broker, "refresh_order", None)
    if refresh is None:
        return order
    deadline = time.monotonic() + timeout_sec
    while order.status not in (OrderStatus.FILLED, OrderStatus.CANCELLED) and time.monotonic() < deadline:
        time.sleep(interval_sec)
        refreshed = refresh(order)
        if refreshed is not None:
            order = refreshed
    return order


def _wait_position(
    broker: Any, ticker: str, min_qty: int, timeout_sec: float, interval_sec: float
) -> Position | None:
    """約定後に /positions へ反映されるのを待つ (通常は即時だが数秒遅れることがある)。"""
    deadline = time.monotonic() + timeout_sec
    while True:
        pos = _position_of(broker, ticker)
        if pos is not None and pos.quantity >= min_qty:
            return pos
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval_sec)


def _commission(broker: Any, order_id: str) -> dict[str, float | None]:
    getter = getattr(broker, "get_order_commission", None)
    if getter is None:
        return {"commission_jpy": None, "commission_tax_jpy": None}
    try:
        return getter(order_id)
    except Exception:
        return {"commission_jpy": None, "commission_tax_jpy": None}


def run_e2e(
    *,
    env: str = "stg",
    ticker: str = "1545.T",
    real_money: bool = False,
    max_cost_jpy: float = 20_000.0,
    diary_dir: Path | None = None,
    broker: Any | None = None,
    kabu_config: dict[str, Any] | None = None,
    now: datetime | None = None,
    confirm: Callable[[str], bool] | None = None,
    fill_timeout_sec: float = 30.0,
    position_timeout_sec: float = 10.0,
    poll_interval_sec: float = 1.5,
    log: Callable[[str], None] = lambda *_: None,
) -> dict[str, Any]:
    """E2E を実行し、各段階の結果を辞書で返す (例外は飲み込んで error に入れる)。

    stage: config → connect → cost → hours → dry_run(本番・real_money 無し) → buy → positions
           → close → verify → done。ok=True は done に到達し、positions 反映と事後の保有ゼロまで
           確認できたときだけ。
    """
    out: dict[str, Any] = {
        "ok": False, "stage": "config", "env": env, "ticker": ticker, "source": SOURCE,
        "trade_files": [], "error": None,
    }
    try:
        if broker is None:
            from infra.brokers.kabu_station import KabuStationBroker
            from infra.container import get_container

            base = kabu_config
            if base is None:
                base = get_container().config_repo().load_trading_config().get("kabu") or {}
            cfg = build_kabu_config(base, env)
            out["endpoint"] = f"{cfg['host']}:{cfg['port']} (sandbox={cfg['sandbox']})"
            broker = KabuStationBroker(cfg)
        else:
            build_kabu_config(kabu_config, env)  # env の妥当性だけ検証
            out.setdefault("endpoint", f"injected broker ({env})")

        # 1. 疎通・残高
        out["stage"] = "connect"
        cash_before = float(broker.get_balance()["cash_jpy"])
        out["cash_before"] = cash_before
        pre = _position_of(broker, ticker)
        pre_qty = pre.quantity if pre else 0
        out["position_before_qty"] = pre_qty
        log(f"買付余力 ¥{cash_before:,.0f} / {ticker} 既存保有 {pre_qty}")

        # 2. 数量と概算コスト
        out["stage"] = "cost"
        unit = _trading_unit(broker, ticker)
        price = broker.get_board_price(ticker)
        if not price or price <= 0:
            raise RuntimeError(f"{ticker} の現在値が取れません (検証環境は板が null)")
        cost = float(price) * unit
        out.update({"quantity": unit, "price": float(price), "estimated_cost_jpy": round(cost)})
        log(f"計画: {ticker} {unit} 口 × ¥{price:,.1f} ≒ ¥{cost:,.0f} (成行 BUY → 即 CLOSE)")
        if cost > max_cost_jpy:
            raise RuntimeError(f"概算コスト ¥{cost:,.0f} が上限 ¥{max_cost_jpy:,.0f} を超えています")
        if cash_before < cost * 1.05:
            raise RuntimeError(f"買付余力 ¥{cash_before:,.0f} が不足 (必要 ¥{cost * 1.05:,.0f})")

        # 3. ザラ場
        out["stage"] = "hours"
        reason = zaraba_reason(now)
        if reason:
            raise RuntimeError(f"発注しません: {reason}")

        # 4. 本番の歯止め
        if env == "prd" and not real_money:
            out["stage"] = "dry_run"
            out["error"] = "本番への発注は --real-money が必要です (発注前の確認はここまで全て通過)"
            return out
        if confirm is not None and not confirm(
            f"{out['endpoint']} で {ticker} を {unit} 口 (約 ¥{cost:,.0f}) 成行で買い、約定後すぐ売ります。実行しますか?"
        ):
            out["stage"] = "aborted"
            out["error"] = "ユーザーが中止"
            return out

        from infra.container import DIARY_DIR
        from infra.repositories.file_diary import FileDiaryRepository

        diary = FileDiaryRepository(diary_dir=diary_dir or DIARY_DIR / "kabu_e2e")

        # 5. BUY (成行 1 単位)
        out["stage"] = "buy"
        buy = broker.place_order(
            ticker=ticker, side=OrderSide.BUY, quantity=unit,
            order_type=OrderType.MARKET, entry_price=float(price),
        )
        buy = _wait_fill(broker, buy, fill_timeout_sec, poll_interval_sec)
        held_qty = buy.filled_quantity if buy.filled_quantity > 0 else (unit if buy.status == OrderStatus.FILLED else 0)
        buy_rec: dict[str, Any] = {
            "success": buy.status == OrderStatus.FILLED,
            "order_id": buy.id, "ticker": ticker, "action": "BUY",
            "quantity": held_qty or unit, "entry_price": float(price), "fill_price": buy.fill_price,
            "status": buy.status.value, "pnl": None,
            "reason": f"kabu E2E ({env}): 1 単位の成行買い",
            "timestamp": datetime.now(UTC).isoformat(), "source": SOURCE, "env": env,
            **_commission(broker, buy.id),
        }
        out["buy"] = buy_rec
        if held_qty <= 0:
            cancelled = broker.cancel_order(buy.id)
            buy_rec["reason"] += f" / 未約定のため取消 (cancelled={cancelled})"
            out["trade_files"].append(diary.save_trade(buy_rec))
            raise RuntimeError(
                f"BUY が {fill_timeout_sec:.0f} 秒以内に約定せず (status={buy.status.value})。取消={cancelled}"
            )
        if buy.status != OrderStatus.FILLED:
            # 部分約定: 残りを取消し、約定分だけ CLOSE へ進む
            broker.cancel_order(buy.id)
            buy_rec["reason"] += f" / 部分約定 {held_qty} のみ (残り取消)"
        out["trade_files"].append(diary.save_trade(buy_rec))
        log(f"BUY {buy.status.value}: {held_qty} 口 @ {buy.fill_price} (order_id={buy.id})")

        # 6. positions 反映
        out["stage"] = "positions"
        pos = _wait_position(broker, ticker, pre_qty + held_qty, position_timeout_sec, poll_interval_sec)
        out["positions_ok"] = pos is not None
        out["position_after_buy"] = pos.to_dict() if pos is not None else None
        if pos is None:
            log("⚠️ /positions に反映が見えない — 約定分の CLOSE は試みる")
        entry_price = float(buy.fill_price) if buy.fill_price else float(price)
        if pos is not None:
            buy_rec["pnl"] = round(pos.pnl, 2)

        # 7. CLOSE (成行で全量)
        out["stage"] = "close"
        close = broker.place_order(
            ticker=ticker, side=OrderSide.SELL, quantity=held_qty,
            order_type=OrderType.MARKET, entry_price=0.0,
        )
        close = _wait_fill(broker, close, fill_timeout_sec, poll_interval_sec)
        closed_qty = close.filled_quantity if close.filled_quantity > 0 else (held_qty if close.status == OrderStatus.FILLED else 0)
        pnl = (
            round((float(close.fill_price) - entry_price) * closed_qty, 2)
            if closed_qty > 0 and close.fill_price is not None else None
        )
        close_rec: dict[str, Any] = {
            "success": close.status == OrderStatus.FILLED,
            "order_id": close.id, "ticker": ticker, "action": "CLOSE",
            "quantity": closed_qty or held_qty, "entry_price": entry_price, "fill_price": close.fill_price,
            "status": close.status.value, "pnl": pnl,
            "reason": f"kabu E2E ({env}): 即時決済", "timestamp": datetime.now(UTC).isoformat(),
            "source": SOURCE, "env": env,
            "entry_time": buy_rec["timestamp"], "hold_days": 0,
            **_commission(broker, close.id),
        }
        out["close"] = close_rec
        out["trade_files"].append(diary.save_trade(close_rec))
        if closed_qty < held_qty:
            if closed_qty <= 0:
                broker.cancel_order(close.id)
            raise RuntimeError(
                f"CLOSE が約定せず (status={close.status.value})。⚠️ {ticker} {held_qty - closed_qty} 口が"
                " 口座に残っています。kabuステーションで手動決済してください"
            )
        log(f"CLOSE {close.status.value}: {closed_qty} 口 @ {close.fill_price} pnl={pnl} (order_id={close.id})")

        # 8. 事後確認 (保有が元に戻り、残高が取れる)
        out["stage"] = "verify"
        after = _position_of(broker, ticker)
        remaining = after.quantity if after else 0
        out["position_after_close_qty"] = remaining
        out["cash_after"] = float(broker.get_balance()["cash_jpy"])
        out["pnl_jpy"] = pnl
        out["commission_jpy"] = sum(
            float(r.get("commission_jpy") or 0) + float(r.get("commission_tax_jpy") or 0)
            for r in (buy_rec, close_rec)
        )
        if not out["positions_ok"]:
            raise RuntimeError("BUY 約定後に /positions への反映が確認できなかった (CLOSE は約定済み)")
        if remaining != pre_qty:
            raise RuntimeError(f"CLOSE 後の保有が {remaining} (期待 {pre_qty})。kabuステーションで確認してください")
        out["stage"] = "done"
        out["ok"] = True
    except Exception as exc:
        out["error"] = str(exc)
    return out

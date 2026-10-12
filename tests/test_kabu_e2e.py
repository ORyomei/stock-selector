"""kabu 実接続 E2E (core/kabu_e2e.py) — 偽ブローカーで段階・歯止め・記録を検証する。"""

import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from core.kabu_e2e import build_kabu_config, run_e2e, zaraba_reason
from domain.models import Order, OrderSide, OrderStatus, OrderType, Position
from infra.brokers.kabu_station import KabuStationError

JST = ZoneInfo("Asia/Tokyo")
ZARABA = datetime(2026, 10, 13, 10, 0, tzinfo=JST)  # 火曜 10:00


class _FakeBroker:
    """成行は fill_after 回の refresh で約定する。ポジションは約定に追随する。"""

    def __init__(self, *, cash=1_000_000.0, price=246.8, fill_after=0, fail_order: Exception | None = None,
                 never_fill=False, hide_position=False):
        self.cash = cash
        self.price = price
        self.fill_after = fill_after
        self.fail_order = fail_order
        self.never_fill = never_fill
        self.hide_position = hide_position
        self.held = 0
        self.entry = 0.0
        self.orders: list[dict] = []
        self.cancelled: list[str] = []
        self._pending: dict[str, int] = {}

    def get_balance(self):
        return {"cash_jpy": self.cash, "cash_usd": 0.0, "timestamp": datetime.now(UTC)}

    def get_board_price(self, ticker):
        return self.price

    def get_trading_unit(self, ticker):
        return 10

    def get_positions(self):
        if self.held <= 0 or self.hide_position:
            return []
        return [Position(ticker="1545.T", quantity=self.held, entry_price=self.entry,
                         current_price=self.price, entry_time=datetime.now(UTC))]

    def _fill(self, order: Order) -> Order:
        order.status = OrderStatus.FILLED
        order.filled_quantity = order.quantity
        order.fill_price = self.price
        if order.side == OrderSide.BUY:
            self.held += order.quantity
            self.entry = self.price
            self.cash -= self.price * order.quantity
        else:
            self.held -= order.quantity
            self.cash += self.price * order.quantity
        return order

    def place_order(self, *, ticker, side, quantity, order_type, entry_price, stop_loss=None, take_profit=None,
                    timespan="swing"):
        if self.fail_order is not None:
            raise self.fail_order
        oid = f"O{len(self.orders) + 1}"
        self.orders.append({"id": oid, "side": side, "qty": quantity, "type": order_type})
        order = Order(id=oid, ticker=ticker, side=side, quantity=quantity, entry_price=entry_price,
                      order_type=order_type, order_time=datetime.now(UTC))
        if self.never_fill:
            return order
        if self.fill_after <= 0:
            return self._fill(order)
        self._pending[oid] = self.fill_after
        return order

    def refresh_order(self, order: Order) -> Order:
        if self.never_fill:
            return order
        left = self._pending.get(order.id, 0) - 1
        self._pending[order.id] = left
        return self._fill(order) if left <= 0 else order

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return True

    def get_order_commission(self, order_id):
        return {"commission_jpy": 0.0, "commission_tax_jpy": 0.0}


def _run(broker, tmp_path: Path, **kw):
    kw.setdefault("env", "prd")
    kw.setdefault("real_money", True)
    kw.setdefault("now", ZARABA)
    kw.setdefault("poll_interval_sec", 0)
    kw.setdefault("fill_timeout_sec", 1)
    kw.setdefault("position_timeout_sec", 0.2)
    return run_e2e(broker=broker, diary_dir=tmp_path / "kabu_e2e", **kw)


def _records(out):
    return [json.loads(Path(p).read_text()) for p in out["trade_files"]]


def test_build_kabu_config_overrides_env_only():
    base = {"host": "localhost", "port": 18081, "sandbox": True, "api_password_env": "KABU_API_PASSWORD_STG",
            "order_exchange": 9, "account_type": 4}
    prd = build_kabu_config(base, "prd")
    assert (prd["port"], prd["sandbox"], prd["api_password_env"]) == (18080, False, "KABU_API_PASSWORD_PRD")
    assert prd["order_exchange"] == 9 and prd["account_type"] == 4 and base["port"] == 18081
    assert build_kabu_config(None, "stg")["host"] == "localhost"
    with pytest.raises(ValueError):
        build_kabu_config(base, "live")


def test_zaraba_reason_windows_and_holidays():
    assert zaraba_reason(datetime(2026, 10, 13, 10, 0, tzinfo=JST)) is None
    assert zaraba_reason(datetime(2026, 10, 13, 12, 45, tzinfo=JST)) is None
    assert "ザラ場外" in zaraba_reason(datetime(2026, 10, 13, 11, 25, tzinfo=JST))
    assert "ザラ場外" in zaraba_reason(datetime(2026, 10, 13, 15, 20, tzinfo=JST))
    assert "休場日" in zaraba_reason(datetime(2026, 10, 12, 10, 0, tzinfo=JST))  # スポーツの日
    assert zaraba_reason(datetime(2026, 10, 10, 10, 0, tzinfo=JST)) == "週末"
    # UTC で渡しても JST に直して判定する (01:00 UTC = 10:00 JST)
    assert zaraba_reason(datetime(2026, 10, 13, 1, 0, tzinfo=UTC)) is None


def test_full_run_buys_one_unit_then_closes_and_records_both(tmp_path):
    broker = _FakeBroker(fill_after=2)
    out = _run(broker, tmp_path)
    assert out["ok"] and out["stage"] == "done", out
    assert [o["side"] for o in broker.orders] == [OrderSide.BUY, OrderSide.SELL]
    assert all(o["qty"] == 10 and o["type"] == OrderType.MARKET for o in broker.orders)
    assert out["estimated_cost_jpy"] == 2468 and out["positions_ok"]
    assert out["position_after_buy"]["quantity"] == 10 and out["position_after_close_qty"] == 0
    assert broker.held == 0 and broker.cancelled == []
    recs = _records(out)
    assert [r["action"] for r in recs] == ["BUY", "CLOSE"]
    assert all(r["source"] == "kabu_e2e" and r["status"] == "FILLED" and r["success"] for r in recs)
    assert recs[1]["pnl"] == 0.0 and recs[1]["entry_price"] == 246.8 and recs[1]["hold_days"] == 0
    assert all(Path(p).parent == tmp_path / "kabu_e2e" / "trades" for p in out["trade_files"])


def test_prd_without_real_money_stops_before_any_order(tmp_path):
    broker = _FakeBroker()
    out = _run(broker, tmp_path, real_money=False)
    assert out["stage"] == "dry_run" and not out["ok"] and "--real-money" in out["error"]
    assert broker.orders == [] and out["trade_files"] == []
    assert out["estimated_cost_jpy"] == 2468  # 発注前の確認は済んでいる


def test_refuses_outside_zaraba_and_on_holiday(tmp_path):
    broker = _FakeBroker()
    out = _run(broker, tmp_path, now=datetime(2026, 10, 12, 10, 0, tzinfo=JST))
    assert out["stage"] == "hours" and "休場日" in out["error"] and broker.orders == []
    out = _run(broker, tmp_path, now=datetime(2026, 10, 13, 15, 30, tzinfo=JST))
    assert out["stage"] == "hours" and "ザラ場外" in out["error"] and broker.orders == []


def test_refuses_when_cost_exceeds_max_or_cash_short(tmp_path):
    broker = _FakeBroker(price=3000.0)  # 10 口 = 30,000 > 20,000
    out = _run(broker, tmp_path)
    assert out["stage"] == "cost" and "上限" in out["error"] and broker.orders == []
    broker = _FakeBroker(cash=2000.0)
    out = _run(broker, tmp_path)
    assert out["stage"] == "cost" and "不足" in out["error"] and broker.orders == []


def test_user_can_abort_at_confirmation(tmp_path):
    broker = _FakeBroker()
    asked = []
    out = _run(broker, tmp_path, confirm=lambda msg: asked.append(msg) is None and False)
    assert out["stage"] == "aborted" and broker.orders == []
    assert asked and "1545.T" in asked[0] and "10 口" in asked[0]


def test_unfilled_buy_is_cancelled_and_no_close_sent(tmp_path):
    broker = _FakeBroker(never_fill=True)
    out = _run(broker, tmp_path)
    assert out["stage"] == "buy" and not out["ok"] and "約定せず" in out["error"]
    assert broker.cancelled == ["O1"] and len(broker.orders) == 1
    recs = _records(out)
    assert len(recs) == 1 and recs[0]["action"] == "BUY" and not recs[0]["success"] and "取消" in recs[0]["reason"]


def test_sandbox_dummy_response_is_reported_at_buy_stage(tmp_path):
    broker = _FakeBroker(fail_order=KabuStationError('kabu 発注応答に OrderId がありません: {"Result":0,"OrderId":null}'))
    out = _run(broker, tmp_path, env="stg")
    assert out["stage"] == "buy" and "OrderId" in out["error"] and out["trade_files"] == []


def test_missing_position_still_closes_but_reports_failure(tmp_path):
    broker = _FakeBroker(hide_position=True)
    out = _run(broker, tmp_path)
    assert out["stage"] == "verify" and not out["ok"] and "positions" in out["error"]
    assert [o["side"] for o in broker.orders] == [OrderSide.BUY, OrderSide.SELL] and broker.held == 0
    assert [r["action"] for r in _records(out)] == ["BUY", "CLOSE"]

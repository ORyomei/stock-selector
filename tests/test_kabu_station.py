"""kabu API のトークン取得失敗メッセージ (環境のヒント付き)。"""

import requests

from infra.brokers.kabu_station import KabuStationBroker, KabuStationError, _token_error_message


class _Resp:
    def __init__(self, text: str):
        self.text = text


def _http_error(text: str) -> requests.HTTPError:
    exc = requests.HTTPError("401 Client Error: Unauthorized")
    exc.response = _Resp(text)  # type: ignore[assignment]
    return exc


def test_token_error_hints_password_mismatch_for_4001013():
    msg = _token_error_message(_http_error('{"Code":4001013,"Message":"トークン取得失敗"}'))
    assert "4001013" in msg and "検証環境" in msg and "_STG" in msg


def test_token_error_hints_host_header_for_httpsys_400():
    msg = _token_error_message(_http_error("<!DOCTYPE HTML ...><HTML><HEAD><TITLE>Bad Request</TITLE>"))
    assert "Host" in msg and "localhost" in msg


def test_token_error_without_response_keeps_generic_hint():
    msg = _token_error_message(requests.ConnectionError("boom"))
    assert "boom" in msg and "起動・ログイン済み" in msg and "body=" not in msg


def test_ensure_token_raises_kabu_error_with_hint(monkeypatch):
    def _post(*_a, **_k):
        raise _http_error('{"Code":4001013,"Message":"トークン取得失敗"}')

    monkeypatch.setattr(requests, "post", _post)
    broker = KabuStationBroker({"host": "localhost", "port": 18081, "sandbox": True, "api_password": "x"})
    try:
        broker._ensure_token()
    except KabuStationError as e:
        assert "検証環境" in str(e)
    else:
        raise AssertionError("KabuStationError が送出されるべき")


class _FakeResp:
    def __init__(self, status: int, payload=None):
        self.status_code = status
        self._payload = payload
        self.text = "" if payload is None else str(payload)
        self.content = b"x" if payload is not None else b""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            exc = requests.HTTPError(f"{self.status_code} Client Error")
            exc.response = self  # type: ignore[assignment]
            raise exc


def _broker_with_token() -> KabuStationBroker:
    b = KabuStationBroker({"host": "localhost", "port": 18081, "sandbox": True, "api_password": "x"})
    b._token = "tok"
    return b


def test_request_retries_after_rate_limit_429(monkeypatch):
    import infra.brokers.kabu_station as ks

    seq = [_FakeResp(429, {"Code": 4001006, "Message": "API実行回数エラー"}), _FakeResp(200, {"StockAccountWallet": 1.0})]
    calls: list[str] = []
    monkeypatch.setattr(requests, "request", lambda method, url, **kw: (calls.append(url), seq.pop(0))[1])
    waits: list[float] = []
    monkeypatch.setattr(ks.time, "sleep", waits.append)
    out = _broker_with_token()._request("GET", "/wallet/cash")
    assert out == {"StockAccountWallet": 1.0} and len(calls) == 2 and waits == [1.2]


def test_request_gives_up_after_retries_on_persistent_429(monkeypatch):
    import infra.brokers.kabu_station as ks

    monkeypatch.setattr(requests, "request", lambda *a, **kw: _FakeResp(429, {"Code": 4001006}))
    monkeypatch.setattr(ks.time, "sleep", lambda *_: None)
    try:
        _broker_with_token()._request("GET", "/wallet/cash")
    except KabuStationError as e:
        assert "4001006" in str(e)
    else:
        raise AssertionError("429 が続いたら KabuStationError")


def test_token_error_hints_station_not_logged_in_for_4001007():
    msg = _token_error_message(_http_error('{"Code":4001007,"Message":"ログイン認証エラー"}'))
    assert "4001007" in msg and "未ログイン" in msg and "再ログイン" in msg or "ログインし直して" in msg


def _broker_with_responses(monkeypatch, responses: dict):
    broker = KabuStationBroker({"host": "localhost", "port": 18080, "api_password": "x"})
    calls = []

    def _request(method, path, *, json_body=None, params=None):
        calls.append((method, path, params))
        return responses[path]

    monkeypatch.setattr(broker, "_request", _request)
    return broker, calls


def test_get_board_price_prefers_ask_then_current_then_previous_close(monkeypatch):
    broker, _ = _broker_with_responses(monkeypatch, {"/board/1545@1": {"AskPrice": 247.1, "CurrentPrice": 246.8}})
    assert broker.get_board_price("1545.T") == 247.1
    broker, _ = _broker_with_responses(monkeypatch, {"/board/1545@1": {"AskPrice": 0, "CurrentPrice": 246.8}})
    assert broker.get_board_price("1545.T") == 246.8
    broker, _ = _broker_with_responses(monkeypatch, {"/board/1545@1": {"PreviousClose": 245.0}})
    assert broker.get_board_price("1545.T") == 245.0
    broker, _ = _broker_with_responses(monkeypatch, {"/board/1545@1": None})  # 検証環境
    assert broker.get_board_price("1545.T") is None


def test_get_order_commission_sums_only_fill_details(monkeypatch):
    rows = [{"ID": "X", "Details": [
        {"RecType": 1, "Commission": 99, "CommissionTax": 9},
        {"RecType": 8, "Commission": 0, "CommissionTax": 0, "Qty": 5},
        {"RecType": 8, "Commission": 55, "CommissionTax": 5.5, "Qty": 5},
    ]}]
    broker, calls = _broker_with_responses(monkeypatch, {"/orders": rows})
    assert broker.get_order_commission("X") == {"commission_jpy": 55.0, "commission_tax_jpy": 5.5}
    assert calls[0][2] == {"id": "X"}


def test_refresh_order_resolves_fill_from_orders(monkeypatch):
    from datetime import UTC, datetime

    from domain.models import Order, OrderSide, OrderStatus, OrderType

    rows = [{"ID": "X", "State": 5, "OrderQty": 10, "CumQty": 10,
             "Details": [{"RecType": 8, "Price": 246.5, "Qty": 10}]}]
    broker, _ = _broker_with_responses(monkeypatch, {"/orders": rows})
    pending = Order(id="X", ticker="1545.T", side=OrderSide.BUY, quantity=10, entry_price=246.8,
                    order_type=OrderType.MARKET, order_time=datetime.now(UTC))
    done = broker.refresh_order(pending)
    assert done.status == OrderStatus.FILLED and done.filled_quantity == 10 and done.fill_price == 246.5
    assert done.id == "X" and done.side == OrderSide.BUY

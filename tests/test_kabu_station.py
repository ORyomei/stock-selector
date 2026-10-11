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


def test_token_error_hints_sandbox_login_for_4001013():
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

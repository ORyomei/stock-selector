"""dashboard CLI の二重起動ガード (systemd 常駐と手動起動の排他)。"""

import socket

from cli.app import _port_in_use


def test_port_in_use_detects_listening_socket():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        assert _port_in_use("127.0.0.1", port) is True
        assert _port_in_use("0.0.0.0", port) is True  # 0.0.0.0 は localhost に読み替える


def test_port_in_use_false_when_nothing_listens():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert _port_in_use("127.0.0.1", port) is False

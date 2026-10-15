"""find_scanner_ips() against real sockets on localhost."""
import socket
import threading

from app import netfind


def test_port_sweep_finds_listener(monkeypatch):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port = srv.getsockname()[1]
    stop = threading.Event()

    def accept():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                srv.accept()[0].close()
            except OSError:
                pass
    t = threading.Thread(target=accept, daemon=True)
    t.start()
    try:
        monkeypatch.setattr(netfind, "local_prefixes", lambda: ["127.0.0"])
        monkeypatch.setattr(netfind, "SCAN_PORT", port)
        monkeypatch.setattr(netfind, "enpc_replies", lambda timeout=1.5: ["127.0.0.9"])
        ips = netfind.find_scanner_ips(connect_timeout=0.3)
    finally:
        stop.set()
        srv.close()
    assert ips[0] == "127.0.0.9"           # Epson broadcast replies come first
    assert "127.0.0.1" in ips
    assert len(ips) == len(set(ips))


def test_local_prefixes_are_private_slash24():
    for p in netfind.local_prefixes():
        assert p.count(".") == 2 and not p.startswith("127.")


def test_enpc_reply_parsing():
    assert netfind.is_enpc_reply(b"EPSONq\x03\x00\x00\x00\x00\x10FF-680W")
    assert not netfind.is_enpc_reply(b"HTTP/1.1 200 OK")

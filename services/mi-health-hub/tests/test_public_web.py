"""Synthetic DNS/HTTP only: public reads must never reach internal services."""

import io
import socket
import time
from types import SimpleNamespace
from threading import Event
from unittest.mock import Mock

import pytest

from mi_health_hub.api import public_web as net
from mi_health_hub.api.app import _tool_read_page


def answer(address):
    return [(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 80))]


@pytest.fixture
def transport(monkeypatch):
    requests, responses, reads = [], [], []
    monkeypatch.setattr(net.socket, "getaddrinfo", Mock(return_value=answer("93.184.216.34")))

    class Response:
        def __init__(self, status=200, headers=None, body=b"<p>synthetic page</p>"):
            self.status, self.headers = status, headers or {}
            self.body = io.BytesIO(body)

        def getheader(self, name, default=None):
            return self.headers.get(name, default)

        def read1(self, size):
            reads.append(size)
            return self.body.read(size)

        def close(self):
            self.body.close()

    class Connection:
        def __init__(self, host, port, address, timeout):
            requests.append((host, port, address))
            self.sock = Mock()

        def connect(self):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return responses.pop(0) if responses else Response()

        def close(self):
            pass

    monkeypatch.setattr(net, "_PinnedHTTP", Connection)
    monkeypatch.setattr(net, "_PinnedHTTPS", Connection)
    return SimpleNamespace(requests=requests, responses=responses, reads=reads, Response=Response)


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.0.0.1", "192.168.1.1", "172.16.0.1", "169.254.169.254",
    "0.0.0.0", "224.0.0.1", "::1", "fc00::1", "fe80::1", "ff02::1",
    "::ffff:127.0.0.1", "::ffff:192.168.1.1", "2002:7f00:1::", "fe80::1%eth0",
])
def test_nonpublic_addresses_are_rejected_before_connect(monkeypatch, transport, address):
    monkeypatch.setattr(net.socket, "getaddrinfo", Mock(return_value=answer(address)))
    with pytest.raises(net.PublicWebError):
        net.read_public_page("https://public.example.test/page")
    assert not transport.requests


def test_one_private_dns_answer_rejects_entire_host(monkeypatch, transport):
    monkeypatch.setattr(net.socket, "getaddrinfo", Mock(return_value=answer("93.184.216.34") + answer("127.0.0.1")))
    with pytest.raises(net.PublicWebError):
        net.read_public_page("https://public.example.test/page")
    assert not transport.requests


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_destination_is_checked(monkeypatch, transport, status):
    resolver = Mock(side_effect=[answer("93.184.216.34"), answer("127.0.0.1")])
    monkeypatch.setattr(net.socket, "getaddrinfo", resolver)
    transport.responses.append(transport.Response(status, {"Location": "http://127.0.0.1/private"}))
    with pytest.raises(net.PublicWebError):
        net.read_public_page("https://public.example.test/page")
    assert len(transport.requests) == 1
    assert resolver.call_count == 2


def test_relative_public_redirect_and_numeric_pin(transport):
    transport.responses.extend([
        transport.Response(302, {"Location": "/second"}), transport.Response(body=b"public content"),
    ])
    assert net.read_public_page("https://public.example.test/first") == "public content"
    assert transport.requests == [("public.example.test", 443, "93.184.216.34")] * 2


def test_redirect_limit_and_non_http_redirect(transport):
    transport.responses.extend([transport.Response(302, {"Location": "/again"}) for _ in range(6)])
    with pytest.raises(net.PublicWebError, match="重定向"):
        net.read_public_page("https://public.example.test/page")
    transport.responses[:] = [transport.Response(302, {"Location": "file:///etc/passwd"})]
    with pytest.raises(net.PublicWebError, match="URL"):
        net.read_public_page("https://public.example.test/page")


@pytest.mark.parametrize("url", [
    "file:///private", "http://localhost./", "http://a.local/", "http://a.internal/",
    "https://user:password@public.example.test/", "http://[fe80::1%25eth0]/",
    "http://public.example.test:invalid/", "http://public.example.test/\nignored",
])
def test_invalid_or_local_urls_never_connect(transport, url):
    with pytest.raises(net.PublicWebError):
        net.read_public_page(url)
    assert not transport.requests


def test_read_is_bounded_without_content_length(transport):
    transport.responses.append(transport.Response(body=b"x" * 5000))
    with pytest.raises(net.PublicWebError, match="大小"):
        net.read_public_page("https://public.example.test/page", max_bytes=4096)
    assert transport.reads == [4097]


def test_declared_large_body_is_rejected_before_read(transport):
    transport.responses.append(transport.Response(headers={"Content-Length": "9999999"}))
    with pytest.raises(net.PublicWebError, match="大小"):
        net.read_public_page("https://public.example.test/page")
    assert not transport.reads


def test_dns_timeout_is_part_of_budget(monkeypatch):
    def slow_dns(*args, **kwargs):
        time.sleep(0.05)
        return answer("93.184.216.34")

    monkeypatch.setattr(net.socket, "getaddrinfo", slow_dns)
    with pytest.raises(net.PublicWebError, match="超时"):
        net.read_public_page("https://public.example.test/page", timeout=0.005)


def test_pinned_https_keeps_original_tls_hostname(monkeypatch):
    raw = Mock()
    connect = Mock(return_value=raw)
    context = Mock()
    monkeypatch.setattr(net.socket, "create_connection", connect)
    monkeypatch.setattr(net.ssl, "create_default_context", Mock(return_value=context))
    connection = net._PinnedHTTPS("public.example.test", 443, "93.184.216.34", 3)
    connection.connect()
    connect.assert_called_once_with(("93.184.216.34", 443), 3)
    context.wrap_socket.assert_called_once_with(raw, server_hostname="public.example.test")


def test_tool_uses_public_reader_and_returns_no_raw_network_error(monkeypatch):
    monkeypatch.setattr("mi_health_hub.api.app.read_public_page", Mock(side_effect=net.PublicWebError("拒绝访问内网/本机地址")))
    assert "拒绝访问" in _tool_read_page({"url": "http://127.0.0.1/"})


def test_total_deadline_interrupts_a_dripping_response(monkeypatch, transport):
    aborted = Event()

    class DrippingResponse(transport.Response):
        def read1(self, size):
            assert aborted.wait(timeout=1), "deadline must interrupt an unfinished response"
            raise OSError("synthetic socket shutdown")

    class HeldConnection:
        def __init__(self, *args):
            self.sock = Mock()
            self.sock.shutdown.side_effect = lambda *args: aborted.set()

        def connect(self):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return DrippingResponse()

        def close(self):
            pass

    monkeypatch.setattr(net, "_PinnedHTTP", HeldConnection)
    with pytest.raises(net.PublicWebError):
        net.read_public_page("http://public.example.test/page", timeout=0.02)
    assert aborted.is_set()

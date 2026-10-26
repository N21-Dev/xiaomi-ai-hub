"""Bounded public HTTP reads with DNS pinning and per-hop validation.

No environment proxy, cookies, authorization or redirects are inherited from
urllib. TLS still verifies the original hostname while the socket connects to
an address that has already passed the public-address check.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from threading import BoundedSemaphore, Timer
from urllib.parse import urljoin, urlsplit

MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_DNS_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="public-web-dns")
_DNS_SLOTS = BoundedSemaphore(4)


class PublicWebError(ValueError):
    pass


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PublicWebError("网页读取超时")
    return remaining


def _addresses(host: str, port: int, deadline: float) -> list[str]:
    if not _DNS_SLOTS.acquire(timeout=_remaining(deadline)):
        raise PublicWebError("网页域名解析繁忙，请稍后重试")
    try:
        future = _DNS_POOL.submit(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    except BaseException:
        _DNS_SLOTS.release()
        raise
    future.add_done_callback(lambda _: _DNS_SLOTS.release())
    try:
        rows = future.result(timeout=_remaining(deadline))
    except FutureTimeout:
        future.cancel()
        raise PublicWebError("网页域名解析超时") from None
    except OSError:
        raise PublicWebError("无法解析网页主机") from None
    addresses = []
    for row in rows:
        try:
            address = ipaddress.ip_address(row[4][0])
        except ValueError:
            raise PublicWebError("网页主机地址无效") from None
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if (
            not address.is_global or address.is_multicast or address.is_reserved
            or address.is_unspecified or getattr(address, "scope_id", None)
            or isinstance(address, ipaddress.IPv6Address) and (address.sixtofour or address.teredo)
        ):
            raise PublicWebError("拒绝访问内网/本机或非公网地址")
        addresses.append(str(address))
    if not addresses:
        raise PublicWebError("无法解析网页主机")
    return list(dict.fromkeys(addresses))


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self.address = address
        self.deadline = time.monotonic() + timeout

    def connect(self) -> None:
        self.sock = socket.create_connection((self.address, self.port), self.timeout)


class _PinnedHTTPS(_PinnedHTTP):
    def connect(self) -> None:
        super().connect()
        try:
            self.sock.settimeout(_remaining(self.deadline))
            self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=self.host)
        except BaseException:
            self.close()
            raise


def read_public_page(url: str, *, timeout: float = 15, max_bytes: int = MAX_PAGE_BYTES) -> str:
    deadline = time.monotonic() + timeout
    current = url
    for redirect in range(MAX_REDIRECTS + 1):
        try:
            parsed = urlsplit(current)
            host = parsed.hostname
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if (
                parsed.scheme not in {"http", "https"} or not host or len(current) > 4096
                or parsed.username is not None or parsed.password is not None
                or any(ord(ch) < 32 for ch in current) or "\\" in current or "%" in host
            ):
                raise ValueError
            host = host.rstrip(".").encode("idna").decode("ascii").lower()
        except (ValueError, UnicodeError):
            raise PublicWebError("无效 URL（仅支持不含账号密码的 http/https 地址）") from None
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise PublicWebError("拒绝访问内网/本机地址")
        addresses = _addresses(host, port, deadline)
        # Connect to a numeric address from this resolution. A later DNS change
        # cannot change the destination; Host and TLS use the original hostname.
        connection_type = _PinnedHTTPS if parsed.scheme == "https" else _PinnedHTTP
        connection = connection_type(host, port, addresses[0], _remaining(deadline))
        deadline_timer = None
        try:
            connection.connect()
            transport = connection.sock

            def abort_read():
                # HTTP socket timeouts are inactivity limits. Shutdown also
                # bounds a peer that drips headers or chunk framing forever.
                try:
                    transport.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            deadline_timer = Timer(_remaining(deadline), abort_read)
            deadline_timer.daemon = True
            deadline_timer.start()
            transport.settimeout(_remaining(deadline))
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            connection.request("GET", path, headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9", "Accept-Encoding": "identity",
            })
            transport.settimeout(_remaining(deadline))
            response = connection.getresponse()
            try:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if not location or redirect >= MAX_REDIRECTS:
                        raise PublicWebError("网页重定向过多或目标无效")
                    current = urljoin(current, location)
                    continue
                if not 200 <= response.status < 300:
                    raise PublicWebError(f"网页返回 HTTP {response.status}")
                if response.getheader("Content-Encoding", "identity").lower() != "identity":
                    raise PublicWebError("网页返回不支持的压缩格式")
                length = response.getheader("Content-Length")
                if length and (not length.isdecimal() or len(length) > 10 or int(length) > max_bytes):
                    raise PublicWebError("网页正文超过大小限制")
                body = bytearray()
                while True:
                    transport.settimeout(_remaining(deadline))
                    chunk = response.read1(min(65536, max_bytes + 1 - len(body)))
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise PublicWebError("网页正文超过大小限制")
                _remaining(deadline)
                return body.decode("utf-8", "ignore")
            finally:
                response.close()
        except (OSError, http.client.HTTPException):
            raise PublicWebError("网页连接或读取失败") from None
        finally:
            if deadline_timer is not None:
                deadline_timer.cancel()
            connection.close()
    raise PublicWebError("网页重定向过多")

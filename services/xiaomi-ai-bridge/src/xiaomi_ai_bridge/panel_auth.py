"""Panel accounts and revocable sessions, separate from model gateway tokens."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import hmac
import ipaddress
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict, deque
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

COOKIE_NAME = "ai_hub_session"
SESSION_SECONDS = 7 * 24 * 3600
PASSWORD_ITERATIONS = 600_000
MAX_SESSIONS = 32
_LOCAL_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "::1/128", "fc00::/7", "fe80::/10",
))


def _address(value: str | None) -> Any:
    try:
        address = ipaddress.ip_address(value or "")
        return getattr(address, "ipv4_mapped", None) or address
    except ValueError:
        return None


def _in_networks(address: Any, networks: tuple) -> bool:
    return address is not None and any(address.version == network.version and address in network for network in networks)


def safe_next(value: str) -> str:
    """Only return to this application's root and optional view fragment."""
    if value == "/" or (value.startswith("/#") and len(value) <= 128 and all(c.isalnum() or c in "/#-_" for c in value)):
        return value
    return "/"


@dataclass(frozen=True)
class PanelSession:
    username: str
    csrf: str
    expires_at: float


class PanelAuth:
    def __init__(self, state_dir: Path, trusted_proxy_cidrs: tuple[str, ...] = ()):
        self.path = state_dir / "panel-auth.sqlite3"
        self.trusted_proxies = tuple(ipaddress.ip_network(value, strict=False) for value in trusted_proxy_cidrs)
        self._lock = threading.RLock()
        self._hash_slots = asyncio.Semaphore(2)
        self._attempts: OrderedDict[str, deque[float]] = OrderedDict()
        self._global_attempts: deque[float] = deque()
        self._sessions: dict[str, PanelSession] = {}
        self._username = ""
        self._salt = secrets.token_bytes(24)
        self._password_hash = secrets.token_bytes(32)
        self._iterations = PASSWORD_ITERATIONS
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS account (id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL, salt BLOB NOT NULL, password_hash BLOB NOT NULL, iterations INTEGER NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires_at REAL NOT NULL, created_at REAL NOT NULL)")
            row = connection.execute("SELECT username,salt,password_hash,iterations FROM account WHERE id=1").fetchone()
            if row is not None:
                username, salt, digest, iterations = row
                if not isinstance(username, str) or not 1 <= len(username) <= 80 or not isinstance(salt, bytes) or len(salt) != 24 or not isinstance(digest, bytes) or len(digest) != 32 or not 100_000 <= iterations <= 2_000_000:
                    raise ValueError("面板账号文件无效，请在服务器重设管理员账号")
                self._username, self._salt, self._password_hash, self._iterations = row
            connection.execute("DELETE FROM sessions WHERE expires_at<=?", (time.time(),))
            if self._username:
                rows = connection.execute("SELECT token_hash,csrf,expires_at FROM sessions ORDER BY created_at DESC LIMIT ?", (MAX_SESSIONS,)).fetchall()
                self._sessions = {key: PanelSession(self._username, csrf, expires) for key, csrf, expires in rows}
        self.path.chmod(0o600)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.execute("PRAGMA journal_mode=DELETE")
            with connection:
                yield connection
        finally:
            connection.close()

    @property
    def configured(self) -> bool:
        return bool(self._username)

    def set_admin(self, username: str, password: str) -> None:
        username = username.strip()
        if not username or len(username) > 80 or any(ord(c) < 32 for c in username):
            raise ValueError("管理员账号需为 1–80 个可显示字符")
        if not 12 <= len(password) <= 256:
            raise ValueError("新密码长度需为 12–256 个字符")
        salt = secrets.token_bytes(24)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ITERATIONS)
        with self._lock, self._connect() as connection:
            connection.execute("INSERT OR REPLACE INTO account VALUES (1,?,?,?,?)", (username, salt, digest, PASSWORD_ITERATIONS))
            connection.execute("DELETE FROM sessions")
            self._username, self._salt, self._password_hash, self._iterations = username, salt, digest, PASSWORD_ITERATIONS
            self._sessions.clear()
        self.path.chmod(0o600)

    def _trusted_proxy(self, request: web.Request) -> bool:
        return _in_networks(_address(request.remote), self.trusted_proxies)

    def scheme(self, request: web.Request) -> str:
        forwarded = request.headers.get("X-Forwarded-Proto", "").casefold()
        if self._trusted_proxy(request) and forwarded in {"http", "https"}:
            return forwarded
        return request.scheme

    def client_address(self, request: web.Request) -> str:
        if self._trusted_proxy(request):
            cloudflare = _address(request.headers.get("CF-Connecting-IP"))
            if cloudflare is not None:
                return str(cloudflare)
        return str(request.remote or "unknown")

    def direct_lan_gateway(self, request: web.Request) -> bool:
        # Forwarding metadata can only make this policy stricter. Never trust a
        # claimed private X-Forwarded-For/CF address to grant keyless access.
        if any(name.casefold().startswith(("x-forwarded-", "cf-")) or name.casefold() == "forwarded" for name in request.headers):
            return False
        if not _in_networks(_address(request.remote), _LOCAL_NETWORKS):
            return False
        try:
            hostname = urlsplit("//" + request.host).hostname
        except ValueError:
            return False
        return hostname in {"localhost", "xiaomi-ai-bridge", "host.docker.internal"} or _in_networks(_address(hostname), _LOCAL_NETWORKS)

    def _throttle(self, address: str) -> int:
        now = time.monotonic()
        while self._global_attempts and self._global_attempts[0] <= now - 300:
            self._global_attempts.popleft()
        attempts = self._attempts.setdefault(address, deque())
        self._attempts.move_to_end(address)
        while len(self._attempts) > 1024:
            self._attempts.popitem(last=False)
        while attempts and attempts[0] <= now - 300:
            attempts.popleft()
        if len(attempts) >= 5:
            return max(1, int(attempts[0] + 300 - now) + 1)
        if len(self._global_attempts) >= 100:
            return max(1, int(self._global_attempts[0] + 300 - now) + 1)
        attempts.append(now)
        self._global_attempts.append(now)
        return 0

    async def login(self, username: str, password: str, address: str) -> tuple[str | None, int]:
        retry_after = self._throttle(address)
        if retry_after:
            return None, retry_after
        async with self._hash_slots:
            digest = await asyncio.to_thread(hashlib.pbkdf2_hmac, "sha256", password.encode(), self._salt, self._iterations)
        correct_password = hmac.compare_digest(digest, self._password_hash)
        correct_user = hmac.compare_digest(username.encode(), self._username.encode())
        if not self.configured or not correct_password or not correct_user:
            return None, 0
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now = time.time()
        session = PanelSession(self._username, secrets.token_urlsafe(32), now + SESSION_SECONDS)
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
            connection.execute("INSERT INTO sessions VALUES (?,?,?,?)", (token_hash, session.csrf, session.expires_at, now))
            connection.execute("DELETE FROM sessions WHERE token_hash NOT IN (SELECT token_hash FROM sessions ORDER BY created_at DESC LIMIT ?)", (MAX_SESSIONS,))
            remaining = {row[0] for row in connection.execute("SELECT token_hash FROM sessions")}
            self._sessions = {key: item for key, item in self._sessions.items() if key in remaining}
            self._sessions[token_hash] = session
        self._attempts.pop(address, None)
        return token, 0

    def session(self, request: web.Request) -> PanelSession | None:
        token = request.cookies.get(COOKIE_NAME, "")
        if not 40 <= len(token) <= 100:
            return None
        key = hashlib.sha256(token.encode()).hexdigest()
        session = self._sessions.get(key)
        if session is not None and session.expires_at <= time.time():
            self._sessions.pop(key, None)
            return None
        return session

    def logout(self, request: web.Request) -> None:
        key = hashlib.sha256(request.cookies.get(COOKIE_NAME, "").encode()).hexdigest()
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash=?", (key,))
            self._sessions.pop(key, None)

    @staticmethod
    def csrf_valid(request: web.Request, session: PanelSession) -> bool:
        return hmac.compare_digest(request.headers.get("X-Panel-CSRF", "").encode(), session.csrf.encode())


def main() -> None:
    parser = argparse.ArgumentParser(description="在服务器初始化或重设面板管理员，不将密码写入命令行")
    parser.add_argument("command", choices=["set-admin"])
    parser.add_argument("--username", required=True)
    parser.add_argument("--state-dir", type=Path)
    args = parser.parse_args()
    import os
    directory = args.state_dir or Path(os.getenv("STATE_DIR", "/data"))
    password = getpass.getpass("管理员密码: ")
    confirmation = getpass.getpass("再次输入密码: ")
    if not hmac.compare_digest(password.encode(), confirmation.encode()):
        parser.error("两次输入的密码不一致，账号未修改")
    PanelAuth(directory).set_admin(args.username, password)
    print("管理员账号已保存。请重启中枢加载账号；原有登录会话已撤销。")


if __name__ == "__main__":
    main()

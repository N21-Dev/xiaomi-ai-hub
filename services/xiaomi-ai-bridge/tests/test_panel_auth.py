"""Panel authentication contracts using synthetic accounts and local HTTP only."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from xiaomi_ai_bridge.health import start_health_server
from xiaomi_ai_bridge.panel_auth import COOKIE_NAME, PanelAuth, safe_next


class PanelAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.auth = PanelAuth(self.root, ("127.0.0.0/8", "::1/128"))
        await asyncio.to_thread(self.auth.set_admin, "fixture-admin", "fixture-password")
        self.queue = asyncio.Queue()
        self.bridge = SimpleNamespace(
            public_config=Mock(return_value={"account": "masked"}),
            public_home=Mock(return_value={"devices": []}),
            public_models=Mock(return_value={"providers": [{"enabled": True, "models": ["fixture-model"]}]}),
            public_hub=Mock(return_value={"resources": []}),
            update_connection_config=Mock(return_value={"changed": True}),
            ingest_hub_event=Mock(side_effect=PermissionError("接入令牌无效")),
            models=SimpleNamespace(authorized=lambda value: True),
            hub=SimpleNamespace(subscribe=Mock(return_value=self.queue), unsubscribe=Mock()),
        )
        self.settings = SimpleNamespace(
            panel_auth_required=True, state_dir=self.root,
            panel_trusted_proxy_cidrs=("127.0.0.0/8", "::1/128"),
        )
        with patch("xiaomi_ai_bridge.health.web.TCPSite") as site:
            site.return_value.start = AsyncMock()
            self.runner = await start_health_server(SimpleNamespace(public_dict=lambda: {"ready": True}), self.bridge, SimpleNamespace(), self.settings, "127.0.0.1", 0)
        self.client = TestClient(TestServer(self.runner.app), cookie_jar=aiohttp.CookieJar(unsafe=True))
        await self.client.start_server()
        self.addAsyncCleanup(self.runner.cleanup)
        self.addAsyncCleanup(self.client.close)

    async def login(self, client=None, **kwargs):
        client = client or self.client
        response = await client.post("/api/auth/login", json={"username": "fixture-admin", "password": "fixture-password", **kwargs})
        self.assertEqual(response.status, 200)
        return response

    async def csrf(self):
        response = await self.client.get("/api/auth/session")
        return (await response.json())["csrf"]

    async def test_anonymous_panel_and_every_data_surface_are_protected(self):
        response = await self.client.get("/", allow_redirects=False)
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["Location"], "/login")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        for path in ["/app.js", "/api/status", "/status", "/readyz", "/api/config", "/api/home", "/api/models", "/api/automations", "/api/health-source", "/api/hub", "/api/hub/stream", "/api/hub/storage", "/api/hub/resources/private/history", "/api/auth/xiaomi/status"]:
            with self.subTest(path=path):
                response = await self.client.get(path)
                self.assertEqual(response.status, 401)
                self.assertEqual((await response.json())["code"], "panel_auth_required")
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.bridge.public_config.assert_not_called()
        self.bridge.public_hub.assert_not_called()
        self.bridge.hub.subscribe.assert_not_called()
        self.assertEqual((await self.client.post("/api/actions/reconnect")).status, 401)

    async def test_login_assets_and_minimal_healthcheck_remain_available(self):
        for path in ["/login", "/login.js", "/app.css", "/healthz"]:
            self.assertEqual((await self.client.get(path)).status, 200)
        response = await self.client.get("/healthz")
        self.assertEqual(await response.json(), {"status": "ok"})
        response = await self.client.get("/api/auth/session")
        self.assertEqual(await response.json(), {"enabled": True, "configured": True, "authenticated": False})

    async def test_login_issues_host_only_httponly_cookie_and_private_page(self):
        response = await self.login(next="/#chat")
        self.assertEqual((await response.json())["next"], "/#chat")
        cookie = response.cookies[COOKIE_NAME]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertEqual(cookie["path"], "/")
        self.assertFalse(cookie["domain"])
        self.assertFalse(cookie["secure"])
        page = await self.client.get("/")
        self.assertEqual(page.status, 200)
        content = await page.text()
        self.assertNotIn("__PANEL_", content)
        self.assertIn("fixture-admin", content)
        self.assertNotIn("fixture-password", content)
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertEqual((await self.client.get("/api/config")).status, 200)
        self.assertEqual((await self.client.get("/login?next=/#chat", allow_redirects=False)).status, 302)

    async def test_passwords_are_salted_and_session_tokens_are_not_stored(self):
        response = await self.login()
        token = response.cookies[COOKIE_NAME].value
        with sqlite3.connect(self.auth.path) as connection:
            account = connection.execute("SELECT salt,password_hash,iterations FROM account").fetchone()
            saved_token = connection.execute("SELECT token_hash FROM sessions").fetchone()[0]
        self.assertEqual(len(account[0]), 24)
        self.assertGreaterEqual(account[2], 600_000)
        self.assertEqual(saved_token, hashlib.sha256(token.encode()).hexdigest())
        self.assertNotEqual(saved_token, token)
        self.assertNotIn(b"fixture-password", self.auth.path.read_bytes())
        self.assertEqual(self.auth.path.stat().st_mode & 0o777, 0o600)
        with tempfile.TemporaryDirectory() as root:
            another = PanelAuth(Path(root))
            await asyncio.to_thread(another.set_admin, "fixture-admin", "fixture-password")
            self.assertNotEqual(account[0], another._salt)
            self.assertNotEqual(account[1], another._password_hash)

    async def test_wrong_username_and_password_share_error_and_are_rate_limited(self):
        for i in range(5):
            response = await self.client.post("/api/auth/login", json={"username": "fixture-admin" if i % 2 else "unknown", "password": "incorrect"})
            self.assertEqual(response.status, 401)
            self.assertEqual((await response.json())["error"], "账号或密码不正确")
        response = await self.client.post("/api/auth/login", json={"username": "fixture-admin", "password": "fixture-password"})
        self.assertEqual(response.status, 429)
        self.assertGreater(int(response.headers["Retry-After"]), 0)

    async def test_mutations_need_both_session_and_csrf_and_same_origin(self):
        await self.login()
        for headers in [{}, {"X-Panel-CSRF": "wrong"}]:
            self.assertEqual((await self.client.put("/api/config", json={}, headers=headers)).status, 403)
        token = await self.csrf()
        response = await self.client.put("/api/config", json={}, headers={"X-Panel-CSRF": token, "Origin": "https://another.example"})
        self.assertEqual(response.status, 403)
        self.bridge.update_connection_config.assert_not_called()
        response = await self.client.put("/api/config", json={}, headers={"X-Panel-CSRF": token, "Origin": str(self.client.make_url('/')).rstrip('/')})
        self.assertEqual(response.status, 200)
        self.bridge.update_connection_config.assert_called_once()

    async def test_logout_revokes_replayed_cookie_but_preserves_other_session(self):
        response = await self.login()
        old_token = response.cookies[COOKIE_NAME].value
        csrf = await self.csrf()
        second = TestClient(TestServer(self.runner.app), cookie_jar=aiohttp.CookieJar(unsafe=True))
        await second.start_server()
        self.addAsyncCleanup(second.close)
        await self.login(client=second)
        response = await self.client.post("/api/auth/logout", json={}, headers={"X-Panel-CSRF": csrf})
        self.assertEqual(response.status, 200)
        self.assertEqual((await self.client.get("/api/config", headers={"Cookie": f"{COOKIE_NAME}={old_token}"})).status, 401)
        self.assertEqual((await second.get("/api/config")).status, 200)

    async def test_sessions_survive_restart_expire_and_password_reset_revokes(self):
        response = await self.login()
        token = response.cookies[COOKIE_NAME].value
        request = make_mocked_request("GET", "/", headers={"Cookie": f"{COOKIE_NAME}={token}"})
        restarted = PanelAuth(self.root)
        self.assertIsNotNone(restarted.session(request))
        with patch("xiaomi_ai_bridge.panel_auth.time.time", return_value=time.time() + 8 * 86400):
            self.assertIsNone(restarted.session(request))
            self.assertEqual((await self.client.get("/api/config")).status, 401)
        await asyncio.to_thread(restarted.set_admin, "fixture-admin", "replacement-password")
        self.assertIsNone(PanelAuth(self.root).session(request))

    async def test_local_gateway_and_authenticated_ingest_keep_independent_policies(self):
        self.assertEqual((await self.client.get("/v1/models", headers={"Authorization": "Bearer anything"})).status, 200)
        self.assertEqual((await self.client.get("/models/")).status, 200)
        response = await self.client.post("/api/hub/events", json={})
        self.assertEqual(response.status, 401)
        self.assertNotIn("panel_auth_required", await response.text())
        self.bridge.ingest_hub_event.assert_called_once()

    async def test_public_host_and_forwarded_gateway_never_inherit_lan_keyless_access(self):
        for path in ["/v1/models", "/models/", "/v1/chat/completions", "/v1/messages", "/v1/responses"]:
            for headers in [{"Host": "hub.example"}, {"CF-Connecting-IP": "192.168.1.2"}, {"X-Forwarded-For": "127.0.0.1"}]:
                method = self.client.get if path.endswith("models") or path.endswith("models/") else self.client.post
                self.assertEqual((await method(path, headers=headers)).status, 401)
        self.bridge.public_models.assert_not_called()

    async def test_cloudflare_https_sets_secure_cookie_and_accepts_same_origin_csrf(self):
        headers = {"Host": "hub.example", "X-Forwarded-Proto": "https", "CF-Connecting-IP": "203.0.113.4", "Origin": "https://hub.example"}
        response = await self.client.post("/api/auth/login", json={"username": "fixture-admin", "password": "fixture-password"}, headers=headers)
        self.assertEqual(response.status, 200)
        self.assertTrue(response.cookies[COOKIE_NAME]["secure"])
        cookie = f"{COOKIE_NAME}={response.cookies[COOKIE_NAME].value}"
        headers["Cookie"] = cookie
        session = await self.client.get("/api/auth/session", headers=headers)
        headers["X-Panel-CSRF"] = (await session.json())["csrf"]
        self.assertEqual((await self.client.put("/api/config", json={}, headers=headers)).status, 200)

    async def test_forged_proxy_headers_from_untrusted_peer_are_ignored(self):
        auth = PanelAuth(self.root, ("10.0.0.0/8",))
        request = make_mocked_request("GET", "/", headers={"Host": "hub.example", "X-Forwarded-Proto": "https", "CF-Connecting-IP": "203.0.113.8"}, transport=Mock(get_extra_info=lambda name, default=None: ("192.168.1.9", 1234) if name == "peername" else default))
        self.assertEqual(auth.scheme(request), "http")
        self.assertEqual(auth.client_address(request), "192.168.1.9")
        self.assertFalse(auth.direct_lan_gateway(request))

    async def test_sse_stops_publishing_after_logout(self):
        await self.login()
        csrf = await self.csrf()
        stream = await self.client.get("/api/hub/stream")
        self.assertIn(b'hub.snapshot', await stream.content.readline())
        await self.client.post("/api/auth/logout", json={}, headers={"X-Panel-CSRF": csrf})
        self.queue.put_nowait({"type": "hub.update", "resource": {"secret": "private-fixture"}})
        body = await asyncio.wait_for(stream.read(), 2)
        self.assertIn(b"panel.auth_required", body)
        self.assertNotIn(b"private-fixture", body)
        self.bridge.hub.unsubscribe.assert_called_once()

    async def test_login_rejects_csrf_bad_types_oversize_and_external_return(self):
        response = await self.client.post("/api/auth/login", json={"username": "fixture-admin", "password": "fixture-password"}, headers={"Origin": "https://malicious.example"})
        self.assertEqual(response.status, 403)
        for body in [[], {"username": [], "password": "text"}, {"username": "a", "password": "a" * 257}]:
            self.assertEqual((await self.client.post("/api/auth/login", json=body)).status, 400)
        self.assertEqual((await self.client.post("/api/auth/login", data="username=value")).status, 415)
        self.assertEqual((await self.client.post("/api/auth/login", data=b" " * 4097, headers={"Content-Type": "application/json"})).status, 413)
        response = await self.login(next="//external.example")
        self.assertEqual((await response.json())["next"], "/")
        for value in ["//external.example", "/\\external.example", "/login?next=//external.example", "https://external.example", "/#chat\r\n"]:
            self.assertEqual(safe_next(value), "/")

    async def test_unconfigured_account_is_closed_by_default(self):
        other = PanelAuth(self.root / "unconfigured")
        self.assertFalse(other.configured)
        with patch("xiaomi_ai_bridge.health.PanelAuth", return_value=other), patch("xiaomi_ai_bridge.health.web.TCPSite") as site:
            site.return_value.start = AsyncMock()
            runner = await start_health_server(SimpleNamespace(), self.bridge, SimpleNamespace(), self.settings, "127.0.0.1", 0)
        self.addAsyncCleanup(runner.cleanup)
        client = TestClient(TestServer(runner.app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        self.assertEqual((await client.get("/api/config")).status, 401)
        self.assertEqual((await client.post("/api/auth/login", json={})).status, 503)

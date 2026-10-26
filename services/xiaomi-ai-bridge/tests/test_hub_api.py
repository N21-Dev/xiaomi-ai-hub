"""Offline HTTP handler contracts; no listeners, credentials or real devices."""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import make_mocked_request

from xiaomi_ai_bridge.health import WEB_DIR, start_health_server


class HubApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hub = SimpleNamespace(
            storage_status=Mock(return_value={"history": {"event_count": 2}}),
            get_resource=Mock(return_value={"resource_id": "pc.hub-admin", "state": {"cpu.usage": 12.3}}),
            history=Mock(return_value={"events": [], "query": {"has_more": False}}),
            subscribe=Mock(),
            unsubscribe=Mock(),
        )
        self.bridge = SimpleNamespace(hub=self.hub, public_hub=Mock(return_value={"resources": []}))
        with patch("xiaomi_ai_bridge.health.web.TCPSite") as site:
            site.return_value.start = AsyncMock()
            self.runner = await start_health_server(
                SimpleNamespace(), self.bridge, SimpleNamespace(), SimpleNamespace(), "127.0.0.1", 0
            )
        self.addAsyncCleanup(self.runner.cleanup)

    async def request(self, path):
        request = make_mocked_request("GET", path, app=self.runner.app)
        match = await self.runner.app.router.resolve(request)
        request._match_info = match
        return await match.handler(request)

    async def test_storage_statistics_are_read_in_worker_thread_without_http_cache(self):
        event_thread = threading.get_ident()
        called_threads = []

        def read_status():
            called_threads.append(threading.get_ident())
            return {"history": {"event_count": 2}}

        self.hub.storage_status.side_effect = read_status
        response = await self.request("/api/hub/storage")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.text)["history"]["event_count"], 2)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotEqual(called_threads, [event_thread])
        self.hub.history.assert_not_called()

    async def test_frontend_revalidates_and_changed_assets_get_new_urls(self):
        first = await self.request('/')
        self.assertEqual(first.headers['Cache-Control'], 'no-cache')
        self.assertNotIn('__ASSET_VERSION__', first.text)
        first_url = re.search(r'src="(/app.js\?v=[^"]+)"', first.text).group(1)
        for path in ['/app.js', '/app.css']:
            self.assertEqual((await self.request(path)).headers['Cache-Control'], 'no-cache')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'index.html').write_text((WEB_DIR/'index.html').read_text())
            (root/'app.js').write_text('// a different synthetic release')
            (root/'app.css').write_text('/* synthetic stylesheet */')
            with patch('xiaomi_ai_bridge.health.WEB_DIR', root), patch('xiaomi_ai_bridge.health.web.TCPSite') as site:
                site.return_value.start = AsyncMock()
                runner = await start_health_server(SimpleNamespace(), self.bridge,
                    SimpleNamespace(), SimpleNamespace(), '127.0.0.1', 0)
                try:
                    request = make_mocked_request('GET', '/', app=runner.app)
                    match = await runner.app.router.resolve(request)
                    request._match_info = match
                    second = await match.handler(request)
                    second_url = re.search(r'src="(/app.js\?v=[^"]+)"', second.text).group(1)
                    self.assertNotEqual(first_url, second_url)
                    self.assertEqual(second.headers['Cache-Control'], 'no-cache')
                finally:
                    await runner.cleanup()

    async def test_unavailable_storage_returns_readable_503(self):
        self.hub.storage_status.side_effect = OSError("fixture failure")
        response = await self.request("/api/hub/storage")
        self.assertEqual(response.status, 503)
        self.assertFalse(json.loads(response.text)["ok"])
        self.assertIn("存储状态暂不可用", json.loads(response.text)["error"])

    async def test_single_resource_read_is_uncached_and_runs_off_event_loop(self):
        event_thread = threading.get_ident()
        called_threads = []

        def read_resource(resource_id):
            called_threads.append(threading.get_ident())
            self.assertEqual(resource_id, "pc.hub-admin")
            return {"resource_id": resource_id, "state": {"cpu.usage": 12.3}}

        self.hub.get_resource.side_effect = read_resource
        response = await self.request("/api/hub/resources/pc.hub-admin")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.text)["resource_id"], "pc.hub-admin")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotEqual(called_threads, [event_thread])

    async def test_single_resource_read_returns_404_for_missing_resource(self):
        self.hub.get_resource.side_effect = ValueError("数据源不存在或未启用")
        response = await self.request("/api/hub/resources/pc.missing")
        self.assertEqual(response.status, 404)
        self.assertFalse(json.loads(response.text)["ok"])

    async def test_history_forwards_chart_projection_and_cursor(self):
        response = await self.request(
            "/api/hub/resources/test.sensor/history?limit=1440&hours=168&projection=chart&before=opaque-cursor"
        )
        self.assertEqual(response.status, 200)
        self.hub.history.assert_called_once_with(
            "test.sensor", 1440, 168, before="opaque-cursor", projection="chart"
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_legacy_history_preserves_full_projection_defaults(self):
        response = await self.request("/api/hub/resources/test.sensor/history")
        self.assertEqual(response.status, 200)
        self.hub.history.assert_called_once_with("test.sensor", 60, None, before=None, projection="full")

    async def test_invalid_projection_is_rejected_before_database_query(self):
        response = await self.request("/api/hub/resources/test.sensor/history?projection=unknown")
        self.assertEqual(response.status, 400)
        self.hub.history.assert_not_called()

    async def test_sse_resync_reads_authoritative_bridge_snapshot(self):
        queue = asyncio.Queue()
        queue.put_nowait({"type": "hub.resync"})
        self.hub.subscribe.return_value = queue
        self.bridge.public_hub.side_effect = [
            {"resources": [], "sampling": {"marker": "initial"}},
            {"resources": [{"resource_id": "test.new"}], "sampling": {"marker": "current"}},
        ]
        stream = SimpleNamespace(prepare=AsyncMock(), write=AsyncMock(side_effect=[None, ConnectionResetError()]))
        with patch("xiaomi_ai_bridge.health.web.StreamResponse", return_value=stream):
            await self.request("/api/hub/stream")
        payload = json.loads(stream.write.call_args_list[1].args[0].decode().removeprefix("data: ").strip())
        self.assertEqual(payload["type"], "hub.snapshot")
        self.assertEqual(payload["hub"]["sampling"]["marker"], "current")
        self.assertEqual(payload["hub"]["resources"][0]["resource_id"], "test.new")
        self.hub.unsubscribe.assert_called_once_with(queue)


if __name__ == "__main__":
    unittest.main()

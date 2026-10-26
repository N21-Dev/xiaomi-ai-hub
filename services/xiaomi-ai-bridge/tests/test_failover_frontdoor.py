"""续篇审查（REVIEW-20261017-FOLLOWUP）的回归测试。

这一组专门盯「故障切换在真实入口上到底通不通」——
原有 test_model_failover 只测了 relay_stream 的 HTTP 状态拒绝，
绕过了 provider pin、连接层失败和上游坏响应这些真实形态。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from xiaomi_ai_bridge.health import gateway_candidates
from xiaomi_ai_bridge.model_gateway import prepare_request, relay_stream
from xiaomi_ai_bridge.model_registry import ModelRegistry


class _FakeModels:
    """只实现网关需要的两个方法，模拟带备用链的注册表。"""

    def __init__(self, fallbacks):
        self._fallbacks = fallbacks

    def fallback_targets(self):
        return self._fallbacks

    def model_settings(self, provider, model):
        return {}


class GatewayCandidatesTest(unittest.TestCase):
    def test_fallback_is_used_even_when_the_caller_pins_a_provider(self):
        primary = {"id": "a", "models": ["m"]}
        backup = {"id": "b", "models": ["n"]}
        models = _FakeModels([(backup, "n")])

        candidates = gateway_candidates(models, primary, "m", {})

        self.assertEqual(
            [(provider["id"], model) for provider, model, _ in candidates],
            [("a", "m"), ("b", "n")],
            "pin 只决定首选渠道，不能把备用链一起关掉",
        )

    def test_primary_is_not_duplicated(self):
        primary = {"id": "a", "models": ["m"]}
        models = _FakeModels([({"id": "a", "models": ["m"]}, "m")])
        self.assertEqual(len(gateway_candidates(models, primary, "m", {})), 1)


class _Server:
    """假上游：要么正常回 SSE，要么在拿到请求后断连/返回坏内容。"""

    def __init__(self, mode: str):
        self.mode = mode
        self.calls = 0
        self.server: TestServer | None = None

    async def __aenter__(self):
        app = web.Application()

        async def handler(request: web.Request):
            self.calls += 1
            if self.mode == "short":
                return web.json_response({"error": {"message": "down"}}, status=503)
            if self.mode == "html":
                return web.Response(
                    status=200, text="<html>not an sse stream</html>",
                    content_type="text/html",
                )
            response = web.StreamResponse(
                status=200, headers={"Content-Type": "text/event-stream"}
            )
            await response.prepare(request)
            payload = json.dumps(
                {"choices": [{"delta": {"content": "ok"}}]}, ensure_ascii=False
            )
            await response.write(f"data: {payload}\n\n".encode())
            await response.write(b"data: [DONE]\n\n")
            await response.write_eof()
            return response

        app.router.add_post("/v1/chat/completions", handler)
        self.server = TestServer(app)
        await self.server.start_server()
        return self

    async def __aexit__(self, *exc):
        if self.server is not None:
            await self.server.close()

    @property
    def provider(self) -> dict:
        return {
            "id": self.mode,
            "name": self.mode,
            "protocol": "openai",
            "base_url": str(self.server.make_url("/v1")),
            "api_key": "test-key",
            "models": ["m"],
            "enabled": True,
        }


async def _relay(candidates, metrics):
    app = web.Application()

    async def handler(request: web.Request):
        body = {
            "model": "",
            "messages": [{"role": "user", "content": "在吗"}],
            "stream": True,
        }
        return await relay_stream(request, candidates, "openai", body, metrics)

    app.router.add_post("/relay", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(str(server.make_url("/relay")), json={}) as response:
                return response.status, await response.text()
    finally:
        await server.close()


class PreHeaderFailoverTest(unittest.IsolatedAsyncioTestCase):
    async def test_connection_refused_falls_over(self):
        """端口没在监听 = 真实世界里最常见的“请求不通”。"""

        async with _Server("ok") as healthy:
            dead = {
                "id": "dead", "name": "dead", "protocol": "openai",
                "base_url": "http://127.0.0.1:9/v1", "api_key": "k",
                "models": ["m"], "enabled": True,
            }
            metrics: dict = {}
            status, text = await _relay(
                [(dead, "m", {}), (healthy.provider, "m", {})], metrics
            )

        self.assertEqual(status, 200)
        self.assertIn("ok", text)
        self.assertTrue(metrics.get("fallback_used"))
        self.assertEqual(healthy.calls, 1)

    async def test_upstream_that_ignores_stream_falls_over(self):
        async with _Server("html") as broken, _Server("ok") as healthy:
            metrics: dict = {}
            status, text = await _relay(
                [(broken.provider, "m", {}), (healthy.provider, "m", {})], metrics
            )

        self.assertEqual(status, 200)
        self.assertIn("ok", text)
        self.assertTrue(metrics.get("fallback_used"))

    async def test_all_candidates_down_reports_failure(self):
        async with _Server("html") as one, _Server("short") as two:
            metrics: dict = {}
            status, _ = await _relay(
                [(one.provider, "m", {}), (two.provider, "m", {})], metrics
            )
        self.assertGreaterEqual(status, 500)
        self.assertEqual(len(metrics.get("fallback_attempts") or []), 2)


class ThinkingBudgetTest(unittest.TestCase):
    """内置助手固定 220 token，开启 Anthropic 思考后不能直接 400。"""

    def _provider(self, settings):
        return {
            "id": "anthropic", "name": "Anthropic", "protocol": "anthropic",
            "base_url": "https://api.anthropic.com/v1", "api_key": "k",
            "models": [{"name": "claude", **settings}], "enabled": True,
        }

    def test_small_client_budget_is_raised_to_fit_thinking(self):
        provider = self._provider(
            {"reasoning_level": "medium", "max_output_tokens": 4096}
        )
        config = {"reasoning_level": "medium", "max_output_tokens": 4096}
        body = {"model": "claude", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 220}

        _, upstream, _, _, _ = prepare_request(provider, "anthropic", body, config)

        self.assertGreater(upstream["max_tokens"], 1024)
        self.assertEqual(upstream["thinking"]["type"], "enabled")
        self.assertLess(upstream["thinking"]["budget_tokens"], upstream["max_tokens"])

    def test_model_without_room_still_reports_error(self):
        provider = self._provider(
            {"reasoning_level": "high", "max_output_tokens": 1024}
        )
        config = {"reasoning_level": "high", "max_output_tokens": 1024}
        body = {"model": "claude", "messages": [{"role": "user", "content": "hi"}]}

        with self.assertRaises(Exception):
            prepare_request(provider, "anthropic", body, config)


class FallbackRevisionTest(unittest.IsolatedAsyncioTestCase):
    """保存备用链后，返回值必须是新的 revision，否则下一次保存必冲突。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup)
        settings = SimpleNamespace(ai_base_url="", ai_api_key="", ai_model="")
        self.registry = ModelRegistry(Path(self.tmp.name), settings)

    async def _cleanup(self):
        self.tmp.cleanup()

    async def test_two_consecutive_saves_succeed(self):
        await self.registry.upsert({
            "id": "main", "name": "主", "protocol": "openai",
            "base_url": "https://main.example.com/v1", "models": ["alpha"],
        })
        await self.registry.upsert({
            "id": "backup", "name": "备", "protocol": "openai",
            "base_url": "https://backup.example.com/v1", "models": ["gamma"],
        })
        await self.registry.activate("main", "alpha")

        first = await self.registry.set_fallback([{"provider_id": "backup", "model": "gamma"}])
        second = await self.registry.set_fallback([], revision=first["revision"])

        self.assertEqual(len(first["fallback"]), 1)
        self.assertEqual(second["fallback"], [])
        self.assertEqual(second["revision"], first["revision"] + 1)


if __name__ == "__main__":
    unittest.main()

"""备用模型链的回归：配置校验、失败切换，以及「什么情况下不该切换」。

设计要点（改动前先读这里）：
- 切换只发生在**上游还没向客户端写出任何字节**之前。一旦开始流式输出，
  失败就只能如实报错，不能重放——否则客户端会收到拼接过的半截内容。
- 客户端自己写错的请求（400/413/422）不切换，换模型也是同样的错，
  只会把真正的报错信息盖掉。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestServer

from xiaomi_ai_bridge.model_gateway import relay_stream, retryable_status
from xiaomi_ai_bridge.model_registry import ModelRegistry


def _provider(provider_id: str, base_url: str, models: list[str]) -> dict:
    return {
        "id": provider_id,
        "name": provider_id,
        "protocol": "openai",
        "base_url": base_url,
        "api_key": "test-key",
        "models": models,
        "enabled": True,
    }


class RetryClassificationTest(unittest.TestCase):
    def test_channel_side_failures_are_retryable(self):
        for status in (401, 402, 403, 404, 429, 500, 502, 503, 504):
            self.assertTrue(retryable_status(status), f"{status} 应该允许切换备用模型")

    def test_client_side_errors_are_not_retryable(self):
        for status in (400, 413, 422):
            self.assertFalse(retryable_status(status), f"{status} 是请求本身的问题，不该换模型")


class RegistryFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup)
        self.settings = SimpleNamespace(ai_base_url="", ai_api_key="", ai_model="")
        self.registry = ModelRegistry(Path(self.tmp.name), self.settings)

    async def _cleanup(self):
        self.tmp.cleanup()

    async def _seed(self):
        await self.registry.upsert({
            "id": "main", "name": "主渠道", "protocol": "openai",
            "base_url": "https://main.example.com/v1", "models": ["alpha", "beta"],
        })
        await self.registry.upsert({
            "id": "backup", "name": "备用渠道", "protocol": "openai",
            "base_url": "https://backup.example.com/v1", "models": ["gamma"],
        })
        await self.registry.activate("main", "alpha")

    async def test_set_and_read_back(self):
        await self._seed()
        result = await self.registry.set_fallback(
            [{"provider_id": "backup", "model": "gamma"}]
        )
        self.assertEqual(result["fallback"][0]["model"], "gamma")
        self.assertTrue(result["fallback"][0]["available"])
        self.assertEqual(result["fallback"][0]["provider_name"], "备用渠道")
        self.assertEqual(
            [model for _, model in self.registry.fallback_targets()], ["gamma"]
        )

    async def test_duplicates_collapse_and_order_is_kept(self):
        await self._seed()
        await self.registry.set_fallback([
            {"provider_id": "backup", "model": "gamma"},
            {"provider_id": "backup", "model": "gamma"},
            {"provider_id": "main", "model": "beta"},
        ])
        self.assertEqual(
            [model for _, model in self.registry.fallback_targets()],
            ["gamma", "beta"],
        )

    async def test_unknown_model_is_rejected(self):
        await self._seed()
        with self.assertRaises(Exception):
            await self.registry.set_fallback([{"provider_id": "backup", "model": "nope"}])

    async def test_disabled_provider_drops_out_of_the_chain(self):
        await self._seed()
        await self.registry.set_fallback([{"provider_id": "backup", "model": "gamma"}])
        await self.registry.upsert({
            "id": "backup", "name": "备用渠道", "protocol": "openai",
            "base_url": "https://backup.example.com/v1", "models": ["gamma"],
            "enabled": False,
        })
        self.assertEqual(self.registry.fallback_targets(), [], "停用的渠道不该还参与兜底")
        view = self.registry.public()["fallback"]
        self.assertFalse(view[0]["available"])

    async def test_chain_is_bounded(self):
        await self._seed()
        with self.assertRaises(Exception):
            await self.registry.set_fallback([
                {"provider_id": "backup", "model": "gamma"},
                {"provider_id": "main", "model": "beta"},
                {"provider_id": "main", "model": "alpha"},
                {"provider_id": "backup", "model": "gamma"},
            ])


class _Upstream:
    """假的 OpenAI 兼容上游：按固定状态码应答，成功时回一段 SSE。"""

    def __init__(self, status: int, content: str = "备用模型已接管"):
        self.status = status
        self.content = content
        self.calls = 0
        self.server: TestServer | None = None

    async def __aenter__(self):
        app = web.Application()

        async def handler(request: web.Request):
            self.calls += 1
            if self.status != 200:
                return web.json_response(
                    {"error": {"message": "上游不可用"}}, status=self.status
                )
            response = web.StreamResponse(
                status=200, headers={"Content-Type": "text/event-stream"}
            )
            await response.prepare(request)
            payload = json.dumps(
                {"choices": [{"delta": {"content": self.content}}]}, ensure_ascii=False
            )
            await response.write(f"data: {payload}\n\n".encode("utf-8"))
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
    def base_url(self) -> str:
        return str(self.server.make_url("/v1"))


async def _relay(candidates, metrics: dict) -> tuple[int, str]:
    """把 relay_stream 挂在一个本地路由上跑，模拟网关被客户端调用。"""

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
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.post(
                str(server.make_url("/relay")), json={"stream": True}
            ) as response:
                text = await response.text()
                return response.status, text
    finally:
        await server.close()


class StreamFailoverTest(unittest.IsolatedAsyncioTestCase):
    async def test_second_model_takes_over_when_first_is_down(self):
        async with _Upstream(503) as broken, _Upstream(200) as healthy:
            candidates = [
                (_provider("down", broken.base_url, ["alpha"]), "alpha", {}),
                (_provider("ok", healthy.base_url, ["gamma"]), "gamma", {}),
            ]
            metrics: dict = {}
            status, text = await _relay(candidates, metrics)

        self.assertEqual(status, 200)
        self.assertIn("备用模型已接管", text)
        self.assertTrue(metrics.get("fallback_used"))
        self.assertEqual(metrics.get("provider_id"), "ok")
        self.assertEqual(metrics.get("fallback_attempts")[0]["status"], 503)

    async def test_quota_failure_also_switches(self):
        async with _Upstream(402) as broken, _Upstream(200) as healthy:
            candidates = [
                (_provider("down", broken.base_url, ["alpha"]), "alpha", {}),
                (_provider("ok", healthy.base_url, ["gamma"]), "gamma", {}),
            ]
            status, text = await _relay(candidates, {})
        self.assertEqual(status, 200)
        self.assertIn("备用模型已接管", text)

    async def test_bad_request_is_reported_not_switched(self):
        async with _Upstream(400) as broken, _Upstream(200) as healthy:
            candidates = [
                (_provider("down", broken.base_url, ["alpha"]), "alpha", {}),
                (_provider("ok", healthy.base_url, ["gamma"]), "gamma", {}),
            ]
            metrics: dict = {}
            status, text = await _relay(candidates, metrics)

        self.assertEqual(status, 400, "请求本身有问题时不能偷偷换模型")
        self.assertIn("上游不可用", json.loads(text)["error"]["message"])
        self.assertEqual(healthy.calls, 0, "备用模型不该被调用")

    async def test_last_failure_is_surfaced(self):
        async with _Upstream(503) as one, _Upstream(500) as two:
            candidates = [
                (_provider("a", one.base_url, ["alpha"]), "alpha", {}),
                (_provider("b", two.base_url, ["gamma"]), "gamma", {}),
            ]
            metrics: dict = {}
            status, _ = await _relay(candidates, metrics)
        self.assertEqual(status, 500, "全部失败时应返回最后一次的真实状态")
        self.assertEqual(len(metrics.get("fallback_attempts") or []), 2)


if __name__ == "__main__":
    unittest.main()

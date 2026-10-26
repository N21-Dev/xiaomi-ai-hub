from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace

from xiaomi_ai_bridge.ai import AIClient


class _Response:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def json(self, **_):
        return {"choices": [{"message": {"content": "收到"}}]}

    async def read(self):
        return b""

    async def text(self):
        return ""


class _Session:
    def __init__(self):
        self.closed = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.request_count = 0

    def post(self, *_args, **_kwargs):
        session = self

        class _Request:
            async def __aenter__(self):
                session.request_count += 1
                session.started.set()
                await session.release.wait()
                return _Response()

            async def __aexit__(self, *_):
                return False

        return _Request()

    async def close(self):
        self.closed = True


def _settings():
    return SimpleNamespace(
        ai_base_url="http://example.test/v1",
        ai_api_key="",
        ai_model="test-model",
        ai_configured=True,
        ai_disable_reasoning=False,
        ai_timeout_seconds=90.0,
        ai_temperature=0.2,
        ai_max_tokens=200,
        ai_panel_max_tokens=4096,
        system_prompt="测试助手",
    )


class PayloadBudgetTests(unittest.IsolatedAsyncioTestCase):
    """控制台文字入口与语音入口使用各自独立的输出预算。"""

    def test_panel_surface_uses_the_panel_budget(self):
        client = AIClient(_settings())
        payload = client.build_messages_payload(
            [{"role": "user", "content": "写一段长分析"}], surface="panel"
        )
        self.assertEqual(payload["max_tokens"], 4096)

    def test_speaker_surface_keeps_the_read_aloud_budget(self):
        client = AIClient(_settings())
        payload = client.build_messages_payload([{"role": "user", "content": "现在几点"}])
        self.assertEqual(payload["max_tokens"], 200)
        self.assertEqual(client.build_messages_payload([], surface="speaker")["max_tokens"], 200)

    def test_panel_budget_follows_the_env_override(self):
        # patch.dict 保证测试结束后进程环境原样恢复，不把夹具值泄漏给其它用例。
        from unittest.mock import patch

        from xiaomi_ai_bridge.config import Settings

        env = {"AI_PANEL_MAX_TOKENS": "16384", "MI_USER_ID": "fixture", "MI_PASS_TOKEN": "fixture"}
        with patch.dict(os.environ, env):
            settings = Settings.from_env()
        self.assertEqual(settings.ai_panel_max_tokens, 16384)


class AIClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_inference_is_not_serialized_by_lifecycle_lock(self):
        client = AIClient(_settings())
        session = _Session()
        client.session = session
        requests = [asyncio.create_task(client.ask("synthetic", [])) for _ in range(2)]
        try:
            for _ in range(10):
                await asyncio.sleep(0)
                if session.request_count == 2:
                    break
            self.assertEqual(session.request_count, 2)
        finally:
            session.release.set()
            await asyncio.gather(*requests)
            await client.close()

    async def test_turn_lease_keeps_retired_client_open_between_tool_rounds(self):
        client = AIClient(_settings())
        session = _Session()
        session.release.set()
        client.session = session
        async with client.lease():
            self.assertEqual(await client.ask("first", []), "收到")
            closing = asyncio.create_task(client.close())
            await asyncio.sleep(0)
            self.assertFalse(closing.done())
            self.assertEqual(await client.ask("second", []), "收到")
            self.assertFalse(session.closed)
        await closing
        self.assertTrue(session.closed)

    async def test_close_waits_for_an_inflight_request(self):
        client = AIClient(_settings())
        session = _Session()
        client.session = session

        request = asyncio.create_task(client.ask("你好", []))
        await session.started.wait()
        closing = asyncio.create_task(client.close())
        await asyncio.sleep(0)
        self.assertFalse(closing.done(), "close must not invalidate an active request")
        session.release.set()

        self.assertEqual(await request, "收到")
        await closing
        self.assertIsNone(client.session)
        self.assertTrue(session.closed)


if __name__ == "__main__":
    unittest.main()

"""Real registry/HTTP handlers; upstream and device I/O are always simulated."""

import asyncio
import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import make_mocked_request

from xiaomi_ai_bridge.ai import AIClient, AIResult, ToolCall
from xiaomi_ai_bridge.bridge import Bridge
from xiaomi_ai_bridge.conversations import ConversationStore
from xiaomi_ai_bridge.health import start_health_server
from xiaomi_ai_bridge.model_registry import ModelRegistry
from test_ai_client import _Session


@dataclass(frozen=True)
class Settings:
    ai_base_url: str = ""
    ai_api_key: str = ""
    ai_model: str = ""
    ai_gateway_enabled: bool = True
    ai_gateway_auth_required: bool = False
    health_port: int = 8080
    ai_disable_reasoning: bool = True
    ai_timeout_seconds: float = 90.0
    ai_temperature: float = 0.2
    ai_max_tokens: int = 200
    system_prompt: str = "Synthetic assistant"

    @property
    def ai_configured(self):
        return bool(self.ai_base_url and self.ai_model)


class ModelSwitchingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        bridge = self.bridge = object.__new__(Bridge)
        bridge.settings = Settings()
        bridge.models = ModelRegistry(Path(self.temp.name), bridge.settings)
        for key in ("a", "b"):
            await bridge.models.upsert({"id": key, "name": key, "base_url": "https://example.test/v1",
                                        "protocol": "openai", "models": ["shared-model", "nvidia/nemotron-3-super"]})
        await bridge.models.activate("a", "shared-model")
        bridge._refresh_gateway_model()
        bridge.ai = AIClient(bridge.settings, provider_id=bridge._gateway_provider_id)
        bridge._retired_ai_tasks = set()
        bridge.conversations = ConversationStore()
        bridge.status = SimpleNamespace(ai_configured=True, last_ai_latency_ms=0)
        bridge.model_usage = SimpleNamespace(record_request=Mock())
        with patch("xiaomi_ai_bridge.health.web.TCPSite") as site:
            site.return_value.start = AsyncMock()
            self.runner = await start_health_server(
                SimpleNamespace(), bridge, SimpleNamespace(), SimpleNamespace(), "127.0.0.1", 0,
            )
        self.addAsyncCleanup(self.cleanup_clients)

    async def cleanup_clients(self):
        await self.bridge.ai.close()
        await asyncio.gather(*self.bridge._retired_ai_tasks)
        await self.runner.cleanup()

    async def handler(self, path, body, headers=None):
        request = make_mocked_request("POST", path, headers=headers or {}, app=self.runner.app)
        request.json = AsyncMock(return_value=body)
        match = await self.runner.app.router.resolve(request)
        request._match_info = match
        return await match.handler(request)

    async def test_real_handler_rejects_stale_revision_and_preserves_current_route(self):
        old_revision = self.bridge.models.public()["revision"]
        await self.bridge.models.activate("a", "shared-model")
        response = await self.handler("/api/models/b/activate", {"model": "shared-model", "revision": old_revision})
        self.assertEqual(response.status, 409)
        self.assertEqual(json.loads(response.text)["code"], "revision_conflict")
        self.assertEqual(self.bridge.models.active()[0]["id"], "a")

    async def test_model_switch_returns_while_old_inference_is_pending(self):
        old = self.bridge.ai
        session = _Session()
        old.session = session
        request = asyncio.create_task(old.ask("synthetic input", []))
        await session.started.wait()
        try:
            response = await asyncio.wait_for(self.handler("/api/models/b/activate", {
                "model": "shared-model", "revision": self.bridge.models.public()["revision"],
            }), timeout=1)
            self.assertEqual(response.status, 200)
            self.assertFalse(request.done())
            self.assertFalse(session.closed)
            self.assertEqual(self.bridge.ai.provider_id, "b")
        finally:
            session.release.set()
            await request
            await asyncio.gather(*self.bridge._retired_ai_tasks)
        self.assertTrue(session.closed)

    async def test_speaker_role_pins_provider_while_preserving_concrete_model_behavior(self):
        await self.bridge.set_model_role("speaker", "b", "nvidia/nemotron-3-super")
        self.assertEqual(self.bridge.ai.provider_id, "b")
        payload = self.bridge.ai.build_payload("synthetic input", [])
        self.assertEqual(payload["model"], "nvidia/nemotron-3-super")
        self.assertTrue(payload["messages"][0]["content"].startswith("/no_think"))
        # Default provider also offers this name; the pinned route must win.
        response_bytes = b'{"choices":[{"message":{"content":"ok"}}]}'
        with patch("xiaomi_ai_bridge.health.forward_request", AsyncMock(return_value=(200, {}, response_bytes))) as forwarded:
            response = await self.handler("/v1/chat/completions", payload, {"X-AI-Gateway-Provider": "b"})
        self.assertEqual(response.status, 200)
        self.assertEqual(forwarded.await_args.args[1]["id"], "b")
        await self.bridge.set_model_role("speaker", "", "")
        self.assertEqual(self.bridge.ai.provider_id, "a")

    async def test_pinned_provider_cannot_fall_back_to_another_channel(self):
        with self.assertRaises(ValueError):
            self.bridge.models.resolve("shared-model", provider_id="missing")
        with self.assertRaises(ValueError):
            self.bridge.models.resolve("missing-model", provider_id="b")

    async def test_removing_role_model_refreshes_assistant_to_default_provider(self):
        await self.bridge.set_model_role("speaker", "b", "shared-model")
        await self.bridge.remove_models("b", ["shared-model"])
        self.assertEqual(self.bridge.ai.provider_id, "a")
        self.assertEqual(self.bridge.ai.settings.ai_model, "shared-model")

    async def test_disabling_role_provider_refreshes_assistant_to_default(self):
        await self.bridge.set_model_role("speaker", "b", "shared-model")
        await self.bridge.save_model({"enabled": False}, "b")
        self.assertEqual(self.bridge.ai.provider_id, "a")

    async def test_removing_active_provider_uses_replacement_without_stale_pin(self):
        await self.bridge.remove_model("a", replacement={"provider_id": "b", "model": "shared-model"})
        self.assertEqual(self.bridge.ai.provider_id, "b")
        self.assertTrue(self.bridge.status.ai_configured)

    async def test_deactivating_last_route_reports_unconfigured(self):
        await self.bridge.remove_model("a", deactivate=True)
        self.assertEqual(self.bridge.ai.settings.ai_model, "")
        self.assertFalse(self.bridge.status.ai_configured)

    async def test_tool_turn_keeps_same_client_after_default_switch(self):
        old = self.bridge.ai
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def completion(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await release.wait()
                return AIResult(content="", tool_calls=(ToolCall("synthetic-call", "unknown-tool", {}),))
            return AIResult(content="old client completed this turn")

        old.complete = AsyncMock(side_effect=completion)
        for name in ("home", "hub", "health_source", "infrared"):
            setattr(self.bridge, name, SimpleNamespace(tool_definitions=lambda: [], devices={}))
        self.bridge.layout = SimpleNamespace(public=lambda: {"assignments": {}})
        turn = asyncio.create_task(self.bridge._answer_with_tools("synthetic input", []))
        await started.wait()
        await self.bridge.activate_model("b", "shared-model")
        self.bridge.ai.complete = AsyncMock(return_value=AIResult(content="new client"))
        release.set()
        self.assertEqual(await turn, "old client completed this turn")
        self.assertEqual(old.complete.await_count, 2)
        self.bridge.ai.complete.assert_not_awaited()

    async def test_client_sends_the_selected_provider_with_concrete_model(self):
        await self.bridge.set_model_role("speaker", "b", "shared-model")
        session = _Session()
        session.release.set()
        self.bridge.ai.session = session
        with patch.object(session, "post", wraps=session.post) as post:
            await self.bridge.ai.ask("synthetic input", [])
        self.assertEqual(post.call_args.kwargs["headers"]["X-AI-Gateway-Provider"], "b")
        self.assertEqual(post.call_args.kwargs["json"]["model"], "shared-model")

    async def test_http_chat_and_clear_preserve_conversation_id(self):
        self.bridge._answer_with_tools = AsyncMock(return_value="synthetic reply")
        a, b = "terminal-a-session-1234", "terminal-b-session-1234"
        for key in (a, b):
            response = await self.handler("/api/actions/chat", {"prompt": key, "conversation_id": key})
            self.assertEqual(json.loads(response.text)["conversation_id"], key)
        response = await self.handler("/api/actions/clear-context", {"conversation_id": a})
        self.assertEqual(response.status, 200)
        self.assertFalse(self.bridge.conversations.entries[a].history)
        self.assertTrue(self.bridge.conversations.entries[b].history)
        response = await self.handler("/api/actions/chat", {"prompt": "hello", "conversation_id": "short"})
        self.assertEqual(response.status, 400)

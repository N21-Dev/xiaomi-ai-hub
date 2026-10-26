from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import aiohttp

from xiaomi_ai_bridge.model_registry import ModelRegistry
from xiaomi_ai_bridge.model_gateway import _anthropic_to_openai, _openai_to_anthropic
from xiaomi_ai_bridge.protocols import anthropic_to_chat, chat_to_anthropic
from xiaomi_ai_bridge.protocol_stream import translate_stream
from xiaomi_ai_bridge.health_source import HEALTH_TOOL_NAME, HealthSource


class ModelGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup)
        self.settings = SimpleNamespace(ai_base_url="", ai_api_key="", ai_model="")
        self.registry = ModelRegistry(Path(self.tmp.name), self.settings)

    async def _cleanup(self):
        self.tmp.cleanup()

    async def test_provider_keys_are_not_in_public_view_and_activation_is_atomic(self):
        public = await self.registry.upsert({
            "id": "anthropic-main", "name": "Anthropic 主账号", "protocol": "anthropic",
            "base_url": "https://api.anthropic.com/v1", "api_key": "secret-provider-key",
            "models": ["claude-sonnet"],
        })
        self.assertTrue(public["providers"][0]["api_key_set"])
        self.assertNotIn("secret-provider-key", json.dumps(public, ensure_ascii=False))
        activated = await self.registry.activate("anthropic-main", "claude-sonnet")
        self.assertEqual(activated["active"], {"provider_id": "anthropic-main", "model": "claude-sonnet"})
        self.assertEqual(self.registry.active()[1], "claude-sonnet")
        self.assertEqual((Path(self.tmp.name) / "ai-models.json").stat().st_mode & 0o777, 0o600)

    async def test_gateway_can_run_lan_no_auth_mode_explicitly(self):
        self.assertTrue(ModelRegistry(Path(self.tmp.name), SimpleNamespace(ai_base_url="", ai_api_key="", ai_model="", ai_gateway_auth_required=False)).authorized("anything"))

    async def _seed_two_models(self):
        await self.registry.upsert({
            "id": "main", "name": "主渠道", "protocol": "openai",
            "base_url": "https://api.example.com/v1", "models": ["alpha", "beta"],
        })
        await self.registry.activate("main", "alpha")

    async def test_the_speaker_alias_starts_out_following_the_default_model(self):
        await self._seed_two_models()
        provider, model = self.registry.resolve("speaker-active")
        self.assertEqual((provider["id"], model), ("main", "alpha"))

    async def test_health_is_no_longer_a_role(self):
        """Health became a tool of the single assistant, not a separate role."""

        await self._seed_two_models()
        self.assertNotIn("health", self.registry.roles())
        with self.assertRaises(ValueError):
            await self.registry.set_role("health", "main", "alpha")
        # The retired alias must not silently resolve to something else.
        with self.assertRaises(ValueError):
            self.registry.resolve("health-active")

    async def test_the_speaker_can_point_at_its_own_model(self):
        await self._seed_two_models()
        public = await self.registry.set_role("speaker", "main", "beta")
        self.assertEqual(public["roles"]["speaker"], {"provider_id": "main", "model": "beta"})
        self.assertEqual(self.registry.resolve("speaker-active")[1], "beta")
        # The default model must be untouched.
        self.assertEqual(self.registry.resolve("hub-active")[1], "alpha")

    async def test_clearing_a_role_sends_it_back_to_the_default_model(self):
        await self._seed_two_models()
        await self.registry.set_role("speaker", "main", "beta")
        public = await self.registry.set_role("speaker", "", "")
        self.assertEqual(public["roles"]["speaker"], {"provider_id": "", "model": ""})
        self.assertEqual(self.registry.resolve("speaker-active")[1], "alpha")

    async def test_a_stale_role_falls_back_instead_of_failing_requests(self):
        """A role pointing at a deleted model must not break its assistant."""

        await self._seed_two_models()
        await self.registry.set_role("speaker", "main", "beta")
        await self.registry.remove_models("main", ["beta"])
        self.assertEqual(self.registry.resolve("speaker-active")[1], "alpha")

    async def test_unknown_role_is_rejected(self):
        await self._seed_two_models()
        with self.assertRaises(ValueError):
            await self.registry.set_role("nobody", "main", "alpha")

    async def test_published_gateway_document_reports_the_real_auth_mode(self):
        """The panel derives its wording from this document, so it cannot be hard-coded."""

        def gateway(*, auth_required):
            registry = ModelRegistry(
                Path(self.tmp.name),
                SimpleNamespace(ai_base_url="", ai_api_key="", ai_model="", ai_gateway_auth_required=auth_required),
            )
            return registry.public()["gateway"]

        protected = gateway(auth_required=True)
        self.assertIs(protected["auth_required"], True)
        self.assertIs(protected["token_required"], True)

        keyless = gateway(auth_required=False)
        self.assertIs(keyless["auth_required"], False)
        # Regression: token_required used to be hard-coded True, so a keyless
        # deployment got displayed — and documented — as token-protected.
        self.assertIs(keyless["token_required"], False)

    async def test_gateway_document_defaults_to_requiring_a_token(self):
        gateway = ModelRegistry(Path(self.tmp.name), self.settings).public()["gateway"]
        self.assertIs(gateway["auth_required"], True)
        self.assertIs(gateway["token_required"], True)

    async def test_auth_flag_governs_enforcement_and_the_published_document_together(self):
        """One flag must drive both the check and what the page is told."""

        for auth_required in (True, False):
            registry = ModelRegistry(
                Path(self.tmp.name),
                SimpleNamespace(ai_base_url="", ai_api_key="", ai_model="", ai_gateway_auth_required=auth_required),
            )
            self.assertIs(registry.auth_required(), auth_required)
            self.assertEqual(registry.authorized(""), not auth_required)
            self.assertEqual(registry.authorized("Bearer wrong-token"), not auth_required)
            self.assertIs(registry.public()["gateway"]["token_required"], auth_required)

    async def test_model_settings_round_trip(self):
        await self.registry.upsert({"id": "openai", "name": "OpenAI", "protocol": "openai", "base_url": "https://example.com/v1", "models": ["vision-model"]})
        public = await self.registry.update_model_settings("openai", "vision-model", {"reasoning_level": "high", "context_window": 200000, "max_output_tokens": 16000, "multimodal": True, "temperature": 0.2})
        settings = next(item for item in public["providers"][0]["model_settings"] if item["model"] == "vision-model")
        self.assertEqual(settings["reasoning_level"], "high")
        self.assertEqual(settings["context_window"], 200000)
        self.assertTrue(settings["multimodal"])

    async def test_gateway_resolves_imported_model_to_its_provider(self):
        await self.registry.upsert({"id": "openai", "name": "OpenAI", "protocol": "openai", "base_url": "https://openai.example/v1", "models": ["gpt-a"]})
        await self.registry.upsert({"id": "anthropic", "name": "Anthropic", "protocol": "anthropic", "base_url": "https://anthropic.example/v1", "models": ["claude-a"]})
        await self.registry.activate("openai", "gpt-a")
        provider, model = self.registry.resolve("claude-a")
        self.assertEqual(provider["id"], "anthropic")
        self.assertEqual(model, "claude-a")
        provider, model = self.registry.resolve("hub-active")
        self.assertEqual((provider["id"], model), ("openai", "gpt-a"))

    async def test_openai_and_anthropic_message_shapes_are_converted(self):
        anthropic = _openai_to_anthropic({
            "model": "claude-sonnet", "max_tokens": 100,
            "messages": [{"role": "system", "content": "be concise"}, {"role": "user", "content": "hi"}],
        }, "claude-sonnet")
        self.assertEqual(anthropic["system"], "be concise")
        self.assertEqual(anthropic["messages"][0]["role"], "user")
        openai = _anthropic_to_openai({
            "model": "gpt", "max_tokens": 100, "system": "be concise",
            "messages": [{"role": "user", "content": "hi"}],
        }, "gpt")
        self.assertEqual(openai["messages"][0], {"role": "system", "content": "be concise"})
        self.assertEqual(openai["messages"][1]["role"], "user")

    async def test_anthropic_context_management_is_dropped_not_rejected(self):
        """Anthropic's context-editing hint has no OpenAI equivalent.

        It used to fail the whole call with "Cannot translate these fields
        across protocols", breaking any client that sends it by default.
        """

        openai = _anthropic_to_openai({
            "model": "gpt", "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
            "context_management": {"edits": [{"type": "clear_tool_uses_20250919"}]},
        }, "gpt")
        self.assertEqual(openai["messages"][0], {"role": "user", "content": "hi"})
        self.assertNotIn("context_management", openai)

    async def test_unknown_anthropic_fields_are_still_rejected(self):
        """Only the explicit ignorable list is dropped — typos must not slip by."""

        with self.assertRaises(ValueError):
            _anthropic_to_openai({
                "model": "gpt", "max_tokens": 100,
                "messages": [{"role": "user", "content": "hi"}],
                "definitely_not_a_real_field": 1,
            }, "gpt")

    async def test_json_object_response_format_becomes_a_system_hint(self):
        """Anthropic has no bare "valid JSON" switch; it must not fail the call."""

        result = chat_to_anthropic({
            "model": "x", "max_tokens": 50, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": "你是分析助手"},
                         {"role": "user", "content": "输出 JSON"}],
        }, "claude")
        self.assertIn("你是分析助手", result["system"])
        self.assertIn("JSON", result["system"])
        self.assertNotIn("response_format", result)

        # json_schema still maps to the native structured-output field.
        strict = chat_to_anthropic({
            "model": "x", "max_tokens": 50,
            "response_format": {"type": "json_schema", "json_schema": {"schema": {"type": "object"}}},
            "messages": [{"role": "user", "content": "hi"}],
        }, "claude")
        self.assertEqual(strict["output_config"]["format"]["type"], "json_schema")

    async def test_empty_messages_are_rejected_in_every_direction(self):
        """A silently forwarded empty array would only surface as an upstream error."""

        with self.assertRaises(ValueError):
            anthropic_to_chat({"model": "x", "max_tokens": 10, "messages": []}, "gpt")
        with self.assertRaises(ValueError):
            chat_to_anthropic({"model": "x", "max_tokens": 10, "messages": []}, "claude")
        # System-only is not a conversation either.
        with self.assertRaises(ValueError):
            chat_to_anthropic({
                "model": "x", "max_tokens": 10,
                "messages": [{"role": "system", "content": "s"}],
            }, "claude")

    async def test_round_trip_preserves_core_fields(self):
        """Anthropic → Chat → Anthropic must not quietly lose settings."""

        original = {
            "model": "x", "max_tokens": 100, "system": "简洁",
            "temperature": 0.3, "stop_sequences": ["STOP"],
            "tools": [{"name": "t", "description": "d", "input_schema": {"type": "object"}}],
            "messages": [{"role": "user", "content": "问"},
                         {"role": "assistant", "content": [{"type": "text", "text": "答"}]},
                         {"role": "user", "content": "再问"}],
        }
        back = chat_to_anthropic(anthropic_to_chat(dict(original), "gpt"), "x")
        self.assertEqual(back["system"], "简洁")
        self.assertEqual(back["max_tokens"], 100)
        self.assertEqual(back["temperature"], 0.3)
        self.assertEqual(back["stop_sequences"], ["STOP"])
        self.assertEqual(len(back["messages"]), len(original["messages"]))
        self.assertEqual(back["tools"][0]["name"], "t")

    async def test_streaming_tool_arguments_are_reassembled(self):
        """Tool arguments arrive in fragments and must be spliced back together."""

        class FakeContent:
            async def iter_chunked(self, size):
                for line in (
                    'data: {"id":"1","choices":[{"index":0,"delta":{"role":"assistant","content":"你"}}]}',
                    'data: {"id":"1","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"c1","type":"function","function":{"name":"t","arguments":""}}]}}]}',
                    'data: {"id":"1","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"city\\""}}]}}]}',
                    'data: {"id":"1","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":":\\"北京\\"}"}}]}}]}',
                    'data: {"id":"1","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}',
                    "data: [DONE]",
                ):
                    yield (line + "\n\n").encode()

        out = []
        async for payload in translate_stream(FakeContent(), "openai", "anthropic", "m", {}):
            out.append(payload)
        text = b"".join(out).decode()
        self.assertIn("message_start", text)
        self.assertIn("message_stop", text)
        self.assertIn("input_json_delta", text)
        self.assertIn("text_delta", text)
        self.assertIn("tool_use", text)
        self.assertIn("北京", text)

    async def test_invalid_provider_protocol_and_url_are_rejected(self):
        with self.assertRaises(ValueError):
            await self.registry.upsert({"id": "bad", "protocol": "gemini", "base_url": "https://example.com", "models": ["x"]})
        with self.assertRaises(ValueError):
            await self.registry.upsert({"id": "bad", "protocol": "openai", "base_url": "file:///tmp/key", "models": ["x"]})

    # ------------------------------------------------------------ 按提供方走代理

    async def test_provider_proxy_is_validated(self):
        """只有 http/https 的纯主机地址能当上游代理：aiohttp 原生不认 socks，
        带路径的地址会静默改语义，都要在保存时就拒掉。"""
        for bad in ("socks5://127.0.0.1:1080", "file:///tmp/sock", "127.0.0.1:7890",
                    "http://", "http://127.0.0.1:7890/some/path", "http://127.0.0.1:7890?a=b"):
            with self.subTest(proxy=bad), self.assertRaises(ValueError):
                await self.registry.upsert({
                    "id": "p", "name": "p", "protocol": "openai",
                    "base_url": "https://api.example.com/v1", "models": ["alpha"], "proxy": bad,
                })
        public = await self.registry.upsert({
            "id": "p", "name": "p", "protocol": "openai",
            "base_url": "https://api.example.com/v1", "models": ["alpha"],
            "proxy": "  http://172.28.0.1:17892  ",
        })
        # 首尾空白会被去掉后再存
        stored = json.loads((Path(self.tmp.name) / "ai-models.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["providers"][0]["proxy"], "http://172.28.0.1:17892")
        self.assertTrue(public["providers"][0]["proxy_set"])
        self.assertEqual(public["providers"][0]["proxy_display"], "http://172.28.0.1:17892")

    async def test_proxy_credentials_are_never_echoed_to_the_panel(self):
        public = await self.registry.upsert({
            "id": "p", "name": "p", "protocol": "openai",
            "base_url": "https://api.example.com/v1", "models": ["alpha"],
            "proxy": "http://relay-user:relay-pass@10.0.0.1:7890",
        })
        row = public["providers"][0]
        self.assertTrue(row["proxy_set"])
        self.assertTrue(row["proxy_auth_set"])
        self.assertEqual(row["proxy_display"], "http://10.0.0.1:7890")
        self.assertNotIn("relay-pass", json.dumps(public, ensure_ascii=False))
        self.assertNotIn("relay-user", json.dumps(public, ensure_ascii=False))

    async def test_masked_proxy_echo_keeps_the_stored_credentials(self):
        """面板只拿到脱敏代理。它原样回传时必须当作「没改」，
        否则用户随手保存一次就把代理凭据洗掉了。"""
        await self.registry.upsert({
            "id": "p", "name": "p", "protocol": "openai",
            "base_url": "https://api.example.com/v1", "models": ["alpha"],
            "proxy": "http://relay-user:relay-pass@10.0.0.1:7890",
        })
        await self.registry.upsert({
            "id": "p", "name": "p", "protocol": "openai",
            "base_url": "https://api.example.com/v1", "models": ["alpha"],
            "proxy": "http://10.0.0.1:7890",  # ← 面板回传的脱敏值
        })
        stored = json.loads((Path(self.tmp.name) / "ai-models.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["providers"][0]["proxy"], "http://relay-user:relay-pass@10.0.0.1:7890")

        # 真的改地址才覆盖
        await self.registry.upsert({
            "id": "p", "name": "p", "protocol": "openai",
            "base_url": "https://api.example.com/v1", "models": ["alpha"],
            "proxy": "http://10.0.0.2:7891",
        })
        stored = json.loads((Path(self.tmp.name) / "ai-models.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["providers"][0]["proxy"], "http://10.0.0.2:7891")

        # 清空 = 改回直连
        public = await self.registry.upsert({
            "id": "p", "name": "p", "protocol": "openai",
            "base_url": "https://api.example.com/v1", "models": ["alpha"], "proxy": "",
        })
        self.assertFalse(public["providers"][0]["proxy_set"])
        self.assertEqual(public["providers"][0]["proxy_display"], "")

    async def test_only_the_provider_with_a_proxy_is_sent_through_it(self):
        """核心承诺：配了 proxy 的上游走代理，没配的必须传 None（直连）。"""
        from xiaomi_ai_bridge.model_gateway import open_upstream

        class FakeResponse:
            def __init__(self, status):
                self.status = status
                self.headers = {}
                self.released = False

            def release(self):
                self.released = True

        class FakeSession:
            def __init__(self, statuses):
                self.statuses = list(statuses)
                self.calls = []

            async def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return FakeResponse(self.statuses.pop(0))

        proxied = {"id": "q", "protocol": "openai", "base_url": "https://api.quickrouter.ai/v1",
                   "api_key": "k", "proxy": "http://host.docker.internal:17892"}
        direct = {"id": "d", "protocol": "openai", "base_url": "https://api.deepseek.com/v1", "api_key": "k"}
        chat = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}

        session = FakeSession([200])
        async with open_upstream(session, proxied, "openai", chat) as (resp, _, _):
            self.assertEqual(resp.status, 200)
        self.assertEqual(session.calls[0][1]["proxy"], "http://host.docker.internal:17892")

        session = FakeSession([200])
        async with open_upstream(session, direct, "openai", chat):
            pass
        self.assertIsNone(session.calls[0][1]["proxy"])

        # Responses 入口撞上 404 会降级成 Chat 再发一次，第二次也必须带上代理
        session = FakeSession([404, 200])
        async with open_upstream(session, proxied, "responses",
                                 {"model": "m", "input": "hi"}) as (_, _, warnings):
            self.assertIn("responses-adapted-via-chat", warnings)
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(all(call[1]["proxy"] == "http://host.docker.internal:17892"
                            for call in session.calls))

    async def test_discovery_uses_the_provider_proxy(self):
        """「自动获取模型」也要走代理，否则配了代理的供应商光拉目录就失败。"""
        seen = {}

        class FakeContent:
            async def iter_chunked(self, size):
                yield json.dumps({"data": [{"id": "alpha"}, {"id": "beta"}]}).encode()

        class FakeResponse:
            status = 200
            content = FakeContent()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def get(self, url, **kwargs):
                seen.update(kwargs, url=url)
                return FakeResponse()

        await self.registry.upsert({
            "id": "q", "name": "q", "protocol": "openai",
            "base_url": "https://api.quickrouter.ai/v1", "models": [],
            "proxy": "http://host.docker.internal:17892",
        })
        with mock.patch.object(aiohttp, "ClientSession", lambda *a, **kw: FakeSession()):
            result = await self.registry.discover("q")

        self.assertEqual(seen["proxy"], "http://host.docker.internal:17892")
        self.assertTrue(seen["url"].endswith("/models"))
        self.assertEqual(result["discovery"]["added"], 2)

    async def test_health_tool_is_explicitly_gated_and_read_only(self):
        disabled = HealthSource(SimpleNamespace(health_service_url="https://health.local", health_web_url="", health_api_key_file=None, health_poll_seconds=300, health_ai_tools_enabled=False))
        self.assertEqual(disabled.tool_definitions(), [])
        enabled = HealthSource(SimpleNamespace(health_service_url="https://health.local", health_web_url="", health_api_key_file=None, health_poll_seconds=300, health_ai_tools_enabled=True))
        self.assertEqual(enabled.tool_definitions()[0]["function"]["name"], HEALTH_TOOL_NAME)
        enabled.snapshot = lambda **kwargs: asyncio.sleep(0, result={"available": True, "selected_date": "2026-10-07", "latest_data_date": "2026-10-07", "series": [], "resources": [], "message": "ok"})
        result = await enabled.execute(HEALTH_TOOL_NAME, {"query": "summary", "days": 7})
        self.assertTrue(result["ok"])
        self.assertTrue(result["read_only"])


if __name__ == "__main__":
    unittest.main()

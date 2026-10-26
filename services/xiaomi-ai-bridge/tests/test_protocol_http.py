from __future__ import annotations
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from xiaomi_ai_bridge.health import start_health_server
from xiaomi_ai_bridge.protocol_stream import events, sse
from test_protocols import Content, chunk
import test_protocols


class ProtocolHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.status = 200
        self.fail_stream = False
        self.tool_mode = False
        self.responses_missing = False
        self.logged = asyncio.Event()
        self.slow_stream = False
        self.upstream_closed = asyncio.Event()
        self.provider = {
            "id": "mock",
            "protocol": "openai",
            "api_key": "private-upstream-key",
        }
        upstream = web.Application()
        upstream.router.add_post("/v1/chat/completions", self.upstream)
        upstream.router.add_post("/v1/messages", self.upstream)
        upstream.router.add_post("/v1/responses", self.upstream)
        self.upstream_server = TestServer(upstream)
        await self.upstream_server.start_server()
        self.addAsyncCleanup(self.upstream_server.close)
        self.provider["base_url"] = str(self.upstream_server.make_url("/v1"))
        self.bridge = SimpleNamespace(
            models=SimpleNamespace(
                authorized=lambda h: h == "Bearer test-key",
                resolve=lambda name: (self.provider, "model"),
                model_settings=lambda *args: {},
            ),
            model_usage=SimpleNamespace(
                record_request=Mock(side_effect=lambda **kwargs: self.logged.set())
            ),
            public_models=Mock(return_value={"providers": []}),
        )
        with patch("xiaomi_ai_bridge.health.web.TCPSite") as site:
            site.return_value.start = AsyncMock()
            self.runner = await start_health_server(
                SimpleNamespace(),
                self.bridge,
                SimpleNamespace(),
                SimpleNamespace(),
                "127.0.0.1",
                0,
            )
        self.client = TestClient(TestServer(self.runner.app))
        await self.client.start_server()
        self.addAsyncCleanup(self.runner.cleanup)
        self.addAsyncCleanup(self.client.close)

    async def upstream(self, request):
        body = await request.json()
        self.calls.append((request.path, body, dict(request.headers)))
        if self.responses_missing and request.path.endswith("/responses"):
            return web.json_response(
                {"error": {"message": "Endpoint unsupported"}}, status=404
            )
        if self.status != 200:
            return web.json_response(
                {"error": {"message": "rate limit private-upstream-key"}},
                status=self.status,
                headers={"Retry-After": "12"},
            )
        if body.get("stream"):
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            if self.slow_stream:
                try:
                    while request.transport and not request.transport.is_closing():
                        await response.write(sse(chunk({"content": "part"})))
                        await asyncio.sleep(0.01)
                except (ConnectionResetError, BrokenPipeError):
                    pass
                finally:
                    self.upstream_closed.set()
                return response
            if request.path.endswith("/messages"):
                stream = test_protocols.StreamTests().a_events()
                if self.fail_stream:
                    stream = stream[:-1]
                for event in stream:
                    await response.write(sse(event, event["type"]))
            elif request.path.endswith("/responses"):
                await response.write(
                    sse(
                        {"type": "response.created", "response": {"id": "resp_mock"}},
                        "response.created",
                    )
                )
                await response.write(
                    sse(
                        {
                            "type": "response.completed",
                            "response": {
                                "id": "resp_mock",
                                "status": "completed",
                                "usage": {
                                    "input_tokens": 3,
                                    "output_tokens": 2,
                                    "total_tokens": 5,
                                },
                            },
                        },
                        "response.completed",
                    )
                )
            else:
                await response.write(sse(chunk({"content": "Hello"})))
                if self.tool_mode:
                    await response.write(
                        sse(
                            chunk(
                                {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call_1",
                                            "type": "function",
                                            "function": {
                                                "name": "weather",
                                                "arguments": '{"city":"杭',
                                            },
                                        }
                                    ]
                                }
                            )
                        )
                    )
                    await response.write(
                        sse(
                            chunk(
                                {
                                    "tool_calls": [
                                        {"index": 0, "function": {"arguments": '州"}'}}
                                    ]
                                }
                            )
                        )
                    )
                if not self.fail_stream:
                    await response.write(
                        sse(chunk(finish="tool_calls" if self.tool_mode else "stop"))
                    )
                    await response.write(
                        sse(
                            chunk(
                                usage={
                                    "prompt_tokens": 3,
                                    "completion_tokens": 2,
                                    "total_tokens": 5,
                                }
                            )
                        )
                    )
                    await response.write(sse("[DONE]"))
            await response.write_eof()
            return response
        if request.path.endswith("/messages"):
            content = (
                [
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "weather",
                        "input": {"city": "杭州"},
                    }
                ]
                if self.tool_mode
                else [{"type": "text", "text": "Hello"}]
            )
            return web.json_response(
                {
                    "id": "msg_mock",
                    "type": "message",
                    "role": "assistant",
                    "model": "model",
                    "content": content,
                    "stop_reason": "tool_use" if self.tool_mode else "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 3, "output_tokens": 2},
                }
            )
        if request.path.endswith("/responses"):
            return web.json_response(
                {
                    "id": "resp_mock",
                    "object": "response",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "Hello"}],
                        }
                    ],
                }
            )
        message = {"role": "assistant", "content": "Hello"}
        if self.tool_mode:
            message["tool_calls"] = [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "weather", "arguments": '{"city":"杭州"}'},
                }
            ]
        return web.json_response(
            {
                "id": "chat_mock",
                "object": "chat.completion",
                "created": 1,
                "model": "model",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if self.tool_mode else "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            }
        )

    async def post(self, path, body, headers=None):
        return await self.client.post(
            path,
            json={"model": "model", **body},
            headers=headers or {"Authorization": "Bearer test-key"},
        )

    async def test_anthropic_sdk_key_style_and_tool_roundtrip(self):
        self.tool_mode = True
        first = await self.post(
            "/v1/messages",
            {
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "Weather?"}]}
                ],
                "max_tokens": 64,
                "tools": [{"name": "weather", "input_schema": {"type": "object"}}],
            },
            {"x-api-key": "test-key", "anthropic-version": "2023-06-01"},
        )
        self.assertEqual(first.status, 200)
        data = await first.json()
        tool = next(b for b in data["content"] if b["type"] == "tool_use")
        self.assertEqual(tool["input"], {"city": "杭州"})
        self.assertEqual(
            data["usage"],
            {
                "input_tokens": 3,
                "output_tokens": 2,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        )
        self.tool_mode = False
        second = await self.post(
            "/v1/messages",
            {
                "max_tokens": 64,
                "messages": [
                    {"role": "user", "content": "Weather?"},
                    {"role": "assistant", "content": data["content"]},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool["id"],
                                "content": "Sunny",
                            }
                        ],
                    },
                ],
            },
        )
        self.assertEqual(second.status, 200)
        self.assertEqual(self.calls[-1][1]["messages"][-1]["role"], "tool")
        self.assertEqual(self.calls[-1][1]["messages"][-1]["tool_call_id"], "call_1")
        self.assertEqual(
            self.calls[-1][2]["Authorization"], "Bearer private-upstream-key"
        )

    async def test_openai_to_anthropic_request_response_tools(self):
        self.provider["protocol"] = "anthropic"
        self.tool_mode = True
        response = await self.post(
            "/v1/chat/completions",
            {
                "messages": [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "Weather?"},
                ],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "weather",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
                "max_tokens": 64,
            },
        )
        self.assertEqual(response.status, 200)
        result = await response.json()
        self.assertEqual(result["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(
            json.loads(
                result["choices"][0]["message"]["tool_calls"][0]["function"][
                    "arguments"
                ]
            ),
            {"city": "杭州"},
        )
        path, body, headers = self.calls[-1]
        self.assertEqual(path, "/v1/messages")
        self.assertEqual(body["max_tokens"], 64)
        self.assertEqual(body["system"], "sys")
        self.assertEqual(headers["x-api-key"], "private-upstream-key")

    async def test_cross_stream_in_both_directions_and_sse_cors(self):
        for source, path in [
            ("openai", "/v1/messages"),
            ("anthropic", "/v1/chat/completions"),
        ]:
            self.provider["protocol"] = source
            self.tool_mode = True
            response = await self.post(
                path,
                {
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 64,
                    "stream": True,
                },
                {"Authorization": "Bearer test-key", "Origin": "capacitor://localhost"},
            )
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")
            raw = await response.read()
            parsed = [
                (n, d if d == "[DONE]" else json.loads(d))
                async for n, d in events(Content(raw))
            ]
            if source == "openai":
                self.assertEqual(parsed[-1][0], "message_stop")
                self.assertEqual(parsed[-2][1]["delta"]["stop_reason"], "tool_use")
            else:
                self.assertEqual(parsed[-1][1], "[DONE]")

    async def test_native_responses_stream_and_state_are_forwarded_unchanged(self):
        response = await self.post(
            "/v1/responses",
            {
                "input": "Hello",
                "stream": True,
                "previous_response_id": "resp_prior",
                "tools": [{"type": "web_search"}],
            },
        )
        self.assertEqual(response.status, 200)
        raw = await response.text()
        self.assertIn("response.completed", raw)
        self.assertEqual(self.calls[-1][0], "/v1/responses")
        self.assertEqual(self.calls[-1][1]["previous_response_id"], "resp_prior")
        self.assertEqual(self.calls[-1][1]["tools"][0]["type"], "web_search")

    async def test_http_errors_map_to_client_protocol_without_key_leaks(self):
        self.status = 429
        for streaming in (False, True):
            response = await self.post(
                "/v1/messages",
                {
                    "max_tokens": 64,
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": streaming,
                },
            )
            self.assertEqual(response.status, 429)
            data = await response.json()
            self.assertEqual(data["type"], "error")
            self.assertEqual(data["error"]["type"], "rate_limit_error")
            self.assertNotIn("private-upstream-key", str(data))
            self.assertEqual(response.headers["Retry-After"], "12")

    async def test_truncated_stream_emits_error_without_success_stop(self):
        self.fail_stream = True
        response = await self.post(
            "/v1/messages",
            {
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
        )
        raw = await response.text()
        self.assertIn("event: error", raw)
        self.assertNotIn("event: message_stop", raw)
        await asyncio.wait_for(self.logged.wait(), 1)
        self.assertEqual(
            self.bridge.model_usage.record_request.call_args.kwargs["status"], 502
        )

    async def test_native_messages_preserves_signed_thinking_and_beta_headers(self):
        self.provider["protocol"] = "anthropic"
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "reason", "signature": "sig"},
                    {"type": "text", "text": "previous"},
                ],
            },
            {"role": "user", "content": "next"},
        ]
        response = await self.post(
            "/v1/messages",
            {"messages": messages, "max_tokens": 64},
            {
                "x-api-key": "test-key",
                "anthropic-beta": "test-beta",
                "anthropic-version": "2023-06-01",
            },
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(self.calls[-1][1]["messages"], messages)
        self.assertEqual(self.calls[-1][2]["anthropic-beta"], "test-beta")

    async def test_auth_error_uses_messages_shape(self):
        response = await self.post(
            "/v1/messages", {"messages": []}, {"x-api-key": "wrong"}
        )
        self.assertEqual(response.status, 401)
        self.assertEqual(
            (await response.json())["error"]["type"], "authentication_error"
        )
        self.assertEqual(self.calls, [])

    async def test_responses_missing_endpoint_falls_back_without_retrying_rate_limits(
        self,
    ):
        self.responses_missing = True
        for streaming in (False, True):
            self.calls.clear()
            response = await self.post(
                "/v1/responses",
                {"input": "Hi", "stream": streaming, "max_output_tokens": 64},
            )
            self.assertEqual(response.status, 200)
            raw = await response.text()
            self.assertIn("Hello", raw)
            self.assertEqual(
                [c[0] for c in self.calls], ["/v1/responses", "/v1/chat/completions"]
            )
            self.assertIn(
                "responses-adapted-via-chat", response.headers["X-AI-Gateway-Warnings"]
            )
        self.responses_missing = False
        self.status = 429
        self.calls.clear()
        response = await self.post("/v1/responses", {"input": "Hi", "stream": True})
        self.assertEqual(response.status, 429)
        self.assertEqual(len(self.calls), 1)

    async def test_native_only_responses_features_do_not_silently_fall_back(self):
        self.responses_missing = True
        response = await self.post(
            "/v1/responses", {"input": "Hi", "previous_response_id": "resp_prior"}
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(len(self.calls), 1)
        self.assertIn(
            "previous_response_id", (await response.json())["error"]["message"]
        )

    async def test_malformed_nested_input_is_a_client_error(self):
        for extras in [
            {"thinking": ["bad"]},
            {"tools": [None]},
            {"tool_choice": {"type": "tool"}},
            {
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "t",
                                "name": "f",
                                "input": "invalid JSON",
                            }
                        ],
                    }
                ]
            },
        ]:
            response = await self.post(
                "/v1/messages",
                {
                    "max_tokens": 64,
                    "messages": [{"role": "user", "content": "Hi"}],
                    **extras,
                },
            )
            self.assertEqual(response.status, 400, await response.text())
        self.assertEqual(self.calls, [])

    async def test_client_disconnect_closes_upstream_and_records_cancellation(self):
        self.slow_stream = True
        response = await self.post(
            "/v1/messages",
            {
                "messages": [{"role": "user", "content": "Hi"}],
                "max_tokens": 64,
                "stream": True,
            },
        )
        await response.content.readline()
        response.close()
        await asyncio.wait_for(self.upstream_closed.wait(), 2)
        await asyncio.wait_for(self.logged.wait(), 2)
        self.assertEqual(
            self.bridge.model_usage.record_request.call_args.kwargs["status"], 499
        )
        self.assertEqual(self.bridge.model_usage.record_request.call_count, 1)

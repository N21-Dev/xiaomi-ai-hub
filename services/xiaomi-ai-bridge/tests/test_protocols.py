from __future__ import annotations
import asyncio
import copy
import json
import unittest
from xiaomi_ai_bridge.protocols import (
    ProtocolError,
    anthropic_to_chat,
    chat_to_anthropic,
    chat_response_to_a,
    a_response_to_chat,
    responses_to_chat,
    chat_to_responses,
)
from xiaomi_ai_bridge.protocol_stream import events, sse, translate_stream
from xiaomi_ai_bridge.model_gateway import prepare_request


class ConversionTests(unittest.TestCase):
    def a_request(self, **extras):
        return {
            "model": "claude",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 2048,
            **extras,
        }

    def chat_request(self, **extras):
        return {
            "model": "gpt",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 2048,
            **extras,
        }

    def test_anthropic_tool_roundtrip_multiple_results_and_error(self):
        request = self.a_request(
            system=[
                {"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}
            ],
            tools=[
                {
                    "name": "lookup",
                    "description": "Find",
                    "input_schema": {
                        "type": "object",
                        "properties": {"q": {"type": "string"}},
                    },
                }
            ],
            tool_choice={
                "type": "tool",
                "name": "lookup",
                "disable_parallel_tool_use": True,
            },
            messages=[
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Checking"},
                        {
                            "type": "tool_use",
                            "id": "one",
                            "name": "lookup",
                            "input": {"q": "中文"},
                        },
                        {
                            "type": "tool_use",
                            "id": "two",
                            "name": "lookup",
                            "input": {},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "one",
                            "content": [{"type": "text", "text": "result"}],
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "two",
                            "content": "failed",
                            "is_error": True,
                        },
                        {"type": "text", "text": "continue"},
                    ],
                },
            ],
        )
        result = anthropic_to_chat(request, "gpt")
        self.assertEqual(result["messages"][0], {"role": "system", "content": "sys"})
        self.assertEqual(
            json.loads(result["messages"][1]["tool_calls"][0]["function"]["arguments"]),
            {"q": "中文"},
        )
        self.assertEqual(
            [m["role"] for m in result["messages"]],
            ["system", "assistant", "tool", "tool", "user"],
        )
        self.assertIn("Tool execution error", result["messages"][3]["content"])
        self.assertEqual(result["tool_choice"]["function"]["name"], "lookup")
        self.assertFalse(result["parallel_tool_calls"])
        back = chat_to_anthropic(result, "claude")
        self.assertEqual(back["messages"][1]["content"][0]["tool_use_id"], "one")
        self.assertEqual(back["messages"][1]["content"][1]["tool_use_id"], "two")
        self.assertEqual(
            back["tools"][0]["input_schema"], request["tools"][0]["input_schema"]
        )

    def test_chat_developer_tool_calls_tool_results(self):
        request = self.chat_request(
            messages=[
                {"role": "developer", "content": "policy"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "id1",
                            "type": "function",
                            "function": {"name": "read", "arguments": '{"x":1}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "id1", "content": "contents"},
            ],
            tools=[
                {
                    "type": "function",
                    "function": {"name": "read", "parameters": {"type": "object"}},
                }
            ],
            tool_choice="required",
        )
        result = chat_to_anthropic(request, "claude")
        self.assertEqual(result["system"], "policy")
        self.assertEqual(result["messages"][0]["content"][0]["input"], {"x": 1})
        self.assertEqual(result["messages"][1]["content"][0]["type"], "tool_result")
        self.assertEqual(result["tool_choice"]["type"], "any")

    def test_text_arrays_are_not_multimodal_images_are(self):
        provider = {
            "protocol": "openai",
            "base_url": "https://upstream.test/v1",
            "api_key": "key",
        }
        body = self.a_request(
            messages=[{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
        )
        target, request, _, _, _ = prepare_request(
            provider, "anthropic", body, {"multimodal": False}
        )
        self.assertEqual(request["messages"][0]["content"][0]["text"], "hello")
        body["messages"][0]["content"].append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": "YWJj"},
            }
        )
        with self.assertRaises(ProtocolError):
            prepare_request(provider, "anthropic", body, {"multimodal": False})
        _, converted, _, _, _ = prepare_request(
            provider, "anthropic", body, {"multimodal": True}
        )
        self.assertEqual(
            converted["messages"][0]["content"][1]["image_url"]["url"],
            "data:image/png;base64,YWJj",
        )
        back = chat_to_anthropic(converted, "claude")
        self.assertEqual(back["messages"][0]["content"][1]["source"]["data"], "YWJj")

    def test_tool_result_images_follow_all_tool_replies(self):
        body = self.a_request(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "first",
                            "content": [
                                {"type": "text", "text": "Screenshot"},
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "url",
                                        "url": "https://example.test/screen.png",
                                    },
                                },
                            ],
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "second",
                            "content": "ok",
                        },
                    ],
                }
            ]
        )
        result = anthropic_to_chat(body, "gpt")
        self.assertEqual(
            [m["role"] for m in result["messages"]], ["tool", "tool", "user"]
        )
        self.assertEqual(
            result["messages"][-1]["content"][1]["image_url"]["url"],
            "https://example.test/screen.png",
        )
        self.assertIn("first", result["messages"][-1]["content"][0]["text"])

    def test_stop_and_cached_usage_mapping(self):
        data = {
            "id": "chat_1",
            "choices": [{"message": {"content": "yes"}, "finish_reason": "length"}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 15,
                "prompt_tokens_details": {"cached_tokens": 20},
            },
        }
        message = chat_response_to_a(data, "gpt")
        self.assertEqual(message["stop_reason"], "max_tokens")
        self.assertEqual(
            message["usage"],
            {
                "input_tokens": 80,
                "output_tokens": 15,
                "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 0,
            },
        )
        back = a_response_to_chat(message, "gpt")
        self.assertEqual(back["usage"]["total_tokens"], 115)
        self.assertEqual(back["choices"][0]["finish_reason"], "length")
        message["stop_reason"] = "stop_sequence"
        message["stop_sequence"] = "END"
        self.assertEqual(
            a_response_to_chat(message, "gpt")["choices"][0]["finish_reason"], "stop"
        )

    def test_nonstream_tools_keep_id_schema_and_json(self):
        data = {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tool_abc",
                    "name": "calc",
                    "input": {"a": 2},
                }
            ],
            "stop_reason": "tool_use",
            "usage": {
                "input_tokens": 20,
                "output_tokens": 10,
                "cache_creation_input_tokens": 5,
            },
        }
        chat = a_response_to_chat(data, "claude")
        self.assertEqual(chat["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(chat["usage"]["prompt_tokens"], 25)
        anth = chat_response_to_a(chat, "claude")
        self.assertEqual(anth["content"][0], data["content"][0])

    def test_unknown_cross_features_and_builtin_tools_are_not_silently_dropped(self):
        # `response_format: {"type": "json_object"}` used to live in this list.
        # It is no longer *dropped* — it is re-expressed as a system hint, see
        # test_bare_json_mode_is_reexpressed_rather_than_dropped below.
        for extras in [
            {"n": 2},
            {"logprobs": True},
            {"tools": [{"type": "web_search"}]},
        ]:
            with self.subTest(extras=extras), self.assertRaises(ProtocolError):
                chat_to_anthropic(self.chat_request(**extras), "claude")
        with self.assertRaises(ProtocolError):
            anthropic_to_chat(
                self.a_request(
                    tools=[{"type": "web_search_20250305", "name": "web_search"}]
                ),
                "gpt",
            )
        with self.assertRaises(ProtocolError):
            anthropic_to_chat(
                self.a_request(
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "document", "source": {"data": "doc"}}
                            ],
                        }
                    ]
                ),
                "gpt",
            )

    def test_system_role_messages_and_safeguards_translate(self):
        """Claude Code >= 2.1.296 sends `safeguards` and injects project
        context as system-role messages inside `messages`.

        `safeguards` is a server-side classification directive with no Chat
        equivalent — it must be ignored, not rejected.  System-role messages
        carry content — they must fold into the leading system message
        (preserving order) instead of failing the whole call with
        "Messages role must be user or assistant".
        """
        request = self.a_request(
            safeguards=[{"type": "dangerous_tool_use"}],
            system=[{"type": "text", "text": "base prompt"}],
            messages=[
                {"role": "user", "content": "hi"},
                {
                    "role": "system",
                    "content": [{"type": "text", "text": "# CLAUDE.md\nfacts"}],
                },
                {"role": "assistant", "content": "hello"},
            ],
        )
        chat = anthropic_to_chat(request, "gpt")
        self.assertEqual(
            [m["role"] for m in chat["messages"]],
            ["system", "user", "assistant"],
        )
        system = chat["messages"][0]["content"]
        self.assertIn("base prompt", system)
        self.assertIn("# CLAUDE.md", system)
        self.assertLess(system.index("base prompt"), system.index("# CLAUDE.md"))

        chat = anthropic_to_chat(
            self.a_request(
                messages=[
                    {"role": "user", "content": "hi"},
                    {"role": "system", "content": "ctx"},
                ],
            ),
            "gpt",
        )
        self.assertEqual(
            chat["messages"][0], {"role": "system", "content": "ctx"}
        )
        self.assertEqual(chat["messages"][1], {"role": "user", "content": "hi"})

    def test_tool_reference_blocks_and_tool_search_are_collapsed(self):
        """Claude Code >= 2.1.310 deferred tool loading: tool_reference blocks
        in message content and the tool-search server tool have no Chat
        counterpart.  The deferred tools travel in the same request with full
        schemas and are forwarded eagerly, so the markers are no-ops; other
        server tools are still rejected.
        """
        request = self.a_request(
            tools=[
                {"type": "tool-search-tool-2025-10-19", "name": "tool_search"},
                {
                    "name": "calc",
                    "description": "c",
                    "input_schema": {"type": "object", "properties": {}},
                },
            ],
            messages=[
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "tool_search",
                            "input": {"query": "calc"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": [
                                {"type": "tool_reference", "tool_name": "calc"},
                                {"type": "text", "text": "found calc"},
                            ],
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_reference", "tool_name": "calc"},
                        {"type": "text", "text": "go on"},
                    ],
                },
            ],
        )
        chat = anthropic_to_chat(request, "gpt")
        self.assertEqual(
            [t["function"]["name"] for t in chat["tools"]], ["calc"]
        )
        flat = json.dumps(chat["messages"])
        self.assertNotIn("tool_reference", flat)
        self.assertIn("found calc", flat)
        self.assertIn("go on", flat)
        with self.assertRaises(ProtocolError):
            anthropic_to_chat(
                self.a_request(
                    tools=[{"type": "web_search_20250305", "name": "web_search"}]
                ),
                "gpt",
            )

    def test_bare_json_mode_is_reexpressed_rather_than_dropped(self):
        """`json_object` is an output-format constraint, not a capability.

        Anthropic has no bare "must be valid JSON" switch.  Rejecting it failed
        the whole call, and clients such as the health service send it by
        default; re-expressing it as a system hint keeps the call usable while
        still stating the requirement.  A real schema still maps natively.
        """

        result = chat_to_anthropic(
            self.chat_request(response_format={"type": "json_object"}), "claude"
        )
        self.assertIn("JSON", result["system"])
        self.assertNotIn("response_format", result)

        # Without a client system prompt the hint still has to be delivered.
        bare = chat_to_anthropic(
            {
                "model": "m", "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}],
                "response_format": {"type": "json_object"},
            },
            "claude",
        )
        self.assertIn("JSON", bare["system"])

        strict = chat_to_anthropic(
            self.chat_request(
                response_format={"type": "json_schema", "json_schema": {"schema": {"type": "object"}}}
            ),
            "claude",
        )
        self.assertEqual(strict["output_config"]["format"]["type"], "json_schema")

    def test_signed_thinking_history_is_not_relabelled_as_user_text(self):
        req = self.a_request(
            messages=[
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "private",
                            "signature": "signed",
                        },
                        {"type": "redacted_thinking", "data": "opaque"},
                        {"type": "text", "text": "answer"},
                    ],
                }
            ]
        )
        result = anthropic_to_chat(req, "gpt")
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("signed", json.dumps(result))
        self.assertIn("answer", json.dumps(result))

    def test_output_defaults_do_not_override_client_and_native_extensions_survive(self):
        provider = {
            "protocol": "openai",
            "base_url": "https://up.test/v1",
            "api_key": "x",
        }
        req = self.chat_request(
            max_tokens=64,
            tools=[
                {"type": "function", "function": {"name": "test", "parameters": {}}}
            ],
        )
        original = copy.deepcopy(req)
        _, body, url, headers, _ = prepare_request(
            provider, "openai", req, {"max_output_tokens": 4096}
        )
        self.assertEqual(body["max_tokens"], 64)
        self.assertEqual(req, original)
        self.assertEqual(url, "https://up.test/v1/chat/completions")
        _, body, url, _, _ = prepare_request(
            provider,
            "responses",
            {
                "model": "gpt",
                "input": "Hi",
                "stream": True,
                "previous_response_id": "prior",
                "tools": [{"type": "web_search"}],
            },
            {},
        )
        self.assertEqual(url, "https://up.test/v1/responses")
        self.assertEqual(body["previous_response_id"], "prior")
        self.assertEqual(body["tools"][0]["type"], "web_search")

    def test_reasoning_maps_by_protocol_with_valid_budget(self):
        p = {"protocol": "anthropic", "base_url": "https://up.test/v1"}
        _, r, _, _, _ = prepare_request(
            p, "openai", self.chat_request(max_tokens=8192, reasoning_effort="high"), {}
        )
        self.assertEqual(r["thinking"]["budget_tokens"], 8191)
        self.assertNotIn("reasoning_effort", r)
        with self.assertRaises(ProtocolError):
            prepare_request(
                p,
                "openai",
                self.chat_request(max_tokens=64, reasoning_effort="high"),
                {},
            )

    def test_responses_tool_history_to_messages_and_back(self):
        body = {
            "model": "claude",
            "instructions": "sys",
            "input": [
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "f",
                    "arguments": "{}",
                },
                {"type": "function_call_output", "call_id": "c1", "output": "ok"},
            ],
            "tools": [
                {"type": "function", "name": "f", "parameters": {"type": "object"}}
            ],
        }
        _, request, _, _, _ = prepare_request(
            {"protocol": "anthropic", "base_url": "https://up.test/v1"},
            "responses",
            body,
            {},
        )
        self.assertEqual(request["system"], "sys")
        self.assertEqual(request["messages"][0]["content"][0]["type"], "tool_use")
        self.assertEqual(request["messages"][1]["content"][0]["tool_use_id"], "c1")
        result = chat_to_responses(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "function": {"name": "f", "arguments": "{}"},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            "claude",
        )
        self.assertEqual(result["output"][0]["call_id"], "c1")
        with self.assertRaises(ProtocolError):
            responses_to_chat({**body, "previous_response_id": "no-native-state"})


class Content:
    def __init__(self, raw, chunk=7):
        self.raw = raw
        self.chunk = chunk

    async def iter_chunked(self, _):
        for offset in range(0, len(self.raw), self.chunk):
            yield self.raw[offset : offset + self.chunk]


def chunk(delta=None, finish=None, usage=None):
    out = {
        "id": "chat_1",
        "object": "chat.completion.chunk",
        "model": "gpt",
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
    }
    if usage is not None:
        out.update(choices=[], usage=usage)
    return out


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, raw, source, target):
        metrics = {}
        result = b"".join(
            [
                x
                async for x in translate_stream(
                    Content(raw), source, target, "model", metrics
                )
            ]
        )
        decoded = []
        async for name, data in events(Content(result)):
            decoded.append((name, data if data == "[DONE]" else json.loads(data)))
        return decoded, metrics

    async def test_sse_fragmented_utf8_crlf_multiline_ping_no_final_blank(self):
        raw = ': ping\r\nevent: sample\r\ndata: {"text":\r\ndata: "中文"}\r\n\r\ndata: [DONE]'.encode()
        parsed = [x async for x in events(Content(raw, 1))]
        self.assertEqual(json.loads(parsed[0][1]), {"text": "中文"})
        self.assertEqual(parsed[1][1], "[DONE]")

    async def test_multiline_sse_survives_reframing(self):
        payload = '{\n"text": "hello"\n}'
        parsed = [p async for p in events(Content(sse(payload, "sample")))]
        self.assertEqual(json.loads(parsed[0][1]), {"text": "hello"})
        self.assertEqual(parsed[0][0], "sample")

    async def test_chat_text_and_interleaved_tools_to_anthropic(self):
        chunks = [
            chunk({"content": "你好"}),
            chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "a",
                            "type": "function",
                            "function": {"name": "first", "arguments": '{"x":'},
                        },
                        {
                            "index": 1,
                            "id": "b",
                            "type": "function",
                            "function": {"name": "second", "arguments": '{"y":'},
                        },
                    ]
                }
            ),
            chunk(
                {
                    "tool_calls": [
                        {"index": 1, "function": {"arguments": "2}"}},
                        {"index": 0, "function": {"arguments": "1}"}},
                    ]
                }
            ),
            chunk(finish="tool_calls"),
            chunk(usage={"prompt_tokens": 10, "completion_tokens": 20}),
        ]
        raw = b"".join(sse(c) for c in chunks) + sse("[DONE]")
        ev, stats = await self.collect(raw, "openai", "anthropic")
        self.assertEqual(ev[0][0], "message_start")
        self.assertEqual(ev[-1][0], "message_stop")
        starts = [v["content_block"] for n, v in ev if n == "content_block_start"]
        self.assertEqual([s["type"] for s in starts], ["text", "tool_use", "tool_use"])
        self.assertEqual([s["id"] for s in starts[1:]], ["a", "b"])
        deltas = [
            v["delta"]["partial_json"]
            for n, v in ev
            if n == "content_block_delta" and v["delta"]["type"] == "input_json_delta"
        ]
        self.assertEqual([json.loads(d) for d in deltas], [{"x": 1}, {"y": 2}])
        self.assertEqual(ev[-2][1]["delta"]["stop_reason"], "tool_use")
        self.assertEqual(ev[-2][1]["usage"]["input_tokens"], 10)
        self.assertEqual(stats["usage"]["completion_tokens"], 20)

    def a_events(self):
        return [
            {
                "type": "message_start",
                "message": {
                    "id": "msg1",
                    "usage": {
                        "input_tokens": 8,
                        "cache_read_input_tokens": 2,
                        "output_tokens": 0,
                    },
                },
            },
            {"type": "ping"},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Hello"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": "tool1",
                    "name": "calc",
                    "input": {},
                },
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"x":1}'},
            },
            {"type": "content_block_stop", "index": 1},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                "usage": {"output_tokens": 12},
            },
            {"type": "message_stop"},
        ]

    async def test_anthropic_to_chat_keeps_deltas_tools_and_usage(self):
        raw = b"".join(sse(e, e["type"]) for e in self.a_events())
        ev, stats = await self.collect(raw, "anthropic", "openai")
        self.assertEqual(ev[-1][1], "[DONE]")
        tool = [
            e["choices"][0]["delta"]["tool_calls"][0]
            for _, e in ev[:-1]
            if e.get("choices") and e["choices"][0]["delta"].get("tool_calls")
        ]
        self.assertEqual(tool[0]["id"], "tool1")
        self.assertEqual(tool[1]["function"]["arguments"], '{"x":1}')
        self.assertEqual(stats["usage"]["total_tokens"], 22)

    async def test_anthropic_to_responses_has_ordered_sequence_and_complete_output(
        self,
    ):
        raw = b"".join(sse(e, e["type"]) for e in self.a_events())
        ev, stats = await self.collect(raw, "anthropic", "responses")
        self.assertEqual([v["sequence_number"] for _, v in ev], list(range(len(ev))))
        self.assertEqual(ev[0][0], "response.created")
        self.assertEqual(ev[-1][0], "response.completed")
        response = ev[-1][1]["response"]
        self.assertEqual(response["output"][0]["content"][0]["text"], "Hello")
        self.assertEqual(response["output"][1]["call_id"], "tool1")
        self.assertEqual(response["usage"]["total_tokens"], 22)

    async def test_stream_failure_is_not_reported_as_success(self):
        for raw, source, target in [
            (sse(chunk({"content": "partial"})), "openai", "anthropic"),
            (
                b"".join(sse(e, e["type"]) for e in self.a_events()[:-1]),
                "anthropic",
                "openai",
            ),
            (
                sse({"error": {"message": "upstream-error"}}, "error"),
                "openai",
                "anthropic",
            ),
            (b"data: {invalid}\n\n", "openai", "anthropic"),
        ]:
            with self.subTest(source=source), self.assertRaises(ProtocolError):
                await self.collect(raw, source, target)

    async def test_first_text_is_delivered_before_upstream_finishes(self):
        gate = asyncio.Event()

        class Gated:
            async def iter_chunked(self, _):
                yield sse(chunk({"content": "first"}))
                await gate.wait()
                yield sse(chunk(finish="stop")) + sse("[DONE]")

        stream = translate_stream(Gated(), "openai", "anthropic", "model", {})
        self.assertIn(b"message_start", await asyncio.wait_for(anext(stream), 1))
        self.assertIn(b"content_block_start", await asyncio.wait_for(anext(stream), 1))
        self.assertIn(b"first", await asyncio.wait_for(anext(stream), 1))
        gate.set()
        await stream.aclose()

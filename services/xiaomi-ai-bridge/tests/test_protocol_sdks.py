"""Optional contract tests against official clients; see MODEL-GATEWAY.md."""

from __future__ import annotations
import unittest

import test_protocol_http

try:
    import anthropic
    import openai
except ImportError:
    anthropic = openai = None


@unittest.skipUnless(
    anthropic and openai, "Install test-only openai and anthropic SDKs"
)
class OfficialSdkTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_protocol_http.ProtocolHttpTests.asyncSetUp
    upstream = test_protocol_http.ProtocolHttpTests.upstream

    def a_client(self):
        client = anthropic.AsyncAnthropic(
            api_key="test-key", base_url=str(self.client.make_url("/")), max_retries=0
        )
        self.addAsyncCleanup(client.close)
        return client

    def o_client(self):
        client = openai.AsyncOpenAI(
            api_key="test-key", base_url=str(self.client.make_url("/v1")), max_retries=0
        )
        self.addAsyncCleanup(client.close)
        return client

    async def test_messages_stream_accumulates_tools_and_accepts_next_tool_result(self):
        client = self.a_client()
        self.tool_mode = True
        history = [{"role": "user", "content": "Weather?"}]
        async with client.messages.stream(
            model="gpt",
            max_tokens=64,
            messages=history,
            tools=[
                {
                    "name": "weather",
                    "input_schema": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    },
                }
            ],
        ) as stream:
            message = await stream.get_final_message()
        self.assertEqual(message.stop_reason, "tool_use")
        self.assertEqual(message.content[0].text, "Hello")
        tool = message.content[1]
        self.assertEqual(tool.input, {"city": "杭州"})
        self.assertEqual(message.usage.input_tokens, 3)
        self.assertEqual(message.usage.output_tokens, 2)
        self.tool_mode = False
        history += [
            {"role": "assistant", "content": [b.model_dump() for b in message.content]},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tool.id, "content": "Sunny"}
                ],
            },
        ]
        answer = await client.messages.create(
            model="gpt", max_tokens=64, messages=history
        )
        self.assertEqual(answer.content[0].text, "Hello")
        self.assertEqual(self.calls[-1][1]["messages"][-1]["tool_call_id"], tool.id)

    async def test_chat_stream_parser_with_anthropic_upstream(self):
        self.provider["protocol"] = "anthropic"
        client = self.o_client()
        async with client.chat.completions.stream(
            model="claude", max_tokens=64, messages=[{"role": "user", "content": "hi"}]
        ) as stream:
            result = await stream.get_final_completion()
        choice = result.choices[0]
        self.assertEqual(choice.message.content, "Hello")
        self.assertEqual(choice.finish_reason, "tool_calls")
        self.assertEqual(choice.message.tool_calls[0].function.arguments, '{"x":1}')
        self.assertEqual(result.usage.total_tokens, 22)

    async def test_responses_stream_parser_with_anthropic_upstream(self):
        self.provider["protocol"] = "anthropic"
        client = self.o_client()
        async with client.responses.stream(
            model="claude", max_output_tokens=64, input="hi"
        ) as stream:
            result = await stream.get_final_response()
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.output_text, "Hello")
        self.assertEqual(result.output[1].call_id, "tool1")
        self.assertEqual(result.output[1].arguments, '{"x":1}')
        self.assertEqual(result.usage.total_tokens, 22)

    async def test_responses_chat_fallback_is_parsed_by_sdk(self):
        self.responses_missing = True
        self.tool_mode = True
        client = self.o_client()
        async with client.responses.stream(
            model="chat-only", max_output_tokens=64, input="hi"
        ) as stream:
            result = await stream.get_final_response()
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.output_text, "Hello")
        self.assertEqual(result.output[1].arguments, '{"city":"杭州"}')
        self.assertEqual(
            [c[0] for c in self.calls], ["/v1/responses", "/v1/chat/completions"]
        )

    async def test_messages_stream_sdk_raises_on_truncation(self):
        self.fail_stream = True
        client = self.a_client()
        with self.assertRaises(anthropic.APIError):
            async with client.messages.stream(
                model="gpt", max_tokens=64, messages=[{"role": "user", "content": "Hi"}]
            ) as stream:
                await stream.get_final_message()

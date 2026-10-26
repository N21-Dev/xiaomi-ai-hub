"""Incremental SSE framing and stateful Messages/Chat/Responses emitters."""

from __future__ import annotations

import codecs
import json
import time
from .protocols import (
    ProtocolError,
    encoded,
    uid,
    a_usage_to_chat,
    chat_usage_to_a,
    chat_to_responses,
    arguments,
)


def sse(data, event=None):
    payload = data if isinstance(data, str) else encoded(data)
    return (
        (f"event: {event}\n" if event else "")
        + "".join("data: " + line + "\n" for line in payload.split("\n"))
        + "\n"
    ).encode()


async def events(content):
    decoder = codecs.getincrementaldecoder("utf-8")()
    buffer = ""
    name = ""
    data = []
    size = 0
    total = 0
    async for raw in content.iter_chunked(16384):
        total += len(raw)
        if total > 32 * 1024 * 1024:
            raise ProtocolError("Upstream stream exceeds 32 MiB", 502)
        try:
            buffer += decoder.decode(raw)
        except UnicodeError:
            raise ProtocolError("Invalid upstream UTF-8 stream", 502) from None
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r")
            size += len(line)
            if size > 1024 * 1024:
                raise ProtocolError("Upstream SSE event too large", 502)
            if not line:
                if data:
                    yield name, "\n".join(data)
                name = ""
                data = []
                size = 0
            elif line.startswith("event:"):
                name = line[6:].lstrip(" ")
            elif line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
        if len(buffer) > 1024 * 1024:
            raise ProtocolError("Upstream SSE line too large", 502)
    try:
        buffer += decoder.decode(b"", final=True)
    except UnicodeError:
        raise ProtocolError("Truncated upstream UTF-8", 502) from None
    # A final frame may omit its empty delimiter. Do not lose the payload.
    if buffer:
        line = buffer.rstrip("\r")
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        elif line.startswith("event:"):
            name = line[6:].lstrip(" ")
    if data:
        yield name, "\n".join(data)


class ChatAccumulator:
    def __init__(self, model):
        self.model = model
        self.id = uid("chatcmpl_")
        self.created = int(time.time())
        self.text = ""
        self.refusal = ""
        self.tools = {}
        self.reasoning = ""
        self.usage = {}
        self.finish = None

    def add(self, chunk):
        self.id = chunk.get("id") or self.id
        self.created = chunk.get("created", self.created)
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if choice.get("index", 0) != 0:
                raise ProtocolError("Multiple stream choices cannot be translated", 502)
            delta = choice.get("delta") or {}
            for field, attr in [
                ("content", "text"),
                ("refusal", "refusal"),
                ("reasoning_content", "reasoning"),
            ]:
                value = delta.get(field)
                if value is not None:
                    if not isinstance(value, str):
                        raise ProtocolError("Unsupported output content", 502)
                    setattr(self, attr, getattr(self, attr) + value)
            for call in delta.get("tool_calls") or []:
                index = call.get("index", 0)
                if type(index) is not int or not 0 <= index < 128:
                    raise ProtocolError("Invalid tool stream index", 502)
                target = self.tools.setdefault(
                    index,
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                if call.get("id"):
                    target["id"] += call["id"]
                if call.get("type", "function") != "function":
                    raise ProtocolError("Unsupported streamed tool type", 502)
                fn = call.get("function") or {}
                for key in ("name", "arguments"):
                    if fn.get(key):
                        target["function"][key] += fn[key]
            if choice.get("finish_reason"):
                self.finish = choice["finish_reason"]

    def response(self):
        if not self.finish:
            raise ProtocolError("Upstream stream ended without a finish reason", 502)
        calls = [self.tools[k] for k in sorted(self.tools)]
        for call in calls:
            if not call["id"] or not call["function"]["name"]:
                raise ProtocolError("Incomplete tool call in stream", 502)
        message = {"role": "assistant", "content": self.text or None}
        if calls:
            message["tool_calls"] = calls
        if self.refusal:
            message["refusal"] = self.refusal
        if self.reasoning:
            message["reasoning_content"] = self.reasoning
        return {
            "id": self.id,
            "object": "chat.completion",
            "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "message": message, "finish_reason": self.finish}],
            "usage": self.usage,
        }


class AnthropicToChat:
    def __init__(self, model):
        self.model = model
        self.id = uid("chatcmpl_")
        self.created = int(time.time())
        self.tools = {}
        self.usage = {}
        self.done = False
        self.started = False
        self.finish = None

    def chunk(self, delta, finish=None):
        return {
            "id": self.id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    def feed(self, event):
        kind = event.get("type")
        if kind == "ping":
            return []
        if kind == "message_start":
            if self.started:
                raise ProtocolError("Duplicate message_start", 502)
            self.started = True
            message = event["message"]
            self.id = message.get("id", self.id)
            self.usage.update(message.get("usage") or {})
            return [self.chunk({"role": "assistant", "content": ""})]
        if not self.started:
            raise ProtocolError("Missing message_start", 502)
        if kind == "content_block_start":
            block = event["content_block"]
            index = event["index"]
            t = block["type"]
            if t == "tool_use":
                tool_index = len(self.tools)
                self.tools[index] = tool_index
                initial = encoded(block["input"]) if block.get("input") else ""
                return [
                    self.chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": tool_index,
                                    "id": block["id"],
                                    "type": "function",
                                    "function": {
                                        "name": block["name"],
                                        "arguments": initial,
                                    },
                                }
                            ]
                        }
                    )
                ]
            if t == "text":
                return (
                    [self.chunk({"content": block["text"]})]
                    if block.get("text")
                    else []
                )
            if t == "thinking":
                return (
                    [self.chunk({"reasoning_content": block["thinking"]})]
                    if block.get("thinking")
                    else []
                )
            if t == "redacted_thinking":
                return []
            raise ProtocolError("Unsupported upstream block: " + t, 502)
        if kind == "content_block_delta":
            delta = event["delta"]
            t = delta["type"]
            if t == "text_delta":
                return [self.chunk({"content": delta["text"]})]
            if t == "thinking_delta":
                return [self.chunk({"reasoning_content": delta["thinking"]})]
            if t == "signature_delta":
                return []
            if t == "input_json_delta":
                if event["index"] not in self.tools:
                    raise ProtocolError("Unknown tool block index", 502)
                return [
                    self.chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": self.tools[event["index"]],
                                    "function": {"arguments": delta["partial_json"]},
                                }
                            ]
                        }
                    )
                ]
            raise ProtocolError("Unsupported upstream delta: " + t, 502)
        if kind == "content_block_stop":
            return []
        if kind == "message_delta":
            self.usage.update(event.get("usage") or {})
            stop = event.get("delta", {}).get("stop_reason")
            if stop:
                if stop not in {
                    "end_turn",
                    "stop_sequence",
                    "max_tokens",
                    "tool_use",
                    "refusal",
                }:
                    raise ProtocolError("Unportable stop_reason: " + stop, 502)
                self.finish = {
                    "max_tokens": "length",
                    "tool_use": "tool_calls",
                    "refusal": "content_filter",
                }.get(stop, "stop")
                return [self.chunk({}, self.finish)]
            return []
        if kind == "message_stop":
            if not self.finish:
                raise ProtocolError("Missing message_delta stop reason", 502)
            self.done = True
            return [
                {
                    "id": self.id,
                    "object": "chat.completion.chunk",
                    "created": self.created,
                    "model": self.model,
                    "choices": [],
                    "usage": a_usage_to_chat(self.usage),
                }
            ]
        raise ProtocolError("Unknown Anthropic event: " + str(kind), 502)


class ChatToAnthropic:
    def __init__(self, model):
        self.acc = ChatAccumulator(model)
        self.started = False
        self.text_open = False
        self.index = 0
        self.id = uid("msg_")

    def start(self):
        if self.started:
            return []
        self.started = True
        return [
            sse(
                {
                    "type": "message_start",
                    "message": {
                        "id": self.id,
                        "type": "message",
                        "role": "assistant",
                        "model": self.acc.model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                },
                "message_start",
            )
        ]

    def feed(self, chunk):
        self.acc.add(chunk)
        output = self.start()
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            text = (delta.get("content") or "") + (delta.get("refusal") or "")
            if text:
                if not self.text_open:
                    output.append(
                        sse(
                            {
                                "type": "content_block_start",
                                "index": self.index,
                                "content_block": {"type": "text", "text": ""},
                            },
                            "content_block_start",
                        )
                    )
                    self.text_open = True
                output.append(
                    sse(
                        {
                            "type": "content_block_delta",
                            "index": self.index,
                            "delta": {"type": "text_delta", "text": text},
                        },
                        "content_block_delta",
                    )
                )
        return output

    def finish(self):
        response = self.acc.response()
        output = self.start()
        if self.text_open:
            output.append(
                sse(
                    {"type": "content_block_stop", "index": self.index},
                    "content_block_stop",
                )
            )
            self.index += 1
        # Parallel Chat tool argument fragments may interleave. Buffer only tool
        # JSON until finalization and emit complete, ordered Anthropic blocks.
        for call in response["choices"][0]["message"].get("tool_calls") or []:
            fn = call["function"]
            args = fn["arguments"] or "{}"
            if self.acc.finish != "length":
                arguments(args)
            output.append(
                sse(
                    {
                        "type": "content_block_start",
                        "index": self.index,
                        "content_block": {
                            "type": "tool_use",
                            "id": call["id"],
                            "name": fn["name"],
                            "input": {},
                        },
                    },
                    "content_block_start",
                )
            )
            output.append(
                sse(
                    {
                        "type": "content_block_delta",
                        "index": self.index,
                        "delta": {"type": "input_json_delta", "partial_json": args},
                    },
                    "content_block_delta",
                )
            )
            output.append(
                sse(
                    {"type": "content_block_stop", "index": self.index},
                    "content_block_stop",
                )
            )
            self.index += 1
        stop = {
            "stop": "end_turn",
            "tool_calls": "tool_use",
            "length": "max_tokens",
            "content_filter": "refusal",
        }.get(self.acc.finish)
        if stop is None:
            raise ProtocolError("Unsupported upstream finish_reason", 502)
        output.append(
            sse(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop, "stop_sequence": None},
                    "usage": chat_usage_to_a(self.acc.usage),
                },
                "message_delta",
            )
        )
        output.append(sse({"type": "message_stop"}, "message_stop"))
        return output


class ChatToResponses:
    def __init__(self, model):
        self.acc = ChatAccumulator(model)
        self.id = uid("resp_")
        self.seq = 0
        self.started = False
        self.message_id = uid("msg_")
        self.text_open = False

    def event(self, kind, **fields):
        output = sse({"type": kind, "sequence_number": self.seq, **fields}, kind)
        self.seq += 1
        return output

    def start(self):
        if self.started:
            return []
        self.started = True
        return [
            self.event(
                "response.created",
                response={
                    "id": self.id,
                    "object": "response",
                    "created_at": self.acc.created,
                    "status": "in_progress",
                    "model": self.acc.model,
                    "output": [],
                },
            )
        ]

    def feed(self, chunk):
        self.acc.add(chunk)
        output = self.start()
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            text = delta.get("content") or delta.get("refusal") or ""
            if not text:
                continue
            if not self.text_open:
                output.extend(
                    [
                        self.event(
                            "response.output_item.added",
                            output_index=0,
                            item={
                                "id": self.message_id,
                                "type": "message",
                                "role": "assistant",
                                "status": "in_progress",
                                "content": [],
                            },
                        ),
                        self.event(
                            "response.content_part.added",
                            item_id=self.message_id,
                            output_index=0,
                            content_index=0,
                            part={"type": "output_text", "text": "", "annotations": []},
                        ),
                    ]
                )
                self.text_open = True
            output.append(
                self.event(
                    "response.output_text.delta",
                    item_id=self.message_id,
                    output_index=0,
                    content_index=0,
                    delta=text,
                    logprobs=[],
                )
            )
        return output

    def finish(self):
        output = self.start()
        chat = self.acc.response()
        response = chat_to_responses(chat, self.acc.model, self.id)
        items = []
        if self.text_open:
            text = self.acc.text + self.acc.refusal
            part = {"type": "output_text", "text": text, "annotations": []}
            item = {
                "id": self.message_id,
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [part],
            }
            items.append(item)
            output.extend(
                [
                    self.event(
                        "response.output_text.done",
                        item_id=self.message_id,
                        output_index=0,
                        content_index=0,
                        text=text,
                        logprobs=[],
                    ),
                    self.event(
                        "response.content_part.done",
                        item_id=self.message_id,
                        output_index=0,
                        content_index=0,
                        part=part,
                    ),
                    self.event("response.output_item.done", output_index=0, item=item),
                ]
            )
        for call in chat["choices"][0]["message"].get("tool_calls") or []:
            index = len(items)
            fn = call["function"]
            args = fn["arguments"] or "{}"
            item_id = uid("fc_")
            if self.acc.finish != "length":
                arguments(args)
            item = {
                "id": item_id,
                "type": "function_call",
                "call_id": call["id"],
                "name": fn["name"],
                "arguments": "",
                "status": "in_progress",
            }
            output.append(
                self.event(
                    "response.output_item.added", output_index=index, item=item.copy()
                )
            )
            output.append(
                self.event(
                    "response.function_call_arguments.delta",
                    item_id=item_id,
                    output_index=index,
                    delta=args,
                )
            )
            output.append(
                self.event(
                    "response.function_call_arguments.done",
                    item_id=item_id,
                    output_index=index,
                    arguments=args,
                )
            )
            item.update(arguments=args, status="completed")
            items.append(item)
            output.append(
                self.event("response.output_item.done", output_index=index, item=item)
            )
        response["output"] = items
        output.append(
            self.event(
                (
                    "response.incomplete"
                    if response["status"] == "incomplete"
                    else "response.completed"
                ),
                response=response,
            )
        )
        return output


async def translate_stream(
    content, source: str, target: str, model: str, metrics: dict
):
    a_reader = AnthropicToChat(model) if source == "anthropic" else None
    writer = (
        ChatToAnthropic(model)
        if target == "anthropic"
        else ChatToResponses(model) if target == "responses" else None
    )
    accumulator = ChatAccumulator(model)
    terminal = False
    async for name, raw in events(content):
        if raw == "[DONE]":
            if source == "anthropic":
                raise ProtocolError("Messages requires message_stop", 502)
            terminal = True
            break
        try:
            data = json.loads(raw)
        except ValueError:
            raise ProtocolError("Invalid upstream SSE JSON", 502) from None
        if not isinstance(data, dict):
            raise ProtocolError("Invalid SSE payload", 502)
        if "error" in data or name == "error":
            raise ProtocolError("Upstream reported a streaming error", 502)
        chunks = a_reader.feed(data) if a_reader else [data]
        if a_reader:
            metrics["usage"] = a_usage_to_chat(a_reader.usage)
        for chunk in chunks:
            accumulator.add(chunk)
            if accumulator.usage:
                metrics["usage"] = accumulator.usage.copy()
            if writer:
                for payload in writer.feed(chunk):
                    yield payload
            else:
                yield sse(chunk)
        if a_reader and a_reader.done:
            terminal = True
            break
    if source == "anthropic" and not terminal:
        raise ProtocolError(
            "Upstream Messages stream disconnected before message_stop", 502
        )
    completed = accumulator.response()
    metrics["usage"] = completed["usage"]
    metrics["finish_reason"] = accumulator.finish
    if writer:
        for payload in writer.finish():
            yield payload
    else:
        yield sse("[DONE]")

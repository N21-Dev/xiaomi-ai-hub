"""Pure conversions for Chat Completions, Messages and stateless Responses.

No HTTP, credentials, execution of tools or model selection belongs here.
Unsupported cross-provider features fail explicitly instead of being flattened.
"""

from __future__ import annotations

import copy
import json
import time
import uuid
from typing import Any


class ProtocolError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def uid(prefix: str) -> str:
    return prefix + uuid.uuid4().hex


def obj(value, name):
    if not isinstance(value, dict):
        raise ProtocolError(f"{name} must be an object")
    return value


def array(value, name):
    if not isinstance(value, list):
        raise ProtocolError(f"{name} must be an array")
    return value


def string(value, name):
    if not isinstance(value, str):
        raise ProtocolError(f"{name} must be a string")
    return value


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def arguments(value, status=502) -> dict:
    try:
        result = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        raise ProtocolError("Tool arguments are not valid JSON", status) from None
    if not isinstance(result, dict):
        raise ProtocolError("Tool arguments must be a JSON object", status)
    return result


# Hints that exist in one protocol only and have no equivalent in the other.
# Dropping them keeps the call usable instead of failing it outright.
IGNORABLE_FIELDS = {
    # Anthropic context editing (auto-trims old tool results).  Purely a
    # server-side optimisation — without it the history is simply kept whole.
    "context_management",
    # Anthropic server-side safety-classification directive (Claude Code
    # >= 2.1.296 sends it by default).  It asks the *server* to run a
    # classifier over the turn; a Chat-protocol upstream has no equivalent,
    # and dropping it changes no translated content.
    "safeguards",
}


def unsupported(body: dict, allowed: set[str]):
    extra = {k for k, v in body.items() if v is not None and k not in allowed and k not in IGNORABLE_FIELDS}
    if extra:
        raise ProtocolError(
            "Cannot translate these fields across protocols: "
            + ", ".join(sorted(extra))
        )


def text_blocks(value, name="content") -> str:
    if isinstance(value, str):
        return value
    pieces = []
    for block in array(value, name):
        obj(block, name)
        if block.get("type") not in {"text", "input_text", "output_text"}:
            raise ProtocolError(f"{name} only accepts text in this position")
        pieces.append(string(block.get("text"), "text"))
    return "\n".join(pieces)


def a_content_to_chat(value):
    if isinstance(value, str):
        return value
    content = []
    for block in array(value, "content"):
        obj(block, "content block")
        kind = block.get("type")
        if kind == "text":
            content.append({"type": "text", "text": string(block.get("text"), "text")})
        elif kind == "image":
            src = obj(block.get("source"), "image source")
            if src.get("type") == "base64":
                if src.get("media_type") not in {
                    "image/png",
                    "image/jpeg",
                    "image/gif",
                    "image/webp",
                }:
                    raise ProtocolError("Unsupported image media_type")
                url = (
                    "data:"
                    + src["media_type"]
                    + ";base64,"
                    + string(src.get("data"), "image data")
                )
            elif src.get("type") == "url":
                url = string(src.get("url"), "image url")
            else:
                raise ProtocolError("Image source must be base64 or url")
            content.append({"type": "image_url", "image_url": {"url": url}})
        else:
            raise ProtocolError("Unsupported Messages content block: " + str(kind))
    return content


def chat_content_to_a(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [{"type": "text", "text": value}] if value else []
    blocks = []
    for part in array(value, "content"):
        obj(part, "content part")
        kind = part.get("type")
        if kind == "text":
            blocks.append({"type": "text", "text": string(part.get("text"), "text")})
        elif kind == "image_url":
            image = obj(part.get("image_url"), "image_url")
            url = string(image.get("url"), "image url")
            if url.startswith("data:"):
                header, sep, data = url.partition(",")
                if not sep or not header.endswith(";base64"):
                    raise ProtocolError("Images must use base64 data URLs")
                media = header[5:-7]
                if media not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
                    raise ProtocolError("Unsupported image media type")
                source = {"type": "base64", "media_type": media, "data": data}
            else:
                if not url.startswith(("https://", "http://")):
                    raise ProtocolError("Unsupported image URL")
                source = {"type": "url", "url": url}
            blocks.append({"type": "image", "source": source})
        else:
            raise ProtocolError("Unsupported Chat content part: " + str(kind))
    return blocks


def anthropic_to_chat(body: dict, model: str) -> dict:
    unsupported(
        body,
        {
            "model",
            "messages",
            "system",
            "max_tokens",
            "stream",
            "temperature",
            "top_p",
            "top_k",
            "stop_sequences",
            "tools",
            "tool_choice",
            "thinking",
            "output_config",
            "metadata",
            "service_tier",
        },
    )
    messages = []
    system_text = text_blocks(body["system"], "system") if body.get("system") else ""
    raw_messages = array(body.get("messages"), "messages")
    if not raw_messages:
        raise ProtocolError("messages 不能为空")
    system_notes = []
    for raw in raw_messages:
        raw = obj(raw, "message")
        role = raw.get("role")
        if role == "system":
            # The Messages API also admits system-role messages inside
            # `messages` (Claude Code >= 2.1.296 injects project context this
            # way).  They are context, not turns, so fold their text into the
            # leading system message instead of failing the whole call.
            system_notes.append(
                text_blocks(raw.get("content", ""), "system message")
            )
            continue
        if role not in {"user", "assistant"}:
            raise ProtocolError("Messages role must be user or assistant")
        content = raw.get("content", "")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        ordinary, calls, results = [], [], []
        for block in array(content, "content"):
            obj(block, "block")
            kind = block.get("type")
            if kind == "tool_use":
                if role != "assistant":
                    raise ProtocolError("tool_use must be in assistant messages")
                calls.append(
                    {
                        "id": string(block.get("id"), "tool id"),
                        "type": "function",
                        "function": {
                            "name": string(block.get("name"), "tool name"),
                            "arguments": encoded(
                                arguments(block.get("input", {}), 400)
                            ),
                        },
                    }
                )
            elif kind == "tool_result":
                if role != "user":
                    raise ProtocolError("tool_result must be in user messages")
                result_content = block.get("content", "")
                if isinstance(result_content, list):
                    text_parts, image_parts = [], []
                    for part in result_content:
                        obj(part, "tool_result block")
                        if part.get("type") == "image":
                            image_parts.append(part)
                        elif part.get("type") == "text":
                            text_parts.append(part)
                        elif part.get("type") == "tool_reference":
                            # Deferred-tool-loading marker; tool schemas are
                            # forwarded in full, so the marker is a no-op here.
                            continue
                        else:
                            raise ProtocolError(
                                "Unsupported tool_result content: "
                                + str(part.get("type"))
                            )
                    value = text_blocks(text_parts, "tool_result")
                    if image_parts:
                        # Chat tool replies only accept text. Attach images to a
                        # following user turn after ALL tool replies are supplied.
                        ordinary.extend(
                            [
                                {
                                    "type": "text",
                                    "text": "Images returned by tool "
                                    + string(block.get("tool_use_id"), "tool_use_id")
                                    + ":",
                                },
                                *image_parts,
                            ]
                        )
                else:
                    value = string(result_content, "tool_result")
                if block.get("is_error"):
                    value = "Tool execution error:\n" + value
                results.append(
                    {
                        "role": "tool",
                        "tool_call_id": string(block.get("tool_use_id"), "tool_use_id"),
                        "content": value,
                    }
                )
            elif kind in {"thinking", "redacted_thinking"}:
                # Provider-specific signed/opaque history is non-portable, never
                # invent a signature or disclose it as user-visible text.
                continue
            elif kind == "tool_reference":
                # Deferred-tool-loading marker ({type, tool_name}) from Claude
                # Code >= 2.1.310.  The referenced tool's full schema travels in
                # the same request and is forwarded below, so the marker itself
                # carries nothing a Chat upstream needs.
                continue
            else:
                ordinary.append(block)
        messages.extend(results)
        if ordinary or calls or not results:
            message = {"role": role, "content": a_content_to_chat(ordinary)}
            if calls:
                message["tool_calls"] = calls
            messages.append(message)
    if system_notes:
        system_text = "\n".join(p for p in [system_text, *system_notes] if p)
    if system_text:
        messages.insert(0, {"role": "system", "content": system_text})
    result = {
        "model": model,
        "messages": messages,
        "max_tokens": body.get("max_tokens", 1024),
    }
    for key in ("temperature", "top_p", "stream"):
        if key in body:
            result[key] = body[key]
    if "stop_sequences" in body:
        result["stop"] = body["stop_sequences"]
    if body.get("top_k") is not None:
        raise ProtocolError("Chat Completions has no portable top_k equivalent")
    if body.get("service_tier") not in (None, "auto"):
        raise ProtocolError("Service tiers cannot be translated across providers")
    if "tools" in body:
        tools = []
        for tool in array(body["tools"], "tools"):
            obj(tool, "tool")
            kind = tool.get("type")
            if isinstance(kind, str) and kind.startswith("tool-search-tool"):
                # Claude Code >= 2.1.310 defers tool loading behind this
                # server-side search tool.  The deferred tools travel in the
                # same request with full schemas, so forwarding every custom
                # tool eagerly is the faithful translation; the search tool
                # itself has no Chat counterpart and is dropped.
                continue
            if kind not in (None, "custom"):
                raise ProtocolError(
                    "Native/server tools cannot be translated; use custom tools"
                )
            function = {
                "name": string(tool.get("name"), "tool name"),
                "parameters": obj(tool.get("input_schema", {}), "input_schema"),
            }
            for key in ("description", "strict"):
                if key in tool:
                    function[key] = tool[key]
            tools.append({"type": "function", "function": function})
        result["tools"] = tools
    if body.get("tool_choice"):
        choice = obj(body["tool_choice"], "tool_choice")
        kind = choice.get("type")
        if kind in {"auto", "none", "any"}:
            result["tool_choice"] = {"any": "required"}.get(kind, kind)
        elif kind == "tool":
            result["tool_choice"] = {
                "type": "function",
                "function": {"name": string(choice.get("name"), "tool name")},
            }
        else:
            raise ProtocolError("Unsupported tool_choice")
        if "disable_parallel_tool_use" in choice:
            result["parallel_tool_calls"] = not choice["disable_parallel_tool_use"]
    thinking = obj(body.get("thinking") or {}, "thinking")
    output = obj(body.get("output_config") or {}, "output_config")
    if output.get("format"):
        fmt = output["format"]
        if fmt.get("type") != "json_schema":
            raise ProtocolError("Unsupported structured output format")
        result["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "response",
                "schema": fmt["schema"],
                "strict": True,
            },
        }
    if thinking.get("type") == "disabled":
        result["reasoning_effort"] = "none"
    elif thinking.get("type") in {"enabled", "adaptive"}:
        effort = output.get("effort")
        budget = thinking.get("budget_tokens", 4096)
        result["reasoning_effort"] = (
            effort
            if effort in {"low", "medium", "high"}
            else ("low" if budget < 2048 else "medium" if budget < 8192 else "high")
        )
    return result


def chat_to_anthropic(body: dict, model: str) -> dict:
    unsupported(
        body,
        {
            "model",
            "messages",
            "max_tokens",
            "max_completion_tokens",
            "temperature",
            "top_p",
            "stop",
            "stream",
            "stream_options",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "reasoning_effort",
            "response_format",
            "n",
            "user",
            "metadata",
            "store",
            "service_tier",
            "chat_template_kwargs",
        },
    )
    if body.get("n", 1) != 1:
        raise ProtocolError("Messages supports one completion per request (n=1)")
    if body.get("service_tier") not in (None, "auto"):
        raise ProtocolError("Service tiers cannot be translated across providers")
    if body.get("chat_template_kwargs"):
        raise ProtocolError(
            "Chat template options are specific to the upstream provider"
        )
    system, messages = [], []
    raw_messages = array(body.get("messages"), "messages")
    if not raw_messages:
        raise ProtocolError("messages 不能为空")
    for raw in raw_messages:
        raw = obj(raw, "message")
        role = raw.get("role")
        content = raw.get("content")
        if role in {"system", "developer"}:
            system.append(text_blocks(content, "system"))
            continue
        if role == "tool":
            blocks = [
                {
                    "type": "tool_result",
                    "tool_use_id": string(raw.get("tool_call_id"), "tool_call_id"),
                    "content": chat_content_to_a(content),
                }
            ]
            role = "user"
        elif role in {"user", "assistant"}:
            blocks = chat_content_to_a(content)
            if raw.get("refusal"):
                blocks.append(
                    {"type": "text", "text": string(raw["refusal"], "refusal")}
                )
            for tool in raw.get("tool_calls") or []:
                if role != "assistant" or tool.get("type", "function") != "function":
                    raise ProtocolError("Invalid function call")
                function = obj(tool.get("function"), "function")
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": string(tool.get("id"), "tool id"),
                        "name": string(function.get("name"), "function name"),
                        "input": arguments(function.get("arguments", "{}"), 400),
                    }
                )
        else:
            raise ProtocolError("Unsupported Chat message role: " + str(role))
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].extend(blocks)
        else:
            messages.append({"role": role, "content": blocks})
    result = {
        "model": model,
        "messages": messages,
        "max_tokens": body.get("max_completion_tokens", body.get("max_tokens", 1024)),
    }
    if not messages:
        raise ProtocolError("至少需要一条 user 或 assistant 消息")
    if system:
        result["system"] = "\n\n".join(system)
    for key in ("temperature", "top_p", "stream"):
        if key in body:
            result[key] = body[key]
    if body.get("stop") is not None:
        result["stop_sequences"] = (
            [body["stop"]] if isinstance(body["stop"], str) else body["stop"]
        )
    if "tools" in body:
        tools = []
        for tool in array(body["tools"], "tools"):
            if tool.get("type") != "function":
                raise ProtocolError("Only function tools can be translated")
            function = obj(tool.get("function"), "function")
            item = {
                "name": string(function.get("name"), "function name"),
                "input_schema": obj(function.get("parameters", {}), "parameters"),
            }
            for key in ("description", "strict"):
                if key in function:
                    item[key] = function[key]
            tools.append(item)
        result["tools"] = tools
    choice = body.get("tool_choice", "auto")
    if isinstance(choice, str) and choice in {"auto", "none", "required"}:
        choice = {"type": {"required": "any"}.get(choice, choice)}
    elif isinstance(choice, dict) and choice.get("type") == "function":
        choice = {"type": "tool", "name": choice["function"]["name"]}
    else:
        raise ProtocolError("Unsupported Chat tool_choice")
    if "parallel_tool_calls" in body:
        choice["disable_parallel_tool_use"] = not body["parallel_tool_calls"]
    if result.get("tools"):
        result["tool_choice"] = choice
    effort = body.get("reasoning_effort")
    if effort in {"none", "minimal"}:
        result["thinking"] = {"type": "disabled"}
    elif effort:
        if effort not in {"low", "medium", "high", "xhigh"}:
            raise ProtocolError("Unsupported reasoning effort")
        budget = {"low": 1024, "medium": 4096, "high": 8192, "xhigh": 16384}[effort]
        if result["max_tokens"] <= 1024:
            raise ProtocolError("Thinking requires max_tokens > 1024")
        result["thinking"] = {
            "type": "enabled",
            "budget_tokens": min(budget, result["max_tokens"] - 1),
        }
        if "temperature" in result and result["temperature"] != 1:
            raise ProtocolError(
                "Anthropic budget thinking requires temperature=1 or omitted"
            )
        if choice["type"] in {"tool", "any"}:
            raise ProtocolError(
                "Forced tool selection cannot be combined with budget thinking"
            )
    fmt = body.get("response_format") or {}
    if fmt:
        if fmt.get("type") == "json_schema":
            result["output_config"] = {
                "format": {
                    "type": "json_schema",
                    "schema": fmt["json_schema"]["schema"],
                }
            }
        elif fmt.get("type") == "json_object":
            # Anthropic has no bare "must be valid JSON" switch.  Asking for it
            # in the system prompt keeps the call usable instead of failing it;
            # the concrete shape is normally already described by the client.
            note = "只输出一个合法的 JSON 对象，不要包含解释文字或 Markdown 代码围栏。"
            existing = result.get("system")
            result["system"] = f"{existing}\n\n{note}" if existing else note
        elif fmt.get("type") != "text":
            raise ProtocolError("Only JSON schema structured output is portable")
    return result


def chat_usage_to_a(usage: dict | None) -> dict:
    usage = usage or {}
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    cached = int(
        (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
    )
    return {
        "input_tokens": max(0, prompt - cached),
        "output_tokens": int(usage.get("completion_tokens", 0) or 0),
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": 0,
    }


def a_usage_to_chat(usage: dict | None) -> dict:
    usage = usage or {}
    cached = int(usage.get("cache_read_input_tokens", 0) or 0)
    prompt = (
        int(usage.get("input_tokens", 0) or 0)
        + cached
        + int(usage.get("cache_creation_input_tokens", 0) or 0)
    )
    completion = int(usage.get("output_tokens", 0) or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "prompt_tokens_details": {"cached_tokens": cached},
    }


def chat_response_to_a(data: dict, model: str) -> dict:
    choices = data.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ProtocolError("Expected one Chat completion", 502)
    choice = choices[0]
    message = obj(choice.get("message"), "message")
    content = chat_content_to_a(message.get("content"))
    if any(block["type"] != "text" for block in content):
        raise ProtocolError("Unsupported upstream output content", 502)
    if message.get("refusal"):
        content.append({"type": "text", "text": message["refusal"]})
    for call in message.get("tool_calls") or []:
        function = call["function"]
        content.append(
            {
                "type": "tool_use",
                "id": call["id"],
                "name": function["name"],
                "input": arguments(function["arguments"]),
            }
        )
    finish = choice.get("finish_reason")
    if finish not in {"stop", "length", "tool_calls", "content_filter"}:
        raise ProtocolError("Unsupported finish_reason", 502)
    return {
        "id": data.get("id") or uid("msg_"),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": {
            "stop": "end_turn",
            "length": "max_tokens",
            "tool_calls": "tool_use",
            "content_filter": "refusal",
        }.get(finish, "end_turn"),
        "stop_sequence": None,
        "usage": chat_usage_to_a(data.get("usage")),
    }


def a_response_to_chat(data: dict, model: str) -> dict:
    texts, calls, reasoning = [], [], []
    for block in array(data.get("content"), "content"):
        kind = block.get("type")
        if kind == "text":
            texts.append(block["text"])
        elif kind == "tool_use":
            calls.append(
                {
                    "id": block["id"],
                    "type": "function",
                    "function": {
                        "name": block["name"],
                        "arguments": encoded(block["input"]),
                    },
                }
            )
        elif kind == "thinking":
            reasoning.append(block.get("thinking", ""))
        elif kind == "redacted_thinking":
            continue
        else:
            raise ProtocolError("Unsupported upstream content block: " + str(kind), 502)
    message = {"role": "assistant", "content": "".join(texts) if texts else None}
    if calls:
        message["tool_calls"] = calls
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    stop = data.get("stop_reason")
    if stop not in {"end_turn", "stop_sequence", "max_tokens", "tool_use", "refusal"}:
        raise ProtocolError("Unportable upstream stop_reason: " + str(stop), 502)
    return {
        "id": data.get("id") or uid("chatcmpl_"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "logprobs": None,
                "finish_reason": {
                    "max_tokens": "length",
                    "tool_use": "tool_calls",
                    "refusal": "content_filter",
                }.get(stop, "stop"),
            }
        ],
        "usage": a_usage_to_chat(data.get("usage")),
    }


def responses_to_chat(body: dict) -> dict:
    unsupported(
        body,
        {
            "model",
            "input",
            "instructions",
            "max_output_tokens",
            "temperature",
            "top_p",
            "stream",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "reasoning",
            "text",
            "store",
            "metadata",
            "truncation",
        },
    )
    source = body.get("input", [])
    messages = []
    if body.get("instructions"):
        messages.append({"role": "system", "content": body["instructions"]})
    if isinstance(source, str):
        messages.append({"role": "user", "content": source})
    else:
        for item in array(source, "input"):
            obj(item, "input item")
            kind = item.get("type", "message")
            if kind == "function_call":
                call = {
                    "id": string(item.get("call_id"), "call_id"),
                    "type": "function",
                    "function": {
                        "name": string(item.get("name"), "function name"),
                        "arguments": string(item.get("arguments"), "arguments"),
                    },
                }
                if messages and messages[-1]["role"] == "assistant":
                    messages[-1].setdefault("tool_calls", []).append(call)
                else:
                    messages.append(
                        {"role": "assistant", "content": None, "tool_calls": [call]}
                    )
            elif kind == "function_call_output":
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": string(item.get("call_id"), "call_id"),
                        "content": text_blocks(item.get("output"), "function output"),
                    }
                )
            elif kind == "message":
                content = item.get("content", "")
                if isinstance(content, list):
                    parts = []
                    for part in content:
                        if part.get("type") in {"input_text", "output_text", "text"}:
                            parts.append({"type": "text", "text": part["text"]})
                        elif part.get("type") == "input_image":
                            if part.get("file_id"):
                                raise ProtocolError("File IDs are provider-specific")
                            parts.append(
                                {
                                    "type": "image_url",
                                    "image_url": {"url": part["image_url"]},
                                }
                            )
                        else:
                            raise ProtocolError(
                                "Unsupported Responses content: "
                                + str(part.get("type"))
                            )
                    content = parts
                messages.append({"role": item.get("role", "user"), "content": content})
            else:
                raise ProtocolError("Unsupported Responses input item: " + str(kind))
    result = {
        "model": body.get("model"),
        "messages": messages,
        "max_tokens": body.get("max_output_tokens", 1024),
    }
    for key in ("temperature", "top_p", "stream", "parallel_tool_calls"):
        if key in body:
            result[key] = body[key]
    reasoning = obj(body.get("reasoning") or {}, "reasoning")
    unsupported(reasoning, {"effort"})
    if reasoning.get("effort"):
        result["reasoning_effort"] = reasoning["effort"]
    if body.get("truncation") not in (None, "disabled"):
        raise ProtocolError("Automatic truncation requires native Responses")
    if body.get("store"):
        raise ProtocolError("Stored response state requires native Responses")
    unsupported(obj(body.get("text") or {}, "text"), {"format"})
    if body.get("tools") is not None:
        result["tools"] = []
        for tool in body["tools"]:
            if tool.get("type") != "function":
                raise ProtocolError(
                    "Responses server tools need a native Responses provider"
                )
            result["tools"].append(
                {
                    "type": "function",
                    "function": {
                        k: v
                        for k, v in tool.items()
                        if k in {"name", "description", "parameters", "strict"}
                    },
                }
            )
    choice = body.get("tool_choice")
    if choice is not None:
        result["tool_choice"] = (
            {"type": "function", "function": {"name": choice["name"]}}
            if isinstance(choice, dict) and choice.get("type") == "function"
            else choice
        )
    fmt = (body.get("text") or {}).get("format")
    if fmt:
        result["response_format"] = (
            {
                "type": "json_schema",
                "json_schema": {k: v for k, v in fmt.items() if k != "type"},
            }
            if fmt.get("type") == "json_schema"
            else fmt
        )
    if not result.get("messages"):
        raise ProtocolError("input 不能为空")
    return result


def chat_to_responses(data: dict, model: str, response_id: str | None = None) -> dict:
    choice = data["choices"][0]
    message = choice["message"]
    items = []
    content = []
    if message.get("content") is not None:
        content.append(
            {"type": "output_text", "text": message["content"], "annotations": []}
        )
    if message.get("refusal"):
        content.append({"type": "refusal", "refusal": message["refusal"]})
    if content:
        items.append(
            {
                "id": uid("msg_"),
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": content,
            }
        )
    for call in message.get("tool_calls") or []:
        items.append(
            {
                "id": uid("fc_"),
                "type": "function_call",
                "call_id": call["id"],
                "name": call["function"]["name"],
                "arguments": call["function"]["arguments"],
                "status": "completed",
            }
        )
    usage = data.get("usage") or {}
    input_tokens = int(usage.get("prompt_tokens", 0))
    output_tokens = int(usage.get("completion_tokens", 0))
    return {
        "id": response_id or uid("resp_"),
        "object": "response",
        "created_at": data.get("created", int(time.time())),
        "status": (
            "incomplete" if choice.get("finish_reason") == "length" else "completed"
        ),
        "model": model,
        "output": items,
        "output_text": message.get("content") or "",
        "error": None,
        "incomplete_details": (
            {"reason": "max_output_tokens"}
            if choice.get("finish_reason") == "length"
            else None
        ),
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "input_tokens_details": {
                "cached_tokens": int(
                    (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
                )
            },
            "output_tokens_details": {
                "reasoning_tokens": int(
                    (usage.get("completion_tokens_details") or {}).get(
                        "reasoning_tokens", 0
                    )
                )
            },
        },
    }

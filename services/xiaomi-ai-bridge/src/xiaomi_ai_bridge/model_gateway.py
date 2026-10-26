"""Protocol selection, bounded transport and conversion shared by all endpoints."""

from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
import json
import math
from typing import Any

import aiohttp
from aiohttp import web

from .model_registry import ModelRegistry, _url, _model_id, _model_settings
from .openrouter_policy import enforce_free_request
from .protocols import (
    ProtocolError,
    anthropic_to_chat as _anthropic_to_openai,
    chat_to_anthropic as _openai_to_anthropic,
    a_response_to_chat,
    chat_response_to_a,
    responses_to_chat,
    chat_to_responses,
)
from .protocol_stream import events, sse, translate_stream

MAX_RESPONSE_BYTES = 16 * 1024 * 1024

# 哪些上游状态码值得换一个模型重试。
# 4xx 里只有「渠道侧」的问题才算：401/403 密钥失效、402 余额不足、404 模型下线、
# 408/409/425/429 限流或临时冲突。400/413/422 是客户端请求本身有问题，
# 换模型也是同样的错，会把诊断信息搅乱，所以不重试。
RETRYABLE_STATUSES = frozenset({401, 402, 403, 404, 408, 409, 425, 429, 500, 502, 503, 504})


def retryable_status(status: int) -> bool:
    return status in RETRYABLE_STATUSES


def error_payload(protocol: str, message: str, status: int = 400) -> dict:
    kind = (
        "authentication_error"
        if status == 401
        else (
            "permission_error"
            if status == 403
            else (
                "rate_limit_error"
                if status == 429
                else (
                    "not_found_error"
                    if status == 404
                    else "api_error" if status >= 500 else "invalid_request_error"
                )
            )
        )
    )
    if protocol == "anthropic":
        return {"type": "error", "error": {"type": kind, "message": message}}
    return {"error": {"type": kind, "message": message, "param": None, "code": kind}}


def safe_error(raw: bytes, status: int, provider: dict) -> str:
    try:
        payload = json.loads(raw)
        err = payload.get("error") or {}
        message = err.get("message") if isinstance(err, dict) else None
    except (ValueError, AttributeError):
        message = None
    if not isinstance(message, str):
        return f"Upstream returned HTTP {status}"
    # Known credentials are always scrubbed even if an upstream echoes them.
    key = provider.get("api_key")
    if key:
        message = message.replace(key, "[redacted]")
    from .logging_setup import scrub

    return scrub(message)[:400]


async def read_body(response, limit=MAX_RESPONSE_BYTES) -> bytes:
    parts = bytearray()
    async for chunk in response.content.iter_chunked(65536):
        parts.extend(chunk)
        if len(parts) > limit:
            raise ProtocolError("Upstream response exceeds limit", 502)
    return bytes(parts)


def has_images(value) -> bool:
    if isinstance(value, dict):
        if value.get("type") in {
            "image",
            "image_url",
            "input_image",
            "input_audio",
            "audio",
            "video",
            "input_video",
            "file",
            "document",
            "input_file",
        }:
            return True
        return any(has_images(v) for v in value.values())
    return isinstance(value, list) and any(has_images(v) for v in value)


def prepare_request(
    provider: dict, surface: str, body: dict, config: dict | None = None
):
    try:
        return _prepare_request(provider, surface, body, config)
    except (KeyError, TypeError, AttributeError, IndexError) as exc:
        raise ProtocolError(
            "Malformed request field; check messages, content, tools and model settings"
        ) from exc


def _prepare_request(
    provider: dict, surface: str, body: dict, config: dict | None = None
):
    config = config or {}
    body = copy.deepcopy(body)
    if surface not in {"openai", "anthropic", "responses"}:
        raise ProtocolError("Unsupported client protocol")
    if not isinstance(body, dict):
        raise ProtocolError("Request must be an object")
    if type(body.get("stream", False)) is not bool:
        raise ProtocolError("stream must be boolean")
    provider_protocol = provider.get("protocol", "openai")
    if provider_protocol not in {"openai", "anthropic"}:
        raise ProtocolError("Unsupported upstream protocol")
    native = (
        surface == provider_protocol
        or surface == "responses"
        and provider_protocol == "openai"
    )
    target = "responses" if surface == "responses" and native else provider_protocol
    warnings = []
    if not body.get("model") or not isinstance(body["model"], str):
        raise ProtocolError("A valid model is required")
    if not isinstance(
        body.get("input" if surface == "responses" else "messages"),
        (str, list) if surface == "responses" else list,
    ):
        raise ProtocolError("Invalid messages/input")
    if has_images(body.get("messages", body.get("input"))) and not config.get(
        "multimodal", False
    ):
        raise ProtocolError("此模型未启用多模态输入；纯文本内容块不受影响")
    # Context size is metadata, not a tokenizer. Keep an honest transport bound;
    # the upstream enforces its actual model-specific token window.
    if len(json.dumps(body, ensure_ascii=False).encode()) > 16 * 1024 * 1024:
        raise ProtocolError("Request exceeds 16 MiB", 413)
    client_max_key = (
        "max_output_tokens"
        if surface == "responses"
        else (
            "max_completion_tokens"
            if surface == "openai" and "max_completion_tokens" in body
            else "max_tokens"
        )
    )
    client_max = body.get(client_max_key)
    if client_max is None:
        client_max = config.get("max_output_tokens", 1024)
    if type(client_max) is not int or not 1 <= client_max <= 2000000:
        raise ProtocolError("Output token limit must be a positive integer")
    if config.get("max_output_tokens") is not None:
        client_max = min(client_max, int(config["max_output_tokens"]))
    body[client_max_key] = client_max
    if not native:
        if surface == "responses":
            body = responses_to_chat(body)
        if surface == "anthropic":
            body = _anthropic_to_openai(body, body["model"])
        elif target == "anthropic":
            body = _openai_to_anthropic(body, body["model"])
        warnings.extend(
            ["provider-cache-hints-not-portable", "signed-thinking-not-portable"]
        )
    max_key = (
        "max_output_tokens"
        if target == "responses"
        else (
            "max_completion_tokens"
            if target == "openai" and "max_completion_tokens" in body
            else "max_tokens"
        )
    )
    requested = body.get(max_key)
    if requested is None:
        requested = config.get("max_output_tokens", 1024)
    if type(requested) is not int or not 1 <= requested <= 2000000:
        raise ProtocolError("Output token limit must be a positive integer")
    configured = config.get("max_output_tokens")
    if configured is not None:
        requested = min(requested, int(configured))
    body[max_key] = requested
    if target == "openai" and max_key == "max_completion_tokens":
        body.pop("max_tokens", None)
    # Use configured sampling only if the client did not specify any sampling
    # or thinking policy. Do not override explicit token budgets or tool intent.
    if (
        "temperature" not in body
        and "top_p" not in body
        and not body.get("thinking")
        and not body.get("reasoning")
        and not body.get("reasoning_effort")
    ):
        if config.get("temperature") is not None:
            body["temperature"] = config["temperature"]
    for key in ("temperature", "top_p"):
        if key in body and body[key] is not None:
            value = body[key]
            upper = 1 if key == "top_p" or target == "anthropic" else 2
            if (
                type(value) not in {int, float}
                or not math.isfinite(value)
                or not 0 <= value <= upper
            ):
                raise ProtocolError(f"{key} outside supported range")
    effort = config.get("reasoning_level", "auto")
    if effort != "auto" and not any(
        k in body for k in ("thinking", "reasoning", "reasoning_effort")
    ):
        if target == "anthropic":
            if effort == "off":
                body["thinking"] = {"type": "disabled"}
            else:
                if requested <= 1024:
                    # 思考开关是模型级配置，调用方给的预算往往只考虑了「说多久」
                    # （内置助手默认 220，为的是语音朗读长度）。这里按模型设置
                    # 抬高生成预算；模型自身没留出空间时仍然如实报错。
                    ceiling = int(config.get("max_output_tokens") or 4096)
                    if ceiling <= 1024:
                        raise ProtocolError(
                            "Thinking requires more than 1024 output tokens"
                        )
                    requested = min(ceiling, 4096)
                    body[max_key] = requested
                body["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": min(
                        {"low": 1024, "medium": 4096, "high": 8192}[effort],
                        requested - 1,
                    ),
                }
                body.pop("temperature", None)
        elif target == "responses":
            body["reasoning"] = {"effort": "none" if effort == "off" else effort}
        else:
            body["reasoning_effort"] = "none" if effort == "off" else effort
    if target == "anthropic" and body.get("thinking", {}).get("type") == "enabled":
        budget = body["thinking"].get("budget_tokens")
        if type(budget) is not int or not 1024 <= budget < requested:
            raise ProtocolError(
                "Thinking budget must be >=1024 and less than max_tokens"
            )
        if body.get("tool_choice", {}).get("type") in {"tool", "any"}:
            raise ProtocolError(
                "Forced tool selection cannot be combined with thinking"
            )
    if body.get("stream") and target == "openai" and not native:
        body["stream_options"] = {"include_usage": True}
    body = enforce_free_request(provider, body)
    base = str(provider["base_url"]).rstrip("/")
    for suffix in ("/chat/completions", "/messages", "/responses"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    path = {
        "openai": "/chat/completions",
        "anthropic": "/messages",
        "responses": "/responses",
    }[target]
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if body.get("stream") else "application/json",
    }
    if target == "anthropic":
        headers.update(
            {
                "x-api-key": provider.get("api_key", ""),
                "anthropic-version": "2023-06-01",
            }
        )
    else:
        headers["Authorization"] = "Bearer " + provider.get("api_key", "")
    return target, body, base + path, headers, warnings


def convert_response(data: dict, source: str, target: str, model: str):
    if source == target:
        return data
    chat = a_response_to_chat(data, model) if source == "anthropic" else data
    if target == "anthropic":
        return chat_response_to_a(chat, model)
    if target == "responses":
        return chat_to_responses(chat, model)
    return chat


@asynccontextmanager
async def open_upstream(
    session, provider, surface, body, config=None, client_headers=None
):
    target, upstream, url, headers, warnings = prepare_request(
        provider, surface, body, config
    )
    if target == surface == "anthropic" and client_headers:
        for key in ("anthropic-version", "anthropic-beta"):
            if client_headers.get(key):
                headers[key] = client_headers[key]
    # 按提供方决定是否走代理：只有配了 proxy 的上游才会经过它，其余一律直连。
    proxy = provider.get("proxy") or None
    incoming = await session.post(
        url, json=upstream, headers=headers, allow_redirects=False, proxy=proxy
    )
    try:
        # Only an explicitly unavailable endpoint can trigger the Chat adapter.
        # Never retry a generation after 429/5xx, network failure or any SSE data.
        if target == "responses" and incoming.status in {404, 405, 501}:
            incoming.release()
            try:
                adapted = responses_to_chat(body)
            except (KeyError, TypeError, AttributeError, IndexError) as exc:
                raise ProtocolError("Malformed Responses input") from exc
            target, upstream, url, headers, warnings = prepare_request(
                provider, "openai", adapted, config
            )
            if upstream.get("stream"):
                upstream["stream_options"] = {"include_usage": True}
            warnings.append("responses-adapted-via-chat")
            incoming = await session.post(
                url, json=upstream, headers=headers, allow_redirects=False, proxy=proxy
            )
        yield incoming, target, warnings
    finally:
        incoming.release()


async def forward_request(
    session, provider, requested_protocol, body, model_config=None, client_headers=None
):
    """Nonstream adapter also used by administrative model probes."""
    if body.get("stream"):
        raise ProtocolError("Use streaming transport for stream=true")
    async with open_upstream(
        session, provider, requested_protocol, body, model_config, client_headers
    ) as (response, target, warnings):
        raw = await read_body(response)
        status = response.status
        extra = {
            k: response.headers[k]
            for k in ("Retry-After", "X-Request-ID")
            if k in response.headers
        }
    extra["Content-Type"] = "application/json"
    if warnings:
        extra["X-AI-Gateway-Warnings"] = ",".join(warnings)
    if not 200 <= status < 300:
        return (
            (status if status >= 400 else 502),
            extra,
            json.dumps(
                error_payload(
                    requested_protocol, safe_error(raw, status, provider), status
                ),
                ensure_ascii=False,
            ).encode(),
        )
    try:
        data = json.loads(raw)
    except ValueError:
        raise ProtocolError("Upstream did not return valid JSON", 502) from None
    if not isinstance(data, dict):
        raise ProtocolError("Invalid upstream response object", 502)
    if data.get("error"):
        return (
            502,
            extra,
            json.dumps(
                error_payload(requested_protocol, safe_error(raw, 502, provider), 502)
            ).encode(),
        )
    try:
        converted = convert_response(data, target, requested_protocol, body["model"])
    except ProtocolError as exc:
        raise ProtocolError(str(exc), 502) from exc
    except (KeyError, TypeError, IndexError, AttributeError, ValueError) as exc:
        raise ProtocolError("Malformed upstream response", 502) from exc
    return status, extra, json.dumps(converted, ensure_ascii=False).encode()


class _UpstreamRefused(Exception):
    """上游在开始流式输出之前就拒绝了请求。

    这个时点很关键：还没有向客户端写过任何字节，所以换下一个模型重试是安全的。
    一旦 `response.prepare()` 之后再失败，就只能如实报错，不能重放。
    """

    def __init__(self, status: int, message: str, retry_after: str | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after


async def relay_stream(request, candidates, surface, body, metrics: dict):
    """按顺序尝试候选模型，只在上游拒绝且尚未输出时切换到下一个。"""
    attempts: list[dict[str, Any]] = []
    refused: _UpstreamRefused | None = None
    for index, (provider, model, config) in enumerate(candidates):
        attempt_body = {**body, "model": model}
        try:
            response = await _relay_stream_once(
                request, provider, surface, attempt_body, config, metrics
            )
        except _UpstreamRefused as exc:
            refused = exc
            attempts.append(
                {"provider_id": provider.get("id"), "model": model, "status": exc.status}
            )
            if index + 1 < len(candidates) and retryable_status(exc.status):
                continue
            metrics["error"] = exc.message
            metrics["fallback_attempts"] = attempts
            return web.json_response(
                error_payload(surface, exc.message, exc.status),
                status=exc.status,
                headers={"Retry-After": exc.retry_after} if exc.retry_after else None,
            )
        except (aiohttp.ClientError, TimeoutError, ProtocolError) as exc:
            # 连接失败、超时、上游没按约定返回 SSE/JSON——都发生在向客户端写出
            # 任何字节之前，此时换下一个模型是安全的。请求本身的错误已在
            # gateway_proxy 里提前判掉，不会走到这里。
            refused_status = getattr(exc, "status", 502)
            attempts.append(
                {
                    "provider_id": provider.get("id"),
                    "model": model,
                    "status": refused_status,
                    "error": type(exc).__name__,
                }
            )
            if index + 1 < len(candidates):
                continue
            metrics["error"] = f"上游未给出可用响应：{type(exc).__name__}"
            metrics["fallback_attempts"] = attempts
            raise
        metrics["provider_id"] = provider.get("id")
        metrics["provider"] = provider
        metrics["protocol"] = str(provider.get("protocol") or "")
        metrics["model"] = model
        if attempts:
            metrics["fallback_attempts"] = attempts
            metrics["fallback_used"] = True
        return response
    raise ProtocolError(refused.message if refused else "没有可用的模型", 503)


async def _relay_stream_once(request, provider, surface, body, config, metrics: dict):
    timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_read=120)
    response = None
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with open_upstream(
            session, provider, surface, body, config, request.headers
        ) as (incoming, target, warnings):
            native = target == surface
            status = incoming.status
            metrics["http_status"] = (
                status if status >= 400 or 200 <= status < 300 else 502
            )
            if not 200 <= status < 300:
                raw = await read_body(incoming, 1024 * 1024)
                raise _UpstreamRefused(
                    status if status >= 400 else 502,
                    safe_error(raw, status, provider),
                    incoming.headers.get("Retry-After"),
                )
            if (
                "text/event-stream"
                not in incoming.headers.get("Content-Type", "").lower()
            ):
                raise ProtocolError(
                    "Upstream ignored stream=true (expected text/event-stream)", 502
                )
            response_headers = {
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            }
            if warnings:
                response_headers["X-AI-Gateway-Warnings"] = ",".join(warnings)
            response = web.StreamResponse(status=200, headers=response_headers)
            await response.prepare(request)
            try:
                if native:
                    terminal = False
                    usage = {}
                    async for name, raw in events(incoming.content):
                        if raw == "[DONE]":
                            terminal = True
                        else:
                            try:
                                event = json.loads(raw)
                            except ValueError:
                                raise ProtocolError(
                                    "Invalid upstream SSE JSON", 502
                                ) from None
                            if isinstance(event, dict):
                                if (
                                    event.get("error")
                                    or name == "error"
                                    or event.get("type") == "response.failed"
                                ):
                                    raise ProtocolError(
                                        "Upstream reported a streaming error", 502
                                    )
                                if event.get("usage"):
                                    usage.update(event["usage"])
                                if event.get("message", {}).get("usage"):
                                    usage.update(event["message"]["usage"])
                                if event.get("response", {}).get("usage"):
                                    usage.update(event["response"]["usage"])
                                if event.get("type") in {
                                    "message_stop",
                                    "response.completed",
                                    "response.incomplete",
                                }:
                                    terminal = True
                        metrics["usage"] = usage.copy()
                        await response.write(sse(raw, name or None))
                        if terminal:
                            break
                    if not terminal:
                        raise ProtocolError(
                            "Upstream stream disconnected before completion", 502
                        )
                    metrics["usage"] = usage
                else:
                    async for payload in translate_stream(
                        incoming.content, target, surface, body["model"], metrics
                    ):
                        await response.write(payload)
                await response.write_eof()
                metrics["http_status"] = 200
            except asyncio.CancelledError:
                metrics.update(http_status=499, error="Client disconnected")
                raise
            except (ConnectionResetError, BrokenPipeError):
                metrics.update(http_status=499, error="Client disconnected")
            except (
                ProtocolError,
                aiohttp.ClientError,
                TimeoutError,
                KeyError,
                TypeError,
                ValueError,
                AttributeError,
            ) as exc:
                metrics.update(
                    http_status=502,
                    error="Upstream stream failed: " + type(exc).__name__,
                )
                error = error_payload(
                    surface, "Upstream stream failed before completion", 502
                )
                try:
                    await response.write(
                        sse(error, "error" if surface != "openai" else None)
                    )
                    await response.write_eof()
                except (ConnectionResetError, BrokenPipeError):
                    pass
            return response

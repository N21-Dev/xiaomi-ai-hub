from __future__ import annotations

import asyncio
import hashlib
import html
import json
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from .bridge import Bridge
from .config import Settings
from .conversations import conversation_id
from .logging_setup import scrub
from .openrouter_policy import enforce_free_request
from .panel_auth import COOKIE_NAME, SESSION_SECONDS, PanelAuth, safe_next
from . import app_release
from .model_gateway import error_payload, forward_request, prepare_request, relay_stream, retryable_status
from .model_registry import RegistryError
from .protocols import ProtocolError
from .state import RuntimeStatus
from .xiaomi_login import XiaomiWebLogin

WEB_DIR = Path(__file__).with_name("web")


_CSRF_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_MODEL_LIST_PATHS = ("/v1/models", "/v1/models/", "/models", "/models/")
_GATEWAY_METHODS = {
    **{path: ("GET", "HEAD") for path in _MODEL_LIST_PATHS},
    "/v1/chat/completions": ("POST",),
    "/v1/messages": ("POST",),
    "/v1/responses": ("POST",),
}


def _upstream_usage(payload: bytes) -> tuple[int, int, int]:
    """Read token counts from either an OpenAI or an Anthropic response body."""

    try:
        data = json.loads(payload)
    except (ValueError, UnicodeError):
        return 0, 0, 0
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return 0, 0, 0
    numbers: list[int] = []
    for candidate in (
        usage.get("prompt_tokens", usage.get("input_tokens", 0)),
        usage.get("completion_tokens", usage.get("output_tokens", 0)),
        usage.get("total_tokens", 0),
    ):
        try:
            numbers.append(max(0, int(candidate)))
        except (TypeError, ValueError):
            numbers.append(0)
    # Messages reports uncached input separately; Responses already includes it.
    for key in ('cache_read_input_tokens', 'cache_creation_input_tokens'):
        try:
            numbers[0] += max(0, int(usage.get(key, 0) or 0))
        except (TypeError, ValueError):
            pass
    return numbers[0], numbers[1], numbers[2] or numbers[0] + numbers[1]


def _upstream_error(payload: bytes, fallback: str) -> str:
    try:
        data = json.loads(payload)
    except (ValueError, UnicodeError):
        return fallback
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict) and error.get("message"):
        return scrub(str(error["message"]))[:400]
    if isinstance(error, str) and error:
        return scrub(error)[:400]
    return fallback


def _same_origin_request(request: web.Request, scheme: str | None = None) -> bool:
    """Reject browser requests whose Origin/Referer points at another site.

    Requests without either header remain valid for local agents and health
    integrations.  They still need their endpoint-specific authentication
    (for example the bearer token on ``/api/hub/events``).
    """

    expected_scheme = (scheme or request.scheme).casefold()
    expected_host = request.host.casefold()
    origin = request.headers.get("Origin", "").strip()
    referer = request.headers.get("Referer", "").strip()
    candidate = origin or referer
    if not candidate:
        return True
    if candidate.casefold() == "null":
        return False
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return False
    if not parsed.scheme or not parsed.netloc:
        return False
    return (
        parsed.scheme.casefold() == expected_scheme
        and parsed.netloc.casefold() == expected_host
    )


def gateway_candidates(
    models: Any,
    provider: dict[str, Any],
    active_model: str,
    model_config: dict[str, Any],
) -> list[tuple[dict[str, Any], str, dict[str, Any]]]:
    """主模型 + 备用链，去重后按尝试顺序返回。

    pin（``X-AI-Gateway-Provider``）只决定“先走哪个渠道”，不表示“只准走这个渠道”。
    内置助手为了同名模型消歧会固定带 pin，如果把它当成禁止兜底，
    音箱和面板就永远切不到备用模型。
    """

    candidates: list[tuple[dict[str, Any], str, dict[str, Any]]] = [
        (provider, active_model, model_config)
    ]
    seen = {(str(provider.get("id") or ""), active_model)}
    fallback_targets = getattr(models, "fallback_targets", None)
    for fallback, fallback_model in (
        fallback_targets() if callable(fallback_targets) else []
    ):
        key = (str(fallback.get("id") or ""), fallback_model)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            (fallback, fallback_model, models.model_settings(fallback, fallback_model))
        )
    return candidates


async def start_health_server(
    status: RuntimeStatus,
    bridge: Bridge,
    xiaomi_login: XiaomiWebLogin,
    settings: Settings,
    host: str,
    port: int,
) -> web.AppRunner:
    panel_auth = (
        PanelAuth(settings.state_dir, settings.panel_trusted_proxy_cidrs)
        if getattr(settings, "panel_auth_required", False) else None
    )

    def apply_security_headers(request: web.Request, response: web.StreamResponse) -> None:
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        if panel_auth and (request.path not in _GATEWAY_METHODS or response.status == 401):
            response.headers["Cache-Control"] = "no-store, no-transform" if request.path == "/api/hub/stream" and response.status == 200 else "no-store"

    @web.middleware
    async def security_headers(
        request: web.Request, handler: web.RequestHandler
    ) -> web.StreamResponse:
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = web.Response(status=exc.status, reason=exc.reason, headers=exc.headers, body=exc.body)
        apply_security_headers(request, response)
        return response

    @web.middleware
    async def require_panel_login(
        request: web.Request, handler: web.RequestHandler
    ) -> web.StreamResponse:
        if panel_auth is None:
            return await handler(request)
        if request.path in {"/login", "/login.js", "/app.css", "/speaker.png", "/healthz", "/api/auth/login", "/api/auth/session", "/api/hub/events", "/api/app/android", "/api/app/android/download"}:
            return await handler(request)
        if request.path in _GATEWAY_METHODS and (request.method == "OPTIONS" or panel_auth.direct_lan_gateway(request)):
            return await handler(request)
        session = panel_auth.session(request)
        if session is None:
            if request.path == "/":
                raise web.HTTPFound("/login")
            return web.json_response({"ok": False, "code": "panel_auth_required", "error": "请先登录管理面板"}, status=401)
        # Cross-origin gateway requests never receive cookie-authenticated access.
        if request.path in _GATEWAY_METHODS and not _same_origin_request(request, panel_auth.scheme(request)):
            return web.json_response({"ok": False, "error": "跨站请求已拒绝"}, status=403)
        if request.method in _CSRF_METHODS and not panel_auth.csrf_valid(request, session):
            return web.json_response({"ok": False, "code": "panel_csrf_invalid", "error": "登录校验已更新，请刷新页面后重试"}, status=403)
        return await handler(request)

    @web.middleware
    async def same_origin_guard(
        request: web.Request, handler: web.RequestHandler
    ) -> web.StreamResponse:
        # Gateway routes use their own token/no-auth policy. Management writes
        # retain the existing same-origin check.
        scheme = panel_auth.scheme(request) if panel_auth else None
        if request.path not in _GATEWAY_METHODS and request.method in _CSRF_METHODS and not _same_origin_request(request, scheme):
            return web.json_response(
                {"ok": False, "error": "跨站请求已拒绝"}, status=403
            )
        return await handler(request)

    @web.middleware
    async def gateway_preflight(
        request: web.Request, handler: web.RequestHandler
    ) -> web.StreamResponse:
        methods = _GATEWAY_METHODS.get(request.path)
        if methods and request.method == "OPTIONS":
            if request.headers.get("Access-Control-Request-Method", "GET").upper() not in methods:
                return web.json_response({"error": {"message": "Method not allowed"}}, status=405)
            return web.Response(status=204)
        return await handler(request)

    async def gateway_response_headers(request: web.Request, response: web.StreamResponse) -> None:
        if request.path not in _GATEWAY_METHODS or not request.headers.get("Origin"):
            return
        # This hook runs before headers are sent, including streamed responses
        # and errors. No browser cookies/credentials are accepted through CORS.
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = ", ".join((*_GATEWAY_METHODS[request.path], "OPTIONS"))
        requested = request.headers.get("Access-Control-Request-Headers", "Authorization, Content-Type, X-API-Key, Anthropic-Version, Anthropic-Beta")
        if len(requested) <= 2048:
            response.headers["Access-Control-Allow-Headers"] = requested
        response.headers["Access-Control-Expose-Headers"] = "Content-Type, Retry-After, X-Request-ID, X-AI-Gateway-Warnings"
        response.headers["Access-Control-Max-Age"] = "600"
        if request.method == "OPTIONS" and request.headers.get("Access-Control-Request-Private-Network") == "true":
            response.headers["Access-Control-Allow-Private-Network"] = "true"

    app = web.Application(
        # State has its own 16 KiB bound; field dictionaries may describe
        # absent properties as well and are validated separately by the hub.
        client_max_size=16 * 1024 * 1024,
        middlewares=[security_headers, gateway_preflight, same_origin_guard, require_panel_login],
    )
    app.on_response_prepare.append(gateway_response_headers)

    async def security_response_headers(request: web.Request, response: web.StreamResponse) -> None:
        apply_security_headers(request, response)

    app.on_response_prepare.append(security_response_headers)

    # Version URLs by the deployed content so existing browsers fetch each new
    # frontend release, even when their old asset responses were cached.
    asset_version = hashlib.sha256(
        (WEB_DIR / "app.js").read_bytes() + (WEB_DIR / "app.css").read_bytes()
    ).hexdigest()[:16]
    index_html = (WEB_DIR / "index.html").read_text(encoding="utf-8").replace(
        "__ASSET_VERSION__", asset_version
    )

    async def index(request: web.Request) -> web.StreamResponse:
        session = panel_auth.session(request) if panel_auth else None
        page = index_html.replace("__PANEL_CSRF__", html.escape(session.csrf, quote=True) if session else "").replace("__PANEL_USER__", html.escape(session.username, quote=True) if session else "")
        return web.Response(text=page, content_type="text/html",
                            headers={"Cache-Control": "no-cache"})

    async def login_page(request: web.Request) -> web.StreamResponse:
        if panel_auth is None or panel_auth.session(request):
            raise web.HTTPFound(safe_next(request.query.get("next", "/")))
        page = (WEB_DIR / "login.html").read_text(encoding="utf-8").replace("__ASSET_VERSION__", asset_version)
        return web.Response(text=page, content_type="text/html")

    async def login_script(_: web.Request) -> web.StreamResponse:
        return web.FileResponse(WEB_DIR / "login.js")

    async def login_session(request: web.Request) -> web.StreamResponse:
        session = panel_auth.session(request) if panel_auth else None
        return web.json_response({
            "enabled": panel_auth is not None,
            "configured": panel_auth.configured if panel_auth else False,
            "authenticated": session is not None,
            **({"username": session.username, "csrf": session.csrf, "expires_at": session.expires_at} if session else {}),
        })

    async def panel_login(request: web.Request) -> web.StreamResponse:
        if panel_auth is None:
            return web.json_response({"ok": False, "error": "未启用面板登录"}, status=409)
        if not panel_auth.configured:
            return web.json_response({"ok": False, "error": "管理员账号尚未初始化，请在服务器设置"}, status=503)
        if request.content_type != "application/json":
            return web.json_response({"ok": False, "error": "登录请求需要 JSON 格式"}, status=415)
        try:
            body_bytes = bytearray()
            async for chunk in request.content.iter_chunked(4096):
                body_bytes.extend(chunk)
                if len(body_bytes) > 4096:
                    return web.json_response({"ok": False, "error": "登录内容过长"}, status=413)
            body = json.loads(body_bytes)
            if not isinstance(body, dict):
                raise ValueError()
            username, password = body.get("username"), body.get("password")
            if not isinstance(username, str) or not isinstance(password, str) or not 1 <= len(username) <= 80 or not 1 <= len(password) <= 256:
                raise ValueError()
            username.encode("utf-8")
            password.encode("utf-8")
        except (ValueError, TypeError, UnicodeError):
            return web.json_response({"ok": False, "error": "请输入有效的账号和密码"}, status=400)
        token, retry_after = await panel_auth.login(username, password, panel_auth.client_address(request))
        if retry_after:
            return web.json_response({"ok": False, "error": "登录尝试过多，请稍后重试", "retry_after": retry_after}, status=429, headers={"Retry-After": str(retry_after)})
        if token is None:
            return web.json_response({"ok": False, "error": "账号或密码不正确"}, status=401)
        response = web.json_response({"ok": True, "next": safe_next(str(body.get("next") or "/"))})
        response.set_cookie(COOKIE_NAME, token, max_age=SESSION_SECONDS, httponly=True, secure=panel_auth.scheme(request) == "https", samesite="Lax", path="/")
        return response

    async def panel_logout(request: web.Request) -> web.StreamResponse:
        if panel_auth:
            panel_auth.logout(request)
        response = web.json_response({"ok": True})
        response.del_cookie(COOKIE_NAME, path="/", httponly=True, samesite="Lax", secure=bool(panel_auth and panel_auth.scheme(request) == "https"))
        return response

    async def stylesheet(_: web.Request) -> web.StreamResponse:
        return web.FileResponse(WEB_DIR / "app.css", headers={"Cache-Control": "no-cache"})

    async def javascript(_: web.Request) -> web.StreamResponse:
        return web.FileResponse(WEB_DIR / "app.js", headers={"Cache-Control": "no-cache"})

    async def speaker_image(_: web.Request) -> web.StreamResponse:
        return web.FileResponse(WEB_DIR / "speaker.png")

    async def healthz(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def readyz(_: web.Request) -> web.Response:
        return web.json_response(
            status.public_dict(), status=200 if status.ready else 503
        )

    async def android_release(_: web.Request) -> web.StreamResponse:
        """安卓客户端的更新信息。没有发布过安装包时返回 404，客户端据此显示「已是最新」。"""
        resolved = app_release.resolve(settings.state_dir)
        if resolved is None:
            return web.json_response(
                {"ok": False, "error": "尚未发布安卓安装包"}, status=404
            )
        manifest, _ = resolved
        payload = {
            "ok": True,
            **manifest,
            "download": "/api/app/android/download",
        }
        return web.json_response(payload, headers={"Cache-Control": "no-store"})

    async def android_apk(_: web.Request) -> web.StreamResponse:
        resolved = app_release.resolve(settings.state_dir)
        if resolved is None:
            raise web.HTTPNotFound()
        manifest, path = resolved
        return web.FileResponse(
            path,
            headers={
                "Content-Type": "application/vnd.android.package-archive",
                "Content-Disposition": f'attachment; filename="{manifest["file"]}"',
                "Cache-Control": "no-store",
            },
        )

    async def public_status(_: web.Request) -> web.Response:
        return web.json_response(status.public_dict())

    async def public_config(_: web.Request) -> web.Response:
        return web.json_response(bridge.public_config())

    async def update_config(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise TypeError("请求内容必须是 JSON 对象")
            config = bridge.update_connection_config(body)
            return web.json_response({"ok": True, "config": config})
        except (TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
            return web.json_response({"ok": False, "error": scrub(exc)}, status=400)

    async def reconnect(_: web.Request) -> web.Response:
        bridge.request_reconnect()
        return web.json_response({"ok": True})

    async def test_ai(_: web.Request) -> web.Response:
        try:
            answer = await bridge.test_ai()
            return web.json_response({"ok": True, "answer": answer})
        except Exception as exc:  # noqa: BLE001 - API error boundary
            return web.json_response({"ok": False, "error": scrub(exc)}, status=502)

    async def test_tts(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            await bridge.test_tts(str(body.get("text") or ""))
            return web.json_response({"ok": True})
        except (ValueError, RuntimeError) as exc:
            return web.json_response({"ok": False, "error": scrub(exc)}, status=409)

    async def models(_: web.Request) -> web.Response:
        return web.json_response(bridge.public_models())

    async def save_model(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise TypeError("请求内容必须是 JSON 对象")
            result = await bridge.save_model(body, request.match_info.get("provider_id"))
            return web.json_response({"ok": True, **result})
        except (TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
            return web.json_response({"ok": False, "error": scrub(exc)}, status=400)

    async def activate_model(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise TypeError("请求内容必须是 JSON 对象")
            result = await bridge.activate_model(
                request.match_info["provider_id"], str(body.get("model") or ""), revision=body.get("revision")
            )
            return web.json_response({"ok": True, **result})
        except RegistryError as exc:
            return web.json_response({"ok": False, "error": scrub(exc), "code": exc.code}, status=exc.status)
        except (TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
            return web.json_response({"ok": False, "error": scrub(exc)}, status=400)

    async def discover_models(request: web.Request) -> web.Response:
        try:
            return web.json_response({"ok": True, **await bridge.discover_models(request.match_info["provider_id"])})
        except (TypeError, ValueError, OSError, aiohttp.ClientError) as exc:
            return web.json_response({"ok": False, "error": scrub(exc)}, status=502)

    async def test_model(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            result = await bridge.test_model(request.match_info["provider_id"], str(body.get("model") or ""))
            return web.json_response(result, status=200 if result.get("ok") else 502)
        except (TypeError, ValueError, OSError, json.JSONDecodeError, aiohttp.ClientError) as exc:
            return web.json_response({"ok": False, "error": scrub(exc)}, status=502)

    async def update_model_settings(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise TypeError("请求内容必须是 JSON 对象")
            model = str(body.pop("model") or "")
            return web.json_response({"ok": True, **await bridge.update_model_settings(request.match_info["provider_id"], model, body)})
        except (TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
            return web.json_response({"ok": False, "error": scrub(exc)}, status=400)

    async def remove_model(request: web.Request) -> web.Response:
        try:
            body = await request.json() if request.can_read_body else {}
            if not isinstance(body, dict):
                raise TypeError("请求内容必须是 JSON 对象")
            result = await bridge.remove_model(
                request.match_info["provider_id"],
                deactivate=body.get("deactivate") is True,
                replacement=body.get("replacement"),
                revision=body.get("revision"),
            )
            return web.json_response({"ok": True, **result})
        except (TypeError, ValueError, OSError) as exc:
            return web.json_response({"ok": False, "error": scrub(exc)}, status=400)

    async def test_models(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise TypeError("请求内容必须是 JSON 对象")
            return web.json_response({"ok": True, **await bridge.test_models(body.get("targets"))})
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return web.json_response({"ok": False, "error": scrub(exc)}, status=400)

    async def set_model_role(request: web.Request) -> web.Response:
        """Point a named role (speaker / health) at its own model.

        An empty provider_id clears the role, meaning "follow the default
        model".  Registered under /api/models/actions/* on purpose: the
        two-segment /api/models/{provider_id} route would otherwise swallow it.
        """

        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise TypeError("请求内容必须是 JSON 对象")
            result = await bridge.set_model_role(
                str(body.get("role") or ""),
                str(body.get("provider_id") or ""),
                str(body.get("model") or ""),
                revision=body.get("revision"),
            )
            return web.json_response({"ok": True, **result})
        except (TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
            return web.json_response({"ok": False, "error": scrub(exc)}, status=400)

    async def set_model_fallback(request: web.Request) -> web.Response:
        """设置备用模型链。

        主模型请求不通时按这里的顺序依次尝试。空数组表示关闭兜底。
        注册在 /api/models/actions/* 下，避免被两段式 /api/models/{provider_id} 吞掉。
        """

        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise TypeError("请求内容必须是 JSON 对象")
            result = await bridge.models.set_fallback(
                body.get("fallback", []), revision=body.get("revision")
            )
            return web.json_response({"ok": True, **result})
        except (TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
            return web.json_response({"ok": False, "error": scrub(exc)}, status=400)

    async def add_models(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise TypeError("请求内容必须是 JSON 对象")
            result = await bridge.add_models(request.match_info["provider_id"], body.get("models"), body.get("revision"))
            return web.json_response({"ok": True, **result})
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return web.json_response({"ok": False, "error": scrub(exc)}, status=400)

    async def remove_models(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise TypeError("请求内容必须是 JSON 对象")
            result = await bridge.remove_models(request.match_info["provider_id"], body.get("models"), body.get("revision"))
            return web.json_response({"ok": True, **result})
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return web.json_response({"ok": False, "error": scrub(exc)}, status=400)

    async def model_probes(_: web.Request) -> web.Response:
        return web.json_response(bridge.public_model_probes())

    async def model_usage(request: web.Request) -> web.Response:
        try:
            hours = int(request.query.get("hours", "24"))
        except ValueError:
            hours = 24
        return web.json_response(bridge.model_usage_summary(hours))

    async def model_logs(request: web.Request) -> web.Response:
        def number(name: str, default: int) -> int:
            try:
                return int(request.query.get(name, str(default)))
            except ValueError:
                return default

        return web.json_response(bridge.model_usage_logs(
            limit=number("limit", 100),
            offset=number("offset", 0),
            provider_id=request.query.get("provider_id", "").strip()[:64],
            model=request.query.get("model", "").strip()[:200],
            only_errors=request.query.get("errors", "") in {"1", "true", "yes"},
        ))

    async def clear_model_logs(_: web.Request) -> web.Response:
        try:
            return web.json_response({"ok": True, **bridge.clear_model_logs()})
        except (OSError, sqlite3.Error) as exc:
            return web.json_response({"ok": False, "error": scrub(exc)}, status=500)

    async def gateway_models(request: web.Request) -> web.Response:
        if not bridge.models.authorized(request.headers.get("Authorization", "") or ("Bearer " + request.headers["x-api-key"] if request.headers.get("x-api-key") else "")):
            return web.json_response(error_payload("anthropic" if request.path.startswith("/v1/messages") else "openai", "AI Gateway token 无效", 401), status=401)
        public = bridge.public_models()
        models = sorted({model for provider in public.get("providers", []) if provider.get("enabled") for model in provider.get("models", [])})
        return web.json_response(
            {"object": "list", "data": [{"id": model, "object": "model", "created": 0, "owned_by": "ai-gateway"} for model in models]},
            headers={"Cache-Control": "no-store"},
        )

    def _log_gateway(context: dict[str, Any], status_code: int, started: float) -> None:
        """Persist one forwarding row.  Telemetry must never break a request."""

        if context.get("provider") is None and not context.get("model"):
            return
        provider = context.get("provider") or {}
        prompt, completion, total = context.get("tokens") or (0, 0, 0)
        try:
            bridge.model_usage.record_request(
                provider_id=str(provider.get("id") or ""),
                provider_name=str(provider.get("name") or ""),
                model=str(context.get("model") or ""),
                protocol=str(context.get("protocol") or ""),
                surface=str(context.get("surface") or ""),
                status=status_code,
                latency_ms=round((time.monotonic() - started) * 1000),
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total,
                error=str(context.get("error") or ""),
            )
        except Exception:  # noqa: BLE001 - the ledger is best-effort telemetry
            pass

    async def gateway_proxy(request: web.Request) -> web.Response:
        if not bridge.models.authorized(request.headers.get("Authorization", "") or ("Bearer " + request.headers["x-api-key"] if request.headers.get("x-api-key") else "")):
            return web.json_response(error_payload("anthropic" if request.path.startswith("/v1/messages") else "openai", "AI Gateway token 无效", 401), status=401)
        started = time.monotonic()
        context: dict[str, Any] = {
            "surface": {"/v1/responses": "responses", "/v1/messages": "anthropic"}.get(request.path, "openai"),
            "protocol": "", "model": "", "provider": None, "tokens": None, "error": "",
        }
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("请求内容必须是 JSON 对象")
            requested_model = str(body.get("model") or "")
            pinned_provider = request.headers.get("X-AI-Gateway-Provider")
            if pinned_provider is not None:
                provider, active_model = bridge.models.resolve(requested_model or None, provider_id=pinned_provider)
            else:
                provider, active_model = bridge.models.resolve(requested_model or None)
            if provider is None:
                raise ValueError("尚未配置可用模型")
            context["provider"] = provider
            context["protocol"] = str(provider.get("protocol") or "")
            body["model"] = active_model
            context["model"] = active_model
            model_config = bridge.models.model_settings(provider, active_model)
            requested_protocol = {"/v1/messages": "anthropic", "/v1/responses": "responses"}.get(request.path, "openai")
            # 请求本身写错时（缺字段、值域越界）换模型也是同样的错，
            # 先在上游调用前判一次，避免重试把 400 变成 502。
            prepare_request(provider, requested_protocol, body, model_config)
            # 备用模型链：主模型拒绝或不可达时按顺序换下一个。
            # pin 只影响首选渠道，不影响是否兜底（见 gateway_candidates）。
            candidates = gateway_candidates(
                bridge.models, provider, active_model, model_config
            )
            context["fallback_count"] = len(candidates) - 1
            if body.get("stream") is True:
                metrics: dict[str, Any] = {}
                response = await relay_stream(request, candidates, requested_protocol, body, metrics)
                context["tokens"] = _upstream_usage(json.dumps({"usage": metrics.get("usage", {})}).encode())
                context["error"] = metrics.get("error", "")
                if metrics.get("provider"):
                    context["provider"] = metrics["provider"]
                if metrics.get("protocol"):
                    context["protocol"] = str(metrics["protocol"])
                if metrics.get("model"):
                    context["model"] = str(metrics["model"])
                context["fallback_attempts"] = metrics.get("fallback_attempts")
                _log_gateway(context, metrics.get("http_status", response.status), started)
                return response
            attempts: list[dict[str, Any]] = []
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
                for index, (candidate, candidate_model, candidate_config) in enumerate(candidates):
                    body["model"] = candidate_model
                    try:
                        status_code, headers, content = await forward_request(
                            session, candidate, requested_protocol, body, candidate_config,
                            client_headers=request.headers,
                        )
                    except (aiohttp.ClientError, TimeoutError):
                        attempts.append({"provider_id": candidate.get("id"), "model": candidate_model, "status": 502})
                        if index + 1 < len(candidates):
                            continue
                        raise
                    except ProtocolError as exc:
                        # 走到这里说明不是请求本身的问题（已在循环前判过），
                        # 而是这个候选模型不适合或上游响应坏了：换下一个。
                        attempts.append({"provider_id": candidate.get("id"), "model": candidate_model, "status": exc.status})
                        if index + 1 < len(candidates):
                            continue
                        raise
                    if status_code >= 400 and index + 1 < len(candidates) and retryable_status(status_code):
                        attempts.append({"provider_id": candidate.get("id"), "model": candidate_model, "status": status_code})
                        continue
                    context["provider"] = candidate
                    context["protocol"] = str(candidate.get("protocol") or "")
                    context["model"] = candidate_model
                    context["fallback_attempts"] = attempts or None
                    context["tokens"] = _upstream_usage(content)
                    if status_code >= 400:
                        context["error"] = _upstream_error(content, f"上游 HTTP {status_code}")
                    _log_gateway(context, status_code, started)
                    return web.Response(status=status_code, headers=headers, body=content)
        except asyncio.CancelledError:
            context["error"] = "Client disconnected"
            _log_gateway(context, 499, started)
            raise
        except ProtocolError as exc:
            context["error"] = str(exc)
            _log_gateway(context, exc.status, started)
            return web.json_response(error_payload(context["surface"], str(exc), exc.status), status=exc.status)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            context["error"] = scrub(exc)
            _log_gateway(context, 400, started)
            return web.json_response(error_payload(context["surface"], "Invalid request: " + scrub(exc)), status=400)
        except (aiohttp.ClientError, TimeoutError):
            context["error"] = "上游模型暂时不可用"
            _log_gateway(context, 502, started)
            return web.json_response(error_payload(context["surface"], "上游模型暂时不可用", 502), status=502)

    async def chat(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("请求内容必须是 JSON 对象")
            chat_id = conversation_id(body.get("conversation_id"))
            # ``context: false`` is a short turn: nothing earlier is sent, and
            # nothing is remembered afterwards.
            answer = await bridge.ask_text(
                str(body.get("prompt") or ""),
                use_context=body.get("context") is not False,
                conversation_id=chat_id,
            )
            return web.json_response({"ok": True, "answer": answer, "conversation_id": chat_id})
        except (ValueError, TypeError) as exc:
            return web.json_response({"ok": False, "error": scrub(exc)}, status=400)
        except RuntimeError as exc:
            return web.json_response({"ok": False, "error": scrub(exc)}, status=502)

    async def stop_turn(_: web.Request) -> web.Response:
        await bridge.stop_active_turn()
        return web.json_response({"ok": True})

    async def clear_context(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("请求内容必须是 JSON 对象")
            if body.get("conversation_id") is not None:
                bridge.clear_chat(conversation_id(body["conversation_id"]))
            # Legacy callers have no shared panel history to clear.
            return web.json_response({"ok": True})
        except (ValueError, TypeError) as exc:
            return web.json_response({"ok": False, "error": scrub(exc)}, status=400)

    async def start_xiaomi_login(request: web.Request) -> web.Response:
        try:
            body = await request.json() if request.can_read_body else {}
            sid = str(body.get("sid") or "micoapi") if isinstance(body, dict) else ""
            login = await xiaomi_login.start(sid)
            return web.json_response({"ok": True, **login})
        except Exception as exc:  # noqa: BLE001 - external auth boundary
            return web.json_response({"ok": False, "error": scrub(exc)}, status=502)

    async def xiaomi_login_status(_: web.Request) -> web.Response:
        return web.json_response(xiaomi_login.public_status())

    async def home_state(_: web.Request) -> web.Response:
        return web.json_response(bridge.public_home())

    async def automations(_: web.Request) -> web.Response:
        return web.json_response(bridge.public_automations(), headers={"Cache-Control": "no-store"})

    async def automation_action(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("请求内容必须是 JSON 对象")
            result = bridge.automation_action(
                request.match_info["id"], str(body.get("action") or "")
            )
            return web.json_response(result)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return web.json_response({"ok": False, "error": scrub(exc)}, status=400)

    async def health_source(request: web.Request) -> web.Response:
        try:
            payload = (
                await request.json()
                if request.method == "POST"
                else dict(request.query)
            )
            if not isinstance(payload, dict) or set(payload) - {"date", "days"}:
                raise ValueError("健康数据查询仅支持日期和 7/30 天范围")
            days = payload.get("days", 7)
            if isinstance(days, str) and days in {"7", "30"}:
                days = int(days)
            if type(days) is not int or days not in {7, 30}:
                raise ValueError("健康趋势范围仅支持 7 或 30 天")
            result = await bridge.health_source.snapshot(
                date=payload.get("date"), days=days, refresh=request.method == "POST"
            )
            return web.json_response(result, headers={"Cache-Control": "no-store"})
        except (TypeError, ValueError) as exc:
            return web.json_response(
                {"available": False, "message": scrub(exc)}, status=400
            )

    async def home_layout(request: web.Request) -> web.Response:
        return web.json_response(
            await bridge.home_layout(refresh=request.method == "POST")
        )

    async def home_extensions(request: web.Request) -> web.Response:
        try:
            result = await bridge.home_extensions(
                request.match_info["key"], refresh=request.method == "POST"
            )
            return web.json_response(result)
        except ValueError as exc:
            return web.json_response(
                {"available": False, "message": scrub(exc)}, status=400
            )

    async def discover_home(_: web.Request) -> web.Response:
        try:
            state = await bridge.discover_home_devices()
            return web.json_response({"ok": True, **state})
        except Exception as exc:  # noqa: BLE001 - Xiaomi cloud boundary
            return web.json_response({"ok": False, "error": scrub(exc)}, status=409)

    async def update_home_device(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
                raise TypeError("enabled 必须是布尔值")
            device = bridge.update_home_device(
                request.match_info["key"],
                alias=str(body.get("alias") or ""),
                enabled=body["enabled"],
            )
            return web.json_response({"ok": True, "device": device})
        except (TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
            return web.json_response({"ok": False, "error": scrub(exc)}, status=400)

    async def test_home_action(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise TypeError("请求内容必须是 JSON 对象")
            result = await bridge.test_home_action(
                str(body.get("device") or ""),
                str(body.get("action") or ""),
                body.get("value"),
            )
            return web.json_response(result, status=200 if result.get("ok") else 409)
        except (TypeError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            return web.json_response({"ok": False, "error": scrub(exc)}, status=409)

    async def hub_state(_: web.Request) -> web.Response:
        return web.json_response(bridge.public_hub())

    async def hub_storage(_: web.Request) -> web.Response:
        try:
            result = await asyncio.to_thread(bridge.hub.storage_status)
            return web.json_response(result, headers={"Cache-Control": "no-store"})
        except (OSError, sqlite3.Error) as exc:
            return web.json_response(
                {"ok": False, "error": "存储状态暂不可用：" + scrub(exc)}, status=503
            )

    async def hub_resource(request: web.Request) -> web.Response:
        try:
            resource = await asyncio.to_thread(
                bridge.hub.get_resource, request.match_info["resource_id"]
            )
            return web.json_response(resource, headers={"Cache-Control": "no-store"})
        except (TypeError, ValueError, OSError, sqlite3.Error) as exc:
            return web.json_response({"ok": False, "error": scrub(exc)}, status=404)

    async def device_state(request: web.Request) -> web.Response:
        try:
            result = await bridge.read_home_device(request.match_info["key"])
            return web.json_response(result, status=200 if result.get("ok") else 409)
        except ValueError as exc:
            return web.json_response(
                {"ok": False, "message": scrub(exc), "error": scrub(exc)}, status=400
            )

    async def control_device(request: web.Request) -> web.Response:
        try:
            result = await bridge.control_home_device(
                request.match_info["key"], await request.json()
            )
            return web.json_response(result, status=200 if result.get("ok") else 409)
        except (TypeError, ValueError) as exc:
            return web.json_response(
                {
                    "ok": False,
                    "outcome": "failed",
                    "message": scrub(exc),
                    "error": scrub(exc),
                },
                status=400,
            )

    async def hub_history(request: web.Request) -> web.Response:
        try:
            limit = int(request.query.get("limit", "60"))
            hours = int(request.query["hours"]) if "hours" in request.query else None
            projection = request.query.get("projection", "full")
            if projection not in {"full", "chart"}:
                raise ValueError("历史展示方式只支持 full 或 chart")
            return web.json_response(
                await asyncio.to_thread(
                    bridge.hub.history,
                    request.match_info["resource_id"],
                    limit,
                    hours,
                    before=request.query.get("before"),
                    projection=projection,
                ),
                headers={"Cache-Control": "no-store"},
            )
        except (TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
            return web.json_response({"ok": False, "error": scrub(exc)}, status=400)

    async def disable_hub_resource(request: web.Request) -> web.Response:
        try:
            disabled = bridge.disable_hub_resource(request.match_info["resource_id"])
            return web.json_response({"ok": True, "disabled": disabled})
        except (TypeError, ValueError, OSError) as exc:
            return web.json_response({"ok": False, "error": scrub(exc)}, status=400)

    async def refresh_hub(_: web.Request) -> web.Response:
        return web.json_response(
            {"ok": True, "hub": await bridge.sample_home_states(force=True)}
        )

    async def hub_stream(_: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(_)
        queue = bridge.hub.subscribe()
        try:
            if panel_auth and panel_auth.session(_) is None:
                await response.write(b'data: {"type":"panel.auth_required"}\n\n')
                return response
            initial = json.dumps(
                {"type": "hub.snapshot", "hub": bridge.public_hub()},
                ensure_ascii=False,
            )
            await response.write(f"data: {initial}\n\n".encode())
            while True:
                if panel_auth and panel_auth.session(_) is None:
                    await response.write(b'data: {"type":"panel.auth_required"}\n\n')
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=20)
                    if panel_auth and panel_auth.session(_) is None:
                        continue
                    if event.get("type") == "hub.resync":
                        event = {"type": "hub.snapshot", "hub": bridge.public_hub()}
                    payload = json.dumps(event, ensure_ascii=False)
                    await response.write(f"data: {payload}\n\n".encode())
                except TimeoutError:
                    if panel_auth and panel_auth.session(_) is None:
                        continue
                    payload = json.dumps(
                        {"type": "hub.snapshot", "hub": bridge.public_hub()},
                        ensure_ascii=False,
                    )
                    await response.write(f"data: {payload}\n\n".encode())
        except (asyncio.CancelledError, ConnectionResetError, RuntimeError):
            pass
        finally:
            bridge.hub.unsubscribe(queue)
        return response

    async def ingest_hub_event(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            resource = bridge.ingest_hub_event(
                body, request.headers.get("Authorization", "")
            )
            return web.json_response({"ok": True, "resource": resource}, status=202)
        except PermissionError as exc:
            return web.json_response({"ok": False, "error": scrub(exc)}, status=401)
        except (TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
            return web.json_response({"ok": False, "error": scrub(exc)}, status=400)

    app.router.add_get("/", index)
    app.router.add_get("/login", login_page)
    app.router.add_get("/login.js", login_script)
    app.router.add_get("/api/auth/session", login_session)
    app.router.add_post("/api/auth/login", panel_login)
    app.router.add_post("/api/auth/logout", panel_logout)
    app.router.add_get("/app.css", stylesheet)
    app.router.add_get("/app.js", javascript)
    app.router.add_get("/speaker.png", speaker_image)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/api/app/android", android_release)
    app.router.add_get("/api/app/android/download", android_apk)
    app.router.add_get("/readyz", readyz)
    app.router.add_get("/status", public_status)
    app.router.add_get("/api/status", public_status)
    app.router.add_get("/api/config", public_config)
    app.router.add_put("/api/config", update_config)
    app.router.add_post("/api/actions/reconnect", reconnect)
    app.router.add_post("/api/actions/test-ai", test_ai)
    app.router.add_post("/api/actions/test-tts", test_tts)
    app.router.add_get("/api/models", models)
    app.router.add_put("/api/models", save_model)
    app.router.add_put("/api/models/{provider_id}", save_model)
    app.router.add_post("/api/models/{provider_id}/activate", activate_model)
    app.router.add_post("/api/models/{provider_id}/discover", discover_models)
    app.router.add_post("/api/models/{provider_id}/test", test_model)
    app.router.add_put("/api/models/{provider_id}/settings", update_model_settings)
    app.router.add_delete("/api/models/{provider_id}", remove_model)
    app.router.add_post("/api/models/actions/test", test_models)
    app.router.add_post("/api/models/actions/role", set_model_role)
    app.router.add_post("/api/models/actions/fallback", set_model_fallback)
    app.router.add_get("/api/models/actions/probes", model_probes)
    app.router.add_get("/api/models/actions/usage", model_usage)
    app.router.add_get("/api/models/actions/logs", model_logs)
    app.router.add_delete("/api/models/actions/logs", clear_model_logs)
    app.router.add_post("/api/models/{provider_id}/models/add", add_models)
    app.router.add_post("/api/models/{provider_id}/models/remove", remove_models)
    for model_path in _MODEL_LIST_PATHS:
        app.router.add_get(model_path, gateway_models)
    app.router.add_post("/v1/chat/completions", gateway_proxy)
    app.router.add_post("/v1/responses", gateway_proxy)
    app.router.add_post("/v1/messages", gateway_proxy)
    app.router.add_post("/api/actions/chat", chat)
    app.router.add_post("/api/actions/stop-turn", stop_turn)
    app.router.add_post("/api/actions/clear-context", clear_context)
    app.router.add_post("/api/auth/xiaomi/start", start_xiaomi_login)
    app.router.add_get("/api/auth/xiaomi/status", xiaomi_login_status)
    app.router.add_get("/api/home", home_state)
    app.router.add_get("/api/automations", automations)
    app.router.add_post("/api/automations/{id}/action", automation_action)
    app.router.add_get("/api/health-source", health_source)
    app.router.add_post("/api/health-source/refresh", health_source)
    app.router.add_get("/api/home/layout", home_layout)
    app.router.add_post("/api/home/layout/refresh", home_layout)
    app.router.add_post("/api/home/discover", discover_home)
    app.router.add_put("/api/home/devices/{key}", update_home_device)
    app.router.add_get("/api/home/devices/{key}/state", device_state)
    app.router.add_get("/api/home/devices/{key}/extensions", home_extensions)
    app.router.add_post("/api/home/devices/{key}/extensions/refresh", home_extensions)
    app.router.add_post("/api/home/devices/{key}/control", control_device)
    app.router.add_post("/api/home/test", test_home_action)
    app.router.add_get("/api/hub", hub_state)
    app.router.add_get("/api/hub/storage", hub_storage)
    app.router.add_get("/api/hub/resources/{resource_id}", hub_resource)
    app.router.add_post("/api/hub/refresh", refresh_hub)
    app.router.add_get("/api/hub/resources/{resource_id}/history", hub_history)
    app.router.add_delete("/api/hub/resources/{resource_id}", disable_hub_resource)
    app.router.add_get("/api/hub/stream", hub_stream)
    app.router.add_post("/api/hub/events", ingest_hub_event)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    return runner

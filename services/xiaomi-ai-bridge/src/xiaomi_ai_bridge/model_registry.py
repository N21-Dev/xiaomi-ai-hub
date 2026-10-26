"""Provider configuration control plane, independent from protocol forwarding.

Uses a compatible versioned JSON store. Mutations validate before atomic commit;
network discovery never holds the write lock. Public documents exclude keys.
"""
from __future__ import annotations

import asyncio
import copy
import hmac
import json
import math
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from .openrouter_policy import free_catalog_model, free_model_id

MODEL_STORE_FILE = "ai-models.json"
GATEWAY_TOKEN_FILE = "ai-gateway-token"
MAX_MODELS = 500
MAX_FALLBACK = 3
MAX_BATCH_TEST = 60
BATCH_TEST_CONCURRENCY = 3
SETTING_KEYS = {"reasoning_level", "context_window", "max_output_tokens", "multimodal", "temperature"}
# Named roles let one gateway serve different assistants with different
# models.  A role with no target follows the default (active) model, so the
# single-switch behaviour stays the default and the roles are an override.
ROLE_ALIASES = {"speaker-active": "speaker"}
ROLE_LABELS = {"speaker": "小爱音箱"}


class RegistryError(ValueError):
    def __init__(self, message: str, code: str = "invalid_config", status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def _url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise RegistryError("API 地址格式不正确")
    text = value.strip().rstrip("/")
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        raise RegistryError("API 地址格式不正确") from None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or port == 0
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or any(char.isspace() or ord(char) < 32 for char in text) or "\\" in text):
        raise RegistryError("API 地址须为 HTTP(S)，且不能包含账号、查询参数或空白")
    for endpoint in ("/chat/completions", "/messages", "/responses", "/models"):
        if parsed.path.endswith(endpoint):
            parsed = parsed._replace(path=parsed.path[:-len(endpoint)])
            break
    if parsed.path in {"", "/"}:
        parsed = parsed._replace(path="/v1")
    return urlunsplit(parsed).rstrip("/")


def _proxy_url(value: object) -> str:
    """校验并规范化上游代理地址；空值表示直连。

    只允许 http/https：aiohttp 原生不认 socks，要支持得额外装 aiohttp_socks。
    与其等到发请求时才炸，不如在保存这一步就拒掉。凭据可以写在 userinfo 里，
    但绝不会回显给面板（见 _mask_proxy）。
    """
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise RegistryError("代理地址必须是文本")
    text = value.strip()
    if not text:
        return ""
    if len(text) > 512:
        raise RegistryError("代理地址过长")
    if any(ord(char) < 33 or ord(char) > 126 for char in text):
        raise RegistryError("代理地址不能包含空白或不可见字符")
    try:
        parsed = urlsplit(text)
        parsed.port
    except ValueError:
        raise RegistryError("代理端口无效") from None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RegistryError("代理地址需形如 http://192.168.1.10:7890")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise RegistryError("代理地址不能带路径、查询参数或片段")
    return text


def _proxy_host(value: object) -> str:
    """代理的 scheme://host:port，去掉 userinfo —— 面板只该看到这部分。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        return ""
    if not parsed.hostname:
        return ""
    return f"{parsed.scheme}://{parsed.hostname}{f':{port}' if port else ''}"


def _proxy_has_auth(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        parsed = urlsplit(text)
    except ValueError:
        return False
    return bool(parsed.username or parsed.password)


def _model_id(item: object) -> str:
    value = item.get("id", item.get("model", "")) if isinstance(item, dict) else item
    if not isinstance(value, str) or not value.strip() or len(value) > 200 or any(ord(c) < 32 for c in value):
        raise RegistryError("模型 ID 须为 1–200 字符")
    return value.strip()


def _model_settings(item: object) -> dict[str, Any]:
    raw = item if isinstance(item, dict) else {}
    level = raw.get("reasoning_level", "auto")
    if level not in {"auto", "off", "low", "medium", "high"}:
        raise RegistryError("请选择有效的思考等级")
    result = {"model": _model_id(item), "reasoning_level": level}
    for key, default, low, high in (("context_window", 128000, 1024, 2000000), ("max_output_tokens", 4096, 64, 128000)):
        value = raw.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise RegistryError(f"{key} 必须是 {low}–{high} 之间的整数")
        result[key] = value
    if result["max_output_tokens"] > result["context_window"]:
        raise RegistryError("最大输出不能超过配置的上下文窗口")
    temperature = raw.get("temperature", 0.3)
    if type(temperature) not in {int, float} or not math.isfinite(temperature) or not 0 <= temperature <= 2:
        raise RegistryError("温度必须是 0–2 之间的有限数值")
    multimodal = raw.get("multimodal", False)
    if type(multimodal) is not bool:
        raise RegistryError("图片输入开关必须是布尔值")
    result.update(temperature=temperature, multimodal=multimodal)
    return result


def _entry(item: object, previous: object = None) -> dict[str, Any]:
    model = _model_id(item)
    prior = previous if isinstance(previous, dict) else {}
    raw = item if isinstance(item, dict) else {}
    config = {**{k: v for k, v in prior.items() if k in SETTING_KEYS},
              **{k: v for k, v in raw.items() if k in SETTING_KEYS}, "id": model}
    _model_settings(config)
    return config


def _atomic(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.chmod(name, 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class ModelRegistry:
    def __init__(self, state_dir: Path, settings: Any, usage: Any = None):
        self.path = Path(state_dir) / MODEL_STORE_FILE
        self.token_path = Path(state_dir) / GATEWAY_TOKEN_FILE
        self.settings = settings
        # Optional usage ledger.  The registry stays usable without one so
        # pure-configuration tests never need a database.
        self.usage = usage
        self._lock = asyncio.Lock()
        self._network_limit = asyncio.Semaphore(2)
        if not self.token_path.exists():
            _atomic(self.token_path, secrets.token_urlsafe(32))

    def gateway_token(self) -> str:
        return self.token_path.read_text(encoding="utf-8").strip()

    def authorized(self, authorization: str) -> bool:
        if not self.auth_required():
            return True
        return authorization.startswith("Bearer ") and hmac.compare_digest(authorization[7:].strip(), self.gateway_token())

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            providers = []
            if self.settings.ai_base_url and self.settings.ai_model:
                providers.append({"id": "env-default", "name": "当前环境配置", "protocol": "openai",
                                  "base_url": _url(self.settings.ai_base_url), "api_key": self.settings.ai_api_key,
                                  "models": [self.settings.ai_model], "enabled": True})
            return {"version": 2, "revision": 0, "providers": providers,
                    "active": {"provider_id": "env-default", "model": self.settings.ai_model} if providers else {"provider_id": "", "model": ""},
                    "roles": {}, "fallback": []}
        except (OSError, ValueError):
            raise RegistryError("模型配置文件无法读取，请检查服务端备份；未加载默认值覆盖它", "store_unavailable", 503) from None
        if not isinstance(data, dict) or not isinstance(data.get("providers"), list) or not isinstance(data.get("active"), dict):
            raise RegistryError("模型配置文件结构无效", "store_unavailable", 503)
        data.setdefault("revision", 0)
        if not isinstance(data.get("roles"), dict):
            data["roles"] = {}
        if not isinstance(data.get("fallback"), list):
            data["fallback"] = []
        return data

    def _write(self, data: dict[str, Any]) -> dict[str, Any]:
        """写盘并**返回写入后的快照**。

        调用方普遍需要新的 revision（前端会把它存起来用于下一次乐观锁提交），
        只返回 None 会让调用方拿着旧版本号继续操作，第二次必然 409。
        """

        written = {**data, "version": 2, "revision": data.get("revision", 0) + 1}
        _atomic(self.path, json.dumps(written, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        return written

    def _check_revision(self, data: dict, revision: Any) -> None:
        if revision is not None and (type(revision) is not int or revision != data.get("revision", 0)):
            raise RegistryError("配置已在另一页面更新，请刷新后重试", "revision_conflict", 409)

    def _provider(self, data: dict, provider_id: str) -> dict:
        item = next((p for p in data["providers"] if p.get("id") == provider_id), None)
        if item is None:
            raise RegistryError("提供方不存在，可能已被删除", "not_found", 404)
        return item

    def auth_required(self) -> bool:
        """Whether the gateway actually demands a bearer token.

        ``AI_GATEWAY_AUTH_REQUIRED=false`` is a supported deployment (trusted
        LAN): every request is admitted and the token file becomes optional
        documentation.  Anything that reports the gateway's auth state must go
        through here so the page can never claim a check that is not running.
        """

        return bool(getattr(self.settings, "ai_gateway_auth_required", True))

    def public(self) -> dict[str, Any]:
        data = self._read()
        providers = [{k: copy.deepcopy(p.get(k, default)) for k, default in (
            ("id", ""), ("name", ""), ("protocol", "openai"), ("base_url", ""),
            ("enabled", True), ("free_only", False), ("last_discovery_at", None))} | {
                "models": [_model_id(x) for x in p.get("models", [])],
                "model_settings": [_model_settings(x) for x in p.get("models", [])],
                "api_key_set": bool(p.get("api_key")),
                # 代理只回显 scheme://host:port，userinfo 里的凭据不出响应。
                "proxy_set": bool(p.get("proxy")),
                "proxy_display": _proxy_host(p.get("proxy")),
                "proxy_auth_set": _proxy_has_auth(p.get("proxy")),
            } for p in data["providers"]]
        # ``token_required`` historically meant "auth is enforced" and was
        # reported to the panel; it must never be hard-coded, or a keyless
        # deployment gets displayed — and documented — as token-protected.
        return {"revision": data.get("revision", 0), "providers": providers, "active": copy.deepcopy(data["active"]),
                "roles": self._roles_from(data),
                "fallback": self._fallback_view(data),
                "role_catalog": [{"id": role, "label": ROLE_LABELS[role], "alias": alias}
                                 for alias, role in ROLE_ALIASES.items()],
                "gateway": {"token_required": self.auth_required(), "active_alias": "hub-active", "openai_path": "/v1/chat/completions",
                            "responses_path": "/v1/responses", "anthropic_path": "/v1/messages",
                            "auth_required": self.auth_required(),
                            "compatibility_note": "支持 OpenAI / Anthropic 双向文本、图片、工具与流式转换；Responses 优先原生接口。模型专属功能取决于上游支持。"}}

    def _active_from(self, data: dict) -> tuple[dict[str, Any] | None, str]:
        target = data["active"]
        p = next((p for p in data["providers"] if p["id"] == target.get("provider_id") and p.get("enabled", True)), None)
        model = str(target.get("model") or "")
        return (copy.deepcopy(p), model) if p and model in [_model_id(x) for x in p.get("models", [])] else (None, "")

    def active(self) -> tuple[dict[str, Any] | None, str]:
        return self._active_from(self._read())

    @staticmethod
    def _role_entry(raw: Any, role: str) -> dict[str, str]:
        item = raw.get(role) if isinstance(raw, dict) else None
        item = item if isinstance(item, dict) else {}
        return {"provider_id": str(item.get("provider_id") or ""), "model": str(item.get("model") or "")}

    def _roles_from(self, data: dict) -> dict[str, dict[str, str]]:
        raw = data.get("roles")
        return {role: self._role_entry(raw, role) for role in ROLE_LABELS}

    def roles(self) -> dict[str, dict[str, str]]:
        return self._roles_from(self._read())

    def role_target(self, role: str) -> tuple[dict[str, Any] | None, str]:
        """Public view of a role target; (None, "") means "follow default"."""

        target = self._role_target(self._read(), role)
        return target if target else (None, "")

    def _role_target(self, data: dict, role: str) -> tuple[dict[str, Any], str] | None:
        """Resolve one role to (provider, model). None means "follow default".

        A stale target (provider removed/disabled, model deleted) also reports
        None so the role silently falls back instead of failing requests.
        """

        entry = self._role_entry(data.get("roles"), role)
        provider_id, model = entry["provider_id"], entry["model"]
        if not provider_id or not model:
            return None
        provider = next((p for p in data["providers"]
                         if p["id"] == provider_id and p.get("enabled", True)), None)
        if provider is None or model not in [_model_id(x) for x in provider.get("models", [])]:
            return None
        return copy.deepcopy(provider), model

    def resolve(self, requested: str | None, *, provider_id: str | None = None) -> tuple[dict, str]:
        key = (requested or "").strip()
        data = self._read()
        if provider_id is not None:
            provider = self._provider(data, provider_id)
            if not provider.get("enabled", True) or key not in [_model_id(x) for x in provider["models"]]:
                raise RegistryError("指定提供方的模型不存在或已停用", "model_unavailable", 400)
            return copy.deepcopy(provider), key
        if not key or key in {"default", "hub-active"}:
            active, model = self._active_from(data)
            if active:
                return active, model
            raise RegistryError("请先在模型中枢启用一个模型", "no_active_model", 503)
        role = ROLE_ALIASES.get(key)
        if role:
            target = self._role_target(data, role)
            if target is None:
                target = self._active_from(data)
            if target[0] is None:
                raise RegistryError("请先在模型中枢启用一个模型", "no_active_model", 503)
            return target
        active, _ = self._active_from(data)
        if active and key in [_model_id(x) for x in active["models"]]:
            return active, key
        matches = [p for p in data["providers"] if p.get("enabled", True) and key in [_model_id(x) for x in p.get("models", [])]]
        if len(matches) != 1:
            raise RegistryError("模型未配置或存在多个同名路由，请启用目标配置或使用 hub-active", "ambiguous_model", 400)
        return copy.deepcopy(matches[0]), key

    def fallback_targets(self) -> list[tuple[dict[str, Any], str]]:
        """备用模型链，按配置顺序展开成可用的 (provider, model)。

        条目指向的渠道被停用、模型被删掉时静默跳过——备用链的价值是「尽量能用」，
        不该因为其中一环失效就让整个请求失败。
        """

        data = self._read()
        targets: list[tuple[dict[str, Any], str]] = []
        for item in self._fallback_raw(data):
            provider = next(
                (
                    p
                    for p in data["providers"]
                    if p.get("id") == item["provider_id"] and p.get("enabled", True)
                ),
                None,
            )
            if provider is None:
                continue
            if item["model"] not in [_model_id(x) for x in provider.get("models", [])]:
                continue
            targets.append((copy.deepcopy(provider), item["model"]))
        return targets

    @staticmethod
    def _fallback_raw(data: dict) -> list[dict[str, str]]:
        raw = data.get("fallback")
        if not isinstance(raw, list):
            return []
        out: list[dict[str, str]] = []
        for item in raw[:MAX_FALLBACK]:
            if not isinstance(item, dict):
                continue
            provider_id = str(item.get("provider_id") or "")
            model = str(item.get("model") or "")
            if provider_id and model:
                out.append({"provider_id": provider_id, "model": model})
        return out

    def _fallback_view(self, data: dict) -> list[dict[str, Any]]:
        view: list[dict[str, Any]] = []
        for item in self._fallback_raw(data):
            provider = next(
                (p for p in data["providers"] if p.get("id") == item["provider_id"]),
                None,
            )
            available = bool(
                provider
                and provider.get("enabled", True)
                and item["model"] in [_model_id(x) for x in provider.get("models", [])]
            )
            view.append(
                {
                    "provider_id": item["provider_id"],
                    "provider_name": str(provider.get("name") or "") if provider else "",
                    "model": item["model"],
                    "available": available,
                }
            )
        return view

    async def set_fallback(self, entries: Any, revision: Any = None) -> dict[str, Any]:
        """设置备用模型链。``entries`` 是 [{provider_id, model}, ...]，顺序即尝试顺序。"""

        async with self._lock:
            data = self._read()
            self._check_revision(data, revision)
            if not isinstance(entries, list):
                raise RegistryError("备用模型必须是数组")
            if len(entries) > MAX_FALLBACK:
                raise RegistryError(f"备用模型最多 {MAX_FALLBACK} 个")
            cleaned: list[dict[str, str]] = []
            seen: set[tuple[str, str]] = set()
            for item in entries:
                if not isinstance(item, dict):
                    raise RegistryError("备用模型条目格式无效")
                provider_id = str(item.get("provider_id") or "")
                model = str(item.get("model") or "")
                if not provider_id or not model:
                    raise RegistryError("备用模型必须同时指定渠道和模型")
                provider = next(
                    (p for p in data["providers"] if p.get("id") == provider_id), None
                )
                if provider is None or not provider.get("enabled", True):
                    raise RegistryError("备用模型指向的渠道不存在或已停用")
                if model not in [_model_id(x) for x in provider.get("models", [])]:
                    raise RegistryError("备用模型不在该渠道的模型列表里")
                key = (provider_id, model)
                if key in seen:
                    continue
                seen.add(key)
                cleaned.append({"provider_id": provider_id, "model": model})
            data["fallback"] = cleaned
            written = self._write(data)
            return {
                "revision": written["revision"],
                "fallback": self._fallback_view(written),
            }

    def model_settings(self, provider: dict, model: str) -> dict:
        return _model_settings(next((x for x in provider.get("models", []) if _model_id(x) == model), model))

    async def upsert(self, body: dict, provider_id: str | None = None) -> dict:
        if not isinstance(body, dict):
            raise RegistryError("提供方配置必须是对象")
        async with self._lock:
            data = self._read(); self._check_revision(data, body.get("revision"))
            item_id = provider_id or body.get("id") or secrets.token_hex(6)
            if not isinstance(item_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", item_id):
                raise RegistryError("提供方 ID 只能包含字母、数字、下划线和连字符")
            old = next((p for p in data["providers"] if p["id"] == item_id), None)
            if provider_id and old is None:
                raise RegistryError("提供方已被删除，请重新创建", "not_found", 404)
            item = copy.deepcopy(old or {"id": item_id, "models": [], "api_key": ""})
            name = body.get("name", item.get("name", item_id))
            protocol = body.get("protocol", item.get("protocol", "openai"))
            enabled = body.get("enabled", item.get("enabled", True))
            if not isinstance(name, str) or not name.strip() or len(name) > 80:
                raise RegistryError("提供方名称须为 1–80 字符")
            if protocol not in {"openai", "anthropic"} or type(enabled) is not bool:
                raise RegistryError("协议或启用状态无效")
            models = body.get("models", item["models"])
            if not isinstance(models, list) or len(models) > MAX_MODELS:
                raise RegistryError(f"模型列表最多 {MAX_MODELS} 项")
            prior = {_model_id(x): x for x in item["models"]}
            items = {_model_id(x): _entry(x, prior.get(_model_id(x))) for x in models}
            item.update(name=name.strip(), protocol=protocol, base_url=_url(body.get("base_url", item.get("base_url", ""))), enabled=enabled, models=list(items.values()))
            free_only = body.get("free_only", item.get("free_only", False))
            if type(free_only) is not bool:
                raise RegistryError("free_only 必须是布尔值")
            if free_only and (urlsplit(item["base_url"]).hostname != "openrouter.ai" or protocol != "openai"):
                raise RegistryError("免费模型策略需要 OpenRouter 官方 OpenAI 接口")
            if free_only and any(not free_model_id(model) for model in items):
                raise RegistryError("此提供方只允许 :free 模型或 openrouter/free")
            item["free_only"] = free_only
            key = body.get("api_key")
            if key not in (None, ""):
                if not isinstance(key, str) or len(key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key.strip()):
                    raise RegistryError("API Key 格式无效")
                item["api_key"] = key.strip()
            if "proxy" in body:
                incoming = body.get("proxy")
                stored = str(item.get("proxy") or "")
                # 面板拿到的是脱敏代理（无 userinfo）。它原样回传时值会跟库里的不同，
                # 这种情况必须视为「没改」，否则保存一次就把代理凭据洗掉了。
                masked_echo = (
                    isinstance(incoming, str) and stored
                    and incoming.strip() == _proxy_host(stored) != stored
                )
                if not masked_echo:
                    item["proxy"] = _proxy_url(incoming)
            if data["active"].get("provider_id") == item_id and (not enabled or data["active"].get("model") not in items):
                raise RegistryError("当前提供方正在使用，请先切换模型再停用或移除它", "active_in_use", 409)
            data["providers"] = [item if p["id"] == item_id else p for p in data["providers"]] if old else [*data["providers"], item]
            self._write(data)
            return {**self.public(), "saved_provider_id": item_id}

    async def activate(self, provider_id: str, model: str, revision: Any = None) -> dict:
        async with self._lock:
            data = self._read(); self._check_revision(data, revision)
            p = self._provider(data, provider_id)
            if not p.get("enabled", True) or model not in [_model_id(x) for x in p["models"]]:
                raise RegistryError("模型不存在或提供方已停用")
            data["active"] = {"provider_id": provider_id, "model": model}
            self._write(data); return self.public()

    async def set_role(self, role: str, provider_id: Any, model: Any, revision: Any = None) -> dict:
        """Point a named role at a provider/model.

        An empty provider clears the role, which means "follow the default
        model" — the initial state for every role.
        """

        if role not in ROLE_LABELS:
            raise RegistryError("未知的角色")
        provider_id = str(provider_id or "").strip()
        model = str(model or "").strip()
        async with self._lock:
            data = self._read(); self._check_revision(data, revision)
            roles = data.setdefault("roles", {})
            if not provider_id:
                roles.pop(role, None)
            else:
                p = self._provider(data, provider_id)
                if not p.get("enabled", True) or model not in [_model_id(x) for x in p["models"]]:
                    raise RegistryError("模型不存在或提供方已停用")
                roles[role] = {"provider_id": provider_id, "model": model}
            self._write(data)
            return self.public()

    async def remove(self, provider_id: str, *, replacement: dict | None = None, deactivate: bool = False, revision: Any = None) -> dict:
        async with self._lock:
            data = self._read(); self._check_revision(data, revision); self._provider(data, provider_id)
            if data["active"].get("provider_id") == provider_id:
                if replacement:
                    p = self._provider(data, replacement.get("provider_id"))
                    if p["id"] == provider_id or not p.get("enabled", True) or replacement.get("model") not in [_model_id(x) for x in p["models"]]:
                        raise RegistryError("替代模型不可用")
                    data["active"] = {"provider_id": p["id"], "model": replacement["model"]}
                elif deactivate is True:
                    data["active"] = {"provider_id": "", "model": ""}
                else:
                    raise RegistryError("请先切换模型，或确认停止使用后删除", "active_in_use", 409)
            data["providers"] = [p for p in data["providers"] if p["id"] != provider_id]
            roles = data.get("roles") or {}
            for role in [name for name, entry in roles.items() if (entry or {}).get("provider_id") == provider_id]:
                roles.pop(role, None)
            self._write(data)
        self._forget_probes(provider_id)
        return self.public()

    def _forget_probes(self, provider_id: str) -> None:
        if self.usage is None:
            return
        try:
            self.usage.forget_provider(provider_id)
        except Exception:  # noqa: BLE001 - the ledger is best-effort telemetry
            pass

    async def add_models(self, provider_id: str, models: list, revision: Any = None) -> dict:
        """Additively append models, keeping every existing setting intact."""

        if not isinstance(models, list) or not models:
            raise RegistryError("请至少提供一个模型 ID")
        async with self._lock:
            data = self._read(); self._check_revision(data, revision)
            p = self._provider(data, provider_id)
            if p.get("free_only") and any(not free_model_id(_model_id(x)) for x in models):
                raise RegistryError("此提供方只允许免费模型")
            existing = {_model_id(x) for x in p["models"]}
            additions = [_entry(x) for x in models if _model_id(x) not in existing]
            if not additions:
                return self.public()
            if len(existing) + len(additions) > MAX_MODELS:
                raise RegistryError(f"合并模型超过 {MAX_MODELS} 个，请先精简列表")
            p["models"] = [*p["models"], *additions]
            self._write(data)
            return self.public()

    async def remove_models(self, provider_id: str, models: list, revision: Any = None) -> dict:
        """Remove individual models from a provider without touching the rest."""

        if not isinstance(models, list) or not models:
            raise RegistryError("请选择要删除的模型")
        targets = {_model_id(x) for x in models}
        async with self._lock:
            data = self._read(); self._check_revision(data, revision)
            p = self._provider(data, provider_id)
            remaining = [x for x in p["models"] if _model_id(x) not in targets]
            if len(remaining) == len(p["models"]):
                raise RegistryError("选中的模型已不存在，请刷新后重试", "not_found", 404)
            if (data["active"].get("provider_id") == provider_id
                    and data["active"].get("model") in targets):
                raise RegistryError("当前正在使用的模型不能删除，请先切换模型", "active_in_use", 409)
            p["models"] = remaining
            roles = data.get("roles") or {}
            for role in [name for name, entry in roles.items()
                         if (entry or {}).get("provider_id") == provider_id
                         and (entry or {}).get("model") in targets]:
                roles.pop(role, None)
            self._write(data)
        if self.usage is not None:
            try:
                self.usage.forget_models(provider_id, sorted(targets))
            except Exception:  # noqa: BLE001 - the ledger is best-effort telemetry
                pass
        return self.public()

    async def update_model_settings(self, provider_id: str, model: str, values: dict) -> dict:
        async with self._lock:
            data = self._read(); self._check_revision(data, values.get("revision"))
            p = self._provider(data, provider_id)
            if model not in [_model_id(x) for x in p["models"]]:
                raise RegistryError("模型不存在", "not_found", 404)
            p["models"] = [_entry({"id": model, **{k: v for k, v in values.items() if k in SETTING_KEYS}}, x) if _model_id(x) == model else x for x in p["models"]]
            self._write(data); return self.public()

    async def discover(self, provider_id: str) -> dict:
        from datetime import UTC, datetime
        snapshot = copy.deepcopy(self._provider(self._read(), provider_id))
        headers = {"Accept": "application/json"}
        if snapshot["protocol"] == "anthropic":
            headers.update({"x-api-key": snapshot.get("api_key", ""), "anthropic-version": "2023-06-01"})
        else:
            headers["Authorization"] = "Bearer " + snapshot.get("api_key", "")
        async with self._network_limit:
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                    async with session.get(_url(snapshot["base_url"]) + "/models", headers=headers,
                                           allow_redirects=False,
                                           proxy=snapshot.get("proxy") or None) as response:
                        if response.status != 200:
                            raise RegistryError(f"获取模型失败：上游 HTTP {response.status}", "upstream_error", 502)
                        raw = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            raw.extend(chunk)
                            if len(raw) > 2 * 1024 * 1024:
                                raise RegistryError("上游模型列表过大", "upstream_error", 502)
                        payload = json.loads(raw)
            except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError, UnicodeError):
                raise RegistryError("无法读取上游模型列表，请检查地址与密钥", "upstream_error", 502) from None
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or len(rows) > 5000:
            raise RegistryError("上游模型列表格式无效", "upstream_error", 502)
        if snapshot.get("free_only"):
            rows = [row for row in rows if free_catalog_model(row)]
        models = list(dict.fromkeys(_model_id(x) for x in rows if isinstance(x, dict) and x.get("id")))
        if not models or len(models) > MAX_MODELS:
            raise RegistryError("上游未返回模型或超过500个模型，请手工填写", "upstream_error", 502)
        async with self._lock:
            data = self._read(); p = self._provider(data, provider_id)
            if any(p.get(k) != snapshot.get(k) for k in ("base_url", "api_key", "protocol", "free_only", "proxy")):
                raise RegistryError("获取期间配置已改变，请重新获取", "revision_conflict", 409)
            existing = {_model_id(x): x for x in p["models"]}
            # Discovery is additive: do not drop manual models, current routes or settings.
            additions = [x for x in models if x not in existing]
            if len(existing) + len(additions) > MAX_MODELS:
                raise RegistryError("合并模型超过500个，请先精简列表")
            p["models"] = [*p["models"], *additions]
            p["last_discovery_at"] = datetime.now(UTC).isoformat()
            self._write(data)
            return {**self.public(), "discovery": {"fetched": len(models), "added": len(additions)}}

    async def test(self, provider_id: str, model: str) -> dict:
        from .model_gateway import forward_request
        p = copy.deepcopy(self._provider(self._read(), provider_id))
        if not p.get("enabled", True) or model not in [_model_id(x) for x in p["models"]]:
            raise RegistryError("模型不存在或提供方已停用")
        started = time.monotonic()
        async with self._network_limit:
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
                    status, _, _ = await forward_request(session, p, "openai", {"model": model, "messages": [{"role": "user", "content": "Reply OK."}], "max_tokens": 64}, {"max_output_tokens": 64})
            except (aiohttp.ClientError, TimeoutError, ValueError):
                raise RegistryError("上游测试未完成，请检查连接或模型支持情况", "upstream_error", 502) from None
        result = {"ok": status == 200, "status": status, "model": model, "latency_ms": round((time.monotonic() - started) * 1000),
                  "message": "测试通过" if status == 200 else f"上游 HTTP {status}"}
        self._remember_probe(provider_id, model, result)
        return result

    def _remember_probe(self, provider_id: str, model: str, result: dict) -> None:
        if self.usage is None:
            return
        try:
            self.usage.record_probe(provider_id, model, result)
        except Exception:  # noqa: BLE001 - the ledger is best-effort telemetry
            pass

    async def test_many(self, targets: list, concurrency: int = BATCH_TEST_CONCURRENCY) -> dict:
        """Probe a batch of models, then persist every outcome.

        Failures are captured per model instead of aborting the batch, so the
        console can show one result row per model even when the upstream is
        partly unavailable.
        """

        if not isinstance(targets, list) or not targets:
            raise RegistryError("请选择要测试的模型")
        if len(targets) > MAX_BATCH_TEST:
            raise RegistryError(f"单次最多测试 {MAX_BATCH_TEST} 个模型，请分批进行")
        known = {p["id"]: p for p in self._read()["providers"]}
        jobs: list[tuple[str, str]] = []
        for item in targets:
            provider_id = str((item or {}).get("provider_id") or "") if isinstance(item, dict) else ""
            model = str((item or {}).get("model") or "") if isinstance(item, dict) else ""
            provider = known.get(provider_id)
            if provider is None or model not in [_model_id(x) for x in provider["models"]]:
                raise RegistryError("测试目标包含不存在的提供方或模型", "not_found", 404)
            jobs.append((provider_id, model))
        limit = asyncio.Semaphore(max(1, min(8, concurrency)))
        results: list[dict] = []

        async def probe(provider_id: str, model: str) -> None:
            async with limit:
                try:
                    outcome = await self.test(provider_id, model)
                except RegistryError as exc:
                    outcome = {"ok": False, "status": 0, "model": model, "latency_ms": 0, "message": str(exc)}
                    self._remember_probe(provider_id, model, outcome)
                results.append({"provider_id": provider_id, **outcome})

        await asyncio.gather(*(probe(provider_id, model) for provider_id, model in jobs))
        passed = sum(1 for item in results if item["ok"])
        return {"results": results, "tested": len(results), "passed": passed, "failed": len(results) - passed}

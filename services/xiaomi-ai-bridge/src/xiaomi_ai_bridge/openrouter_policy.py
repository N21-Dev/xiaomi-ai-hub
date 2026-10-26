"""Explicit free-only OpenRouter policy for catalog and forwarding paths."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit


def free_model_id(value: object) -> bool:
    return isinstance(value, str) and (value.endswith(":free") or value == "openrouter/free")


def free_catalog_model(model: object) -> bool:
    if not isinstance(model, dict) or not free_model_id(model.get("id")):
        return False
    pricing = model.get("pricing")
    if not isinstance(pricing, dict) or not all(k in pricing for k in ("prompt", "completion")):
        return False
    try:
        return all(value is not None and Decimal(str(value)) == 0 for value in pricing.values())
    except (InvalidOperation, TypeError, ValueError):
        return False


def enforce_free_request(provider: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    if not provider.get("free_only"):
        return body
    if urlsplit(provider.get("base_url", "")).hostname != "openrouter.ai":
        raise ValueError("免费模型策略仅适用于 OpenRouter 官方地址")
    if not free_model_id(body.get("model")):
        raise ValueError("此提供方仅允许免费模型")
    fallback = body.get("models", [])
    if not isinstance(fallback, list) or any(not free_model_id(model) for model in fallback):
        raise ValueError("免费模型不能回退到付费模型")
    if body.get("plugins") or body.get("web_search_options") or body.get("preset"):
        raise ValueError("免费提供方不启用可能额外计费的插件、搜索或预设")
    route = body.get("provider", {})
    if not isinstance(route, dict):
        raise ValueError("provider 必须是对象")
    return {**body, "provider": {**route, "max_price": {"prompt": 0, "completion": 0}}}

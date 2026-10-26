"""Cache identity follows model inputs, excluding the payload build time."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..config import AIConfig
from .prompts import PROMPT_VERSION


def input_fingerprint(payload: dict[str, Any]) -> str:
    content = {key: value for key, value in payload.items() if key != "generated_at"}
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def cache_matches(metadata: dict[str, Any], ai: AIConfig, fingerprint: str) -> bool:
    return (
        metadata.get("model") == ai.model
        and metadata.get("prompt_version") == PROMPT_VERSION
        and metadata.get("config_fingerprint") == ai.fingerprint()
        and metadata.get("input_fingerprint") == fingerprint
    )

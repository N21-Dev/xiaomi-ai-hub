"""日志初始化 + 凭据脱敏。

凭据脱敏是硬要求：passToken 泄露等于小米账号被接管。
所有日志经过 SensitiveFilter，命中即打码。有单测覆盖（tests/test_logging_redaction.py）。
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any

# 需要打码的敏感键名（大小写不敏感）
SENSITIVE_KEYS = (
    "passtoken",
    "pass_token",
    "servicetoken",
    "service_token",
    "ssecurity",
    "authorization",
    "cookie",
    "set-cookie",
    "userid",
    "user_id",
    "api_key",
    "apikey",
    "signature",
    "rc4_hash__",
    "_nonce",
    "location",
)

REDACTED = "***REDACTED***"

# 值级别的兜底：(正则, 替换串)，按顺序应用
_PAIR_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"(?i)\b(passToken|pass_token|serviceToken|ssecurity)\s*[=:]\s*[^\s,;&\"'}]+"),
        rf"\1={REDACTED}",
    ),
    (
        re.compile(r"(?i)\b(userId|user_id)\s*[=:]\s*[^\s,;&\"'}]+"),
        rf"\1={REDACTED}",
    ),
    (re.compile(r"(?i)\b(Cookie|Set-Cookie)\s*[=:]\s*[^\r\n]+"), rf"\1={REDACTED}"),
    (re.compile(r"(?i)\bAuthorization\s*[=:]\s*[^\r\n]+"), f"Authorization={REDACTED}"),
    (re.compile(r"(?i)\b(X-API-Key|api_key)\s*[=:]\s*[^\s,;&\"'}]+"), rf"\1={REDACTED}"),
]

# 小米账号 cookie 里典型的长 token 形状（60+ 位字母数字混合）
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_\-]{60,}\b")


def scrub(text: str) -> str:
    """对任意字符串做脱敏，用于日志与异常消息。"""
    if not text:
        return str(text)
    result = str(text)
    # 小米登录响应首部的哨兵前缀之后再无有用信息，整体打掉
    if result.startswith("&&&START&&&"):
        return "&&&START&&&" + REDACTED
    for pattern, replacement in _PAIR_PATTERNS:
        result = pattern.sub(replacement, result)
    return _LONG_TOKEN.sub(REDACTED, result)


def scrub_mapping(data: Any, _depth: int = 0) -> Any:
    """递归脱敏 dict/list，用于结构化日志字段。"""
    if _depth > 6:
        return REDACTED
    if isinstance(data, dict):
        cleaned: dict[str, Any] = {}
        for key, value in data.items():
            if str(key).lower().replace("-", "_") in {
                k.replace("-", "_") for k in SENSITIVE_KEYS
            }:
                cleaned[str(key)] = REDACTED
            else:
                cleaned[str(key)] = scrub_mapping(value, _depth + 1)
        return cleaned
    if isinstance(data, (list, tuple)):
        return [scrub_mapping(item, _depth + 1) for item in data]
    if isinstance(data, str):
        return scrub(data)
    return data


class SensitiveFilter(logging.Filter):
    """把日志消息与参数里的敏感信息替换掉。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        record.msg = scrub(message)
        record.args = ()
        for attr in ("extra_payload",):
            if hasattr(record, attr):
                setattr(record, attr, scrub_mapping(getattr(record, attr)))
        return True


class JsonFormatter(logging.Formatter):
    """结构化 JSON 日志，便于容器日志采集。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("event", "tier", "key_name", "count", "duration_ms", "status"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = scrub(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO", fmt: str = "json") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(SensitiveFilter())
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # httpx 会打请求日志，里面可能带 URL 参数，降到 WARNING
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

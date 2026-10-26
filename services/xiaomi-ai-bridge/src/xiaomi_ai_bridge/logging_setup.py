from __future__ import annotations

import logging
import re
import sys

REDACTED = "***REDACTED***"

_PATTERNS = (
    re.compile(
        r"(?i)\b(passToken|pass_token|serviceToken|service_token|ssecurity)"
        r"\s*[=:]\s*[^\s,;&\"'}]+"
    ),
    re.compile(r"(?i)\b(userId|user_id)\s*[=:]\s*[^\s,;&\"'}]+"),
    re.compile(r"(?i)\b(Cookie|Set-Cookie|Authorization)\s*[=:]\s*[^\r\n]+"),
    re.compile(r"(?i)\b(X-API-Key|api_key|apiKey)\s*[=:]\s*[^\s,;&\"'}]+"),
)
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_+/:=-]{60,}\b")


def scrub(value: object) -> str:
    result = str(value)
    if result.startswith("&&&START&&&"):
        return "&&&START&&&" + REDACTED
    for pattern in _PATTERNS:
        result = pattern.sub(lambda match: match.group(1) + "=" + REDACTED, result)
    return _LONG_TOKEN.sub(REDACTED, result)


class SensitiveFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - logging must never break the service
            message = str(record.msg)
        record.msg = scrub(message)
        record.args = ()
        return True


def setup_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(SensitiveFilter())
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))
    logging.getLogger("miservice").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

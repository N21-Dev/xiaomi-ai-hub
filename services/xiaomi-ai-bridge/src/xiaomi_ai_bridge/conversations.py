"""Bounded in-memory conversation state; locks and history belong to an ID."""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


def conversation_id(value: object = None) -> str:
    if value is None:
        return secrets.token_urlsafe(24)
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", value):
        raise ValueError("conversation_id 必须为 16 至 128 位字母、数字、下划线或连字符")
    return value


@dataclass
class Conversation:
    history: list[dict[str, str]] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    touched_at: float = field(default_factory=time.monotonic)
    users: int = 0
    version: int = 0


class ConversationStore:
    def __init__(self, *, limit: int = 64, ttl_seconds: float = 1800):
        self.limit = limit
        self.ttl_seconds = ttl_seconds
        self.entries: OrderedDict[str, Conversation] = OrderedDict()

    def _prune(self) -> None:
        now = time.monotonic()
        for key, item in list(self.entries.items()):
            if not item.users and now - item.touched_at >= self.ttl_seconds:
                del self.entries[key]

    @asynccontextmanager
    async def turn(self, key: str):
        key = conversation_id(key)
        self._prune()
        item = self.entries.get(key)
        if item is None:
            if len(self.entries) >= self.limit:
                victim = next((name for name, entry in self.entries.items() if not entry.users), None)
                if victim is None:
                    raise RuntimeError("当前对话较多，请稍后重试")
                del self.entries[victim]
            item = self.entries[key] = Conversation()
        item.users += 1  # Queued turns cannot be evicted before acquiring the lock.
        if item.users > 8:
            item.users -= 1
            raise RuntimeError("此对话正在处理较多请求，请稍后重试")
        self.entries.move_to_end(key)
        try:
            async with item.lock:
                yield item
        finally:
            item.users -= 1
            item.touched_at = time.monotonic()

    def clear(self, key: str) -> None:
        key = conversation_id(key)
        self._prune()
        item = self.entries.get(key)
        if item is not None:
            item.history.clear()
            # An answer already being generated must not recreate cleared memory.
            item.version += 1
            item.touched_at = time.monotonic()

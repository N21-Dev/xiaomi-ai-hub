from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from .state import RuntimeStatus
from .xiaomi import XiaomiClient


class AudioScheduler:
    """Serializes speaker output and suppresses the native XiaoAI response."""

    def __init__(
        self,
        xiaomi: XiaomiClient,
        status: RuntimeStatus,
        *,
        suppress_native: bool,
        suppress_interval: float = 0.65,
    ) -> None:
        self.xiaomi = xiaomi
        self.status = status
        self.suppress_native = suppress_native
        self.suppress_interval = suppress_interval
        self.lock = asyncio.Lock()

    async def run_turn(self, produce: Callable[[], Awaitable[str]]) -> str:
        async with self.lock:
            suppression: asyncio.Task[None] | None = None
            try:
                if self.suppress_native:
                    self.status.audio_state = "suppressing"
                    await self._pause(force=True)
                    suppression = asyncio.create_task(self._suppression_loop())
                self.status.audio_state = "generating"
                answer = await produce()
                if suppression is not None:
                    suppression.cancel()
                    await asyncio.gather(suppression, return_exceptions=True)
                    suppression = None
                if self.suppress_native:
                    self.status.audio_state = "handoff"
                    await self._pause(force=True)
                    await asyncio.sleep(0.12)
                self.status.audio_state = "speaking"
                await self.xiaomi.speak(answer)
                return answer
            finally:
                if suppression is not None:
                    suppression.cancel()
                    await asyncio.gather(suppression, return_exceptions=True)
                self.status.audio_state = "idle"

    async def speak_direct(self, text: str) -> None:
        async with self.lock:
            try:
                if self.suppress_native:
                    self.status.audio_state = "handoff"
                    await self._pause(force=True)
                    await asyncio.sleep(0.12)
                self.status.audio_state = "speaking"
                await self.xiaomi.speak(text)
            finally:
                self.status.audio_state = "idle"

    async def _suppression_loop(self) -> None:
        checks = 0
        while True:
            interval = self.suppress_interval if checks < 4 else 1.0
            await asyncio.sleep(interval)
            await self._pause(force=False)
            checks += 1

    async def _pause(self, *, force: bool) -> None:
        if await self.xiaomi.suppress_native_response(force=force):
            self.status.native_pauses += 1

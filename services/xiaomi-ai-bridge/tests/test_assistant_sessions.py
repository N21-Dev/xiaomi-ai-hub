import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from xiaomi_ai_bridge.bridge import Bridge
from xiaomi_ai_bridge.conversations import ConversationStore, conversation_id


A = "terminal-a-session-1234"
B = "terminal-b-session-1234"


class AssistantSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bridge = object.__new__(Bridge)
        self.bridge.conversations = ConversationStore()
        self.bridge.status = SimpleNamespace(last_ai_latency_ms=0)
        self.seen = []

        async def answer(prompt, history, **kwargs):
            self.seen.append((prompt, list(history)))
            return "synthetic reply"

        self.bridge._answer_with_tools = AsyncMock(side_effect=answer)

    async def test_separate_terminals_do_not_share_history(self):
        await self.bridge.ask_text("A first", conversation_id=A)
        await self.bridge.ask_text("B first", conversation_id=B)
        await self.bridge.ask_text("A second", conversation_id=A)
        self.assertEqual(self.seen[1][1], [])
        self.assertEqual(self.seen[2][1][0]["content"], "A first")
        self.assertNotIn("B first", str(self.seen[2][1]))

    async def test_legacy_callers_without_id_are_stateless(self):
        await self.bridge.ask_text("first")
        await self.bridge.ask_text("second")
        self.assertEqual(self.seen[1][1], [])

    async def test_short_turn_and_clear_only_affect_target_conversation(self):
        await self.bridge.ask_text("A", conversation_id=A)
        await self.bridge.ask_text("B", conversation_id=B)
        await self.bridge.ask_text("independent", use_context=False, conversation_id=A)
        self.assertEqual(self.seen[-1][1], [])
        self.assertEqual(self.bridge.conversations.entries[A].history, [])
        self.assertTrue(self.bridge.conversations.entries[B].history)
        self.bridge.clear_chat(A)
        self.assertTrue(self.bridge.conversations.entries[B].history)

    async def test_clearing_inflight_answer_does_not_restore_old_context(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def held(*args, **kwargs):
            started.set()
            await release.wait()
            return "old reply"

        self.bridge._answer_with_tools.side_effect = held
        request = asyncio.create_task(self.bridge.ask_text("old input", conversation_id=A))
        await started.wait()
        self.bridge.clear_chat(A)
        release.set()
        await request
        self.assertEqual(self.bridge.conversations.entries[A].history, [])

    async def test_unrelated_conversations_can_run_in_parallel(self):
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def held(prompt, *args, **kwargs):
            calls.append(prompt)
            if len(calls) == 2:
                started.set()
            await release.wait()
            return "reply"

        self.bridge._answer_with_tools.side_effect = held
        requests = [asyncio.create_task(self.bridge.ask_text(key, conversation_id=key)) for key in (A, B)]
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
        finally:
            release.set()
            await asyncio.gather(*requests)

    async def test_same_conversation_is_serialized_and_history_is_bounded(self):
        for index in range(10):
            await self.bridge.ask_text(str(index), conversation_id=A)
        self.assertEqual(len(self.bridge.conversations.entries[A].history), 12)
        store = ConversationStore(limit=1)
        async with store.turn(A):
            with self.assertRaises(RuntimeError):
                async with store.turn(B):
                    self.fail("active history must not be evicted")
        async with store.turn(B):
            self.assertNotIn(A, store.entries)

    async def test_expired_idle_conversations_are_pruned(self):
        store = ConversationStore(ttl_seconds=1)
        async with store.turn(A):
            pass
        store.entries[A].touched_at = 0
        async with store.turn(B):
            self.assertNotIn(A, store.entries)

    def test_ids_are_validated_and_generated_without_shared_default(self):
        self.assertNotEqual(conversation_id(), conversation_id())
        for key in ("short", "../../secrets", "x" * 129, 123):
            with self.assertRaises(ValueError):
                conversation_id(key)

"""Offline sampling regressions; all state lives in temporary directories."""

from __future__ import annotations

import asyncio
import copy
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from xiaomi_ai_bridge.bridge import Bridge, home_resource_id
from xiaomi_ai_bridge.config import Settings
from xiaomi_ai_bridge.miot_tools import DeviceRecord, MiotToolManager, PropertySpec
from xiaomi_ai_bridge.state import RuntimeStatus


def device(key: str, *, model: str = "test.sensor") -> DeviceRecord:
    return DeviceRecord(
        key=key,
        did="fixture-" + key,
        name=key,
        alias=key,
        model=model,
        category="sensor",
        enabled=True,
        cloud_online=True,
        properties={
            "temperature": PropertySpec(
                siid=2,
                piid=1,
                name="temperature",
                format="float",
                readable=True,
                writable=False,
            )
        },
    )


class FakeCloud:
    def __init__(self):
        self.calls = 0
        self.fail = False
        self.last_property_reads = {}

    async def miot_get_props(self, did, properties):
        self.calls += 1
        if self.fail:
            raise RuntimeError("fixture cloud unavailable")
        for siid, piid in properties:
            self.last_property_reads[(did, siid, piid)] = {
                "status": "ok",
                "source_updated_at": "2026-01-01T00:00:00+00:00",
            }
        return [21.5 for _ in properties]


class TickStop:
    """Immediate scheduler ticks without wall-clock sleeps or fake clocks."""

    def __init__(self):
        self.stopped = False

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    async def wait(self):
        if self.stopped:
            return True
        raise TimeoutError


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manager = MiotToolManager(
            SimpleNamespace(state_dir=Path(self.directory.name), home_state_poll_seconds=60)
        )
        self.cloud = FakeCloud()
        self.manager._connect = AsyncMock(return_value=self.cloud)

    async def test_budget_retains_completed_samples_and_rotates_past_slow_device(self):
        records = [device(name) for name in ("first", "slow", "last")]
        self.manager.devices = {item.key: item for item in records}
        order = []
        observed = "2026-01-02T00:00:00+00:00"

        async def read(service, record, *, force):
            order.append(record.key)
            if record.key == "slow":
                await asyncio.Event().wait()
            return {
                "state": {"temperature": 21.5},
                "fresh_fields": ["temperature"],
                "observed_at": observed,
            }

        self.manager._read_state = read
        first = await self.manager.snapshot_enabled_states(budget_seconds=0.03)
        self.assertEqual([item.key for item, _ in first], ["first"])
        self.assertEqual(first[0][1]["observed_at"], observed)
        self.assertIn("cycle", self.manager.sampling_errors)
        self.assertFalse(self.manager._lock.locked())
        order.clear()
        second = await self.manager.snapshot_enabled_states(budget_seconds=0.03)
        self.assertEqual(order[0], "last")
        self.assertEqual([item.key for item, _ in second], ["last", "first"])

    async def test_device_failure_keeps_other_devices(self):
        self.manager.devices = {name: device(name) for name in ("broken", "working")}
        real_read = self.manager._read_state

        async def read(service, record, *, force):
            if record.key == "broken":
                raise RuntimeError("fixture failure")
            return await real_read(service, record, force=force)

        self.manager._read_state = read
        with self.assertLogs("xiaomi_ai_bridge.miot_tools", level="WARNING"):
            result = await self.manager.snapshot_enabled_states()
        self.assertEqual([record.key for record, _ in result], ["working"])
        self.assertEqual(set(self.manager.sampling_errors), {"broken"})

    async def test_external_cancellation_propagates_and_releases_lock(self):
        self.manager.devices = {"sensor": device("sensor")}
        entered = asyncio.Event()

        async def read(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.manager._read_state = read
        task = asyncio.create_task(self.manager.snapshot_enabled_states())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.manager._lock.locked())
        self.assertNotIn("cycle", self.manager.sampling_errors)

    async def test_cached_cycle_preserves_success_and_cloud_provenance(self):
        record = device("sensor")
        self.manager.devices = {record.key: record}
        initial = await self.manager._read_state(self.cloud, record, force=True)
        before = copy.deepcopy(self.manager.read_diagnostics[record.key]["temperature"])
        cached = await self.manager._read_state(self.cloud, record, force=False)
        after = self.manager.read_diagnostics[record.key]["temperature"]
        self.assertEqual(self.cloud.calls, 1)
        self.assertTrue(cached["cached_only"])
        self.assertEqual(cached["fresh_fields"], [])
        self.assertEqual(cached["observed_at"], initial["observed_at"])
        self.assertEqual(after["observed_at"], before["observed_at"])
        self.assertEqual(after["attempted_at"], before["attempted_at"])
        self.assertEqual(after["source_updated_at"], before["source_updated_at"])

    async def test_permission_revoked_while_reading_does_not_return_sample(self):
        record = device("sensor")
        self.manager.devices = {record.key: record}

        async def read(*args, **kwargs):
            record.enabled = False
            return {"fresh_fields": ["temperature"], "state": {"temperature": 21.5}}

        self.manager._read_state = read
        self.assertEqual(await self.manager.snapshot_enabled_states(), [])


class BridgeSamplingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        with patch.dict(
            os.environ,
            {"MI_USER_ID": "fixture", "MI_PASS_TOKEN": "fixture", "STATE_DIR": self.directory.name},
            clear=True,
        ):
            settings = Settings.from_env()
        self.bridge = Bridge(settings, RuntimeStatus())
        self.cloud = FakeCloud()
        self.bridge.home._connect = AsyncMock(return_value=self.cloud)

    async def test_cloud_failures_recover_and_cached_checks_do_not_claim_new_success(self):
        record = device("sensor")
        self.bridge.home.devices = {record.key: record}
        self.cloud.fail = True
        with self.assertLogs("xiaomi_ai_bridge.miot_tools", level="WARNING"):
            failed = await self.bridge.sample_home_states(force=True)
        self.assertEqual(failed["sampling"]["sources"]["cloud"]["consecutive_failures"], 1)
        self.assertIsNone(failed["sampling"]["sources"]["cloud"]["last_success_at"])
        self.cloud.fail = False
        self.bridge.home_sample_started = None
        healthy = await self.bridge.sample_home_states(force=True)
        status = healthy["sampling"]["sources"]["cloud"]
        self.assertEqual(status["consecutive_failures"], 0)
        self.assertTrue(status["last_success_at"])
        self.assertTrue(healthy["sampling"]["last_finished_at"])
        self.assertTrue(healthy["sampling"]["next_due_at"])
        self.assertIsInstance(healthy["sampling"]["duration_seconds"], float)
        observed = healthy["resources"][0]["observed_at"]
        self.bridge.home_sample_started = None
        cached = await self.bridge.sample_home_states()
        self.assertEqual(cached["sampling"]["sources"]["cloud"]["last_success_at"], status["last_success_at"])
        self.assertEqual(cached["resources"][0]["observed_at"], observed)
        self.assertEqual(self.cloud.calls, 2)

    async def test_disabled_resource_does_not_count_as_persisted_success(self):
        record = device("sensor")
        self.bridge.home.devices = {record.key: record}
        initial = await self.bridge.sample_home_states(force=True)
        success = initial["sampling"]["sources"]["cloud"]["last_success_at"]
        self.bridge.hub.disable_resource(home_resource_id(record.category, record.key))
        self.bridge.home_sample_started = None
        result = await self.bridge.sample_home_states(force=True)
        self.assertEqual(result["sampling"]["sources"]["cloud"]["last_success_at"], success)
        self.assertEqual(result["resources"], [])
        self.assertEqual(self.cloud.calls, 1)

    async def test_background_sampler_retries_unexpected_cycle_error(self):
        stop = TickStop()
        calls = 0

        async def sample(**kwargs):
            nonlocal calls
            calls += 1
            self.assertFalse(kwargs["include_router"])
            if calls == 1:
                raise RuntimeError("fixture persistence failure")
            stop.set()

        self.bridge.sample_home_states = sample
        with self.assertLogs("xiaomi_ai_bridge.bridge", level="WARNING"):
            await self.bridge._home_state_loop(stop)
        self.assertEqual(calls, 2)

    async def test_router_loop_can_run_while_cloud_is_blocked(self):
        stop = asyncio.Event()
        cloud_started = asyncio.Event()
        router_called = asyncio.Event()

        async def sample(**kwargs):
            cloud_started.set()
            await asyncio.Event().wait()

        async def router_sample():
            router_called.set()
            stop.set()

        self.bridge.sample_home_states = sample
        self.bridge._sample_router_states = router_sample
        cloud_task = asyncio.create_task(self.bridge._home_state_loop(stop))
        await cloud_started.wait()
        await self.bridge._router_state_loop(stop)
        self.assertTrue(router_called.is_set())
        self.assertFalse(cloud_task.done())
        cloud_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cloud_task

    async def test_router_and_speaker_errors_do_not_escape_cloud_cycle(self):
        router = device("router")
        speaker = device("speaker", model="xiaomi.wifispeaker.fixture")
        self.bridge.home.devices = {item.key: item for item in (router, speaker)}
        self.bridge._is_local_router = lambda item: item.key == "router"
        self.bridge.home.snapshot_enabled_states = AsyncMock(return_value=[])
        self.bridge.refresh_router_device = AsyncMock(side_effect=ValueError("catalog changed"))
        self.bridge.home_extensions = AsyncMock(side_effect=RuntimeError("fixture unavailable"))
        with self.assertLogs("xiaomi_ai_bridge.bridge", level="WARNING"):
            result = await self.bridge.sample_home_states()
        self.assertTrue(result["sampling"]["last_finished_at"])
        self.bridge.refresh_router_device.assert_awaited_once()
        self.bridge.home_extensions.assert_awaited_once()

    async def test_router_cached_projection_does_not_change_last_success(self):
        projection = {"available": True, "stale": False}
        self.bridge._refresh_router_device = AsyncMock(
            side_effect=[(projection, True), (projection, False)]
        )
        await self.bridge.refresh_router_device("router")
        success = self.bridge.sampling_sources["router"]["last_success_at"]
        await self.bridge.refresh_router_device("router")
        self.assertEqual(self.bridge.sampling_sources["router"]["last_success_at"], success)
        self.assertIsNone(self.bridge.sampling_sources["cloud"]["last_success_at"])

    async def test_cancelled_cycle_does_not_record_completion_or_failure(self):
        entered = asyncio.Event()

        async def collect(**kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.bridge.home.snapshot_enabled_states = collect
        task = asyncio.create_task(self.bridge.sample_home_states())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.bridge.home_sampling_lock.locked())
        self.assertIsNone(self.bridge.home_sampling_status["last_finished_at"])
        self.assertEqual(self.bridge.sampling_sources["cloud"]["consecutive_failures"], 0)
        self.assertIsNone(self.bridge.sampling_sources["cloud"]["last_success_at"])


if __name__ == "__main__":
    unittest.main()

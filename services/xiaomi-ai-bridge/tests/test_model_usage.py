from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from xiaomi_ai_bridge.model_registry import ModelRegistry, RegistryError
from xiaomi_ai_bridge.model_usage import ModelUsageStore, probe_key


class ModelUsageStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ModelUsageStore(Path(self.tmp.name))

    def _record(self, **overrides):
        payload = {
            "provider_id": "env-default",
            "provider_name": "当前环境配置",
            "model": "nvidia/nemotron",
            "protocol": "openai",
            "surface": "openai",
            "status": 200,
            "latency_ms": 420,
            "prompt_tokens": 12,
            "completion_tokens": 30,
            "total_tokens": 42,
        }
        payload.update(overrides)
        self.store.record_request(**payload)

    def test_database_is_private_and_empty_by_default(self):
        self.assertEqual(self.store.summary(24)["total"], 0)
        self.assertEqual(self.store.logs()["items"], [])
        if os.name != "nt":
            self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_summary_aggregates_requests_and_failures(self):
        self._record(latency_ms=100)
        self._record(latency_ms=300)
        self._record(status=429, latency_ms=800, total_tokens=0, prompt_tokens=0,
                     completion_tokens=0, error="上游 HTTP 429")
        summary = self.store.summary(24)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["ok"], 2)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["success_rate"], 66.7)
        self.assertEqual(summary["tokens"], 84)
        self.assertEqual(summary["models"][0]["key"], "nvidia/nemotron")
        self.assertEqual(summary["models"][0]["requests"], 3)
        self.assertEqual(summary["models"][0]["failed"], 1)
        self.assertEqual(summary["errors"][0]["message"], "上游 HTTP 429")
        self.assertEqual(len(summary["series"]), 24)
        self.assertEqual(sum(item["total"] for item in summary["series"]), 3)

    def test_summary_ignores_rows_outside_the_window(self):
        self._record()
        self._record(when=datetime.now(UTC) - timedelta(hours=30))
        self.assertEqual(self.store.summary(24)["total"], 1)
        self.assertEqual(self.store.summary(72)["total"], 2)

    def test_logs_support_filtering_and_paging(self):
        self._record(model="alpha")
        self._record(model="beta", status=500, error="上游 HTTP 500")
        self._record(model="alpha", provider_id="other", provider_name="其它")
        page = self.store.logs(limit=2, offset=0)
        self.assertEqual(page["total"], 3)
        self.assertEqual(len(page["items"]), 2)
        self.assertEqual(page["items"][0]["id"] > page["items"][1]["id"], True)
        self.assertEqual(self.store.logs(model="alpha")["total"], 2)
        self.assertEqual(self.store.logs(provider_id="other")["total"], 1)
        errors = self.store.logs(only_errors=True)
        self.assertEqual(errors["total"], 1)
        self.assertEqual(errors["items"][0]["error"], "上游 HTTP 500")
        self.assertFalse(errors["items"][0]["ok"])

    def test_probes_upsert_and_forget(self):
        self.store.record_probe("p1", "m1", {"ok": True, "status": 200, "latency_ms": 90, "message": "测试通过"})
        self.store.record_probe("p1", "m1", {"ok": False, "status": 503, "latency_ms": 12, "message": "上游 HTTP 503"})
        probes = self.store.probes()
        self.assertEqual(len(probes), 1)
        entry = probes[probe_key("p1", "m1")]
        self.assertFalse(entry["ok"])
        self.assertEqual(entry["status"], 503)
        self.store.forget_models("p1", ["m1"])
        self.assertEqual(self.store.probes(), {})

    def test_clear_logs_and_prune(self):
        self._record()
        self._record()
        self.assertEqual(self.store.clear_logs(), 2)
        self.assertEqual(self.store.logs()["total"], 0)
        self._record(when=datetime.now(UTC) - timedelta(days=45))
        self._record()
        self.assertGreaterEqual(self.store.prune(), 1)
        self.assertEqual(self.store.logs()["total"], 1)


class ModelRegistryConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup)
        self.usage = ModelUsageStore(Path(self.tmp.name))
        self.settings = SimpleNamespace(ai_base_url="", ai_api_key="", ai_model="")
        self.registry = ModelRegistry(Path(self.tmp.name), self.settings, usage=self.usage)

    async def _cleanup(self):
        self.tmp.cleanup()

    async def test_add_and_remove_individual_models(self):
        await self.registry.upsert({
            "id": "p1", "name": "提供方", "protocol": "openai",
            "base_url": "https://example.com/v1", "models": ["alpha"],
        })
        public = await self.registry.add_models("p1", ["beta", "alpha"])
        self.assertEqual(public["providers"][0]["models"], ["alpha", "beta"])
        public = await self.registry.remove_models("p1", ["beta"])
        self.assertEqual(public["providers"][0]["models"], ["alpha"])
        with self.assertRaises(RegistryError):
            await self.registry.remove_models("p1", ["missing"])

    async def test_active_model_cannot_be_removed(self):
        await self.registry.upsert({
            "id": "p1", "name": "提供方", "protocol": "openai",
            "base_url": "https://example.com/v1", "models": ["alpha", "beta"],
        })
        await self.registry.activate("p1", "alpha")
        with self.assertRaises(RegistryError) as caught:
            await self.registry.remove_models("p1", ["alpha"])
        self.assertEqual(caught.exception.code, "active_in_use")
        public = await self.registry.remove_models("p1", ["beta"])
        self.assertEqual(public["providers"][0]["models"], ["alpha"])

    async def test_deleting_a_provider_forgets_its_probes(self):
        await self.registry.upsert({
            "id": "p1", "name": "提供方", "protocol": "openai",
            "base_url": "https://example.com/v1", "models": ["alpha"],
        })
        self.usage.record_probe("p1", "alpha", {"ok": True, "status": 200, "latency_ms": 5, "message": "测试通过"})
        self.assertEqual(len(self.usage.probes()), 1)
        await self.registry.remove("p1")
        self.assertEqual(self.usage.probes(), {})

    async def test_batch_test_rejects_unknown_targets(self):
        await self.registry.upsert({
            "id": "p1", "name": "提供方", "protocol": "openai",
            "base_url": "https://example.com/v1", "models": ["alpha"],
        })
        with self.assertRaises(RegistryError):
            await self.registry.test_many([{"provider_id": "p1", "model": "ghost"}])
        with self.assertRaises(RegistryError):
            await self.registry.test_many([])

    async def test_public_view_never_leaks_provider_keys(self):
        await self.registry.upsert({
            "id": "p1", "name": "提供方", "protocol": "openai",
            "base_url": "https://example.com/v1", "api_key": "sk-console-secret",
            "models": ["alpha"],
        })
        self.assertNotIn("sk-console-secret", json.dumps(self.registry.public(), ensure_ascii=False))

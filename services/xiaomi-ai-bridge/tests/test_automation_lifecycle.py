"""Temporary databases and simulated devices only."""

import asyncio
import sqlite3
import tempfile
import unittest
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from xiaomi_ai_bridge.automation import AutomationStore
from xiaomi_ai_bridge.bridge import Bridge
from test_automation_tool import _ToolBridge


class AutomationLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = AutomationStore(self.root)
        self.now = datetime(2026, 10, 17, tzinfo=UTC)
        self.target = {"kind": "xiaomi", "device": "synthetic-lamp", "device_key": "lamp"}

    def create(self, schedule=None, target=None, value=None):
        return self.store.create(target=target or self.target, action="turn_on", value=value,
                                 schedule=schedule or {"type": "delay", "delay_seconds": 60}, now=self.now)

    def bridge(self):
        bridge = object.__new__(Bridge)
        bridge.automations = self.store
        bridge.home = SimpleNamespace(validate_scheduled_action=Mock(return_value=None))
        bridge.control_home_device = AsyncMock(return_value={"ok": True, "outcome": "confirmed"})
        return bridge

    async def test_cancel_claimed_task_prevents_device_send_and_old_claim_cannot_revive(self):
        first, second = self.create(), self.create()
        claimed = self.store.claim_due(now=self.now + timedelta(minutes=1), limit=10)
        bridge = self.bridge()
        self.store.cancel(second["id"])
        for item in claimed:
            await bridge._execute_automation(item)
        self.assertEqual(bridge.control_home_device.await_count, 1)
        self.assertEqual(self.store.get(second["id"])["status"], "cancelled")
        self.assertEqual(self.store.get(first["id"])["status"], "completed")

    async def test_cancellation_after_execution_boundary_is_explicitly_rejected(self):
        item = self.create()
        claim = self.store.claim_due(now=self.now + timedelta(minutes=1))[0]
        started = self.store.begin_execution(item["id"], claim["attempt_id"])
        with self.assertRaisesRegex(ValueError, "无法保证撤回"):
            self.store.cancel(item["id"])
        self.assertEqual(self.store.get(item["id"])["status"], "running")
        self.store.complete(item["id"], attempt_id=started["attempt_id"], outcome="confirmed", message="ok")

    async def test_restart_releases_unsent_claim_without_reusing_attempt(self):
        item = self.create()
        old = self.store.claim_due(now=self.now + timedelta(minutes=1))[0]
        restarted = AutomationStore(self.root)
        restarted.recover_interrupted(now=self.now + timedelta(minutes=2))
        new = restarted.claim_due(now=self.now + timedelta(minutes=2))[0]
        self.assertNotEqual(old["attempt_id"], new["attempt_id"])
        self.assertIsNone(restarted.begin_execution(item["id"], old["attempt_id"]))
        self.assertIsNotNone(restarted.begin_execution(item["id"], new["attempt_id"]))

    async def test_restart_never_replays_started_or_legacy_running_send(self):
        item = self.create()
        claim = self.store.claim_due(now=self.now + timedelta(minutes=1))[0]
        self.store.begin_execution(item["id"], claim["attempt_id"], now=self.now + timedelta(minutes=1))
        legacy = self.create()
        with sqlite3.connect(self.store.path) as connection:
            connection.execute("UPDATE automations SET status='running',attempt_id=NULL WHERE id=?", (legacy["id"],))
        restarted = AutomationStore(self.root)
        restarted.recover_interrupted(now=self.now + timedelta(days=1))
        self.assertEqual(restarted.claim_due(now=self.now + timedelta(days=1)), [])
        for record in (item, legacy):
            result = restarted.get(record["id"])
            self.assertEqual((result["status"], result["last_outcome"]), ("unknown", "unconfirmed"))
            self.assertIsNone(result["next_run_at"])
        # A late completion from the interrupted process cannot overwrite recovery.
        result = restarted.complete(item["id"], attempt_id=claim["attempt_id"], outcome="confirmed", message="late")
        self.assertEqual(result["status"], "unknown")

    async def test_interrupted_recurring_task_schedules_future_without_replay(self):
        item = self.create({"type": "daily", "at": "09:00"})
        claim = self.store.claim_due(now=self.now + timedelta(hours=2))[0]
        self.store.begin_execution(item["id"], claim["attempt_id"], now=self.now + timedelta(hours=2))
        recovery_time = self.now + timedelta(days=1)
        self.store.recover_interrupted(now=recovery_time)
        result = self.store.get(item["id"])
        self.assertEqual((result["status"], result["last_outcome"]), ("pending", "unconfirmed"))
        self.assertGreater(datetime.fromisoformat(result["next_run_at"]), recovery_time)
        self.assertEqual(self.store.claim_due(now=recovery_time), [])

    async def test_resume_daily_and_weekly_skip_missed_occurrences(self):
        for schedule in ({"type": "daily", "at": "09:00"}, {"type": "weekly", "at": "09:00", "days": [5]}):
            item = self.create(schedule)
            self.store.pause(item["id"], now=self.now)
            resume_time = self.now + timedelta(days=8)
            result = self.store.resume(item["id"], now=resume_time)
            self.assertGreater(datetime.fromisoformat(result["next_run_at"]), resume_time)
            self.assertEqual(self.store.claim_due(now=resume_time), [])
            self.store.cancel(item["id"])

    async def test_delay_resume_preserves_remaining_time(self):
        item = self.create()
        self.store.pause(item["id"], now=self.now + timedelta(seconds=20))
        resume_time = self.now + timedelta(days=1)
        result = self.store.resume(item["id"], now=resume_time)
        self.assertEqual(datetime.fromisoformat(result["next_run_at"]), resume_time + timedelta(seconds=40))

    async def test_redelivering_same_claim_does_not_repeat_send(self):
        self.create()
        claim = self.store.claim_due(now=self.now + timedelta(minutes=1))[0]
        bridge = self.bridge()
        await bridge._execute_automation(claim)
        await bridge._execute_automation(claim)
        self.assertEqual(bridge.control_home_device.await_count, 1)

    async def test_all_outcomes_are_preserved(self):
        for outcome in ("confirmed", "accepted", "unconfirmed", "failed"):
            item = self.create()
            claim = self.store.claim_due(now=self.now + timedelta(minutes=1))[0]
            bridge = self.bridge()
            bridge.control_home_device.return_value = {"ok": outcome in {"confirmed", "accepted"}, "outcome": outcome}
            await bridge._execute_automation(claim)
            result = self.store.get(item["id"])
            self.assertEqual(result["last_outcome"], outcome)
            self.assertEqual(result["status"], "completed" if outcome in {"accepted", "confirmed"} else "unknown" if outcome == "unconfirmed" else "failed")

    async def test_save_rejects_invalid_settings_and_revoked_permission(self):
        bridge = _ToolBridge(self.root / "tool", [{
            "kind": "infrared_ac", "device": "synthetic-ac", "device_key": "ac",
            "profile_key": "kelvinator:ac", "actions": ["set_settings"],
        }])
        body = {"operation": "create", "device": "synthetic-ac", "profile_key": "kelvinator:ac",
                "action": "set_settings", "schedule": {"type": "delay", "delay_seconds": 60}}
        for settings in ({"temperature": 100}, {"unknown": True}, {}, {"temperature": True}, {"turbo": True, "quiet": True}):
            result = await Bridge.execute_automation_tool(bridge, {**body, "settings": settings})
            self.assertFalse(result["ok"], settings)
        self.assertEqual(bridge.automations.list(), [])
        bridge.infrared._ac_template.return_value = (bridge.infrared._ac_template.return_value[0], {"ai_control_enabled": False}, {})
        self.assertFalse((await Bridge.execute_automation_tool(bridge, {**body, "settings": {"power": True}}))["ok"])

    async def test_execution_checks_current_permission_before_sending(self):
        self.create()
        claim = self.store.claim_due(now=self.now + timedelta(minutes=1))[0]
        bridge = self.bridge()
        bridge.home.validate_scheduled_action.side_effect = ValueError("设备未授权")
        await bridge._execute_automation(claim)
        bridge.control_home_device.assert_not_awaited()
        self.assertEqual(self.store.get(claim["id"])["last_outcome"], "failed")

    async def test_infrared_audit_failure_does_not_change_accepted_outcome(self):
        item = self.store.create(
            target={"kind": "infrared_ac", "device": "synthetic-ac", "device_key": "ac", "profile_key": "profile"},
            action="set_settings", value={"power": True},
            schedule={"type": "delay", "delay_seconds": 60}, now=self.now,
        )
        bridge = self.bridge()
        bridge.home.devices = {"ac": object()}
        bridge.home._audit_result = AsyncMock(side_effect=ValueError("synthetic audit failure"))
        bridge.infrared = SimpleNamespace(
            validate_ac_settings=Mock(return_value={"power": True}),
            execute=AsyncMock(return_value={"ok": True, "outcome": "accepted"}),
        )
        await bridge._execute_automation(self.store.claim_due(now=self.now + timedelta(minutes=1))[0])
        self.assertEqual(self.store.get(item["id"])["last_outcome"], "accepted")
        bridge.infrared.execute.assert_awaited_once()

    async def test_malformed_adapter_outcome_does_not_strand_task(self):
        item = self.create()
        bridge = self.bridge()
        bridge.control_home_device.return_value = {"ok": True, "outcome": {"invalid": True}}
        await bridge._execute_automation(self.store.claim_due(now=self.now + timedelta(minutes=1))[0])
        self.assertEqual(self.store.get(item["id"])["status"], "failed")

    async def test_upgrade_preserves_legacy_pending_tasks_and_quarantines_old_running(self):
        legacy_root = self.root / "legacy"
        legacy_root.mkdir()
        with sqlite3.connect(legacy_root / "automations.sqlite3") as connection:
            connection.execute("""CREATE TABLE automations (
                id TEXT PRIMARY KEY,schedule_json TEXT NOT NULL,next_run_at TEXT,target_json TEXT NOT NULL,
                action TEXT NOT NULL,value_json TEXT,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                last_run_at TEXT,last_outcome TEXT,last_message TEXT,run_count INTEGER NOT NULL DEFAULT 0)""")
            for name, status in (("auto_legacy_pending", "pending"), ("auto_legacy_running", "running")):
                connection.execute("INSERT INTO automations VALUES (?,?,?,?,?,?,?, ?,?,NULL,NULL,NULL,0)", (
                    name, json.dumps({"type": "delay", "delay_seconds": 60}), self.now.isoformat(),
                    json.dumps(self.target), "turn_on", None, status, self.now.isoformat(), self.now.isoformat(),
                ))
        upgraded = AutomationStore(legacy_root)
        upgraded.recover_interrupted(now=self.now + timedelta(minutes=1))
        claims = upgraded.claim_due(now=self.now + timedelta(minutes=1))
        self.assertEqual([item["id"] for item in claims], ["auto_legacy_pending"])
        self.assertEqual(claims[0]["target"], self.target)
        self.assertEqual(upgraded.get("auto_legacy_running")["last_outcome"], "unconfirmed")
        # New inserts must explicitly name columns after the additive migration.
        self.assertEqual(upgraded.create(target=self.target, action="turn_on", value=None,
                                        schedule={"type": "delay", "delay_seconds": 60})["status"], "pending")

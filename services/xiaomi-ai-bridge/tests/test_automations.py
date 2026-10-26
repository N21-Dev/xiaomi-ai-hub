from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from xiaomi_ai_bridge.automation import AutomationStore


class AutomationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = AutomationStore(Path(self.temp.name))
        self.target = {"kind": "xiaomi", "device_key": "lamp-1", "device": "客厅灯"}
        self.now = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_delay_survives_reload_and_claims_once(self):
        task = self.store.create(
            target=self.target, action="turn_on", value=None,
            schedule={"type": "delay", "delay_seconds": 60}, now=self.now,
        )
        self.assertEqual(task["status"], "pending")
        reloaded = AutomationStore(Path(self.temp.name))
        self.assertEqual(reloaded.get(task["id"])["target"]["device"], "客厅灯")
        self.assertEqual(reloaded.claim_due(now=self.now), [])
        claimed = reloaded.claim_due(now=self.now + timedelta(minutes=1))
        self.assertEqual([item["id"] for item in claimed], [task["id"]])
        self.assertEqual(reloaded.claim_due(now=self.now + timedelta(minutes=1)), [])
        started = reloaded.begin_execution(task["id"], claimed[0]["attempt_id"], now=self.now + timedelta(minutes=1))
        completed = reloaded.complete(task["id"], attempt_id=started["attempt_id"], outcome="confirmed", message="已执行", now=self.now + timedelta(minutes=1))
        self.assertEqual(completed["status"], "completed")

    def test_daily_and_weekly_get_next_local_time(self):
        daily = self.store.create(
            target=self.target, action="turn_off", value=None,
            schedule={"type": "daily", "at": "19:30"}, now=self.now,
        )
        self.assertTrue(daily["next_run_at"].startswith("2026-10-12T11:30"))
        weekly = self.store.create(
            target=self.target, action="turn_off", value=None,
            schedule={"type": "weekly", "at": "08:00", "days": [0]}, now=self.now,
        )
        self.assertTrue(weekly["next_run_at"].startswith("2026-10-19T00:00"))

    def test_pause_resume_cancel_and_invalid_past_once(self):
        with self.assertRaises(ValueError):
            self.store.create(
                target=self.target, action="turn_on", value=None,
                schedule={"type": "once", "at": "2026-10-12 09:00"}, now=self.now,
            )
        task = self.store.create(
            target=self.target, action="turn_on", value=None,
            schedule={"type": "once", "at": "2026-10-12 20:00"}, now=self.now,
        )
        self.assertEqual(self.store.pause(task["id"])["status"], "paused")
        # resume 会校验一次性任务是否已过期，必须把测试注入的时间一并传进去，
        # 否则这条断言依赖真实日历：任务时间一旦成为过去就会失败。
        self.assertEqual(self.store.resume(task["id"], now=self.now)["status"], "pending")
        self.assertEqual(self.store.cancel(task["id"])["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()

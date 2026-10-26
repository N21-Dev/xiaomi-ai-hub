from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from xiaomi_ai_bridge.hub_data import HubDataStore, decode_event_fields


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.store = HubDataStore(self.path)
        self.manager = self.store.maintenance

    def ingest(self, when=None):
        return self.store.ingest({
            "resource_id": "sensor.test", "kind": "sensor", "name": "Test",
            "source": "xiaomi", "state": {"temperature": 23.5},
            "observed_at": (when or datetime.now(UTC)).isoformat(),
        })

    def test_backup_can_be_restored_and_permissions_are_private(self):
        self.ingest()
        target = self.manager.backup()
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
        with sqlite3.connect(target) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0], 1)
        self.assertFalse(self.manager._backup_due())
        fresh_store = HubDataStore(self.path)
        self.assertFalse(fresh_store.maintenance._backup_due())

    def test_legacy_compression_is_lossless_and_reversible(self):
        self.ingest()
        # Model a real legacy row while preserving arbitrary historical metadata.
        fields = json.dumps([{"key": "temperature", "label": "温度", "observed_at": "2026-01-01"}])
        with sqlite3.connect(self.store.db_path) as conn:
            conn.execute("UPDATE hub_events SET fields_json=?,fields_blob=NULL", (fields,))
        self.assertEqual(self.manager.compress_batch(), 1)
        with sqlite3.connect(self.store.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM hub_events").fetchone()
            self.assertEqual(row["fields_json"], "[]")
            self.assertEqual(decode_event_fields(row), fields)
        self.assertEqual(self.manager.compress_batch(), 0)
        self.assertEqual(self.manager.restore_fields(), 1)
        with sqlite3.connect(self.store.db_path) as conn:
            row = conn.execute("SELECT fields_json,fields_blob FROM hub_events").fetchone()
            self.assertEqual(row, (fields, None))

    def test_failed_backup_prevents_pruning_and_migration(self):
        self.ingest(datetime.now(UTC) - timedelta(days=45))
        with patch.object(self.manager, "backup", side_effect=OSError("test")):
            self.manager.maintain()
        with sqlite3.connect(self.store.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM hub_events").fetchone()[0], 1)
        state = self.manager.status()
        self.assertFalse(state["maintenance"]["running"])
        self.assertIn("OSError", state["maintenance"]["last_error"])

    def test_history_age_pruning_keeps_resource_and_verified_backup(self):
        self.ingest(datetime.now(UTC) - timedelta(days=45))
        self.manager.maintain()
        with sqlite3.connect(self.store.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM hub_events").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0], 1)
        backups = list(self.manager.backup_dir.glob("hub-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM hub_events").fetchone()[0], 1)

    def test_bad_backup_does_not_prune_previous_copies(self):
        self.ingest()
        before = self.manager.backup()
        with patch.object(self.manager, "verify_database", side_effect=ValueError("bad copy")):
            with self.assertRaises(ValueError):
                self.manager.backup()
        self.assertTrue(before.exists())
        self.assertEqual(list(self.manager.backup_dir.glob("*.partial")), [])

    def test_late_backfill_waits_for_a_backup_before_age_pruning(self):
        first_backup = self.manager.backup()
        self.ingest(datetime.now(UTC) - timedelta(days=45))
        self.manager.maintain()
        with sqlite3.connect(self.store.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM hub_events").fetchone()[0], 1)
        with sqlite3.connect(first_backup) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM hub_events").fetchone()[0], 0)
        self.manager.backup()
        self.manager.maintain()
        with sqlite3.connect(self.store.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM hub_events").fetchone()[0], 0)

    def test_backup_integrity_scan_respects_deadline(self):
        self.ingest()
        with patch("xiaomi_ai_bridge.hub_maintenance.time.monotonic", return_value=999):
            with self.assertRaises(TimeoutError):
                self.manager.verify_database(self.store.db_path, deadline=1)

    def test_retention_keeps_daily_and_weekly_groups(self):
        self.manager.backup_dir.mkdir()
        for offset in range(80):
            day = datetime.now(UTC) - timedelta(days=offset)
            if day.weekday() == 6:
                continue
            for hour in (4, 12):
                stamp = day.replace(hour=hour).strftime("%Y%m%dT%H%M%S%fZ")
                (self.manager.backup_dir / f"hub-{stamp}.sqlite3").touch()
        unknown = self.manager.backup_dir / "hub-manual.sqlite3"
        unknown.touch()
        self.manager._prune_backups()
        known = [p for p in self.manager.backup_dir.glob("hub-*.sqlite3") if p != unknown]
        self.assertEqual(len(known), self.manager.keep_daily + self.manager.keep_weekly)
        self.assertTrue(unknown.exists())

    def test_status_reports_archive_and_policy_without_values(self):
        self.ingest()
        state = self.manager.status()
        self.assertEqual(state["history"]["event_count"], 1)
        self.assertEqual(state["history"]["compressed_events"], 1)
        self.assertEqual(state["policy"]["max_age_days"], 30)
        self.assertNotIn("temperature", json.dumps(state))
        state["history"]["event_count"] = 999
        self.assertEqual(self.manager.status()["history"]["event_count"], 1)


if __name__ == "__main__":
    unittest.main()

"""Storage regressions use temporary databases and synthetic measurements only."""

from __future__ import annotations

import base64
import json
import sqlite3
import tempfile
import unittest
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from xiaomi_ai_bridge.field_catalog import REDACTED, build_field_catalog
from xiaomi_ai_bridge.hub_data import (
    MAX_EVENT_FIELDS_BYTES,
    HubDataStore,
    decode_event_fields,
    encode_event_fields,
)


class HubDataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = HubDataStore(Path(self.temporary.name))
        self.now = datetime.now(UTC) - timedelta(minutes=1)

    def packet(self, value=20, *, seconds=0, **changes):
        return {
            "resource_id": "test.sensor",
            "kind": "sensor",
            "name": "Synthetic sensor",
            "state": {"temperature": value},
            "observed_at": (self.now + timedelta(seconds=seconds)).isoformat(),
            **changes,
        }

    def event_count(self):
        with self.store._database() as connection:
            return connection.execute("SELECT count(*) FROM hub_events").fetchone()[0]

    def test_late_arrival_keeps_current_snapshot_and_valid_history(self):
        latest = self.store.ingest(self.packet(25))
        returned = self.store.ingest(self.packet(10, seconds=-3600))
        self.assertEqual(returned, self.store.get_resource("test.sensor"))
        self.assertEqual(returned["state"], latest["state"])
        self.assertEqual(returned["observed_at"], latest["observed_at"])
        self.assertEqual(returned["received_at"], latest["received_at"])
        self.assertGreater(returned["history_revision"], latest["history_revision"])
        repeated = self.store.ingest(self.packet(10, seconds=-3600))
        self.assertEqual(repeated["history_revision"], returned["history_revision"])
        events = self.store.history("test.sensor")["events"]
        self.assertEqual([event["state"]["temperature"] for event in events], [10, 25])

    def test_exact_retry_is_idempotent_across_store_instances(self):
        packet = self.packet(state={"temperature": 20, "humidity": 40})
        original = self.store.ingest(packet)
        another = HubDataStore(Path(self.temporary.name))
        repeated = another.ingest({**packet, "state": {"humidity": 40, "temperature": 20}})
        self.assertEqual(self.event_count(), 1)
        self.assertEqual(repeated["received_at"], original["received_at"])
        self.assertEqual(repeated["history_revision"], original["history_revision"])

    def test_same_value_new_observation_is_not_deduplicated(self):
        self.store.ingest(self.packet())
        self.store.ingest(self.packet(seconds=1))
        self.assertEqual(self.event_count(), 2)

    def test_corrected_value_at_same_timestamp_is_retained(self):
        self.store.ingest(self.packet(20))
        self.store.ingest(self.packet(21))
        self.assertEqual(self.event_count(), 2)
        self.assertEqual(self.store.get_resource("test.sensor")["state"]["temperature"], 21)

    def test_implicit_timestamp_does_not_deduplicate_measurements(self):
        packet = self.packet()
        packet.pop("observed_at")
        self.store.ingest(packet)
        self.store.ingest(packet)
        self.assertEqual(self.event_count(), 2)

    def test_concurrent_identical_retry_creates_one_event(self):
        packet = self.packet()
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(self.store.ingest, [packet] * 4))
        self.assertEqual(self.event_count(), 1)

    def test_unknown_or_disabled_history_is_unavailable(self):
        with self.assertRaisesRegex(ValueError, "不存在或未启用"):
            self.store.history("test.sensor")
        self.store.ingest(self.packet())
        self.store.disable_resource("test.sensor")
        with self.assertRaisesRegex(ValueError, "不存在或未启用"):
            self.store.history("test.sensor")
        self.store.enable_resource("test.sensor")
        self.assertEqual(len(self.store.history("test.sensor")["events"]), 1)

    def test_current_sensitive_declaration_redacts_all_old_history(self):
        fields = [{"key": "reading", "unit": "°C", "role": "measurement"}]
        self.store.ingest(self.packet(state={"reading": 23}, fields=fields))
        self.store.ingest(self.packet(seconds=1, state={"reading": 24},
                                      fields=[{**fields[0], "sensitive": True}]))
        full = self.store.history("test.sensor")["events"]
        self.assertEqual([event["state"]["reading"] for event in full], [REDACTED, REDACTED])
        self.assertTrue(all(event["fields"][0]["status"] == "sensitive" for event in full))
        charts = self.store.history("test.sensor", projection="chart")["events"]
        self.assertTrue(all(event["state"] == {} and event["fields"] == [] for event in charts))

    def test_sensitive_parent_hides_historical_nested_values(self):
        self.store.ingest(self.packet(state={"private": {"temperature": 22}}))
        self.store.ingest(self.packet(seconds=1, state={"private": {"temperature": 23}},
                                      fields=[{"key": "private", "sensitive": True}]))
        events = self.store.history("test.sensor", projection="chart")["events"]
        self.assertNotIn("22", json.dumps(events[0]["state"]))
        self.assertFalse(any(field.get("value") == 22 for field in events[0]["fields"]))

    def test_keyset_pages_cover_equal_timestamps_once_during_new_insert(self):
        for value in range(5):
            self.store.ingest(self.packet(value))
        page = self.store.history("test.sensor", limit=2)
        self.assertTrue(page["has_more"])
        self.assertTrue(page["truncated"])
        self.assertEqual(page["query"]["returned_count"], 2)
        original_ids = {event["id"] for event in self.store.history("test.sensor")["events"]}
        seen = [event["id"] for event in page["events"]]
        self.store.ingest(self.packet(99, seconds=1))
        while page["has_more"]:
            page = self.store.history("test.sensor", limit=2, before=page["next_cursor"])
            seen.extend(event["id"] for event in page["events"])
        self.assertEqual(set(seen), original_ids)
        self.assertEqual(len(seen), len(original_ids))
        self.assertIsNone(page["next_cursor"])

    def test_cursor_validation_rejects_wrong_resource_and_malformed_values(self):
        self.store.ingest(self.packet())
        self.store.ingest(self.packet(seconds=1))
        cursor = self.store.history("test.sensor", limit=1)["next_cursor"]
        self.store.ingest(self.packet(resource_id="test.other"))
        with self.assertRaisesRegex(ValueError, "游标"):
            self.store.history("test.other", before=cursor)
        malformed = ["", "!bad!", "a" * 513,
                     base64.urlsafe_b64encode(b'[1,"test.sensor","bad",1]').decode().rstrip("="),
                     base64.urlsafe_b64encode(json.dumps([1, "test.sensor", self.now.isoformat(), True]).encode()).decode().rstrip("=")]
        for value in malformed:
            with self.subTest(cursor=value), self.assertRaisesRegex(ValueError, "游标"):
                self.store.history("test.sensor", before=value)

    def test_time_window_uses_observation_not_recent_arrival(self):
        self.store.ingest(self.packet(seconds=-7200))
        self.store.ingest(self.packet())
        self.assertEqual(len(self.store.history("test.sensor", hours=1)["events"]), 1)
        self.assertEqual(len(self.store.history("test.sensor", hours=168)["events"]), 2)
        self.assertEqual(len(self.store.history("test.sensor", hours=720)["events"]), 2)

    def test_chart_projection_keeps_paths_and_original_sampling_diagnostics(self):
        fields = [
            {"key": "nested-temperature", "path": ["items", 1, "temperature"],
             "type": "number", "role": "measurement", "unit": "°C",
             "observed_at": (self.now - timedelta(hours=1)).isoformat(),
             "attempted_at": self.now.isoformat(), "cached": True},
            {"key": "steps", "role": "counter", "type": "number"},
            {"key": "volume", "role": "setting", "type": "number"},
        ]
        state = {"items": [{"name": "ignore"}, {"temperature": 20, "serial": 123}],
                 "steps": 42, "volume": 15, "device_id": 500}
        self.store.ingest(self.packet(state=state, fields=fields))
        event = self.store.history("test.sensor", projection="chart")["events"][0]
        self.assertEqual(event["state"], {"items": [None, {"temperature": 20}], "steps": 42})
        self.assertEqual({field["key"] for field in event["fields"]}, {"nested-temperature", "steps"})
        temperature = next(field for field in event["fields"] if field["key"] == "nested-temperature")
        self.assertEqual(temperature["observed_at"], fields[0]["observed_at"])
        self.assertEqual(temperature["attempted_at"], fields[0]["attempted_at"])
        self.assertTrue(temperature["cached"])

    def test_optimized_chart_matches_complete_catalog_replay(self):
        fixtures = [
            {"state": {"temperature": 20, "device_id": 1234,
                       **{f"detail_{i}": f"description {i}" for i in range(30)}}},
            {"state": {"items": [{"name": "ignored"},
                                  {"temperature": 20, "serial": 123}], "model": "demo"}},
            {"state": {"status": "offline", "temperature": 20}},
            {"state": {"status": "online"}, "fields": [
                {"key": "temperature", "type": "number", "role": "measurement",
                 "unit": "°C", "status": "unavailable", "code": -1,
                 "attempted_at": self.now.isoformat()}]},
            {"state": {"temperature": 20, "model": "demo"}, "fields": [
                {"key": "temperature", "observed_at": self.now.isoformat(),
                 "cached": True, "source_sample_cached": True,
                 "source_updated_at": self.now.isoformat(), "source_age_seconds": 60}]},
            {"state": {"private": {"temperature": 20}, "humidity": 50}},
            {"state": {"uptime-seconds": 90, "delay-off-countdown": 5,
                       "connected-device-count": 4, "temperature": 22},
             "fields": [{"key": "delay-off-countdown", "unit": "分钟"},
                        {"key": "connected-device-count", "unit": "台"}]},
        ]
        for index, fixture in enumerate(fixtures):
            resource_id = f"test.chart-{index}"
            self.store.ingest(self.packet(resource_id=resource_id, **fixture))
            if index == 2:
                # Older catalogs may say "ok" while the resource is offline;
                # replay must still honor the original resource status.
                with self.store._database() as connection:
                    row = connection.execute(
                        "SELECT * FROM hub_events WHERE resource_id=?", (resource_id,)
                    ).fetchone()
                    fields = json.loads(decode_event_fields(row))
                    for field in fields:
                        if field["key"] == "temperature":
                            field.update(status="ok", message="已读取")
                    connection.execute(
                        "UPDATE hub_events SET fields_blob=? WHERE id=?",
                        (encode_event_fields(json.dumps(fields)), row["id"]),
                    )
            if "private" in fixture["state"]:
                self.store.ingest(self.packet(
                    resource_id=resource_id, seconds=1, state=fixture["state"],
                    fields=[{"key": "private", "sensitive": True}],
                ))
            if "uptime-seconds" in fixture["state"]:
                # Catalogs from older releases classified clocks as numeric
                # measurements; replay and optimized selection must agree.
                with self.store._database() as connection:
                    row = connection.execute(
                        "SELECT * FROM hub_events WHERE resource_id=?", (resource_id,)
                    ).fetchone()
                    fields = json.loads(decode_event_fields(row))
                    for field in fields:
                        if field["key"] in {"uptime-seconds", "delay-off-countdown"}:
                            field.update(role="measurement", chartable=True)
                        elif field["key"] == "connected-device-count":
                            field.update(role="metadata", chartable=True)
                    connection.execute(
                        "UPDATE hub_events SET fields_blob=? WHERE id=?",
                        (encode_event_fields(json.dumps(fields)), row["id"]),
                    )
            with patch("xiaomi_ai_bridge.hub_data._chart_declarations", return_value=None):
                original = self.store.history(resource_id, projection="chart")
            with patch("xiaomi_ai_bridge.hub_data.build_field_catalog",
                       wraps=build_field_catalog) as builder:
                optimized = self.store.history(resource_id, projection="chart")
            with self.subTest(fixture=index):
                self.assertEqual(optimized, original)
                self.assertTrue(all("device_id" not in event["state"]
                                    for event in optimized["events"]))
                if index == 0:
                    self.assertEqual(len(builder.call_args.args[1]), 1)

    def test_chart_prefilter_still_rejects_misclassified_numeric_identifier(self):
        self.store.ingest(self.packet(state={"temperature": 20, "device_id": 1234}))
        with self.store._database() as connection:
            row = connection.execute("SELECT * FROM hub_events").fetchone()
            fields = json.loads(decode_event_fields(row))
            for field in fields:
                if field["key"] == "device_id":
                    field.update(role="measurement", chartable=True, comparable=True)
            connection.execute("UPDATE hub_events SET fields_blob=? WHERE id=?",
                               (encode_event_fields(json.dumps(fields)), row["id"]))
        event = self.store.history("test.sensor", projection="chart")["events"][0]
        self.assertEqual(event["state"], {"temperature": 20})
        self.assertEqual([field["key"] for field in event["fields"]], ["temperature"])

    def test_incomplete_legacy_catalog_falls_back_to_inferred_measurements(self):
        self.store.ingest(self.packet(state={"temperature": 20, "device_id": 1234}))
        with self.store._database() as connection:
            row = connection.execute("SELECT * FROM hub_events").fetchone()
            fields = [field for field in json.loads(decode_event_fields(row))
                      if field["key"] == "device_id"]
            connection.execute("UPDATE hub_events SET fields_json=?,fields_blob=NULL WHERE id=?",
                               (json.dumps(fields), row["id"]))
        event = self.store.history("test.sensor", projection="chart")["events"][0]
        self.assertEqual(event["state"], {"temperature": 20})
        self.assertEqual([field["key"] for field in event["fields"]], ["temperature"])

    def test_live_field_expires_without_expiring_recent_resource(self):
        fields = [{"key": "temperature", "status": "ok",
                   "observed_at": (self.now - timedelta(hours=6)).isoformat()}]
        current = self.store.ingest(self.packet(source="xiaomi", fields=fields))
        self.assertFalse(current["stale"])
        self.assertEqual(current["fields"][0]["status"], "stale")
        historic = self.store.history("test.sensor")["events"][0]
        self.assertEqual(historic["fields"][0]["status"], "ok")

    def test_unconfigured_external_field_uses_resource_ttl(self):
        current = self.store.ingest(self.packet())
        self.assertEqual(current["fields"][0]["status"], "ok")

    def test_low_frequency_field_remains_valid_after_resource_expiration(self):
        fields = [{"key": "model", "observed_at": self.now.isoformat()}]
        current = self.store.ingest(self.packet(seconds=-3600, source="xiaomi",
                                                state={"model": "synthetic"}, fields=fields))
        self.assertTrue(current["stale"])
        self.assertEqual(current["fields"][0]["status"], "ok")
        self.assertTrue(current["fields"][0]["cached"])

    def test_new_history_is_compressed_and_legacy_history_still_reads(self):
        self.store.ingest(self.packet(20))
        with self.store._database() as connection:
            first = connection.execute("SELECT * FROM hub_events").fetchone()
            self.assertEqual(first["fields_json"], "[]")
            self.assertIsInstance(first["fields_blob"], bytes)
            original = decode_event_fields(first)
            connection.execute("UPDATE hub_events SET fields_json=?, fields_blob=NULL WHERE id=?",
                               (original, first["id"]))
        self.store.ingest(self.packet(21, seconds=1))
        self.assertEqual([event["state"]["temperature"] for event in self.store.history("test.sensor")["events"]], [20, 21])
        HubDataStore(Path(self.temporary.name))
        with self.store._database() as connection:
            first = connection.execute("SELECT * FROM hub_events ORDER BY id LIMIT 1").fetchone()
            self.assertIsNone(first["fields_blob"], "Opening a store must not rewrite legacy rows")

    def test_opening_legacy_schema_preserves_old_readings(self):
        legacy_dir = Path(self.temporary.name) / "legacy"
        legacy_dir.mkdir()
        observed = (self.now - timedelta(seconds=1)).isoformat()
        old_fields = '[{"key":"temperature","unit":"°C","status":"ok"}]'
        with sqlite3.connect(legacy_dir / "hub-data.sqlite3") as connection:
            connection.execute(
                "CREATE TABLE hub_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "resource_id TEXT, kind TEXT, summary TEXT, state_json TEXT, "
                "observed_at TEXT, received_at TEXT, source TEXT, fields_json TEXT)"
            )
            connection.execute(
                "INSERT INTO hub_events VALUES (1,?,?,?,?,?,?,?,?)",
                ("test.sensor", "sensor", "old reading", '{"temperature":18}',
                 observed, observed, "external", old_fields),
            )
        migrated = HubDataStore(legacy_dir)
        migrated.ingest(self.packet(20))
        events = migrated.history("test.sensor")["events"]
        self.assertEqual([event["state"]["temperature"] for event in events], [18, 20])
        with migrated._database() as connection:
            row = connection.execute("SELECT * FROM hub_events WHERE id=1").fetchone()
            self.assertEqual(row["fields_json"], old_fields)
            self.assertIsNone(row["fields_blob"])

    def test_retention_keeps_latest_observations_not_latest_arrivals(self):
        with patch("xiaomi_ai_bridge.hub_data.HUB_HISTORY_MAX_EVENTS", 3):
            for seconds in [0, 1, 2, -3600]:
                self.store.ingest(self.packet(seconds=seconds))
        events = self.store.history("test.sensor")["events"]
        self.assertEqual([event["observed_at"] for event in events],
                         [(self.now + timedelta(seconds=i)).isoformat() for i in range(3)])

    def test_queue_overflow_requests_snapshot_resynchronization(self):
        slow = self.store.subscribe()
        fast = self.store.subscribe()
        for number in range(17):
            self.store._publish({"type": "hub.update", "number": number})
            self.assertEqual(fast.get_nowait()["number"], number)
        self.assertEqual(slow.qsize(), 1)
        self.assertEqual(slow.get_nowait(), {"type": "hub.resync"})
        self.store.unsubscribe(slow)
        self.store.unsubscribe(fast)

    def test_observed_history_index_avoids_temporary_sort(self):
        with self.store._database() as connection:
            plan = connection.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM hub_events WHERE resource_id=? "
                "AND observed_at>=? ORDER BY observed_at DESC,id DESC LIMIT ?",
                ("test.sensor", "", 60),
            ).fetchall()
        self.assertTrue(any("idx_hub_events_observed" in row[3] for row in plan))
        self.assertFalse(any("TEMP B-TREE" in row[3] for row in plan))


class CatalogCompressionTests(unittest.TestCase):
    def test_compression_round_trip_unicode(self):
        original = json.dumps([{"label": "温度", "unit": "°C"}], ensure_ascii=False)
        self.assertEqual(decode_event_fields({"fields_blob": encode_event_fields(original)}), original)

    def test_compression_rejects_invalid_truncated_and_oversized_data(self):
        compressed = encode_event_fields("[]")
        invalid = [b"invalid", compressed[:-1], compressed + b"extra",
                   zlib.compress(b"x" * (MAX_EVENT_FIELDS_BYTES + 1)),
                   zlib.compress(b"\xff")]
        for blob in invalid:
            with self.subTest(blob_length=len(blob)), self.assertRaises(ValueError):
                decode_event_fields({"fields_blob": blob})
        with self.assertRaises(ValueError):
            encode_event_fields("x" * (MAX_EVENT_FIELDS_BYTES + 1))

    def test_historical_catalog_does_not_reinterpret_age(self):
        observed = (datetime.now(UTC) - timedelta(days=7)).isoformat()
        common = dict(source="xiaomi", observed_at=observed)
        historical = build_field_catalog({"temperature": 20}, freshness=False, **common)
        current = build_field_catalog({"temperature": 20}, freshness=True, **common)
        self.assertEqual(historical[0]["status"], "ok")
        self.assertEqual(current[0]["status"], "stale")


if __name__ == "__main__":
    unittest.main()

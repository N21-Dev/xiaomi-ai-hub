"""Display semantics must not invent units, values, or analysis permissions."""

from __future__ import annotations

import unittest

from xiaomi_ai_bridge.field_catalog import (
    build_field_catalog,
    normalize_fields,
)
from xiaomi_ai_bridge.field_presentation import presentation_for_field


class FieldPresentationTests(unittest.TestCase):
    def catalog(self, state, fields=None, **kwargs):
        return {
            field["key"]: field
            for field in build_field_catalog(state, fields, **kwargs)
        }

    def test_current_total_and_accumulated_counts_are_distinct(self):
        state = {
            "local.connected-device-count": 0,
            "local.known-device-count": 14,
            "active-threads": 2,
            "total-threads": 20,
            "steps": 7500,
            "input_tokens": 120,
        }
        fields = [
            {"key": "local.connected-device-count", "unit": "台", "role": "measurement"},
            {"key": "local.known-device-count", "unit": "台", "role": "counter"},
        ]
        catalog = self.catalog(state, fields, source="xiaomi")
        expected = {
            "local.connected-device-count": "current_count",
            "local.known-device-count": "total_count",
            "active-threads": "current_count",
            "total-threads": "total_count",
            "steps": "cumulative_count",
            "input_tokens": "cumulative_count",
        }
        for key, kind in expected.items():
            with self.subTest(key=key):
                self.assertEqual(catalog[key]["presentation"]["group"], "usage")
                self.assertEqual(catalog[key]["presentation"]["kind"], kind)
                self.assertEqual(catalog[key]["role"], "counter")
                self.assertTrue(catalog[key]["chartable"])
                self.assertEqual(catalog[key]["priority"], "primary")
        self.assertEqual(catalog["local.connected-device-count"]["value"], 0)

    def test_generic_counter_does_not_claim_accumulation(self):
        field = self.catalog({"occupancy": 4}, [{"key": "occupancy", "role": "counter"}])["occupancy"]
        self.assertEqual(field["presentation"]["kind"], "current_count")
        self.assertNotIn("累计", field["presentation"]["label"])

    def test_countdown_uses_declared_minutes_despite_seconds_in_name(self):
        fields = [{"key": "work-countdown-sec", "type": "number", "unit": "minutes", "role": "measurement"}]
        field = self.catalog({"work-countdown-sec": 30}, fields, source="xiaomi")["work-countdown-sec"]
        self.assertEqual(field["value"], 30)
        self.assertEqual(field["unit"], "分钟")
        self.assertEqual(field["role"], "state")
        self.assertEqual(field["presentation"], {"group": "states", "kind": "countdown", "label": "倒计时"})
        self.assertFalse(field["chartable"])
        self.assertFalse(field["comparable"])
        self.assertEqual(field["refresh_seconds"], 60)

    def test_uptime_is_running_state_and_keeps_raw_seconds(self):
        fields = [{"key": "local.uptime-seconds", "role": "counter", "unit": "seconds"}]
        field = self.catalog({"local.uptime-seconds": 90061}, fields)["local.uptime-seconds"]
        self.assertEqual(field["presentation"]["kind"], "duration")
        self.assertEqual(field["presentation"]["group"], "states")
        self.assertEqual(field["role"], "state")
        self.assertFalse(field["chartable"])
        self.assertEqual(field["value"], 90061)
        self.assertEqual(field["unit"], "秒")

    def test_filter_remaining_percentage_and_used_hours_keep_different_units(self):
        catalog = self.catalog(
            {"filter-life-level": 75, "filter-used-time": 120},
            [{"key": "filter-used-time", "unit": "hours"}],
        )
        remaining = catalog["filter-life-level"]
        used = catalog["filter-used-time"]
        self.assertEqual(remaining["presentation"]["group"], "usage")
        self.assertEqual(remaining["presentation"]["kind"], "percentage")
        self.assertEqual(used["presentation"]["group"], "usage")
        self.assertEqual(used["presentation"]["kind"], "duration")
        self.assertEqual(used["presentation"]["label"], "累计时长")
        self.assertEqual(used["unit"], "小时")
        self.assertEqual(used["role"], "measurement")
        self.assertTrue(used["chartable"])

    def test_humidity_and_cpu_percentage_belong_to_different_groups(self):
        catalog = self.catalog({"humidity": 0, "cpu-percent": 0, "progress": 0})
        self.assertEqual(catalog["humidity"]["presentation"]["group"], "measurements")
        self.assertEqual(catalog["cpu-percent"]["presentation"]["group"], "usage")
        for field in catalog.values():
            self.assertEqual(field["presentation"]["kind"], "percentage")
            self.assertEqual(field["value"], 0)

    def test_writable_countdown_stays_a_setting(self):
        field = self.catalog({"countdown-time": 15}, [{"key": "countdown-time", "unit": "minutes", "writable": True}])["countdown-time"]
        self.assertEqual(field["role"], "setting")
        self.assertEqual(field["presentation"]["group"], "settings")
        self.assertEqual(field["presentation"]["kind"], "countdown")
        self.assertTrue(field["writable"])
        self.assertFalse(field["chartable"])

    def test_boolean_and_enum_state_keep_actual_values(self):
        catalog = self.catalog(
            {"on": False, "mode": 0, "child-lock": True},
            [
                {"key": "on", "writable": True},
                {"key": "mode", "options": [{"value": 0, "label": "自动"}]},
                {"key": "child-lock", "writable": True},
            ],
        )
        self.assertEqual(catalog["on"]["presentation"]["group"], "states")
        self.assertEqual(catalog["on"]["presentation"]["kind"], "boolean")
        self.assertIs(catalog["on"]["value"], False)
        self.assertEqual(catalog["mode"]["presentation"]["kind"], "enum")
        self.assertEqual(catalog["mode"]["value"], 0)
        self.assertEqual(catalog["child-lock"]["presentation"]["group"], "settings")

    def test_numeric_ids_and_unknown_numbers_are_not_measurements(self):
        catalog = self.catalog(
            {"device_id": 123, "unknown-code": 42},
            [{"key": "device_id", "unit": "%", "role": "measurement"}],
        )
        self.assertEqual(catalog["device_id"]["presentation"]["kind"], "identifier")
        for field in catalog.values():
            self.assertEqual(field["presentation"]["group"], "profile")
            self.assertFalse(field["chartable"])
            self.assertFalse(field["comparable"])

    def test_large_numeric_identifier_has_exact_display_text(self):
        identifier = 9007199254740993123
        field = self.catalog({"device_id": identifier})["device_id"]
        self.assertEqual(field["value"], identifier)
        self.assertEqual(field["type"], "number")
        self.assertEqual(field["presentation"]["kind"], "identifier")
        self.assertEqual(field["presentation"]["text"], "9007199254740993123")

    def test_sensitive_numeric_identifier_never_has_display_text(self):
        field = self.catalog(
            {"device_id": 9007199254740993123},
            [{"key": "device_id", "sensitive": True}],
        )["device_id"]
        self.assertEqual(field["status"], "sensitive")
        self.assertNotIn("value", field)
        self.assertNotIn("text", field["presentation"])

    def test_identifier_without_visible_value_has_no_display_text(self):
        field = self.catalog({"device_id": 9007199254740993123}, offline=True)["device_id"]
        self.assertEqual(field["presentation"]["kind"], "identifier")
        self.assertNotIn("value", field)
        self.assertNotIn("text", field["presentation"])

    def test_string_identifier_and_large_measurement_keep_normal_shape(self):
        catalog = self.catalog({"device_id": "000123", "steps": 9007199254740993123})
        self.assertEqual(catalog["device_id"]["value"], "000123")
        self.assertNotIn("text", catalog["device_id"]["presentation"])
        self.assertNotIn("text", catalog["steps"]["presentation"])

    def test_timestamp_remains_profile_even_when_numeric(self):
        catalog = self.catalog({"updated_at": 1791210000, "observed-at": "2026-10-06T12:00:00+00:00"})
        for field in catalog.values():
            self.assertEqual(field["presentation"]["group"], "profile")
            self.assertEqual(field["presentation"]["kind"], "timestamp")
            self.assertFalse(field["chartable"])

    def test_nested_arrays_are_records_without_forcing_numbers_into_charts(self):
        catalog = self.catalog({"events": [{"note": "开机", "values": [0, 1]}]})
        for key in ("events", "/events/0", "/events/0/values"):
            self.assertEqual(catalog[key]["presentation"]["group"], "records")
            self.assertEqual(catalog[key]["presentation"]["kind"], "collection")
        for key in ("/events/0/note", "/events/0/values/0", "/events/0/values/1"):
            self.assertEqual(catalog[key]["presentation"]["group"], "records")
            self.assertFalse(catalog[key]["chartable"])

    def test_container_names_do_not_turn_nested_measurements_into_ids(self):
        catalog = self.catalog({"device_id": {"temperature": 21}, "model": {"humidity": 40}})
        self.assertEqual(catalog["device_id"]["presentation"]["group"], "records")
        self.assertEqual(catalog["model"]["presentation"]["group"], "records")
        for key in ("/device_id/temperature", "/model/humidity"):
            self.assertEqual(catalog[key]["role"], "measurement")
            self.assertEqual(catalog[key]["presentation"]["group"], "measurements")
            self.assertTrue(catalog[key]["chartable"])

    def test_source_field_ancestor_does_not_override_leaf_meaning(self):
        field = self.catalog({"temperature": 21}, [{"key": "temperature", "source_field": "devices.id.temperature"}])["temperature"]
        self.assertEqual(field["role"], "measurement")
        self.assertTrue(field["chartable"])

    def test_dotted_key_container_does_not_override_leaf_meaning(self):
        field = self.catalog({"device_id.temperature": 21})["device_id.temperature"]
        self.assertEqual(field["role"], "measurement")
        self.assertTrue(field["chartable"])

    def test_declared_semantic_cannot_disguise_an_identifier(self):
        field = self.catalog(
            {"device_id": 123},
            [{"key": "device_id", "semantic": "temperature", "role": "measurement", "unit": "℃"}],
        )["device_id"]
        self.assertEqual(field["role"], "identity")
        self.assertEqual(field["presentation"]["kind"], "identifier")
        self.assertFalse(field["chartable"])

    def test_sensitive_ancestor_still_redacts_nested_measurements(self):
        catalog = self.catalog({"api_key": {"temperature": 21}})
        self.assertEqual(catalog["api_key"]["status"], "sensitive")
        self.assertFalse(catalog["api_key"]["readable"])
        self.assertFalse(catalog["api_key"]["chartable"])
        self.assertNotIn("/api_key/temperature", catalog)
        self.assertNotIn("value", catalog["api_key"])

    def test_declared_sensitive_leaf_cannot_override_redaction_or_presentation(self):
        field = self.catalog(
            {"temperature": 21},
            [{"key": "temperature", "sensitive": True, "writable": True,
              "presentation": {"group": "measurements", "kind": "percentage"}}],
        )["temperature"]
        self.assertEqual(field["presentation"]["group"], "profile")
        self.assertEqual(field["status"], "sensitive")
        self.assertFalse(field["readable"])
        self.assertFalse(field["writable"])
        self.assertFalse(field["chartable"])
        self.assertNotIn("value", field)

    def test_input_presentation_is_discarded_and_recomputed(self):
        declaration = {"key": "humidity", "presentation": {"group": "profile", "kind": "identifier"}}
        self.assertNotIn("presentation", normalize_fields([declaration])[0])
        field = self.catalog({"humidity": 50}, [declaration])["humidity"]
        self.assertEqual(field["presentation"]["group"], "measurements")

    def test_missing_percentage_does_not_get_a_synthetic_zero(self):
        field = self.catalog({}, [{"key": "progress", "type": "number"}])["progress"]
        self.assertEqual(field["presentation"]["kind"], "percentage")
        self.assertEqual(field["status"], "unavailable")
        self.assertNotIn("value", field)

    def test_offline_state_keeps_semantic_group_and_hides_old_value(self):
        field = self.catalog({"humidity": 0}, offline=True)["humidity"]
        self.assertEqual(field["presentation"]["group"], "measurements")
        self.assertEqual(field["status"], "offline")
        self.assertNotIn("value", field)

    def test_explicit_analysis_restrictions_are_preserved(self):
        field = self.catalog({"steps": 42}, [{"key": "steps", "chartable": False, "comparable": False}])["steps"]
        self.assertEqual(field["presentation"]["kind"], "cumulative_count")
        self.assertFalse(field["chartable"])
        self.assertFalse(field["comparable"])

    def test_health_duration_keeps_measurement_and_chart_contract(self):
        field = self.catalog(
            {"sleep-duration": 420},
            [{"key": "sleep-duration", "role": "measurement", "unit": "minutes"}],
            source="mi-health-hub",
        )["sleep-duration"]
        self.assertEqual(field["presentation"]["kind"], "duration")
        self.assertEqual(field["presentation"]["group"], "measurements")
        self.assertEqual(field["role"], "measurement")
        self.assertEqual(field["value"], 420)
        self.assertTrue(field["chartable"])
        self.assertEqual(field["refresh_seconds"], 0)

    def test_explicit_identity_role_is_not_guessed_away_under_container(self):
        key = "/device_id/temperature"
        field = self.catalog(
            {"device_id": {"temperature": 21}},
            [{"key": key, "path": ["device_id", "temperature"], "role": "identity"}],
        )[key]
        self.assertEqual(field["role"], "identity")
        self.assertEqual(field["presentation"]["group"], "profile")
        self.assertFalse(field["chartable"])

    def test_presentation_does_not_mutate_its_input(self):
        field = {"role": "measurement", "semantic": "humidity", "type": "number", "unit": "%", "value": 0}
        before = dict(field)
        self.assertEqual(presentation_for_field(field)["kind"], "percentage")
        self.assertEqual(field, before)


if __name__ == "__main__":
    unittest.main()

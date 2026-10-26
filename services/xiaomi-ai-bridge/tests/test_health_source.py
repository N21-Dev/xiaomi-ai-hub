"""健康工具的运动记录窗口回归。

背景：健康中心的每日覆盖度是同步后生成的，可能还不包含今天。
若直接拿「最近有覆盖数据的日期」当窗口终点，今天刚结束的运动就查不到，
表现为「中枢助手不知道刚刚的运动」。这里锁住窗口终点必须到今天就止。
"""

import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from xiaomi_ai_bridge.health_source import CN_TZ, HealthSource


class WorkoutWindowTest(unittest.TestCase):
    def test_window_reaches_today_when_coverage_lags(self):
        today = date(2024, 2, 10)
        yesterday = today - timedelta(days=1)
        start, end = HealthSource.workout_window(yesterday, today, 7, False)
        self.assertEqual(end, today, "覆盖度停在昨天时，运动窗口仍必须查到今天")
        self.assertEqual(start, yesterday - timedelta(days=6))

    def test_window_keeps_today_when_coverage_is_fresh(self):
        today = date(2024, 2, 10)
        start, end = HealthSource.workout_window(today, today, 7, False)
        self.assertEqual(end, today)
        self.assertEqual(start, today - timedelta(days=6))

    def test_explicit_date_is_respected(self):
        selected = date(2026, 10, 10)
        today = date(2024, 2, 10)
        start, end = HealthSource.workout_window(selected, today, 30, True)
        self.assertEqual(end, selected, "指定了日期就不该越界到今天")
        self.assertEqual(start, selected - timedelta(days=29))


class WorkoutsTest(unittest.TestCase):
    def test_formats_today_workout(self):
        today = date(2024, 2, 10)
        started = datetime(2024, 2, 10, 10, 0, tzinfo=CN_TZ)
        payload = {
            "items": [
                {
                    "start_ts": int(started.timestamp()),
                    "type": "swimming",
                    "type_cn": "游泳",
                    "duration_s": 3600,
                    "distance_m": 1500.0,
                    "calories": 250.0,
                    "avg_hr": 100,
                    "avg_pace_text": "4:00/100米",
                }
            ]
        }
        rows = HealthSource._workouts(payload, today - timedelta(days=6), today)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date"], "2024-02-10")
        self.assertEqual(rows[0]["label"], "游泳")
        self.assertEqual(rows[0]["duration_min"], 60.0)
        self.assertEqual(rows[0]["distance_km"], 1.5)

    def test_out_of_window_is_dropped(self):
        today = date(2024, 2, 10)
        started = datetime(2026, 10, 1, 8, 0, tzinfo=CN_TZ)
        payload = {"items": [{"start_ts": int(started.timestamp()), "type": "running"}]}
        rows = HealthSource._workouts(payload, today - timedelta(days=6), today)
        self.assertEqual(rows, [], "窗口外的运动不应出现")


class LoadWindowTest(unittest.IsolatedAsyncioTestCase):
    async def test_workouts_request_ends_today(self):
        settings = SimpleNamespace(
            health_service_url="http://health.test:8321",
            health_web_url="",
            health_api_key_file=None,
            health_poll_seconds=300,
            health_ai_tools_enabled=True,
        )
        source = HealthSource(settings)
        today = datetime.now(tz=CN_TZ).date()
        yesterday = today - timedelta(days=1)
        calls: list[tuple[str, dict]] = []

        async def fake_get(_self, path, params):
            calls.append((path, dict(params)))
            if path == "/api/coverage":
                return {
                    "items": [
                        {
                            "date": yesterday.isoformat(),
                            "key_name": "steps",
                            "sample_count": 1,
                            "source": "mi-health-hub",
                        }
                    ]
                }
            if path == "/api/summary":
                return {"date": params["date"]}
            if path == "/api/workouts":
                return {"items": []}
            return {}

        with patch.object(HealthSource, "_get", fake_get):
            snapshot = await source._load(None, 7)

        workout_calls = [call for call in calls if call[0] == "/api/workouts"]
        self.assertEqual(len(workout_calls), 1)
        self.assertEqual(
            workout_calls[0][1]["end"],
            today.isoformat(),
            "覆盖度只有昨天时，运动记录仍要查到今天",
        )
        self.assertEqual(snapshot["workouts_end_date"], today.isoformat())


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from xiaomi_ai_bridge.weather_source import (
    LOCATION_TOOL_NAME,
    WEATHER_TOOL_NAME,
    WeatherSource,
)


def settings(**overrides):
    values = {
        "weather_tools_enabled": True,
        "location_default_query": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class WeatherToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_tools_are_explicitly_gated(self):
        disabled = WeatherSource(settings(weather_tools_enabled=False))
        self.assertEqual(disabled.tool_definitions(), [])
        self.assertEqual(
            await disabled.execute(WEATHER_TOOL_NAME, {"location_query": "杭州"}),
            {"ok": False, "error": "定位和天气工具未启用，请先设置 WEATHER_TOOLS_ENABLED=true"},
        )

        enabled = WeatherSource(settings())
        names = [item["function"]["name"] for item in enabled.tool_definitions()]
        self.assertEqual(names, [LOCATION_TOOL_NAME, WEATHER_TOOL_NAME])

    async def test_resolve_location_returns_safe_candidates(self):
        source = WeatherSource(settings())
        source._request_json = AsyncMock(return_value={
            "results": [
                {
                    "name": "杭州",
                    "latitude": 30.2741,
                    "longitude": 120.1551,
                    "country": "中国",
                    "country_code": "CN",
                    "admin1": "浙江省",
                    "timezone": "Asia/Shanghai",
                    "elevation": 12.4,
                },
                {"name": "杭州测试", "latitude": 31, "longitude": 120},
            ]
        })
        result = await source.execute(LOCATION_TOOL_NAME, {"query": "杭州"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["ambiguous"])
        self.assertEqual(result["candidates"][0]["latitude"], 30.2741)
        self.assertNotIn("url", result)
        source._request_json.assert_awaited_once()

    async def test_weather_uses_geocoded_location_and_formats_forecast(self):
        source = WeatherSource(settings())
        source._request_json = AsyncMock(side_effect=[
            {"results": [{"name": "杭州", "latitude": 30.2741, "longitude": 120.1551, "country": "中国"}]},
            {
                "timezone": "Asia/Shanghai",
                "current": {
                    "temperature_2m": 23.4,
                    "relative_humidity_2m": 61,
                    "apparent_temperature": 24.1,
                    "is_day": 1,
                    "precipitation": 0.2,
                    "weather_code": 61,
                    "wind_speed_10m": 8.8,
                    "wind_direction_10m": 120,
                },
                "daily": {
                    "time": ["2026-10-20", "2026-10-21"],
                    "weather_code": [61, 3],
                    "temperature_2m_max": [25, 22],
                    "temperature_2m_min": [19, 17],
                    "precipitation_probability_max": [70, 10],
                    "precipitation_sum": [4.2, 0],
                    "wind_speed_10m_max": [18, 12],
                    "sunrise": ["2026-10-20T06:02", "2026-10-21T06:03"],
                    "sunset": ["2026-10-20T17:28", "2026-10-21T17:27"],
                },
            },
        ])
        result = await source.execute(WEATHER_TOOL_NAME, {"location_query": "杭州", "forecast_days": 2})
        self.assertTrue(result["ok"])
        self.assertEqual(result["location"]["name"], "杭州")
        self.assertEqual(result["current"]["weather"]["label"], "小雨")
        self.assertEqual(result["current"]["temperature_c"], 23.4)
        self.assertEqual(result["daily"][0]["precipitation_probability_percent"], 70.0)
        self.assertEqual(result["daily"][1]["weather"]["label"], "阴")
        self.assertTrue(result["read_only"])
        self.assertEqual(source._request_json.await_count, 2)

    async def test_weather_accepts_coordinates_without_geocoding(self):
        source = WeatherSource(settings())
        source._request_json = AsyncMock(return_value={
            "timezone": "Asia/Shanghai",
            "current": {"temperature_2m": 20, "weather_code": 0},
            "daily": {"time": ["2026-10-20"], "weather_code": [0]},
        })
        result = await source.execute(WEATHER_TOOL_NAME, {"latitude": 30, "longitude": 120, "forecast_days": 1})
        self.assertTrue(result["ok"])
        self.assertEqual(result["location"]["name"], "指定坐标")
        source._request_json.assert_awaited_once()

    async def test_missing_location_does_not_guess(self):
        source = WeatherSource(settings())
        result = await source.execute(WEATHER_TOOL_NAME, {"forecast_days": 1})
        self.assertFalse(result["ok"])
        self.assertIn("提供城市", result["error"])

    async def test_default_location_is_used_only_when_explicitly_configured(self):
        source = WeatherSource(settings(location_default_query="上海"))
        source._request_json = AsyncMock(side_effect=[
            {"results": [{"name": "上海", "latitude": 31.23, "longitude": 121.47}]},
            {"timezone": "Asia/Shanghai", "current": {}, "daily": {"time": [], "weather_code": []}},
        ])
        result = await source.execute(WEATHER_TOOL_NAME, {"forecast_days": 1})
        self.assertTrue(result["ok"])
        self.assertEqual(result["location"]["name"], "上海")


if __name__ == "__main__":
    unittest.main()

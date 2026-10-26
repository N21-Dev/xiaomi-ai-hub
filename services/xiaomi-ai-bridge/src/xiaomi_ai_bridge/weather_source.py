"""Read-only location resolution and weather queries for the single assistant.

The service deliberately does not infer a home location from the NAS public IP
or read a phone GPS position.  A tool call must contain a city/place, or the
operator may set LOCATION_DEFAULT_QUERY.  Requests are limited to the fixed
Open-Meteo geocoding and forecast hosts; no user-supplied URL is fetched.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import time
from typing import Any
from urllib.parse import urlencode

import aiohttp

LOCATION_TOOL_NAME = "resolve_location"
WEATHER_TOOL_NAME = "query_weather"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
MAX_RESPONSE_BYTES = 512 * 1024
WEATHER_CACHE_SECONDS = 300.0
LOCATION_CACHE_SECONDS = 3600.0

WEATHER_CODES = {
    0: "晴",
    1: "大致晴",
    2: "局部多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "较强毛毛雨",
    56: "冻毛毛雨",
    57: "较强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "小阵雨",
    81: "中阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "强阵雪",
    95: "雷雨",
    96: "雷雨伴小冰雹",
    99: "雷雨伴强冰雹",
}


def _number(value: Any, *, minimum: float, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        return None
    return number


def _text(value: Any, limit: int = 120) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        return None
    if any(ord(char) < 32 for char in value):
        return None
    return value.strip()


def _weather_code(value: Any) -> dict[str, Any]:
    try:
        code = int(value)
    except (TypeError, ValueError):
        return {"code": None, "label": "未知天气"}
    return {"code": code, "label": WEATHER_CODES.get(code, "未知天气")}


def _location_row(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    latitude = _number(row.get("latitude"), minimum=-90, maximum=90)
    longitude = _number(row.get("longitude"), minimum=-180, maximum=180)
    name = _text(row.get("name"))
    if latitude is None or longitude is None or name is None:
        return None
    result: dict[str, Any] = {
        "name": name,
        "latitude": round(latitude, 5),
        "longitude": round(longitude, 5),
    }
    for key in ("country", "country_code", "admin1", "admin2", "timezone"):
        if (value := _text(row.get(key), 80)) is not None:
            result[key] = value
    if isinstance(row.get("elevation"), (int, float)) and math.isfinite(float(row["elevation"])):
        result["elevation_m"] = round(float(row["elevation"]), 1)
    return result


class WeatherSource:
    """Bounded, read-only location and weather client used by the assistant."""

    def __init__(self, settings: Any):
        self.enabled = bool(getattr(settings, "weather_tools_enabled", False))
        self.default_query = str(getattr(settings, "location_default_query", "") or "").strip()
        if self.default_query and _text(self.default_query) is None:
            raise ValueError("LOCATION_DEFAULT_QUERY 不能超过 120 个字符")
        self._session: aiohttp.ClientSession | None = None
        self._semaphore = asyncio.Semaphore(4)
        self._location_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._weather_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def tool_definitions(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        return [
            {
                "type": "function",
                "function": {
                    "name": LOCATION_TOOL_NAME,
                    "description": (
                        "只读解析用户提供的城市、地区或地点名称为坐标。"
                        "服务器不会读取手机 GPS，也不会根据公网 IP 猜测家庭位置；"
                        "用户问当前位置时，只有配置了 LOCATION_DEFAULT_QUERY 才能使用默认地点。"
                        "地点有多个候选时返回候选列表，先向用户确认，不要擅自选择。"
                    ),
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "query": {"type": "string", "description": "城市或地点名称，例如 杭州、上海浦东"},
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": WEATHER_TOOL_NAME,
                    "description": (
                        "只读查询指定地点的当前天气和未来预报。优先使用用户明确说出的地点；"
                        "没有地点时使用 LOCATION_DEFAULT_QUERY，未配置则先询问城市。"
                        "返回的是 Open-Meteo 天气模型数据，当前条件不是家中传感器实测值；"
                        "不要把模型预报说成室内温度或精确实时观测。"
                    ),
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "location_query": {"type": "string", "description": "城市或地点名称"},
                            "latitude": {"type": "number", "minimum": -90, "maximum": 90},
                            "longitude": {"type": "number", "minimum": -180, "maximum": 180},
                            "forecast_days": {"type": "integer", "minimum": 1, "maximum": 7, "default": 3},
                        },
                        "required": [],
                    },
                },
            },
        ]

    async def execute(self, name: str, arguments: object) -> dict[str, Any]:
        if name not in {LOCATION_TOOL_NAME, WEATHER_TOOL_NAME}:
            return {"ok": False, "error": "不支持的位置或天气工具"}
        if not self.enabled:
            return {"ok": False, "error": "定位和天气工具未启用，请先设置 WEATHER_TOOLS_ENABLED=true"}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return {"ok": False, "error": "工具参数不是有效 JSON"}
        if not isinstance(arguments, dict):
            return {"ok": False, "error": "工具参数必须是对象"}
        allowed = {"query"} if name == LOCATION_TOOL_NAME else {"location_query", "latitude", "longitude", "forecast_days"}
        if set(arguments) - allowed:
            return {"ok": False, "error": "工具参数包含未允许字段"}
        try:
            if name == LOCATION_TOOL_NAME:
                return await self.resolve(arguments.get("query"))
            return await self.weather(arguments)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except (TimeoutError, aiohttp.ClientError):
            return {"ok": False, "error": "天气服务暂时无法连接，请稍后重试"}

    async def resolve(self, query: Any = None) -> dict[str, Any]:
        if query is not None and _text(query) is None:
            return {"ok": False, "error": "地点名称必须是 2 到 120 个字符的普通文本"}
        value = _text(query) if query is not None else self.default_query
        if not value:
            return {"ok": False, "error": "请先提供城市或地点名称；服务器没有手机 GPS 定位"}
        if len(value) < 2:
            return {"ok": False, "error": "地点名称至少需要 2 个字符"}
        rows = await self._geocode(value)
        if not rows:
            return {"ok": False, "error": f"没有找到地点：{value}"}
        return {
            "ok": True,
            "query": value,
            "candidates": rows,
            "ambiguous": len(rows) > 1,
            "read_only": True,
            "source": "Open-Meteo Geocoding / GeoNames",
        }

    async def weather(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raw_query = arguments.get("location_query")
        if raw_query is not None and _text(raw_query) is None:
            raise ValueError("地点名称必须是 2 到 120 个字符的普通文本")
        query = _text(raw_query) if raw_query is not None else None
        latitude = arguments.get("latitude")
        longitude = arguments.get("longitude")
        if (latitude is None) != (longitude is None):
            raise ValueError("latitude 和 longitude 必须同时提供")
        location: dict[str, Any]
        if latitude is not None:
            lat = _number(latitude, minimum=-90, maximum=90)
            lon = _number(longitude, minimum=-180, maximum=180)
            if lat is None or lon is None:
                raise ValueError("坐标范围无效")
            location = {"name": "指定坐标", "latitude": round(lat, 5), "longitude": round(lon, 5)}
        else:
            value = query or self.default_query
            if not value:
                raise ValueError("请提供城市或地点名称；也可以配置 LOCATION_DEFAULT_QUERY")
            rows = await self._geocode(value)
            if not rows:
                raise ValueError(f"没有找到地点：{value}")
            # Open-Meteo returns ranked matches. We expose the selected match
            # so the answer names the exact city instead of hiding the choice.
            location = rows[0]
        days = arguments.get("forecast_days", 3)
        if type(days) is not int or not 1 <= days <= 7:
            raise ValueError("forecast_days 必须是 1 到 7 的整数")
        cache_key = f"{location['latitude']:.5f},{location['longitude']:.5f}:{days}"
        cached = self._weather_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < WEATHER_CACHE_SECONDS:
            return {"ok": True, **copy.deepcopy(cached[1]), "cached": True}
        payload = await self._request_json(
            FORECAST_URL,
            {
                "latitude": location["latitude"],
                "longitude": location["longitude"],
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,is_day,precipitation,rain,showers,snowfall,weather_code,wind_speed_10m,wind_direction_10m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,apparent_temperature_max,apparent_temperature_min,precipitation_probability_max,precipitation_sum,wind_speed_10m_max,sunrise,sunset",
                "forecast_days": days,
                "timezone": "auto",
            },
        )
        result = self._format_weather(location, payload, days)
        self._weather_cache[cache_key] = (time.monotonic(), result)
        return {"ok": True, **copy.deepcopy(result), "cached": False}

    async def _geocode(self, query: str) -> list[dict[str, Any]]:
        key = query.casefold()
        cached = self._location_cache.get(key)
        if cached and time.monotonic() - cached[0] < LOCATION_CACHE_SECONDS:
            return copy.deepcopy(cached[1])
        payload = await self._request_json(
            GEOCODING_URL,
            {"name": query, "count": 5, "language": "zh", "format": "json"},
        )
        rows = [_location_row(row) for row in payload.get("results", [])] if isinstance(payload, dict) else []
        values = [row for row in rows if row is not None]
        self._location_cache[key] = (time.monotonic(), values)
        return copy.deepcopy(values)

    async def _request_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        if url not in {GEOCODING_URL, FORECAST_URL}:
            raise ValueError("天气请求地址不在固定白名单中")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10, connect=3),
                cookie_jar=aiohttp.DummyCookieJar(),
            )
        async with self._semaphore:
            async with self._session.get(url, params=params, allow_redirects=False) as response:
                if 300 <= response.status < 400:
                    raise ValueError("天气服务返回重定向，已停止请求")
                if response.status != 200:
                    raise ValueError("天气服务暂未返回有效数据")
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise ValueError("天气服务响应超过读取限制")
                try:
                    payload = json.loads(body)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise ValueError("天气服务返回的数据格式无法识别") from exc
                if not isinstance(payload, dict) or payload.get("error"):
                    raise ValueError("天气服务返回的数据结构无法识别")
                return payload

    @staticmethod
    def _format_weather(location: dict[str, Any], payload: dict[str, Any], days: int) -> dict[str, Any]:
        current_raw = payload.get("current") if isinstance(payload.get("current"), dict) else {}
        current: dict[str, Any] = {"weather": _weather_code(current_raw.get("weather_code"))}
        for target, source in (
            ("temperature_c", "temperature_2m"),
            ("apparent_temperature_c", "apparent_temperature"),
            ("humidity_percent", "relative_humidity_2m"),
            ("precipitation_mm", "precipitation"),
            ("rain_mm", "rain"),
            ("showers_mm", "showers"),
            ("snowfall_cm", "snowfall"),
            ("wind_speed_kmh", "wind_speed_10m"),
            ("wind_direction_deg", "wind_direction_10m"),
        ):
            value = _number(current_raw.get(source), minimum=-1000, maximum=10000)
            if value is not None:
                current[target] = round(value, 1)
        if isinstance(current_raw.get("is_day"), (bool, int)):
            current["is_day"] = bool(current_raw["is_day"])
        daily_raw = payload.get("daily") if isinstance(payload.get("daily"), dict) else {}
        times = daily_raw.get("time") if isinstance(daily_raw.get("time"), list) else []
        daily: list[dict[str, Any]] = []
        for index, day in enumerate(times[:days]):
            if not isinstance(day, str):
                continue
            item: dict[str, Any] = {"date": day}
            code_values = daily_raw.get("weather_code", [])
            item["weather"] = _weather_code(code_values[index] if index < len(code_values) else None)
            for target, source, minimum, maximum in (
                ("temperature_max_c", "temperature_2m_max", -1000, 1000),
                ("temperature_min_c", "temperature_2m_min", -1000, 1000),
                ("apparent_max_c", "apparent_temperature_max", -1000, 1000),
                ("apparent_min_c", "apparent_temperature_min", -1000, 1000),
                ("precipitation_probability_percent", "precipitation_probability_max", 0, 100),
                ("precipitation_sum_mm", "precipitation_sum", 0, 10000),
                ("wind_speed_max_kmh", "wind_speed_10m_max", 0, 1000),
            ):
                values = daily_raw.get(source, [])
                value = _number(values[index] if index < len(values) else None, minimum=minimum, maximum=maximum)
                if value is not None:
                    item[target] = round(value, 1)
            for target in ("sunrise", "sunset"):
                values = daily_raw.get(target, [])
                if index < len(values) and isinstance(values[index], str):
                    item[target] = values[index]
            daily.append(item)
        return {
            "location": location,
            "timezone": _text(payload.get("timezone"), 80),
            "current": current,
            "daily": daily,
            "forecast_days": len(daily),
            "source": "Open-Meteo forecast models",
            "read_only": True,
            "message": "天气为公开模型预报数据，不是室内传感器实测值",
        }

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
        self._session = None

"""Read-only federation with the separate mi-health-hub application.

Health remains the system of record. This adapter neither opens its database nor
calls synchronization, authentication, analysis, or other mutating endpoints.
"""

from __future__ import annotations

import asyncio
import copy
import ipaddress
import json
import math
import re
import time
from datetime import UTC, datetime, timedelta, timezone
from datetime import date as Date
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import aiohttp

from .field_catalog import build_field_catalog
from .logging_setup import scrub

CN_TZ = timezone(timedelta(hours=8))
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SOURCE = "mi-health-hub"
HEALTH_TOOL_NAME = "query_health_data"
METRICS = {
    "steps": ("步数", "步"),
    "resting_hr": ("静息心率", "次/分"),
    "calories": ("活动消耗", "千卡"),
    "sleep_total": ("睡眠时长", "分钟"),
    "sleep_deep": ("深睡时长", "分钟"),
    "sleep_rem": ("REM 时长", "分钟"),
    "nap": ("小睡时长", "分钟"),
    "active_min": ("活动时间（心率推算）", "分钟"),
    "vigorous_min": ("高强度活动（心率推算）", "分钟"),
    "hr_avg": ("日均心率", "次/分"),
    "sleep_efficiency": ("睡眠效率", "%"),
    "spo2_min": ("日最低血氧", "%"),
    "stress_avg": ("平均压力", "分"),
}
COVERAGE_SOURCES = {
    "steps": "活动",
    "sleep": "睡眠",
    "heart_rate": "心率",
    "spo2": "血氧",
    "stress": "压力",
}
RESOURCE_FIELDS = {
    "activity": {
        "steps": ("步数", "步"),
        "distance_m": ("活动距离", "m"),
        "calories": ("活动消耗", "千卡"),
        "active_min": ("活动时间", "分钟"),
        "standing_count": ("有效站立次数", "次"),
        "vigorous_min": ("高强度活动（心率推算）", "分钟"),
    },
    "sleep": {
        "total_min": ("实际睡眠", "分钟"),
        "in_bed_min": ("卧床时长", "分钟"),
        "deep_min": ("深睡", "分钟"),
        "light_min": ("浅睡", "分钟"),
        "rem_min": ("REM 睡眠", "分钟"),
        "awake_min": ("清醒", "分钟"),
        "nap_min": ("小睡", "分钟"),
        "efficiency_percent": ("睡眠效率", "%"),
        "score": ("睡眠评分", "分"),
        "avg_hr": ("睡眠平均心率", "次/分"),
        "min_hr": ("睡眠最低心率", "次/分"),
        "max_hr": ("睡眠最高心率", "次/分"),
        "avg_spo2": ("睡眠平均血氧", "%"),
        "min_spo2": ("睡眠最低血氧", "%"),
        "max_spo2": ("睡眠最高血氧", "%"),
        "avg_breath": ("睡眠呼吸率", "次/分"),
        "awake_count": ("觉醒次数", "次"),
        "has_stage": ("包含睡眠分期", ""),
        "has_rem": ("已识别 REM", ""),
        "start_time": ("入睡时间", ""),
        "end_time": ("醒来时间", ""),
    },
    "heart": {
        "resting_hr": ("静息心率", "次/分"),
        "average_hr": ("日均心率", "次/分"),
        "min_spo2": ("日最低血氧", "%"),
        "stress_average": ("平均压力", "分"),
    },
    "training": {"count": ("运动次数", "次"), "duration_min": ("运动总时长", "分钟")},
}
RESOURCE_NAMES = {
    "activity": "日常活动",
    "sleep": "睡眠",
    "heart": "心率与身体指标",
    "training": "运动汇总",
}


class _HealthError(RuntimeError):
    """Safe diagnostic: never constructed from raw upstream error contents."""


def _now() -> datetime:
    return datetime.now(UTC)


def _date(value: Any) -> Date:
    if not isinstance(value, str) or not DATE_PATTERN.fullmatch(value):
        raise ValueError("日期必须是 YYYY-MM-DD")
    try:
        return Date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("日期不是有效的日历日期") from exc


def _number(value: Any) -> int | float | None:
    if type(value) not in (int, float):
        return None
    if type(value) is float and not math.isfinite(value):
        return None
    return value if 0 <= value <= 1e12 else None


def _timestamp(value: Any) -> str | None:
    number = _number(value)
    if number is None or not 946684800 <= number <= _now().timestamp() + 300:
        return None
    return datetime.fromtimestamp(number, CN_TZ).isoformat()


def _base_url(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 500:
        raise ValueError("健康服务地址必须是有效的 HTTP 或 HTTPS 地址")
    try:
        parts = urlsplit(value)
        valid = (
            parts.scheme in {"http", "https"}
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
            and not parts.query
            and not parts.fragment
            and parts.port != 0
            and not any(char.isspace() or ord(char) < 32 for char in value)
            and "\\" not in value
            and ".." not in unquote(parts.path).split("/")
        )
        if parts.hostname:
            try:
                address = ipaddress.ip_address(parts.hostname)
            except ValueError:
                # Configured LAN DNS / Compose service names are also valid.
                pass
            else:
                valid = (
                    valid and not address.is_multicast and not address.is_unspecified
                )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("健康服务地址不能包含账号密码、查询参数或无效路径")
    return value.rstrip("/")


class HealthSource:
    def __init__(self, settings: Any):
        configured = getattr(settings, "health_service_url", "") or ""
        self.base_url = _base_url(configured) if configured else ""
        web = getattr(settings, "health_web_url", "") or configured
        self.web_url = _base_url(web) if web else ""
        self.key_file = getattr(settings, "health_api_key_file", None)
        poll = getattr(settings, "health_poll_seconds", 300)
        if type(poll) not in (int, float) or not 30 <= poll <= 3600:
            raise ValueError("健康服务缓存时间必须在 30 到 3600 秒之间")
        self.poll_seconds = float(poll)
        self.ai_tools_enabled = bool(getattr(settings, "health_ai_tools_enabled", False))
        self._session: aiohttp.ClientSession | None = None
        self._semaphore = asyncio.Semaphore(6)
        self._inflight: dict[tuple[str, int], asyncio.Task[dict[str, Any]]] = {}
        self._cache: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}

    def tool_definitions(self) -> list[dict[str, Any]]:
        """Expose bounded, read-only health queries only when explicitly enabled."""
        if not self.ai_tools_enabled or not self.base_url:
            return []
        return [{
            "type": "function",
            "function": {
                "name": HEALTH_TOOL_NAME,
                "description": (
                    "只读查询独立健康中心的数据。健康中心是健康数据库的唯一数据源，"
                    "本工具不会同步、修改、登录或打开数据库。可查询所选日期摘要、7/30天趋势、"
                    "运动记录和数据覆盖度。运动记录（query=workouts）覆盖最近 days 天直到当前时刻，"
                    "包含今天刚结束的单次运动；问「刚刚/今天运动了什么」就用它。"
                    "健康数据缺失、过期或部分不可读时必须如实说明。"
                    "只在用户明确询问自己的睡眠、步数、心率、运动或健康记录时使用。"
                ),
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "query": {"type": "string", "enum": ["summary", "trend", "workouts", "coverage"], "description": "查询类型"},
                        "date": {"type": "string", "description": "可选日期 YYYY-MM-DD；不填使用最近有数据日期"},
                        "days": {"type": "integer", "enum": [7, 30], "description": "趋势或覆盖范围，默认7天"},
                        "metric": {"type": "string", "enum": sorted(METRICS), "description": "trend 查询的指标"},
                    },
                    "required": ["query"],
                },
            },
        }]

    async def execute(self, name: str, arguments: object) -> dict[str, Any]:
        if name != HEALTH_TOOL_NAME:
            return {"ok": False, "error": "不支持的健康工具"}
        if not self.ai_tools_enabled:
            return {"ok": False, "error": "健康数据 AI 工具未启用，请先设置 HEALTH_AI_TOOLS_ENABLED=true"}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return {"ok": False, "error": "健康工具参数不是有效 JSON"}
        if not isinstance(arguments, dict) or set(arguments) - {"query", "date", "days", "metric"}:
            return {"ok": False, "error": "健康工具参数不符合只读查询格式"}
        query = str(arguments.get("query") or "").strip()
        if query not in {"summary", "trend", "workouts", "coverage"}:
            return {"ok": False, "error": "健康查询类型不支持"}
        days = arguments.get("days", 7)
        if type(days) is not int or days not in {7, 30}:
            return {"ok": False, "error": "健康查询范围只支持7或30天"}
        metric = str(arguments.get("metric") or "").strip()
        if query == "trend" and metric not in METRICS:
            return {"ok": False, "error": "趋势查询必须选择支持的指标"}
        date = arguments.get("date")
        if date is not None and (not isinstance(date, str) or not DATE_PATTERN.fullmatch(date)):
            return {"ok": False, "error": "日期必须是 YYYY-MM-DD"}
        try:
            snapshot = await self.snapshot(date=date, days=days)
        except (ValueError, _HealthError) as exc:
            return {"ok": False, "error": scrub(exc)}
        if not snapshot.get("available"):
            return {"ok": False, "error": scrub(snapshot.get("message") or "健康中心暂不可用"), "stale": snapshot.get("stale", False)}
        if query == "summary":
            result = {"query": query, "selected_date": snapshot.get("selected_date"), "latest_data_date": snapshot.get("latest_data_date"), "resources": snapshot.get("resources", []), "partial": snapshot.get("partial", False)}
        elif query == "trend":
            result = {"query": query, "metric": metric, "start_date": snapshot.get("start_date"), "end_date": snapshot.get("end_date"), "series": next((item for item in snapshot.get("series", []) if item.get("metric") == metric), None)}
        elif query == "workouts":
            result = {"query": query, "start_date": snapshot.get("workouts_start_date"), "end_date": snapshot.get("workouts_end_date"), "as_of": snapshot.get("fetched_at"), "workouts": snapshot.get("workouts", [])[:10], "status": snapshot.get("workouts_status")}
        else:
            result = {"query": query, "start_date": snapshot.get("start_date"), "end_date": snapshot.get("end_date"), "coverage": snapshot.get("coverage", {})}
        result["read_only"] = True
        result["message"] = snapshot.get("message", "健康数据已读取")
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 48 * 1024:
            return {"ok": False, "error": "健康查询结果超过语音工具大小限制，请缩小查询范围"}
        return {"ok": True, **result}

    async def close(self) -> None:
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
        if self._session is not None:
            await self._session.close()
        self._session = None

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.key_file:
            try:
                path = Path(self.key_file)
                if path.stat().st_size > 4096:
                    raise ValueError("oversize")
                key = path.read_text(encoding="utf-8").strip()
                if not key or any(ord(char) < 33 or ord(char) > 126 for char in key):
                    raise ValueError("invalid key")
            except (OSError, ValueError):
                raise _HealthError(
                    "健康服务 API Key 文件不可用，请检查 NAS 后台配置"
                ) from None
            headers["X-API-Key"] = key
        return headers

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        allowed = {"/api/summary", "/api/coverage", "/api/workouts"}
        allowed.update(f"/api/series/{metric}" for metric in METRICS)
        if path not in allowed:
            raise ValueError("此健康接口不在只读白名单中")
        if not self.base_url:
            raise _HealthError("尚未配置健康中心连接")
        headers = self._headers()
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=8, connect=3),
                cookie_jar=aiohttp.DummyCookieJar(),
            )
        try:
            async with (
                self._semaphore,
                self._session.get(
                    self.base_url + path,
                    params=params,
                    headers=headers,
                    allow_redirects=False,
                ) as response,
            ):
                if response.status in {401, 403}:
                    raise _HealthError("健康服务需要有效的 API Key，请在 NAS 后台配置")
                if 300 <= response.status < 400:
                    raise _HealthError("健康服务返回重定向，已停止读取以保护访问凭据")
                if response.status != 200:
                    raise _HealthError(
                        "健康服务暂未返回有效数据，请在健康中心检查服务状态"
                    )
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > 2 * 1024 * 1024:
                        raise _HealthError("健康服务响应超过读取限制")
                try:
                    payload = json.loads(body)
                except (ValueError, UnicodeError):
                    raise _HealthError("健康服务返回的数据格式无法识别") from None
                if not isinstance(payload, dict):
                    raise _HealthError("健康服务返回的数据结构无法识别")
                return payload
        except TimeoutError:
            raise _HealthError("健康服务连接超时，请稍后刷新") from None
        except aiohttp.ClientError:
            raise _HealthError("健康服务暂时无法连接，请检查 NAS 服务状态") from None

    def _empty(self, selected: str | None, days: int) -> dict[str, Any]:
        return {
            "service": {
                "name": "健康中心",
                "web_url": self.web_url,
                "source": SOURCE,
                "read_only": True,
            },
            "available": False,
            "stale": False,
            "message": "尚未配置健康中心连接",
            "fetched_at": None,
            "checked_at": _now().isoformat(),
            "selected_date": selected,
            "latest_data_date": None,
            "days": days,
            "resources": [],
            "series": [],
            "workouts": [],
        }

    async def snapshot(
        self, date: str | None = None, days: int = 7, refresh: bool = False
    ) -> dict[str, Any]:
        if type(days) is not int or not 1 <= days <= 31:
            raise ValueError("健康趋势范围必须是 1 到 31 天")
        selected = _date(date) if date is not None else None
        today = _now().astimezone(CN_TZ).date()
        if selected is not None and not Date(2000, 1, 1) <= selected <= today:
            raise ValueError("健康查询日期必须在 2000 年至今天之间")
        if not self.base_url:
            return self._empty(date, days)
        key = (date or "latest", days)
        cached = self._cache.get(key)
        if not refresh and cached:
            ttl = (
                self.poll_seconds
                if cached[1]["available"]
                else min(self.poll_seconds, 30)
            )
            if time.monotonic() - cached[0] < ttl:
                return {**copy.deepcopy(cached[1]), "cached": True}
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._fetch_snapshot(key, date, days))
            self._inflight[key] = task
            task.add_done_callback(
                lambda done: (
                    self._inflight.pop(key, None)
                    if self._inflight.get(key) is done
                    else None
                )
            )
        try:
            return copy.deepcopy(await asyncio.shield(task))
        finally:
            if task.done() and self._inflight.get(key) is task:
                self._inflight.pop(key, None)

    async def _fetch_snapshot(
        self, key: tuple[str, int], date: str | None, days: int
    ) -> dict[str, Any]:
        try:
            result = await asyncio.wait_for(self._load(date, days), timeout=35)
        except Exception as exc:  # noqa: BLE001 - do not reveal clinical data or credentials
            cached = self._cache.get(key)
            result = copy.deepcopy(cached[1]) if cached else self._empty(date, days)
            result.update(
                available=False,
                stale=bool(cached and cached[1].get("fetched_at")),
                checked_at=_now().isoformat(),
                cached=False,
                message=str(exc)
                if isinstance(exc, _HealthError)
                else "健康服务读取未完成，请稍后刷新",
            )
            if result["stale"]:
                result["message"] += "；保留上次读取结果并标记为过期"
                for resource in result.get("resources", []):
                    resource["stale"] = True
                    for field in resource.get("fields", []):
                        if field.get("status") == "ok":
                            field.update(
                                status="stale",
                                message="健康连接未更新，这是上次读取结果",
                            )
                for series in result.get("series", []):
                    series["stale"] = True
        self._cache[key] = (time.monotonic(), result)
        if len(self._cache) > 12:
            oldest = min(self._cache, key=lambda item: self._cache[item][0])
            self._cache.pop(oldest, None)
        return result

    @staticmethod
    def _coverage(
        payload: dict[str, Any], start: Date, end: Date
    ) -> list[dict[str, Any]]:
        rows = payload.get("items")
        if not isinstance(rows, list) or len(rows) > 5000:
            raise _HealthError("健康覆盖度数据结构无法识别")
        items = []
        for row in rows:
            if not isinstance(row, dict) or row.get("key_name") not in COVERAGE_SOURCES:
                continue
            try:
                day = _date(row.get("date"))
            except ValueError:
                continue
            count = _number(row.get("sample_count"))
            if not start <= day <= end or count is None:
                continue
            source = row["key_name"]
            items.append(
                {
                    "date": day.isoformat(),
                    "source": source,
                    "label": COVERAGE_SOURCES[source],
                    "sample_count": count,
                }
            )
        return items

    @staticmethod
    def _series(metric: str, payload: Any, start: Date, end: Date) -> dict[str, Any]:
        label, unit = METRICS[metric]
        output = {
            "metric": metric,
            "label": label,
            "unit": unit,
            "status": "unavailable",
            "message": "此时间范围暂无数据",
            "points": [],
        }
        if isinstance(payload, Exception):
            output["message"] = "此指标暂时无法读取"
            return output
        if (
            not isinstance(payload, dict)
            or payload.get("metric") != metric
            or not isinstance(payload.get("points"), list)
        ):
            output["message"] = "此指标返回格式无法识别"
            return output
        if len(payload["points"]) > 366:
            output["message"] = "此指标返回的数据量异常"
            return output
        points: dict[str, int | float] = {}
        for point in payload["points"]:
            if not isinstance(point, dict):
                continue
            try:
                day = _date(point.get("x"))
            except ValueError:
                continue
            value = _number(point.get("y"))
            if value is None or not start <= day <= end:
                continue
            if metric == "sleep_efficiency":
                if value > 1:
                    continue
                value = round(value * 100, 2)
            points[day.isoformat()] = value
        output["points"] = [
            {"date": day, "value": points[day]} for day in sorted(points)
        ]
        if points:
            output.update(status="ok", message="已读取")
        return output

    @staticmethod
    def _resources(
        summary: dict[str, Any],
        series: list[dict[str, Any]],
        selected: str,
        fetched: str,
    ) -> list[dict[str, Any]]:
        sections = {
            key: dict(value) if isinstance(value, dict) else {}
            for key, value in summary.items()
        }
        activity, sleep = sections.get("activity", {}), sections.get("sleep", {})
        heart, training = sections.get("resting_hr", {}), sections.get("workout", {})
        # A mismatched row date must not be presented as the selected day's measurement.
        for section in (activity, sleep, heart):
            if section.get("date", selected) != selected:
                section.clear()
        values: dict[str, dict[str, Any]] = {key: {} for key in RESOURCE_FIELDS}
        for group, section in (("activity", activity), ("sleep", sleep)):
            for key in RESOURCE_FIELDS[group]:
                value = _number(section.get(key))
                if value is not None:
                    values[group][key] = value
        for target, original in (("standing_count", "standing_cnt"),):
            if (value := _number(activity.get(original))) is not None:
                values["activity"][target] = value
        if (value := _number(sleep.get("awake_cnt"))) is not None:
            values["sleep"]["awake_count"] = value
        for key in ("has_stage", "has_rem"):
            if sleep.get(key) in (0, 1) and type(sleep.get(key)) in (int, bool):
                values["sleep"][key] = bool(sleep[key])
        for target, original in (("start_time", "start_ts"), ("end_time", "end_ts")):
            if timestamp := _timestamp(sleep.get(original)):
                values["sleep"][target] = timestamp
        if (value := _number(sleep.get("efficiency"))) is not None and value <= 1:
            values["sleep"]["efficiency_percent"] = round(value * 100, 2)
        if (value := _number(heart.get("bpm"))) is not None:
            values["heart"]["resting_hr"] = value
        if (value := _number(training.get("count"))) is not None:
            values["training"]["count"] = value
        if (value := _number(training.get("duration_s"))) is not None:
            values["training"]["duration_min"] = round(value / 60, 2)
        mapping = {
            "steps": ("activity", "steps"),
            "calories": ("activity", "calories"),
            "active_min": ("activity", "active_min"),
            "vigorous_min": ("activity", "vigorous_min"),
            "resting_hr": ("heart", "resting_hr"),
            "hr_avg": ("heart", "average_hr"),
            "spo2_min": ("heart", "min_spo2"),
            "stress_avg": ("heart", "stress_average"),
            "sleep_total": ("sleep", "total_min"),
            "sleep_deep": ("sleep", "deep_min"),
            "sleep_rem": ("sleep", "rem_min"),
            "nap": ("sleep", "nap_min"),
            "sleep_efficiency": ("sleep", "efficiency_percent"),
        }
        for item in series:
            group, key = mapping[item["metric"]]
            point = next(
                (point for point in item["points"] if point["date"] == selected), None
            )
            if point is not None:
                values[group].setdefault(key, point["value"])
        # A cloud placeholder of zero is not proof of a measured sleep stage.
        if sleep.get("has_stage") == 0:
            for key in ("deep_min", "light_min", "rem_min"):
                values["sleep"].pop(key, None)
        elif sleep.get("has_rem") == 0:
            values["sleep"].pop("rem_min", None)
        resources = []
        for group, definitions in RESOURCE_FIELDS.items():
            state = values[group]
            declarations = []
            for key, (label, unit) in definitions.items():
                field_type = (
                    "boolean"
                    if key.startswith("has_")
                    else "string"
                    if key.endswith("_time")
                    else "number"
                )
                field = {
                    "key": key,
                    "label": label,
                    "unit": unit,
                    "type": field_type,
                    "source": SOURCE,
                    "readable": True,
                    "writable": False,
                    "role": "measurement"
                    if field_type == "number"
                    else "state"
                    if field_type == "boolean"
                    else "metadata",
                    "chartable": field_type == "number",
                    "comparable": field_type == "number",
                }
                if key not in state:
                    field.update(
                        status="unavailable",
                        message="健康中心在所选日期没有此项有效读数",
                    )
                declarations.append(field)
            resource = {
                "resource_id": f"health.{group}",
                "kind": "health",
                "name": RESOURCE_NAMES[group],
                "date": selected,
                "source": SOURCE,
                "available": bool(state),
                "state": state,
                "fields": build_field_catalog(
                    state, declarations, source=SOURCE, attempted_at=fetched
                ),
            }
            section = {
                "activity": activity,
                "sleep": sleep,
                "heart": heart,
                "training": training,
            }[group]
            if timestamp := _timestamp(section.get("updated_at")):
                resource["synced_at"] = timestamp
            resources.append(resource)
        return resources

    @staticmethod
    def workout_window(
        selected: Date, today: Date, days: int, explicit: bool
    ) -> tuple[Date, Date]:
        """运动记录的查询窗口（含首尾）。

        每日覆盖度由健康中心同步后生成，未必包含今天。若直接把它当窗口终点，
        今天刚结束的运动就落不进查询范围，表现为「助手看不到刚刚的运动」。
        因此没有显式指定日期时，终点一律取到今天。
        """
        start = selected - timedelta(days=days - 1)
        end = selected if explicit else max(selected, today)
        return start, end

    @staticmethod
    def _workouts(payload: Any, start: Date, end: Date) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            return []
        workouts = []
        for row in payload["items"][:10]:
            if not isinstance(row, dict) or not (
                timestamp := _timestamp(row.get("start_ts"))
            ):
                continue
            day = datetime.fromisoformat(timestamp).date()
            if not start <= day <= end:
                continue
            item: dict[str, Any] = {"date": day.isoformat(), "start_time": timestamp}
            for target, original in (
                ("type", "type"),
                ("label", "type_cn"),
                ("pace", "avg_pace_text"),
            ):
                if isinstance(row.get(original), str):
                    item[target] = scrub(row[original])[:80]
            for key in ("calories", "avg_hr", "train_load"):
                if (value := _number(row.get(key))) is not None:
                    item[key] = value
            for target, original, divisor in (
                ("duration_min", "duration_s", 60),
                ("distance_km", "distance_m", 1000),
            ):
                if (value := _number(row.get(original))) is not None:
                    item[target] = round(value / divisor, 2)
            workouts.append(item)
        return workouts

    async def _load(self, date: str | None, days: int) -> dict[str, Any]:
        today = _now().astimezone(CN_TZ).date()
        search_start = today - timedelta(days=365)
        coverage = self._coverage(
            await self._get(
                "/api/coverage",
                {"start": search_start.isoformat(), "end": today.isoformat()},
            ),
            search_start,
            today,
        )
        latest = max(
            (item["date"] for item in coverage if item["sample_count"] > 0),
            default=None,
        )
        selected = _date(date or latest or today.isoformat())
        start = selected - timedelta(days=days - 1)
        params = {"start": start.isoformat(), "end": selected.isoformat()}
        workout_start, workout_end = self.workout_window(selected, today, days, bool(date))
        workout_params = {
            "start": workout_start.isoformat(),
            "end": workout_end.isoformat(),
        }
        calls = [
            self._get("/api/summary", {"date": selected.isoformat()}),
            self._get("/api/workouts", {**workout_params, "limit": 10, "offset": 0}),
            *(self._get(f"/api/series/{metric}", params) for metric in METRICS),
        ]
        if start < search_start:
            calls.append(self._get("/api/coverage", params))
        responses = await asyncio.gather(*calls, return_exceptions=True)
        summary, workout_payload = responses[:2]
        if isinstance(summary, dict) and summary.get("date") != selected.isoformat():
            summary = _HealthError("健康摘要日期与请求不一致")
        metric_responses = responses[2 : 2 + len(METRICS)]
        if all(isinstance(item, Exception) for item in [summary, *metric_responses]):
            raise _HealthError("健康数据接口暂时不可用，请在健康中心检查服务状态")
        series = [
            self._series(metric, payload, start, selected)
            for metric, payload in zip(METRICS, metric_responses, strict=True)
        ]
        fetched = _now().isoformat()
        resources = self._resources(
            summary if isinstance(summary, dict) else {},
            series,
            selected.isoformat(),
            fetched,
        )
        if start < search_start and isinstance(responses[-1], dict):
            coverage = self._coverage(responses[-1], start, selected)
        selected_coverage = [
            item
            for item in coverage
            if start.isoformat() <= item["date"] <= selected.isoformat()
        ]
        active_coverage = [
            item for item in selected_coverage if item["sample_count"] > 0
        ]
        partial = any(isinstance(item, Exception) for item in responses)
        has_data = any(resource["available"] for resource in resources)
        message = "健康数据已读取；原始记录由独立健康中心管理"
        if not has_data:
            message = (
                "所选日期暂无可用健康摘要；请查看趋势或打开健康中心检查手机上传情况"
            )
        if latest is None:
            message = "近 365 天没有已确认的覆盖度记录；不会把空白数据当作今天的测量"
        if partial:
            message += "；部分接口暂时不可读"
        return {
            **self._empty(selected.isoformat(), days),
            "available": True,
            "stale": False,
            "partial": partial,
            "message": message,
            "fetched_at": fetched,
            "checked_at": fetched,
            "latest_data_date": latest,
            "data_age_days": (today - _date(latest)).days if latest else None,
            "start_date": start.isoformat(),
            "end_date": selected.isoformat(),
            "resources": resources,
            "series": series,
            "workouts": self._workouts(workout_payload, workout_start, workout_end),
            "workouts_start_date": workout_start.isoformat(),
            "workouts_end_date": workout_end.isoformat(),
            "workouts_status": "unavailable"
            if isinstance(workout_payload, Exception)
            else "ok",
            "coverage": {
                "start_date": start.isoformat(),
                "end_date": selected.isoformat(),
                "searched_start": search_start.isoformat(),
                "searched_end": today.isoformat(),
                "latest_data_date": latest,
                "data_days": len({item["date"] for item in active_coverage}),
                "source_count": len({item["source"] for item in active_coverage}),
                "items": selected_coverage,
            },
        }

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import secrets
import time
from dataclasses import replace
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from typing import Any

from .ai import AIClient
from .automation import AUTOMATION_TOOL_NAME, AUTOMATION_TZ, AutomationStore
from .audio_scheduler import AudioScheduler
from .config import Settings
from .conversations import ConversationStore, conversation_id
from .field_catalog import build_field_catalog
from .health_source import HEALTH_TOOL_NAME, HealthSource
from .home_layout import HomeLayout
from .hub_data import HUB_TOOL_NAME, HubDataStore
from .infrared import SETTING_SCHEMA, InfraredAdapter
from .logging_setup import scrub
from .miot_tools import MAX_TOOL_CALLS, TOOL_NAME, MiotToolManager
from .model_registry import ModelRegistry
from .model_usage import ModelUsageStore
from .router_source import RouterSource
from .routing import (
    Action,
    RouteResult,
    home_target_clarification,
    route_question,
    sanitize_tts,
)
from .speaker_control import SpeakerController
from .state import CursorState, RuntimeStatus, StateStore, utc_now
from .xiaomi import ConversationRecord, XiaomiClient
from .weather_source import LOCATION_TOOL_NAME, WEATHER_TOOL_NAME, WeatherSource
from .web_search import WEB_SEARCH_TOOL_NAME, WebSearchSource

LOGGER = logging.getLogger(__name__)

# Console replies are text rather than speech, so they are not capped at a
# read-aloud length; this only bounds what we keep in the chat history.
WEB_ANSWER_MAX_CHARS = 8000


def home_resource_id(category: str, key: str) -> str:
    return f"home.{category}.{key}"


class ReconnectRequested(RuntimeError):
    pass


class Bridge:
    def __init__(self, settings: Settings, status: RuntimeStatus):
        self.settings = settings
        self.model_usage = ModelUsageStore(settings.state_dir)
        self.models = ModelRegistry(settings.state_dir, settings, usage=self.model_usage)
        self._refresh_gateway_model()
        settings = self.settings
        self.status = status
        self.store = StateStore(settings.state_dir)
        self.cursor = self.store.load()
        self.xiaomi = XiaomiClient(settings)
        self.ai = AIClient(settings, provider_id=getattr(self, "_gateway_provider_id", ""))
        self._retired_ai_tasks: set[asyncio.Task] = set()
        self.home = MiotToolManager(settings)
        self.layout = HomeLayout(self.home)
        self.speakers = SpeakerController(
            self.xiaomi,
            device_is_current=lambda device: (
                self.home.devices.get(device.key) is device
            ),
        )
        self.infrared = InfraredAdapter(self.home)
        # 音箱动作不走 MIoT 目录，AI 工具执行时由这里转给 SpeakerController
        self.home.extension_handler = self._speaker_extension_call
        self.automations = AutomationStore(settings.state_dir)
        self.extension_snapshots: dict[str, dict[str, Any]] = {}
        self.hub = HubDataStore(settings.state_dir)
        # Health owns clinical history; this adapter keeps only a short-lived
        # read cache and never inserts health data into generic hub/AI state.
        self.health_source = HealthSource(settings)
        self.weather = WeatherSource(settings)
        self.websearch = WebSearchSource(settings)
        self.router = RouterSource(settings)
        self.router_snapshots: dict[str, dict[str, Any]] = {}
        self.audio = AudioScheduler(
            self.xiaomi,
            status,
            suppress_native=settings.mute_xiaoai,
        )
        self.history: list[dict[str, str]] = []
        self.reconnect_event = asyncio.Event()
        self.active_turn_task: asyncio.Task[None] | None = None
        self.continuous_deadline = 0.0
        self.conversations = ConversationStore()
        self.home_sampling_lock = asyncio.Lock()
        self.home_sample_started: float | None = None
        self.home_last_sample_at: str | None = None
        self.home_sample_error = ""
        self.home_sampling_status: dict[str, Any] = {
            "last_attempt_at": None,
            "last_finished_at": None,
            "next_due_at": None,
            "duration_seconds": None,
            "consecutive_failures": 0,
        }
        self.sampling_sources: dict[str, dict[str, Any]] = {
            name: {
                "last_attempt_at": None,
                "last_success_at": None,
                "last_error": "",
                "consecutive_failures": 0,
            }
            for name in ("cloud", "router")
        }
        self.router_sampling_lock = asyncio.Lock()
        self.control_requests: OrderedDict[
            str, tuple[str, asyncio.Task[dict[str, Any]]]
        ] = OrderedDict()
        self.control_pending: set[str] = set()

    def _refresh_gateway_model(self) -> None:
        """Keep both the provider route and the concrete model's behavior."""

        self._gateway_provider_id = ""
        if not self.settings.ai_gateway_enabled:
            return
        provider, model = self.models.role_target("speaker")
        if not model:
            provider, model = self.models.active()
            if provider is None or not model:
                self.settings = replace(self.settings, ai_model="")
                return
        self.settings = replace(
            self.settings,
            ai_base_url=f"http://127.0.0.1:{self.settings.health_port}/v1",
            ai_api_key=self.models.gateway_token(),
            ai_model=model,
        )
        self._gateway_provider_id = provider["id"]

    async def _restart_ai(self) -> None:
        old = self.ai
        self.ai = AIClient(self.settings, provider_id=self._gateway_provider_id)
        draining = asyncio.create_task(old.close())
        self._retired_ai_tasks.add(draining)

        def finished(task):
            self._retired_ai_tasks.discard(task)
            if not task.cancelled() and task.exception() is not None:
                LOGGER.warning("Unable to close retired AI session: %s", scrub(task.exception()))

        draining.add_done_callback(finished)

    async def _sync_ai_route(self) -> None:
        previous = (self.settings.ai_base_url, self.settings.ai_model, self._gateway_provider_id)
        self._refresh_gateway_model()
        current = (self.settings.ai_base_url, self.settings.ai_model, self._gateway_provider_id)
        if current != previous:
            await self._restart_ai()
        self.status.ai_configured = self.settings.ai_configured

    def request_reconnect(self) -> None:
        self.reconnect_event.set()

    def public_config(self) -> dict[str, object]:
        config = self.xiaomi.public_config()
        behavior = self.behavior_config()
        config.update(
            {
                "ai_base_url": self.settings.ai_base_url,
                "ai_model": self.settings.ai_model,
                "ai_api_key_set": bool(self.settings.ai_api_key),
                **behavior,
            }
        )
        config["panel_name"] = "AI 中枢"
        config["ai_gateway_enabled"] = self.settings.ai_gateway_enabled
        config["ai_gateway_auth_required"] = self.settings.ai_gateway_auth_required
        config["active_model"] = self.models.public().get("active", {})
        return config

    def public_models(self) -> dict[str, Any]:
        return self.models.public()

    async def save_model(self, body: dict[str, Any], provider_id: str | None = None) -> dict[str, Any]:
        result = await self.models.upsert(body, provider_id)
        await self._sync_ai_route()
        return result

    async def remove_model(self, provider_id: str, **options: Any) -> dict[str, Any]:
        result = await self.models.remove(provider_id, **options)
        await self._sync_ai_route()
        return result

    async def discover_models(self, provider_id: str) -> dict[str, Any]:
        result = await self.models.discover(provider_id)
        await self._sync_ai_route()
        return result

    async def test_model(self, provider_id: str, model: str) -> dict[str, Any]:
        return await self.models.test(provider_id, model)

    async def test_models(self, targets: list[Any]) -> dict[str, Any]:
        return await self.models.test_many(targets)

    async def add_models(self, provider_id: str, models: list[Any], revision: Any = None) -> dict[str, Any]:
        result = await self.models.add_models(provider_id, models, revision)
        await self._sync_ai_route()
        return result

    async def remove_models(self, provider_id: str, models: list[Any], revision: Any = None) -> dict[str, Any]:
        result = await self.models.remove_models(provider_id, models, revision)
        await self._sync_ai_route()
        return result

    def public_model_probes(self) -> dict[str, Any]:
        return {"probes": self.model_usage.probes()}

    def model_usage_summary(self, hours: int = 24) -> dict[str, Any]:
        return self.model_usage.summary(hours)

    def model_usage_logs(self, **options: Any) -> dict[str, Any]:
        return self.model_usage.logs(**options)

    def clear_model_logs(self) -> dict[str, Any]:
        return {"removed": self.model_usage.clear_logs()}

    async def update_model_settings(self, provider_id: str, model: str, values: dict[str, Any]) -> dict[str, Any]:
        return await self.models.update_model_settings(provider_id, model, values)

    async def activate_model(self, provider_id: str, model: str, revision: Any = None) -> dict[str, Any]:
        result = await self.models.activate(provider_id, model, revision=revision)
        if self.models.active()[0] is None:
            raise ValueError("模型提供方不可用")
        await self._sync_ai_route()
        return result

    async def set_model_role(self, role: str, provider_id: str, model: str, revision: Any = None) -> dict[str, Any]:
        """Give one assistant its own model, or clear it back to the default."""

        result = await self.models.set_role(role, provider_id, model, revision=revision)
        if role == "speaker":
            await self._sync_ai_route()
        return result

    def public_home(self) -> dict[str, Any]:
        public = self.home.public_state()
        assignments = self.layout.public().get("assignments", {})
        resources = {
            item["resource_id"]: item for item in self.hub.public_state()["resources"]
        }
        for device in public["devices"]:
            record = self.home.devices.get(device["key"])
            if record is not None:
                device["properties"] = self._home_fields(record)
            device.update(assignments.get(device["key"], {}))
            if device["key"] in self.extension_snapshots:
                device["extensions"] = self.extension_snapshots[device["key"]]
            resource_id = home_resource_id(device["category"], device["key"])
            resource = resources.get(resource_id) or {}
            device.update(
                resource_id=resource_id,
                state=resource.get("state", {}),
                observed_at=resource.get("observed_at"),
                stale=resource.get("stale", True),
                online=resource.get("online", False),
                busy=device["key"] in self.control_pending,
            )
            if device.get("cloud_online") is False:
                device.update(online=False, stale=True)
                if record is not None:
                    device["state"] = self._offline_inventory_state(
                        record, device["state"]
                    )
            if record is not None and self._is_local_router(record):
                local = self._router_projection(record, resource)
                device["local_source"] = local
                device["state"] = {
                    **{
                        name: value
                        for name, value in device["state"].items()
                        if not name.startswith("local.")
                    },
                    **local.get("state", {}),
                }
                if local.get("available") and not local.get("stale"):
                    device.update(online=True, stale=False)
        # View entries are kept outside MiotToolManager's physical registry:
        # discovery, polling and authorization continue to target the gateway.
        public["infrared_devices"] = self.infrared.device_entries()
        for device in public["infrared_devices"]:
            device.update(assignments.get(device["infrared_ac"]["parent_key"], {}))
        return public

    def public_automations(self) -> dict[str, Any]:
        return {"ok": True, "tasks": self.automations.list()}

    def automation_action(self, task_id: str, action: str) -> dict[str, Any]:
        if not isinstance(task_id, str) or not task_id.startswith("auto_"):
            raise ValueError("自动化任务标识无效")
        if action == "pause":
            task = self.automations.pause(task_id)
        elif action == "resume":
            task = self.automations.resume(task_id)
        elif action == "cancel":
            task = self.automations.cancel(task_id)
        else:
            raise ValueError("自动化操作仅支持 pause、resume、cancel")
        if task is None:
            raise ValueError("自动化任务不存在")
        return {"ok": True, "task": task}

    def _automation_targets(self) -> list[dict[str, Any]]:
        assignments = self.layout.public().get("assignments", {})
        targets: list[dict[str, Any]] = []
        for device in self.home.devices.values():
            if not device.enabled:
                continue
            actions = [action for action in device.actions() if action != "get_state"]
            targets.append({
                "kind": "xiaomi", "device": device.alias, "device_key": device.key,
                "room": assignments.get(device.key, {}).get("room_name", ""),
                "actions": actions,
            })
            if self.infrared.supports(device):
                for profile in self.infrared.ac_profiles(device):
                    if profile.get("ai_control_enabled"):
                        targets.append({
                            "kind": "infrared_ac", "device": device.alias,
                            "device_key": device.key, "profile_key": profile["key"],
                            "profile_name": profile["name"],
                            "room": assignments.get(device.key, {}).get("room_name", ""),
                            "actions": ["set_settings"],
                        })
        return targets

    def automation_tool_definitions(self) -> list[dict[str, Any]]:
        targets = self._automation_targets()
        target_text = json.dumps(targets, ensure_ascii=False)
        return [{"type": "function", "function": {
            "name": AUTOMATION_TOOL_NAME,
            "description": (
                "创建、查看、暂停、恢复或取消家庭设备自动化任务。创建任务只保存计划，不会立即操作设备；"
                "到点由服务器按保存的动作执行，并重新检查设备授权。面板中已经授权的设备才可加入任务。"
                "使用 delay 表示从现在起延迟秒数，once 使用未来日期时间，daily 使用每天 HH:MM，weekly 使用 HH:MM 和 days（0=周一到6=周日）。"
                "当前北京时间：" + datetime.now(UTC).astimezone(AUTOMATION_TZ).strftime("%Y-%m-%d %H:%M") + "。可用目标：" + target_text
            ),
            "parameters": {"type": "object", "additionalProperties": False,
                "properties": {
                    "operation": {"type": "string", "enum": ["create", "list", "pause", "resume", "cancel"]},
                    "task_id": {"type": "string"},
                    "device": {"type": "string", "description": "目标设备别名"},
                    "profile_key": {"type": "string", "description": "红外空调档案需要"},
                    "action": {"type": "string", "description": "设备动作；红外空调使用 set_settings"},
                    "value": {"description": "普通设备动作的值；开关动作省略"},
                    "settings": {"type": "object", "additionalProperties": False, "properties": SETTING_SCHEMA, "description": "红外空调设置"},
                    "schedule": {"type": "object", "additionalProperties": False,
                        "properties": {
                            "type": {"type": "string", "enum": ["once", "delay", "daily", "weekly"]},
                            "at": {"type": "string"}, "delay_seconds": {"type": "integer", "minimum": 1},
                            "days": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 6}},
                        }, "required": ["type"]},
                }, "required": ["operation"]},
        }}]

    async def execute_automation_tool(self, arguments: object) -> dict[str, Any]:
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return {"ok": False, "message": "自动化参数不是有效 JSON"}
        if not isinstance(arguments, dict):
            return {"ok": False, "message": "自动化参数必须是对象"}
        operation = arguments.get("operation")
        try:
            if operation == "list":
                return self.public_automations()
            task_id = arguments.get("task_id")
            if operation in {"pause", "resume", "cancel"}:
                return self.automation_action(task_id, operation)
            if operation != "create":
                raise ValueError("不支持的自动化操作")
            if set(arguments) - {"operation", "device", "profile_key", "action", "value", "settings", "schedule"}:
                raise ValueError("自动化参数包含未允许字段")
            device_alias = str(arguments.get("device") or "").strip()
            candidates = [item for item in self._automation_targets() if item["device"] == device_alias]
            profile_key = arguments.get("profile_key")
            if any(item.get("kind") == "infrared_ac" for item in candidates) and not profile_key:
                raise ValueError("红外空调自动化必须指定 profile_key（空调档案）")
            if profile_key:
                candidates = [item for item in candidates if item.get("profile_key") == profile_key]
            if len(candidates) != 1:
                raise ValueError("自动化目标不存在或不唯一，请指定设备和红外空调档案")
            target = candidates[0]
            action = str(arguments.get("action") or "").strip()
            if action not in target["actions"]:
                raise ValueError("该设备不支持此自动化动作")
            value = arguments.get("settings") if target["kind"] == "infrared_ac" else arguments.get("value")
            if target["kind"] == "infrared_ac":
                if "value" in arguments:
                    raise ValueError("红外空调请使用 settings")
                value = self.infrared.validate_ac_settings(target["device_key"], target["profile_key"], value)
            else:
                if "settings" in arguments:
                    raise ValueError("普通设备请使用 value")
                value = self.home.validate_scheduled_action(target["device_key"], action, value)
            task = self.automations.create(
                target={key: target[key] for key in ("kind", "device_key", "device", "profile_key") if key in target},
                action=action, value=value, schedule=arguments.get("schedule"),
            )
            return {"ok": True, "task": task, "message": "自动化任务已保存，到点执行"}
        except (TypeError, ValueError, OSError) as exc:
            return {"ok": False, "message": str(exc)}

    async def _execute_automation(self, task: dict[str, Any]) -> None:
        task = self.automations.begin_execution(task["id"], task.get("attempt_id") or "")
        if task is None:
            return  # Cancelled, paused, recovered or already executed claim.
        target = task["target"]
        message = ""
        outcome = "failed"
        try:
            if target.get("kind") == "xiaomi":
                value = self.home.validate_scheduled_action(target["device_key"], task["action"], task.get("value"))
                result = await self.control_home_device(
                    target["device_key"],
                    {"action": task["action"], "value": value,
                     "request_id": "auto_" + task["attempt_id"], "confirmed": True},
                )
            elif target.get("kind") == "infrared_ac":
                settings = self.infrared.validate_ac_settings(target["device_key"], target["profile_key"], task.get("value"))
                result = await self.infrared.execute(
                    self.home.devices[target["device_key"]], "ir_ac_send",
                    {"profile_key": target["profile_key"], "settings": settings},
                    confirmed=True, ai_request=True,
                )
                try:
                    await self.home._audit_result(target["device"], "ir_ac_send", bool(result.get("ok")),
                                                  "" if result.get("ok") else str(result.get("message") or "结果未确认"), result)
                except Exception:  # noqa: BLE001 - audit failure must not change a send outcome
                    LOGGER.warning("Unable to persist automation infrared audit")
            else:
                raise ValueError("自动化目标类型无效")
            reported = result.get("outcome")
            outcome = reported if isinstance(reported, str) and reported in {"confirmed", "accepted", "unconfirmed", "failed"} else "failed"
            if not result.get("ok") and outcome in {"confirmed", "accepted"}:
                outcome = "failed"
            message = str(result.get("message") or "执行结果已记录")
        except Exception as exc:  # noqa: BLE001 - isolate one scheduled task
            message = str(exc)[:500]
        self.automations.complete(task["id"], attempt_id=task["attempt_id"], outcome=outcome, message=scrub(message))

    async def _automation_loop(self, stop_event: asyncio.Event) -> None:
        recovered = False
        while not stop_event.is_set():
            try:
                if not recovered:
                    self.automations.recover_interrupted()
                    recovered = True
                for task in self.automations.claim_due():
                    await self._execute_automation(task)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - scheduler must keep polling
                LOGGER.warning("Automation scheduler unavailable: %s", scrub(exc))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1.0)
            except TimeoutError:
                pass

    async def home_layout(self, *, refresh: bool = False) -> dict[str, Any]:
        return await self.layout.describe(refresh)

    def _home_fields(
        self, device: Any, *, include_local: bool = True
    ) -> list[dict[str, Any]]:
        diagnostics = getattr(self.home, "read_diagnostics", {}).get(device.key, {})
        policies = self.home._field_policies(device)
        fields = device.public_dict()["properties"]
        inputs = {
            p["property"]
            for action in device.catalog_actions.values()
            for p in action.get("inputs", [])
            if p.get("property")
        }
        outputs = {
            p["property"]
            for action in device.catalog_actions.values()
            for p in action.get("outputs", [])
            if p.get("property")
        }
        event_arguments = {
            p["property"]
            for event in device.catalog_events
            for p in event.get("arguments", [])
            if p.get("property")
        }
        for field in fields:
            field["read_status"] = diagnostics.get(field["key"], {})
            policy = policies.get(field["key"], {})
            for name in (
                "role",
                "chartable",
                "comparable",
                "priority",
                "layer",
                "refresh_seconds",
                "refresh_policy",
                "cache_ttl_seconds",
            ):
                if name in policy:
                    field[name] = field["read_status"].get(name, policy[name])
            if not field.get("readable") and not field.get("sensitive"):
                key = field["key"]
                if key in inputs:
                    field.update(role="parameter", usage="action_input")
                    field["read_status"] = {
                        **field["read_status"],
                        "status": "not_readable",
                        "message": "动作输入参数，在执行相应操作时填写，不是可采样的读数",
                    }
                elif key in outputs or key in event_arguments:
                    field.update(
                        role="event",
                        usage="action_output" if key in outputs else "event_argument",
                    )
                    field["read_status"] = {
                        **field["read_status"],
                        "status": "not_readable",
                        "message": "动作或事件的返回字段，仅在相应功能触发时产生，不提供持续读取",
                    }
            if device.cloud_online is False and field.get("readable"):
                field["read_status"] = {
                    **field["read_status"],
                    "status": "offline",
                    "message": "米家显示设备离线",
                }
                metadata = self.home._safe_device_metadata.get(device.did, {})
                if field["key"] in {"model", "firmware-revision"} and metadata.get(
                    field["key"]
                ):
                    field["read_status"] = {
                        "status": "ok",
                        "source": "xiaomi.device_list",
                        "message": "设备离线；此值来自米家账号目录，不是设备实时回读",
                        "observed_at": metadata.get("observed_at"),
                        "attempted_at": metadata.get("observed_at"),
                    }
        if include_local and self._is_local_router(device):
            fields.extend(self._router_projection(device).get("fields", []))
        return fields

    def _is_local_router(self, device: Any) -> bool:
        return bool(
            self.settings.router_url
            and self.settings.router_device_key == device.key
            and device.model == "xiaomi.router.ra67"
        )

    def _router_projection(
        self, device: Any, resource: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not self._is_local_router(device) or not device.enabled:
            return {}
        snapshot = copy.deepcopy(self.router_snapshots.get(device.key) or {})
        if resource is None:
            try:
                resource = self.hub.get_resource(
                    home_resource_id(device.category, device.key)
                )
            except ValueError:
                resource = {}
        if not snapshot:
            fields = [
                copy.deepcopy(field)
                for field in resource.get("fields", [])
                if str(field.get("key", "")).startswith("local.")
            ]
            state = {
                key: value
                for key, value in resource.get("state", {}).items()
                if key.startswith("local.")
            }
            snapshot = {
                "kind": "router",
                "source": "xiaomi.router.local",
                "available": False,
                "stale": bool(state),
                "message": "等待路由器本地连接，资料来自上次读取"
                if state
                else "等待路由器本地连接",
                "state": state,
                "fields": fields,
            }
        else:
            saved_fields = {
                field["key"]: copy.deepcopy(field)
                for field in resource.get("fields", [])
                if field["key"].startswith("local.")
            }
            saved_state = {
                key: value
                for key, value in resource.get("state", {}).items()
                if key.startswith("local.")
            }
            for incoming in snapshot.get("fields", []):
                name = incoming.get("key", "")
                if not name.startswith("local."):
                    continue
                prior = saved_fields.get(name, {})
                field = {**prior, **incoming}
                if incoming.get("status") != "ok" and prior.get("observed_at"):
                    field["observed_at"] = prior["observed_at"]
                if incoming.get("status") in {
                    "unsupported",
                    "not_readable",
                    "sensitive",
                }:
                    saved_state.pop(name, None)
                    field.pop("value", None)
                saved_fields[name] = field
            snapshot["state"] = {**saved_state, **snapshot.get("state", {})}
            snapshot["fields"] = list(saved_fields.values())
            if not snapshot.get("available"):
                snapshot["stale"] = bool(snapshot["state"])
        fields = []
        now = datetime.now(UTC)
        for item in snapshot.get("fields", []):
            field = copy.deepcopy(item)
            if not str(field.get("key", "")).startswith("local."):
                continue
            expiry = field.get("cache_ttl_seconds") or max(
                90, self.settings.router_poll_seconds * 3
            )
            observed = field.get("observed_at")
            expired = True
            if observed:
                try:
                    at = datetime.fromisoformat(str(observed))
                    expired = (now - at.astimezone(UTC)).total_seconds() > expiry
                except (TypeError, ValueError, OverflowError):
                    pass
            if field.get("status") == "ok" and (expired or snapshot.get("stale")):
                field.update(status="stale", message="本地读数未更新，显示上次读取结果")
            field.pop("value", None)
            fields.append(field)
        snapshot["fields"] = fields
        snapshot["state"] = {
            key: value
            for key, value in snapshot.get("state", {}).items()
            if key.startswith("local.")
        }
        if fields and not any(
            field.get("status") == "ok" and field.get("priority") != "detail"
            for field in fields
        ):
            snapshot["stale"] = True
        return snapshot

    def _publish_home_snapshot(
        self,
        device: Any,
        state: dict[str, Any],
        fields: list[dict[str, Any]],
        observed_at: str,
        ttl_seconds: int,
        *,
        local: bool = False,
    ) -> dict[str, Any] | None:
        resource_id = home_resource_id(device.category, device.key)
        if (
            self.home.devices.get(device.key) is not device
            or not device.enabled
            or self.hub.resource_enabled(resource_id) is False
        ):
            return None
        if self._is_local_router(device):
            try:
                previous = self.hub.get_resource(resource_id)
            except ValueError:
                previous = {}
            if local:
                retained_state = {
                    key: value
                    for key, value in previous.get("state", {}).items()
                    if not key.startswith("local.")
                }
                retained_fields = [
                    copy.deepcopy(field)
                    for field in previous.get("fields", [])
                    if not field["key"].startswith("local.")
                ]
                known = {field["key"] for field in retained_fields}
                retained_fields.extend(
                    field
                    for field in self._home_fields(device, include_local=False)
                    if field["key"] not in known
                )
            else:
                retained = self._router_projection(device, previous)
                retained_state, retained_fields = (
                    retained.get("state", {}),
                    retained.get("fields", []),
                )
            for field in retained_fields:
                field["cached"] = True
            state = {**retained_state, **state}
            fields = [*retained_fields, *fields]
            if previous.get("observed_at"):
                observed_at = max(observed_at, previous["observed_at"])
            ttls = [
                field["cache_ttl_seconds"]
                for field in fields
                if field.get("status") == "ok"
                and isinstance(field.get("cache_ttl_seconds"), (int, float))
                and field["cache_ttl_seconds"] > 0
            ]
            if ttls:
                ttl_seconds = max(30, min(86400, int(min(ttls))))
        return self.hub.ingest(
            {
                "resource_id": resource_id,
                "kind": "sensor" if device.category == "air-purifier" else "service",
                "name": device.alias,
                "summary": f"{device.alias} 状态已更新",
                "state": state,
                "source": "xiaomi",
                "fields": fields,
                "location": self._home_location(device.key),
                "observed_at": observed_at,
                "ttl_seconds": ttl_seconds,
            }
        )

    async def refresh_router_device(
        self, key: str, *, refresh: bool = False
    ) -> dict[str, Any]:
        async with self.router_sampling_lock:
            self.sampling_sources["router"]["last_attempt_at"] = utc_now()
            try:
                projection, persisted = await self._refresh_router_device(
                    key, refresh=refresh
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                self._finish_sampling_source(
                    "router", error="路由器本地数据暂时无法刷新，显示上次数据"
                )
                raise
            self._finish_sampling_source(
                "router",
                persisted=persisted,
                error=""
                if projection.get("available") and not projection.get("stale")
                else "路由器本地数据暂时无法刷新，显示上次数据",
            )
            self.hub._publish({"type": "hub.snapshot", "hub": self.public_hub()})
            return projection

    async def _refresh_router_device(
        self, key: str, *, refresh: bool = False
    ) -> tuple[dict[str, Any], bool]:
        device = self.home.devices.get(key)
        if device is None or not self._is_local_router(device) or not device.enabled:
            raise ValueError("该路由器未配置本地只读接入或已停用")
        resource_id = home_resource_id(device.category, key)
        if self.hub.resource_enabled(resource_id) is False:
            raise ValueError("数据源已停用")
        try:
            snapshot = await self.router.snapshot(device, refresh=refresh)
        except Exception:  # noqa: BLE001 - authenticated URLs and payloads stay private
            snapshot = {
                **self.router_snapshots.get(key, {}),
                "kind": "router",
                "available": False,
                "stale": True,
                "message": "路由器本地接口暂不可用，保留上次数据",
            }
        if (
            self.home.devices.get(key) is not device
            or not device.enabled
            or self.hub.resource_enabled(resource_id) is False
        ):
            return {
                "kind": "router",
                "available": False,
                "message": "设备授权或目录已变化",
            }, False
        prior = self.router_snapshots.get(key, {})
        observed = snapshot.get("observed_at")
        try:
            previous = self.hub.get_resource(resource_id)
        except ValueError:
            previous = {}
        published_at = max(
            (
                field.get("observed_at") or ""
                for field in previous.get("fields", [])
                if field["key"].startswith("local.")
            ),
            default="",
        )
        newest_known = max(published_at, prior.get("observed_at") or "")
        if observed and newest_known and observed < newest_known:
            if snapshot.get("available"):
                return self._router_projection(device, previous), False
            snapshot = {
                **snapshot,
                "state": {},
                "fields": [],
                "observed_at": newest_known,
            }
        self.router_snapshots[key] = copy.deepcopy(snapshot)
        projection = self._router_projection(device, previous)
        persisted = False
        if (
            snapshot.get("available")
            and not snapshot.get("stale")
            and snapshot.get("state")
            and observed
            and observed > published_at
        ):
            resource = self._publish_home_snapshot(
                device,
                projection.get("state", {}),
                projection.get("fields", []),
                observed,
                int(
                    snapshot.get("ttl_seconds") or self.settings.router_poll_seconds * 3
                ),
                local=True,
            )
            if resource:
                self.home_last_sample_at = utc_now()
                persisted = True
        return projection, persisted

    def _offline_inventory_state(
        self, device: Any, state: dict[str, Any]
    ) -> dict[str, Any]:
        result = dict(state)
        metadata = self.home._safe_device_metadata.get(device.did, {})
        for key in ("model", "firmware-revision"):
            if key in device.properties and metadata.get(key):
                result[key] = metadata[key]
        return result

    def _home_location(self, key: str) -> str:
        assignment = self.layout.public().get("assignments", {}).get(key, {})
        return " / ".join(
            filter(None, [assignment.get("home_name"), assignment.get("room_name")])
        )[:80]

    @staticmethod
    def _extension_kind(device: Any) -> str:
        if device.model.startswith("xiaomi.wifispeaker."):
            return "speaker"
        if InfraredAdapter.supports(device):
            return "infrared"
        return ""

    async def home_extensions(
        self, key: str, *, refresh: bool = False
    ) -> dict[str, Any]:
        device = self.home.devices.get(key)
        if device is None:
            raise ValueError("设备不存在，请重新发现设备")
        kind = self._extension_kind(device)
        if not kind:
            return {"kind": "", "available": False, "message": "该设备使用通用能力面板"}
        controller = self.speakers if kind == "speaker" else self.infrared
        try:
            async with asyncio.timeout(22):
                snapshot = await controller.describe(device, refresh=refresh)
        except Exception:  # noqa: BLE001 - private upstream errors stay server-side
            snapshot = {
                "kind": kind,
                "available": False,
                "message": "专用接口暂时无法读取，请稍后刷新",
            }
        snapshot["observed_at"] = utc_now()
        if kind == "speaker":
            # Shortcuts use this device's verified MIoT catalogue, never guessed
            # MiNA methods. They retain the normal confirmation/validation path.
            shortcuts = []
            for action in device.catalog_actions.values():
                if (
                    action.get("label")
                    in {"上一首", "下一首", "唤醒", "播放音乐", "播放电台", "停止闹钟"}
                    and action.get("enabled")
                    and not action.get("inputs")
                ):
                    shortcuts.append(
                        {
                            "action": action["action"],
                            "label": action["label"],
                            "type": "button",
                            "available": device.enabled
                            and device.cloud_online is not False,
                            "requires_confirmation": True,
                        }
                    )
            labels = {item["label"] for item in shortcuts}
            snapshot["controls"] = [
                item
                for item in snapshot.get("controls", [])
                if item.get("label") not in labels
            ] + shortcuts
        self.extension_snapshots[key] = snapshot
        self.hub._publish(
            {"type": "home.extensions", "device_key": key, "extensions": snapshot}
        )
        state = snapshot.get("state") or {}
        if (
            kind == "speaker"
            and snapshot.get("available")
            and state
            and not state.get("stale")
        ):
            resource_id = f"speaker.{key}"
            observed = state.get("updated_at")
            if observed and self.hub.resource_enabled(resource_id) is not False:
                try:
                    previous = self.hub.get_resource(resource_id)
                except ValueError:
                    previous = {}
                if previous.get("observed_at") != observed:
                    try:
                        self.hub.ingest(
                            {
                                "resource_id": resource_id,
                                "kind": "service",
                                "name": f"{device.alias[:60]} · 播放状态",
                                "summary": "小爱音箱播放状态",
                                "source": "xiaomi.mina",
                                "state": state,
                                "location": self._home_location(key),
                                "observed_at": observed,
                                "ttl_seconds": min(
                                    86400, self.settings.home_state_poll_seconds * 3
                                ),
                            }
                        )
                    except (ValueError, TypeError, OSError):
                        LOGGER.warning("Unable to persist speaker telemetry")
        return snapshot

    async def _speaker_extension_call(
        self, device: Any, action: str, value: object
    ) -> dict[str, Any]:
        """AI 工具调用音箱专用动作的入口。

        用户明确说出「让小爱播报 / 调音量」这类请求就是授权，所以这里带 confirmed=True
        交给 _extension_operation；参数校验、超时与结果判定仍复用面板那条路径。
        """
        action = str(action)
        value = self.speakers.coerce(action, value)
        return await self._device_operation(device.key, action, value, True)

    async def _extension_operation(
        self, key: str, action: str, value: object, confirmed: bool
    ) -> dict[str, Any]:
        device = self.home.devices[key]
        kind = self._extension_kind(device)
        if (action.startswith("speaker_") and kind != "speaker") or (
            action.startswith("ir_") and kind != "infrared"
        ):
            raise ValueError("该设备不支持此专用控制")
        local_ir = action in {"ir_learn_save", "ir_learn_rename", "ir_learn_delete", "ir_ac_ai_permission", "ir_ac_create"}
        if not device.enabled or (device.cloud_online is False and not local_ir):
            raise ValueError("设备未启用或已离线")
        controller = self.speakers if kind == "speaker" else self.infrared
        try:
            async with asyncio.timeout(35):
                if kind == "speaker":
                    self.speakers.validate(action, value)
                    if action in self.speakers.CONFIRM_ACTIONS and not confirmed:
                        raise ValueError("请先确认将在这台音箱上播放声音")
                    selected = self.xiaomi.device
                    if selected is not None and selected.miot_did == device.did:
                        await self._cancel_active_turn(count=True)
                        async with self.audio.lock:
                            result = await controller.execute(
                                device, action, value, confirmed=confirmed
                            )
                    else:
                        result = await controller.execute(
                            device, action, value, confirmed=confirmed
                        )
                else:
                    result = await controller.execute(
                        device, action, value, confirmed=confirmed
                    )
        except (ValueError, TypeError) as exc:
            result = {"ok": False, "outcome": "failed", "message": str(exc)}
        except Exception:  # noqa: BLE001 - external write outcome can be uncertain
            result = {
                "ok": False,
                "outcome": "unconfirmed",
                "message": "指令结果未确认，请刷新状态；系统不会自动重发",
            }
        # Extensions carry their own readbacks; never replace all MIoT readings
        # with a partial media response or invent a target appliance's IR state.
        try:
            await self.home._audit_result(
                device.alias,
                action,
                bool(result.get("ok")),
                "" if result.get("ok") else result.get("message", "操作未完成"),
                result,
            )
        except OSError:
            LOGGER.warning("Unable to persist device operation audit")
        output = {
            **result,
            "device_key": key,
            "state": {},
            "observed_at": None,
        }
        if result.get("ok"):
            try:
                output["extensions"] = await self.home_extensions(key)
            except ValueError:
                output["refresh_message"] = "设备目录已变化，请刷新；指令不会重发"
        return output

    def public_hub(self) -> dict[str, Any]:
        public = self.hub.public_state()
        by_id = {item["resource_id"]: item for item in public["resources"]}
        for device in self.home.devices.values():
            resource_id = home_resource_id(device.category, device.key)
            if not device.enabled or self.hub.resource_enabled(resource_id) is False:
                continue
            resource = by_id.get(resource_id)
            if resource is None:
                resource = {
                    "resource_id": resource_id,
                    "kind": "service",
                    "name": device.alias,
                    "state": {},
                    "summary": "等待设备提供可读取数据",
                    "source": "xiaomi",
                    "observed_at": None,
                    "received_at": None,
                    "expires_at": None,
                    "age_seconds": 0,
                    "stale": True,
                    "online": False,
                    "catalog_only": True,
                }
                public["resources"].append(resource)
            resource["location"] = self._home_location(device.key)
            if device.cloud_online is False:
                resource.update(online=False, stale=True)
                resource["state"] = self._offline_inventory_state(
                    device, resource["state"]
                )
            if self._is_local_router(device):
                local = self._router_projection(device, resource)
                resource["local_source"] = local
                resource["state"] = {
                    **{
                        name: value
                        for name, value in resource["state"].items()
                        if not name.startswith("local.")
                    },
                    **local.get("state", {}),
                }
                if local.get("available") and not local.get("stale"):
                    resource.update(online=True, stale=False)
            resource["fields"] = build_field_catalog(
                resource["state"],
                self._home_fields(device),
                source="xiaomi",
                observed_at=resource.get("observed_at"),
                stale=resource["stale"],
                freshness=True,
            )
        public["resource_count"] = len(public["resources"])
        return {
            **public,
            "sampling": {
                **self.home_sampling_status,
                "interval_seconds": self.settings.home_state_poll_seconds,
                "last_sample_at": self.home_last_sample_at,
                "last_error": self.home_sample_error,
                "sampling": self.home_sampling_lock.locked(),
                "sources": copy.deepcopy(self.sampling_sources),
            },
        }

    def hub_history(
        self, resource_id: str, limit: int = 60, hours: int | None = None
    ) -> dict[str, Any]:
        return self.hub.history(resource_id, limit, hours)

    def disable_hub_resource(self, resource_id: str) -> bool:
        return self.hub.disable_resource(resource_id)

    def ingest_hub_event(self, payload: object, authorization: str) -> dict[str, Any]:
        if not self.hub.authenticate(authorization):
            raise PermissionError("数据接入令牌无效")
        return self.hub.ingest(payload)

    def accept_xiaomi_login(self, data: dict[str, object]) -> object:
        if str(data.get("sid") or "micoapi") == "xiaomiio":
            self.home.save_browser_credentials(data)
            return self.home.public_state()
        return self.update_connection_config(data)

    async def discover_home_devices(self) -> dict[str, Any]:
        await self.home.discover()
        return self.public_home()

    def update_home_device(
        self, key: str, *, alias: str, enabled: bool
    ) -> dict[str, Any]:
        updated = self.home.update_device(key, alias=alias, enabled=enabled)
        resource_id = home_resource_id(str(updated["category"]), key)
        if enabled:
            self.hub.enable_resource(resource_id)
        else:
            self.hub.disable_resource(resource_id)
        return updated

    async def test_home_action(
        self, device: str, action: str, value: object = None
    ) -> dict[str, Any]:
        result = await self.home.execute(
            "control_xiaomi_home",
            {"device": device, "action": action, "value": value},
        )
        record = next(
            (item for item in self.home.devices.values() if item.alias == device), None
        )
        if record:
            self._publish_control_readback(record.key, result)
        return result

    def _publish_control_readback(
        self, key: str, result: dict[str, Any]
    ) -> dict[str, Any] | None:
        device = self.home.devices.get(key)
        if device is None or not device.enabled or not result.get("readback"):
            return None
        resource_id = home_resource_id(device.category, key)
        if self.hub.resource_enabled(resource_id) is False:
            return None
        readback = dict(result["readback"])
        try:
            previous = self.hub.get_resource(resource_id)
        except ValueError:
            previous = {}
        previous_fields = {field["key"]: field for field in previous.get("fields", [])}
        diagnostics = self.home.read_diagnostics.setdefault(key, {})
        for name, value in previous.get("state", {}).items():
            prop = device.properties.get(name)
            if name in readback or prop is None or prop.sensitive or not prop.readable:
                continue
            readback[name] = value
            detail = diagnostics.get(name, {})
            diagnostics[name] = {
                **detail,
                "status": "stale",
                "message": "本次未取得此字段的回读，显示上次读数",
                "source": detail.get("source")
                or previous_fields.get(name, {}).get("source", "miot.cloud"),
                "observed_at": detail.get("observed_at")
                or previous_fields.get(name, {}).get("observed_at")
                or previous.get("observed_at"),
            }
        resource = self._publish_home_snapshot(
            device,
            readback,
            self._home_fields(device, include_local=False),
            result.get("observed_at") or utc_now(),
            self.home.device_ttl_seconds(device),
        )
        result["readback"] = resource["state"] if resource else readback
        return resource

    async def _device_operation(
        self, key: str, action: str, value: object = None, confirmed: bool = False
    ) -> dict[str, Any]:
        device = self.home.devices.get(key)
        if device is None:
            raise ValueError("设备不存在，请重新发现设备")
        if action.startswith(("speaker_", "ir_")):
            return await self._extension_operation(key, action, value, confirmed)
        scheduled_audio = False
        try:
            async with asyncio.timeout(35):

                async def execute() -> dict[str, Any]:
                    return await self.home.execute(
                        TOOL_NAME,
                        {"device": device.alias, "action": action, "value": value},
                        device_key=key,
                        confirmed=confirmed,
                    )

                operation = device.catalog_actions.get(action)
                selected = self.xiaomi.device
                if (
                    self._extension_kind(device) == "speaker"
                    and operation
                    and operation.get("enabled")
                    and confirmed
                    and device.enabled
                    and device.cloud_online is not False
                ):
                    # Validate arguments before touching the current AI audio.
                    arguments = {} if value is None else value
                    inputs = operation.get("inputs") or []
                    if isinstance(arguments, dict) and set(arguments) == {
                        p["key"] for p in inputs
                    }:
                        try:
                            for parameter in inputs:
                                self.home._validate_property_value(
                                    device.properties[parameter["property"]],
                                    arguments[parameter["key"]],
                                )
                            scheduled_audio = (
                                selected is not None and selected.miot_did == device.did
                            )
                        except (ValueError, TypeError, KeyError):
                            pass
                if scheduled_audio:
                    await self._cancel_active_turn(count=True)
                    async with self.audio.lock:
                        result = await execute()
                else:
                    result = await execute()
        except TimeoutError:
            result = {
                "ok": False,
                "outcome": "unconfirmed",
                "message": "操作超时，结果未确认，请刷新设备状态",
            }
        resource = None
        try:
            resource = self._publish_control_readback(key, result)
        except (ValueError, TypeError, OSError) as exc:
            LOGGER.warning("Unable to save device readback: %s", scrub(exc))
        output = {
            **result,
            "device_key": key,
            "state": result.get("readback") or {},
            "observed_at": result.get("observed_at"),
            "message": result.get("message") or result.get("error") or "操作未完成",
            "outcome": result.get("outcome")
            or ("confirmed" if result.get("ok") else "failed"),
        }
        if resource:
            output["resource"] = resource
        if scheduled_audio and result.get("ok"):
            output["extensions"] = await self.home_extensions(key, refresh=True)
        return output

    async def read_home_device(self, key: str) -> dict[str, Any]:
        device = self.home.devices.get(key)
        if device is None or not self._is_local_router(device):
            return await self._device_operation(key, "get_state")
        local = await self.refresh_router_device(key, refresh=True)
        result = await self._device_operation(key, "get_state")
        result["local_source"] = local
        if local.get("available") and not local.get("stale"):
            resource = self.hub.get_resource(home_resource_id(device.category, key))
            result.update(
                ok=True,
                outcome="confirmed",
                message="路由器本地状态已读取，云端未开放项单独标注",
                state=resource["state"],
                resource=resource,
                observed_at=resource["observed_at"],
            )
        return result

    async def control_home_device(self, key: str, payload: object) -> dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) - {
            "action",
            "value",
            "request_id",
            "confirmed",
        }:
            raise ValueError("设备控制参数无效")
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{8,128}", request_id
        ):
            raise ValueError("缺少有效操作标识，请刷新页面")
        action = payload.get("action")
        if not isinstance(action, str) or action == "get_state":
            raise ValueError("控制动作无效")
        value = payload.get("value")
        if value is not None and not isinstance(value, (str, int, float, dict)):
            raise ValueError("控制值必须为文字或数字")
        if isinstance(value, dict) and not action.startswith(
            ("action_", "ir_", "speaker_")
        ):
            raise ValueError("只有设备动作支持参数对象")
        confirmed = payload.get("confirmed", False)
        if not isinstance(confirmed, bool):
            raise TypeError("操作确认值无效")
        fingerprint = json.dumps(
            [key, action, value, confirmed], ensure_ascii=False, allow_nan=False
        )
        if len(fingerprint) > 8192:
            raise ValueError("控制参数过长")
        if request_id in self.control_requests:
            previous, task = self.control_requests[request_id]
            if previous != fingerprint:
                raise ValueError("操作标识已使用，请勿重复发送不同操作")
            return await asyncio.shield(task)
        if key in self.control_pending:
            return {
                "ok": False,
                "outcome": "failed",
                "device_key": key,
                "state": {},
                "message": "设备正在处理上一条指令，请稍候",
            }
        if len(self.control_requests) >= 128:
            for previous_id, (_, task) in list(self.control_requests.items()):
                if task.done():
                    del self.control_requests[previous_id]
                    break
            else:
                raise ValueError("请求繁忙，请稍后重试")

        async def operate() -> dict[str, Any]:
            try:
                return await self._device_operation(key, action, value, confirmed)
            finally:
                self.control_pending.discard(key)

        self.control_pending.add(key)
        task = asyncio.create_task(operate())
        self.control_requests[request_id] = (fingerprint, task)
        return await asyncio.shield(task)

    def update_connection_config(self, data: dict[str, object]) -> dict[str, object]:
        self.xiaomi.update_connection_config(data)
        self.audio.suppress_native = self.behavior_config()["mute_xiaoai"] is True
        connection_keys = {
            "user_id",
            "pass_token",
            "c_user_id",
            "service_token",
            "account_device_id",
            "mi_did",
            "mi_device_name",
            "mi_hardware",
        }
        if connection_keys.intersection(data):
            self.request_reconnect()
        return self.public_config()

    def behavior_config(self) -> dict[str, object]:
        overrides = self.xiaomi.runtime_store.load()
        keywords = (
            tuple(
                item.strip()
                for item in (
                    overrides.trigger_keywords
                    or ",".join(self.settings.trigger_keywords)
                ).split(",")
                if item.strip()
            )
            or self.settings.trigger_keywords
        )
        try:
            answer_max_chars = int(overrides.answer_max_chars)
        except (TypeError, ValueError):
            answer_max_chars = self.settings.answer_max_chars
        try:
            continuous_timeout = int(overrides.continuous_timeout_seconds)
        except (TypeError, ValueError):
            continuous_timeout = 30
        mute_raw = overrides.mute_xiaoai.strip().casefold()
        mute_xiaoai = (
            mute_raw in {"1", "true", "yes", "on"}
            if mute_raw
            else self.settings.mute_xiaoai
        )
        return {
            "trigger_keywords": list(keywords),
            "answer_max_chars": max(40, min(answer_max_chars, 500)),
            "continuous_timeout_seconds": max(5, min(continuous_timeout, 300)),
            "mute_xiaoai": mute_xiaoai,
        }

    async def ask_text(self, prompt: str, use_context: bool = True, conversation_id: str | None = None) -> str:
        value = prompt.strip()
        if not value or len(value) > 1000:
            raise ValueError("文字问题长度必须为 1 到 1000 个字符")
        key = self._conversation_key(conversation_id)
        async with self.conversations.turn(key) as chat:
            started = time.perf_counter()
            # A short turn starts from a clean slate: no earlier exchange is
            # sent, and whatever was remembered before is dropped afterwards.
            version = chat.version
            history = list(chat.history) if use_context else []
            answer = await self._answer_with_tools(value, history, surface="panel")
            # The console is text, so the reply must not be squeezed into a
            # read-aloud length; the cap only bounds the stored history.
            final = answer.strip()[:WEB_ANSWER_MAX_CHARS]
            if not final:
                raise RuntimeError("AI 没有返回可显示的最终答案")
            if use_context and chat.version == version:
                chat.history.extend(
                    [
                        {"role": "user", "content": value},
                        {"role": "assistant", "content": final},
                    ]
                )
                chat.history[:] = chat.history[-12:]
            elif not use_context and chat.version == version:
                chat.history.clear()
            self.status.last_ai_latency_ms = round(
                (time.perf_counter() - started) * 1000
            )
            return final

    @staticmethod
    def _conversation_key(value: str | None) -> str:
        return conversation_id(value)

    def clear_chat(self, conversation_id: str) -> None:
        self.conversations.clear(conversation_id)

    async def stop_active_turn(self) -> None:
        await self._cancel_active_turn(count=True)
        await self.xiaomi.suppress_native_response(force=True)
        self.status.audio_state = "idle"

    def clear_context(self) -> None:
        self.history.clear()
        self.status.continuous_mode = False
        self.continuous_deadline = 0.0

    async def test_ai(self) -> str:
        answer = await self.ai.ask("用一句简短中文回复：AI 服务连接正常。", [])
        return sanitize_tts(answer, 120)

    async def test_tts(self, text: str) -> None:
        value = text.strip()
        if not value or len(value) > 200:
            raise ValueError("测试文本长度必须为 1 到 200 个字符")
        await self.audio.speak_direct(value)

    async def run(self, stop_event: asyncio.Event) -> None:
        await self.ai.start()
        home_state_task = asyncio.create_task(self._home_state_loop(stop_event))
        router_state_task = asyncio.create_task(self._router_state_loop(stop_event))
        automation_task = asyncio.create_task(self._automation_loop(stop_event))
        try:
            while not stop_event.is_set():
                if not self.settings.mi_user_id or not self.settings.mi_pass_token:
                    # The panel and model gateway work without a speaker.
                    # Account setup wakes this loop through request_reconnect.
                    await self._wait_for_retry(stop_event)
                    continue
                try:
                    device = await self.xiaomi.connect()
                    self.status.xiaomi_connected = True
                    self.status.device_selected = True
                    self.status.device_hardware = device.hardware
                    self.status.last_error = None
                    self.reconnect_event.clear()
                    LOGGER.info(
                        "Connected to Xiaomi speaker hardware=%s", device.hardware
                    )
                    if not self.settings.ai_configured:
                        LOGGER.warning(
                            "AI is not configured; Xiaomi polling will continue without answering"
                        )
                    await self._poll_loop(stop_event)
                except ReconnectRequested:
                    self.status.xiaomi_connected = False
                    self.status.device_selected = False
                    self.status.last_error = None
                    LOGGER.info("Bridge reconnect requested")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - reconnect boundary
                    self.status.xiaomi_connected = False
                    self.status.device_selected = False
                    self.status.last_error = str(exc)
                    LOGGER.error("Bridge connection failed: %s", exc)
                finally:
                    await self._cancel_active_turn(count=False)
                    await self.xiaomi.close()

                if not stop_event.is_set():
                    await self._wait_for_retry(stop_event)
        finally:
            home_state_task.cancel()
            router_state_task.cancel()
            automation_task.cancel()
            await asyncio.gather(
                home_state_task, router_state_task, automation_task, return_exceptions=True
            )
            pending = [
                task for _, task in self.control_requests.values() if not task.done()
            ]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await self.ai.close()
            await asyncio.gather(*self._retired_ai_tasks, return_exceptions=True)
            await self.home.close()
            await self.health_source.close()
            weather = getattr(self, "weather", None)
            if weather is not None:
                await weather.close()
            websearch = getattr(self, "websearch", None)
            if websearch is not None:
                await websearch.close()
            await self.router.close()

    async def _home_state_loop(self, stop_event: asyncio.Event) -> None:
        interval = self.settings.home_state_poll_seconds
        while not stop_event.is_set():
            self.home_sampling_status["next_due_at"] = None
            try:
                await self.sample_home_states(include_router=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the sampler alive
                self.home_sample_error = "本轮采集未完成，稍后自动重试"
                self.home_sampling_status["consecutive_failures"] += 1
                self.home_sampling_status["last_finished_at"] = utc_now()
                LOGGER.warning("Home sampler cycle failed: %s", scrub(exc))
            # Property deadlines start when a read finishes. Waiting one interval
            # after completion avoids landing just before them on every other tick.
            delay = interval
            self.home_sampling_status["next_due_at"] = (
                datetime.now(UTC) + timedelta(seconds=delay)
            ).isoformat()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def _router_state_loop(self, stop_event: asyncio.Event) -> None:
        """LAN telemetry follows its own clock, independent of slow cloud reads."""
        while not stop_event.is_set():
            try:
                await self._sample_router_states()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - retry on the next LAN tick
                self._finish_sampling_source(
                    "router", error="路由器本轮采集未完成，稍后自动重试"
                )
                LOGGER.warning("Router sampler cycle failed: %s", scrub(exc))
            # RouterSource's cache lifetime starts at completion as well.
            delay = self.settings.router_poll_seconds
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except TimeoutError:
                pass

    def _finish_sampling_source(
        self, source: str, *, persisted: bool = False, error: str = ""
    ) -> None:
        status = self.sampling_sources[source]
        status["last_error"] = error
        status["consecutive_failures"] = (
            status["consecutive_failures"] + 1 if error else 0
        )
        if persisted:
            status["last_success_at"] = utc_now()

    async def _sample_router_states(self, *, force: bool = False) -> None:
        for device in list(self.home.devices.values()):
            if not device.enabled or not self._is_local_router(device):
                continue
            try:
                if self.hub.resource_enabled(
                    home_resource_id(device.category, device.key)
                ) is False:
                    continue
                await self.refresh_router_device(device.key, refresh=force)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - isolate catalog/persistence races
                LOGGER.warning("Router sampling unavailable: %s", scrub(exc))

    async def sample_home_states(
        self, *, force: bool = False, include_router: bool = True
    ) -> dict[str, Any]:
        async with self.home_sampling_lock:
            # Shared cooldown prevents multiple browser tabs from multiplying cloud reads.
            if (
                self.home_sample_started is not None
                and time.monotonic() - self.home_sample_started < 15
            ):
                snapshot = self.public_hub()
                snapshot["sampling"]["sampling"] = False
                return snapshot
            self.home_sample_started = time.monotonic()
            self.home_sampling_status["last_attempt_at"] = utc_now()
            self.sampling_sources["cloud"]["last_attempt_at"] = utc_now()
            saved = 0
            try:
                device_keys = {
                    device.key
                    for device in self.home.devices.values()
                    if device.enabled
                    and self.hub.resource_enabled(
                        home_resource_id(device.category, device.key)
                    ) is not False
                }
                snapshots = await self.home.snapshot_enabled_states(
                    force=force, device_keys=device_keys
                )
                self.home_sample_error = (
                    "部分设备暂时无法读取，显示上次数据"
                    if self.home.sampling_errors
                    else ""
                )
                for device, result in snapshots:
                    resource_id = home_resource_id(device.category, device.key)
                    # Permission can be withdrawn while waiting for the cloud response.
                    if (
                        not device.enabled
                        or self.hub.resource_enabled(resource_id) is False
                    ):
                        continue
                    try:
                        resource = self._publish_home_snapshot(
                            device,
                            result.get("state") or {},
                            self._home_fields(device, include_local=False),
                            result.get("observed_at") or utc_now(),
                            result.get("ttl_seconds")
                            or self.home.device_ttl_seconds(device),
                        )
                        if resource is not None:
                            saved += 1
                    except Exception as exc:  # noqa: BLE001 - isolate persistence failures
                        self.home_sample_error = "部分设备的数据暂时无法保存"
                        LOGGER.warning("Home state persistence failed: %s", scrub(exc))
                if saved:
                    self.home_last_sample_at = utc_now()
                for device in self.home.devices.values():
                    resource_id = home_resource_id(device.category, device.key)
                    if not device.enabled:
                        self.hub.disable_resource(resource_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - sampling is best effort
                self.home_sample_error = "米家数据暂时无法刷新，显示上次数据"
                LOGGER.warning("Home state sampling unavailable: %s", scrub(exc))
            # Cancellation propagates before status is recorded as a completed check.
            self._finish_sampling_source(
                "cloud", persisted=bool(saved), error=self.home_sample_error
            )
            self.home_sampling_status.update(
                last_finished_at=utc_now(),
                duration_seconds=round(time.monotonic() - self.home_sample_started, 3),
                consecutive_failures=(
                    self.home_sampling_status["consecutive_failures"] + 1
                    if self.home_sample_error
                    else 0
                ),
            )
            if self.home_sampling_status["next_due_at"] is None:
                self.home_sampling_status["next_due_at"] = (
                    datetime.now(UTC)
                    + timedelta(seconds=self.settings.home_state_poll_seconds)
                ).isoformat()
        if include_router:
            await self._sample_router_states(force=force)
        for device in list(self.home.devices.values()):
            if (
                device.enabled
                and device.cloud_online is not False
                and self._extension_kind(device) == "speaker"
            ):
                try:
                    if self.hub.resource_enabled(f"speaker.{device.key}") is False:
                        continue
                    await self.home_extensions(device.key)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - keep other sources running
                    LOGGER.warning("Speaker sampling unavailable: %s", scrub(exc))
        snapshot = self.public_hub()
        self.hub._publish({"type": "hub.snapshot", "hub": snapshot})
        return snapshot

    async def _answer_with_tools(
        self, prompt: str, history: list[dict[str, str]], surface: str = "speaker"
    ) -> str:
        async with self.ai.lease() as ai:
            return await self._answer_with_client(prompt, history, surface, ai)

    async def _answer_with_client(
        self, prompt: str, history: list[dict[str, str]], surface: str, ai: AIClient
    ) -> str:
        messages: list[dict[str, object]] = [
            *history,
            {"role": "user", "content": prompt},
        ]
        weather = getattr(self, "weather", None)
        websearch = getattr(self, "websearch", None)
        tools = [
            *self.home.tool_definitions(),
            *self.hub.tool_definitions(),
            *self.health_source.tool_definitions(),
            *self.infrared.tool_definitions(),
            *(weather.tool_definitions() if weather is not None else []),
            *(websearch.tool_definitions() if websearch is not None else []),
            *self.automation_tool_definitions(),
        ]
        remaining = MAX_TOOL_CALLS
        completed_controls: list[str] = []
        while True:
            offered_tools = tools if remaining > 0 else None
            result = await ai.complete(messages, offered_tools, surface)
            if not result.tool_calls:
                if not result.content:
                    raise RuntimeError("AI 没有返回最终答复")
                return result.content
            if offered_tools is None:
                raise RuntimeError("AI 在工具调用上限后仍请求工具")
            assignments = self.layout.public().get("assignments", {})
            targets = [
                {
                    "alias": item.alias,
                    "category": item.category,
                    "room": assignments.get(item.key, {}).get("room_name", ""),
                }
                for item in getattr(self.home, "devices", {}).values()
                if item.enabled
            ]
            # Check the entire batch before executing any guessed device target.
            for call in result.tool_calls:
                if call.name not in {TOOL_NAME, "control_infrared_ac"}:
                    continue
                try:
                    arguments = json.loads(call.arguments) if isinstance(call.arguments, str) else call.arguments
                except json.JSONDecodeError:
                    continue  # The executor rejects malformed arguments.
                if not isinstance(arguments, dict):
                    continue
                if call.name == "control_infrared_ac":
                    entries = [
                        (device, profile)
                        for device in self.home.devices.values()
                        if device.enabled and self.infrared.supports(device)
                        for profile in self.infrared.ac_profiles(device)
                        if profile["ai_control_enabled"]
                    ]
                    names = [profile["name"] for _, profile in entries]
                    ac_targets = []
                    selected = ""
                    for device, profile in entries:
                        alias = profile["name"] if names.count(profile["name"]) == 1 else f"{device.alias} / {profile['name']}"
                        ac_targets.append({"alias": alias, "category": "air-conditioner",
                                           "room": assignments.get(device.key, {}).get("room_name", "")})
                        if device.alias == arguments.get("device") and profile["key"] == arguments.get("profile_key"):
                            selected = alias
                    clarification = home_target_clarification(prompt, selected, ac_targets, history)
                else:
                    clarification = home_target_clarification(
                        prompt, str(arguments.get("device") or ""), targets, history
                    )
                if clarification:
                    done = f"已完成{'、'.join(dict.fromkeys(completed_controls))}的操作。" if completed_controls else ""
                    return done + clarification
            messages.append(result.assistant_message())
            for call in result.tool_calls:
                if remaining <= 0:
                    outcome = {"ok": False, "error": "本轮工具调用已达到上限"}
                else:
                    if call.name == "control_infrared_ac":
                        executor = self.infrared
                    elif call.name == HEALTH_TOOL_NAME:
                        executor = self.health_source
                    elif call.name in {LOCATION_TOOL_NAME, WEATHER_TOOL_NAME}:
                        executor = weather
                        if executor is None:
                            outcome = {"ok": False, "error": "定位和天气工具未注册"}
                            messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": json.dumps(outcome, ensure_ascii=False)})
                            remaining -= 1
                            continue
                    elif call.name == WEB_SEARCH_TOOL_NAME:
                        executor = websearch
                        if executor is None:
                            outcome = {"ok": False, "error": "网页搜索工具未注册"}
                            messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": json.dumps(outcome, ensure_ascii=False)})
                            remaining -= 1
                            continue
                    elif call.name == HUB_TOOL_NAME:
                        executor = self.hub
                    elif call.name == TOOL_NAME:
                        executor = self.home
                    elif call.name == AUTOMATION_TOOL_NAME:
                        outcome = await self.execute_automation_tool(call.arguments)
                        remaining -= 1
                        self.status.tool_calls += 1
                        if outcome.get("ok"):
                            self.status.tool_successes += 1
                        messages.append({
                            "role": "tool", "tool_call_id": call.id,
                            "name": call.name,
                            "content": json.dumps(outcome, ensure_ascii=False),
                        })
                        continue
                    else:
                        outcome = {"ok": False, "error": "工具未注册"}
                        messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(outcome, ensure_ascii=False)})
                        remaining -= 1
                        continue
                    if executor is self.infrared:
                        outcome = await executor.execute_tool(call.name, call.arguments)
                    else:
                        outcome = await executor.execute(call.name, call.arguments)
                    if executor is self.infrared and outcome.get("action") == "ir_ac_send":
                        try:
                            await self.home._audit_result(
                                str(outcome.get("device") or "红外空调"), "ir_ac_send",
                                bool(outcome.get("ok")),
                                "" if outcome.get("ok") else str(outcome.get("message") or "结果未确认"),
                                outcome,
                            )
                        except OSError:
                            LOGGER.warning("Unable to persist infrared AI operation audit")
                        # Share the new last-command metadata with all panels;
                        # no appliance telemetry is fabricated for IR controls.
                        try:
                            await self.home_extensions(str(outcome["device_key"]))
                        except ValueError:
                            pass
                    if executor is self.home and outcome.get("readback"):
                        record = next(
                            (
                                item
                                for item in self.home.devices.values()
                                if item.alias == outcome.get("device")
                            ),
                            None,
                        )
                        if record:
                            self._publish_control_readback(record.key, outcome)
                    remaining -= 1
                    self.status.tool_calls += 1
                    if outcome.get("ok"):
                        self.status.tool_successes += 1
                        if (
                            executor is self.home
                            and outcome.get("action") != "get_state"
                            and outcome.get("outcome") == "confirmed"
                        ):
                            completed_controls.append(str(outcome.get("device") or "设备"))
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": json.dumps(outcome, ensure_ascii=False),
                    }
                )

    async def _poll_loop(self, stop_event: asyncio.Event) -> None:
        failures = 0
        while not stop_event.is_set():
            self._expire_continuous_mode()
            if self.reconnect_event.is_set():
                raise ReconnectRequested
            try:
                records = await self.xiaomi.latest_questions()
                self.status.last_poll_at = utc_now()
                self.status.last_error = None
                failures = 0
                await self._consume(records)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                self.status.last_error = str(exc)
                LOGGER.warning("Xiaomi polling failed (%d/3): %s", failures, exc)
                if failures >= 3:
                    raise

            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self.settings.poll_interval_seconds
                )
            except TimeoutError:
                pass

    async def _wait_for_retry(self, stop_event: asyncio.Event) -> None:
        stop_task = asyncio.create_task(stop_event.wait())
        reconnect_task = asyncio.create_task(self.reconnect_event.wait())
        try:
            await asyncio.wait(
                {stop_task, reconnect_task},
                timeout=self.settings.reconnect_delay_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            stop_task.cancel()
            reconnect_task.cancel()
            await asyncio.gather(stop_task, reconnect_task, return_exceptions=True)
        if self.reconnect_event.is_set():
            self.reconnect_event.clear()

    async def _consume(self, records: list[ConversationRecord]) -> None:
        if not records:
            return
        if self.cursor.last_timestamp_ms == 0:
            latest = records[-1]
            self._advance_cursor(latest)
            LOGGER.info("Conversation cursor initialized; old questions were skipped")
            return

        latest_turn: tuple[ConversationRecord, RouteResult] | None = None
        for record in records:
            if record.timestamp_ms < self.cursor.last_timestamp_ms:
                continue
            if (
                record.timestamp_ms == self.cursor.last_timestamp_ms
                and record.request_id == self.cursor.last_request_id
            ):
                continue
            self._advance_cursor(record)
            age_seconds = max(0, time.time() - record.timestamp_ms / 1000)
            if age_seconds > self.settings.max_question_age_seconds:
                continue
            result = route_question(
                record.query,
                continuous=self.status.continuous_mode,
                trigger_keywords=tuple(self.behavior_config()["trigger_keywords"]),
                start_keyword=self.settings.start_continuous_keyword,
                end_keyword=self.settings.end_continuous_keyword,
            )
            if result.action != Action.IGNORE:
                latest_turn = (record, result)
        if latest_turn is not None:
            await self._schedule_turn(*latest_turn)

    def _advance_cursor(self, record: ConversationRecord) -> None:
        self.cursor = CursorState(record.timestamp_ms, record.request_id)
        self.store.save(self.cursor)

    def _expire_continuous_mode(self) -> None:
        if (
            self.status.continuous_mode
            and self.continuous_deadline
            and time.monotonic() >= self.continuous_deadline
        ):
            self.status.continuous_mode = False
            self.continuous_deadline = 0.0
            self.history.clear()
            LOGGER.info("Continuous conversation expired")

    async def _schedule_turn(
        self, record: ConversationRecord, result: RouteResult
    ) -> None:
        await self._cancel_active_turn(count=True)
        self.active_turn_task = asyncio.create_task(self._run_turn(record, result))

    async def _cancel_active_turn(self, *, count: bool) -> None:
        task = self.active_turn_task
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if count:
            self.status.cancelled_turns += 1
            LOGGER.info("Cancelled the previous turn for a newer question")
        await self.xiaomi.suppress_native_response(force=True)

    async def _run_turn(self, record: ConversationRecord, result: RouteResult) -> None:
        started = time.perf_counter()
        self.status.active_turn = True
        try:
            await self._handle(record, result)
            self.status.last_turn_latency_ms = round(
                (time.perf_counter() - started) * 1000
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - isolated turn boundary
            self.status.last_error = str(exc)
            LOGGER.error("Turn failed: %s", exc)
        finally:
            self.status.active_turn = False

    async def _handle(self, record: ConversationRecord, result: RouteResult) -> None:
        behavior = self.behavior_config()
        self.audio.suppress_native = behavior["mute_xiaoai"] is True
        if result.action == Action.ENTER_CONTINUOUS:
            self.status.continuous_mode = True
            self.continuous_deadline = time.monotonic() + int(
                behavior["continuous_timeout_seconds"]
            )
            await self.audio.speak_direct("已进入连续对话，请说。")
            return
        if result.action == Action.EXIT_CONTINUOUS:
            self.status.continuous_mode = False
            self.continuous_deadline = 0.0
            self.history.clear()
            await self.audio.speak_direct("已退出连续对话。")
            return
        if not self.settings.ai_configured:
            LOGGER.warning("Question ignored because AI is not configured")
            if self.settings.speak_errors:
                await self.audio.speak_direct("AI 服务还没有配置完成。")
            return

        if self.status.continuous_mode:
            self.continuous_deadline = time.monotonic() + int(
                behavior["continuous_timeout_seconds"]
            )

        if self.settings.log_content:
            LOGGER.info("Question: %s", result.prompt)
        else:
            LOGGER.info("Accepted a question with %d characters", len(result.prompt))

        try:

            async def produce() -> str:
                ai_started = time.perf_counter()
                # Every speaker turn is independent: no earlier exchange is sent,
                # so each wake-up starts from a clean slate.
                answer = await self._answer_with_tools(result.prompt, [])
                self.status.last_ai_latency_ms = round(
                    (time.perf_counter() - ai_started) * 1000
                )
                spoken_answer = sanitize_tts(answer, int(behavior["answer_max_chars"]))
                if not spoken_answer:
                    raise RuntimeError("AI answer became empty after TTS sanitization")
                return spoken_answer

            spoken = await self.audio.run_turn(produce)
            self.status.last_processed_at = utc_now()
            self.status.last_error = None
            if self.settings.log_content:
                LOGGER.info("Answer: %s", spoken)
            else:
                LOGGER.info("Played an answer with %d characters", len(spoken))
        except Exception as exc:  # noqa: BLE001 - one failed turn must not stop polling
            self.status.last_error = str(exc)
            LOGGER.error("Question processing failed: %s", exc)
            if self.settings.speak_errors:
                await self.audio.speak_direct("AI 服务暂时没有响应，请稍后再试。")

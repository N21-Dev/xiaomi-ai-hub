"""Allowlisted MQTT telemetry consumer for the data hub.

The MQTT broker is deliberately kept outside the main bridge process.  This
module accepts only topics declared in a local catalog, validates each payload
against that catalog, and forwards the resulting event to the authenticated
hub HTTP endpoint.  It never writes the hub SQLite database directly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import signal
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any

import aiohttp
from paho.mqtt import client as mqtt

from .hub_data import ALLOWED_KINDS, RESOURCE_ID_PATTERN
from .logging_setup import setup_logging

LOGGER = logging.getLogger("xiaomi_ai_bridge.mqtt")
MAX_PAYLOAD_BYTES = 16 * 1024
METADATA_KEYS = frozenset({"observed_at", "timestamp", "ts"})
DEFAULT_TTL_SECONDS = 180
DEFAULT_RATE_SECONDS = 2.0


class CatalogError(ValueError):
    """Raised when the static topic catalog is unsafe or malformed."""


@dataclass(frozen=True)
class FieldRule:
    name: str
    value_type: str
    minimum: float | None = None
    maximum: float | None = None
    max_length: int = 200
    choices: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, name: object, raw: object) -> FieldRule:
        if not isinstance(name, str) or not name or len(name) > 64:
            raise CatalogError("fields 中的字段名无效")
        if not isinstance(raw, Mapping):
            raise CatalogError(f"字段 {name} 必须是对象")
        value_type = str(raw.get("type") or "number").strip().lower()
        if value_type not in {"number", "integer", "boolean", "string"}:
            raise CatalogError(f"字段 {name} 的 type 不受支持")

        def optional_number(key: str) -> float | None:
            value = raw.get(key)
            if value is None:
                return None
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise CatalogError(f"字段 {name} 的 {key} 必须是数字")
            number = float(value)
            if not math.isfinite(number):
                raise CatalogError(f"字段 {name} 的 {key} 必须是有限数字")
            return number

        minimum = optional_number("min")
        maximum = optional_number("max")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise CatalogError(f"字段 {name} 的 min 不能大于 max")
        max_length = int(raw.get("max_length", 200))
        if not 1 <= max_length <= 500:
            raise CatalogError(f"字段 {name} 的 max_length 必须在 1 到 500 之间")
        choices_raw = raw.get("enum", ())
        if choices_raw and (
            not isinstance(choices_raw, list)
            or not all(isinstance(item, str) and item for item in choices_raw)
        ):
            raise CatalogError(f"字段 {name} 的 enum 必须是字符串数组")
        choices = tuple(str(item) for item in choices_raw)
        return cls(
            name=name,
            value_type=value_type,
            minimum=minimum,
            maximum=maximum,
            max_length=max_length,
            choices=choices,
        )

    def validate(self, value: object) -> object:
        if self.value_type == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"字段 {self.name} 必须是数字")
            clean: object = float(value) if isinstance(value, float) else value
            if not math.isfinite(float(clean)):
                raise ValueError(f"字段 {self.name} 必须是有限数字")
            number = float(clean)
            if self.minimum is not None and number < self.minimum:
                raise ValueError(f"字段 {self.name} 小于允许的最小值")
            if self.maximum is not None and number > self.maximum:
                raise ValueError(f"字段 {self.name} 大于允许的最大值")
            return clean
        if self.value_type == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"字段 {self.name} 必须是整数")
            if self.minimum is not None and value < self.minimum:
                raise ValueError(f"字段 {self.name} 小于允许的最小值")
            if self.maximum is not None and value > self.maximum:
                raise ValueError(f"字段 {self.name} 大于允许的最大值")
            return value
        if self.value_type == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"字段 {self.name} 必须是布尔值")
            return value
        if not isinstance(value, str):
            raise TypeError(f"字段 {self.name} 必须是字符串")
        if len(value) > self.max_length:
            raise ValueError(f"字段 {self.name} 过长")
        if self.choices and value not in self.choices:
            raise ValueError(f"字段 {self.name} 不在允许值列表中")
        return value


@dataclass(frozen=True)
class TopicRule:
    topic: str
    resource_id: str
    kind: str
    name: str
    location: str
    summary: str
    ttl_seconds: int
    max_rate_seconds: float
    fields: Mapping[str, FieldRule]

    @classmethod
    def from_config(cls, topic: object, raw: object) -> TopicRule:
        if (
            not isinstance(topic, str)
            or not topic
            or len(topic) > 200
            or "+" in topic
            or "#" in topic
        ):
            raise CatalogError("topic 必须是长度不超过 200 的精确主题")
        if not isinstance(raw, Mapping):
            raise CatalogError(f"主题 {topic} 的配置必须是对象")
        resource_id = str(raw.get("resource_id") or "").strip().casefold()
        if not RESOURCE_ID_PATTERN.fullmatch(resource_id):
            raise CatalogError(f"主题 {topic} 的 resource_id 无效")
        kind = str(raw.get("kind") or "sensor").strip().casefold()
        if kind not in ALLOWED_KINDS:
            raise CatalogError(f"主题 {topic} 的 kind 不受支持")
        name = str(raw.get("name") or "").strip()
        if not 1 <= len(name) <= 80:
            raise CatalogError(f"主题 {topic} 的 name 长度无效")
        location = str(raw.get("location") or "").strip()
        summary = str(raw.get("summary") or f"{name} 状态更新").strip()
        if len(location) > 80 or not 1 <= len(summary) <= 500:
            raise CatalogError(f"主题 {topic} 的 location 或 summary 长度无效")
        try:
            ttl_seconds = int(raw.get("ttl_seconds", DEFAULT_TTL_SECONDS))
        except (TypeError, ValueError) as exc:
            raise CatalogError(f"主题 {topic} 的 ttl_seconds 无效") from exc
        if not 30 <= ttl_seconds <= 86400:
            raise CatalogError(f"主题 {topic} 的 ttl_seconds 必须在 30 到 86400 之间")
        try:
            max_rate_seconds = float(raw.get("max_rate_seconds", DEFAULT_RATE_SECONDS))
        except (TypeError, ValueError) as exc:
            raise CatalogError(f"主题 {topic} 的 max_rate_seconds 无效") from exc
        if not math.isfinite(max_rate_seconds) or not 0.1 <= max_rate_seconds <= 86400:
            raise CatalogError(
                f"主题 {topic} 的 max_rate_seconds 必须在 0.1 到 86400 之间"
            )
        fields_raw = raw.get("fields")
        if not isinstance(fields_raw, Mapping) or not fields_raw:
            raise CatalogError(f"主题 {topic} 必须配置非空 fields")
        if len(fields_raw) > 32:
            raise CatalogError(f"主题 {topic} 的 fields 过多")
        fields = {
            str(field_name): FieldRule.from_config(field_name, field_config)
            for field_name, field_config in fields_raw.items()
        }
        return cls(
            topic=topic,
            resource_id=resource_id,
            kind=kind,
            name=name,
            location=location,
            summary=summary,
            ttl_seconds=ttl_seconds,
            max_rate_seconds=max_rate_seconds,
            fields=fields,
        )

    def event_from_payload(self, payload: bytes) -> dict[str, Any]:
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise ValueError("MQTT payload 超过 16 KiB 限制")
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("MQTT payload 必须是 UTF-8 JSON") from exc
        if not isinstance(decoded, dict) or not decoded:
            raise ValueError("MQTT payload 必须是非空 JSON 对象")

        # Accept either a compact direct object or {state: {...}, ts: ...}.
        if "state" in decoded:
            raw_state = decoded.get("state")
            if not isinstance(raw_state, dict) or not raw_state:
                raise ValueError("payload.state 必须是非空 JSON 对象")
            unknown_outer = set(decoded) - {"state", *METADATA_KEYS}
            if unknown_outer:
                raise ValueError("payload 包含未声明字段")
            state_source = raw_state
        else:
            state_source = {
                key: value for key, value in decoded.items() if key not in METADATA_KEYS
            }

        unknown = set(state_source) - set(self.fields)
        if unknown:
            raise ValueError("state 包含未声明字段")
        if not state_source:
            raise ValueError("state 不能为空")
        state = {
            key: self.fields[key].validate(value) for key, value in state_source.items()
        }
        event: dict[str, Any] = {
            "resource_id": self.resource_id,
            "kind": self.kind,
            "name": self.name,
            "location": self.location,
            "summary": self.summary,
            "state": state,
            "ttl_seconds": self.ttl_seconds,
        }
        observed_at = next(
            (
                decoded[key]
                for key in ("observed_at", "timestamp", "ts")
                if key in decoded
            ),
            None,
        )
        if observed_at is not None:
            if not isinstance(observed_at, str) or len(observed_at) > 80:
                raise ValueError("observed_at 必须是短 ISO 8601 字符串")
            event["observed_at"] = observed_at
        return event


class TopicCatalog:
    def __init__(self, rules: Mapping[str, TopicRule]):
        if not rules:
            raise CatalogError("主题目录不能为空")
        resource_ids = [rule.resource_id for rule in rules.values()]
        if len(resource_ids) != len(set(resource_ids)):
            raise CatalogError("每个 resource_id 只能绑定一个 MQTT 主题")
        self._rules = dict(rules)

    @classmethod
    def from_file(cls, path: Path) -> TopicCatalog:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise CatalogError(f"无法读取 MQTT 主题目录: {path}") from exc
        except json.JSONDecodeError as exc:
            raise CatalogError("MQTT 主题目录不是有效 JSON") from exc
        if not isinstance(raw, dict):
            raise CatalogError("MQTT 主题目录必须是对象")
        return cls(
            {
                topic: TopicRule.from_config(topic, config)
                for topic, config in raw.items()
            }
        )

    @property
    def topics(self) -> tuple[str, ...]:
        return tuple(self._rules)

    def get(self, topic: str) -> TopicRule | None:
        return self._rules.get(topic)


class MqttIngestor:
    """Paho callback adapter with validation and per-topic rate limiting."""

    def __init__(self, catalog: TopicCatalog, queue_size: int = 256):
        if not 1 <= queue_size <= 4096:
            raise ValueError("queue_size 必须在 1 到 4096 之间")
        self.catalog = catalog
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_size)
        self._last_accepted: dict[str, float] = {}
        self._rate_lock = threading.Lock()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.dropped = 0

    def _accept_rate(self, rule: TopicRule) -> bool:
        now = monotonic()
        with self._rate_lock:
            previous = self._last_accepted.get(rule.topic)
            if previous is not None and now - previous < rule.max_rate_seconds:
                return False
            self._last_accepted[rule.topic] = now
        return True

    def parse_message(self, topic: str, payload: bytes) -> dict[str, Any] | None:
        rule = self.catalog.get(topic)
        if rule is None:
            self.dropped += 1
            LOGGER.warning("丢弃未授权 MQTT 主题: %s", topic[:200])
            return None
        try:
            event = rule.event_from_payload(payload)
        except (TypeError, ValueError) as exc:
            self.dropped += 1
            LOGGER.warning("丢弃无效 MQTT 消息 topic=%s reason=%s", rule.topic, exc)
            return None
        if not self._accept_rate(rule):
            self.dropped += 1
            return None
        return event

    def on_message(
        self, _client: mqtt.Client, _userdata: object, message: object
    ) -> None:
        topic = str(getattr(message, "topic", ""))
        payload = bytes(getattr(message, "payload", b""))
        event = self.parse_message(topic, payload)
        if event is None or self.loop is None:
            return
        try:
            self.loop.call_soon_threadsafe(self._enqueue, event)
        except RuntimeError:
            self.dropped += 1

    def _enqueue(self, event: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1
            LOGGER.warning("MQTT 转发队列已满，丢弃一条状态")


async def _post_event(
    session: aiohttp.ClientSession,
    url: str,
    token: str,
    event: dict[str, Any],
) -> bool:
    try:
        async with session.post(
            url,
            json=event,
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            if response.status == 202:
                return True
            if response.status >= 500:
                raise RuntimeError(f"数据中枢返回 {response.status}")
            LOGGER.warning("数据中枢拒绝 MQTT 状态，HTTP %s", response.status)
            return False
    except (aiohttp.ClientError, TimeoutError, RuntimeError) as exc:
        LOGGER.warning("转发 MQTT 状态失败: %s", exc)
        return False


def _read_secret(path_value: str, *, name: str) -> str:
    path = Path(path_value)
    try:
        secret = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"无法读取 {name} 文件") from exc
    if not secret:
        raise RuntimeError(f"{name} 不能为空")
    return secret


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} 必须是布尔值")


async def run(config: argparse.Namespace) -> None:
    catalog = TopicCatalog.from_file(Path(config.topics_file))
    token = _read_secret(config.hub_token_file, name="HUB_INGEST_TOKEN")
    password = _read_secret(config.mqtt_password_file, name="MQTT_PASSWORD")
    try:
        queue_size = int(config.queue_size)
    except ValueError as exc:
        raise RuntimeError("MQTT_QUEUE_SIZE 必须是整数") from exc
    ingestor = MqttIngestor(catalog, queue_size=queue_size)
    ingestor.loop = asyncio.get_running_loop()
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=config.client_id,
        protocol=mqtt.MQTTv311,
        clean_session=False,
    )
    client.username_pw_set(config.username, password)
    if config.tls:
        client.tls_set(ca_certs=config.ca_file or None)
        client.tls_insecure_set(False)
    client.reconnect_delay_set(min_delay=1, max_delay=60)

    connected = threading.Event()

    def on_connect(
        mqtt_client: mqtt.Client,
        _userdata: object,
        _flags: object,
        reason_code: object,
        _properties: object,
    ) -> None:
        if getattr(reason_code, "is_failure", False):
            LOGGER.error("MQTT 连接被拒绝: %s", reason_code)
            return
        connected.set()
        for topic in catalog.topics:
            mqtt_client.subscribe(topic, qos=1)
        LOGGER.info("MQTT 已连接，订阅 %d 个白名单主题", len(catalog.topics))

    def on_disconnect(
        _mqtt_client: mqtt.Client,
        _userdata: object,
        _disconnect_flags: object,
        reason_code: object,
        _properties: object,
    ) -> None:
        connected.clear()
        LOGGER.warning("MQTT 连接断开: %s", reason_code)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = ingestor.on_message
    client.connect_async(config.host, config.port, config.keepalive)
    client.loop_start()
    timeout = aiohttp.ClientTimeout(total=10)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except NotImplementedError:
            pass

    async with aiohttp.ClientSession(timeout=timeout) as session:
        while not stop_event.is_set():
            try:
                event = await asyncio.wait_for(ingestor.queue.get(), timeout=1)
            except TimeoutError:
                continue
            delivered = False
            for attempt in range(3):
                delivered = await _post_event(session, config.hub_url, token, event)
                if delivered:
                    break
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
            if not delivered:
                LOGGER.warning("放弃转发一条 MQTT 状态")
    client.disconnect()
    client.loop_stop()
    if connected.is_set():
        LOGGER.info("MQTT ingest 已停止")


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Forward allowlisted MQTT telemetry to hub"
    )
    parser.add_argument("--host", default=os.getenv("MQTT_BROKER_HOST", "mqtt"))
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("MQTT_BROKER_PORT", "1883"))
    )
    parser.add_argument("--username", default=os.getenv("MQTT_USERNAME", "mqtt-ingest"))
    parser.add_argument(
        "--mqtt-password-file",
        default=os.getenv("MQTT_PASSWORD_FILE", "/run/secrets/mqtt-password"),
    )
    parser.add_argument(
        "--topics-file", default=os.getenv("MQTT_TOPICS_FILE", "/config/topics.json")
    )
    parser.add_argument(
        "--client-id", default=os.getenv("MQTT_CLIENT_ID", "xiaomi-ai-mqtt-ingest")
    )
    parser.add_argument(
        "--keepalive", type=int, default=int(os.getenv("MQTT_KEEPALIVE", "60"))
    )
    parser.add_argument("--queue-size", default=os.getenv("MQTT_QUEUE_SIZE", "256"))
    parser.add_argument(
        "--hub-url",
        default=os.getenv(
            "HUB_EVENTS_URL", "http://xiaomi-ai-bridge:8080/api/hub/events"
        ),
    )
    parser.add_argument(
        "--hub-token-file",
        default=os.getenv("HUB_INGEST_TOKEN_FILE", "/run/secrets/hub-ingest-token"),
    )
    parser.add_argument(
        "--tls", action="store_true", default=_bool_env("MQTT_TLS", False)
    )
    parser.add_argument("--ca-file", default=os.getenv("MQTT_TLS_CA_FILE", ""))
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    config = _arguments(argv)
    setup_logging(config.log_level.upper())
    try:
        asyncio.run(run(config))
    except (CatalogError, RuntimeError, OSError) as exc:
        LOGGER.error("MQTT ingest 启动失败: %s", exc)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()

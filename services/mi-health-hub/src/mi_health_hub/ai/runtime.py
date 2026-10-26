"""线程安全的 AI 运行时配置提供器。"""

from __future__ import annotations

import os
import threading
from dataclasses import replace

from ..config import AIConfig
from .settings import AISettingsStore, validate_ai_config

_AI_ENV_NAMES = {
    "enabled": "MI_AI_ENABLED",
    "protocol": "MI_AI_PROTOCOL",
    "base_url": "MI_AI_BASE_URL",
    "api_key": "MI_AI_API_KEY",
    "model": "MI_AI_MODEL",
    "timeout_s": "MI_AI_TIMEOUT_S",
    "max_tokens": "MI_AI_MAX_TOKENS",
    "temperature": "MI_AI_TEMPERATURE",
    "auto_after_sync": "MI_AI_AUTO_AFTER_SYNC",
    "auto_period_reports": "MI_AI_AUTO_PERIOD_REPORTS",
    "json_mode": "MI_AI_JSON_MODE",
    "allow_private_base_url": "MI_AI_ALLOW_PRIVATE_BASE_URL",
    "anthropic_version": "MI_AI_ANTHROPIC_VERSION",
    "detail_level": "MI_AI_DETAIL_LEVEL",
    "include_health_notes": "MI_AI_INCLUDE_HEALTH_NOTES",
    "web_tools_enabled": "MI_AI_WEB_TOOLS_ENABLED",
    "custom_instructions": "MI_AI_CUSTOM_INSTRUCTIONS",
}


class AIConfigProvider:
    """把启动配置、持久化设置和环境变量锁定字段合并成不可变快照。"""

    def __init__(self, initial: AIConfig, store: AISettingsStore | None = None) -> None:
        self._lock = threading.RLock()
        self.store = store or AISettingsStore()
        self._initial = replace(initial)
        self._has_environment_defaults = any(
            os.environ.get(env_name) not in (None, "")
            for env_name in _AI_ENV_NAMES.values()
        )
        # 兼容既有部署：环境变量先作为设置页的默认值。只有运维方显式开启
        # MI_AI_LOCK_ENV 时，环境变量字段才变成只读，避免页面永远改不了旧配置。
        lock_environment = os.environ.get("MI_AI_LOCK_ENV", "").strip().lower() in {
            "1", "true", "yes", "on", "y",
        }
        self._locked_fields = (
            {
                field for field, env_name in _AI_ENV_NAMES.items()
                if os.environ.get(env_name) not in (None, "")
            }
            if lock_environment
            else set()
        )
        self._config = self._load_effective()

    def _load_effective(self) -> AIConfig:
        persisted = self.store.load(self._initial)
        values = {
            field: getattr(self._initial, field)
            for field in self._locked_fields
        }
        return validate_ai_config(replace(persisted, **values), strict_url=False)

    def snapshot(self) -> AIConfig:
        with self._lock:
            return replace(self._config)

    def source(self) -> str:
        if self._locked_fields and self.store.exists():
            return "settings+environment"
        if self._locked_fields:
            return "environment"
        if self.store.exists():
            return "settings"
        if self._has_environment_defaults:
            return "environment-default"
        return "config"

    @property
    def locked_fields(self) -> list[str]:
        return sorted(self._locked_fields)

    def preview(self, candidate: AIConfig) -> AIConfig:
        """应用环境变量锁定字段并校验，但不持久化。连接测试与模型发现使用。"""
        with self._lock:
            values = {
                field: getattr(self._initial, field)
                for field in self._locked_fields
            }
            return validate_ai_config(replace(candidate, **values))

    def save(self, candidate: AIConfig) -> AIConfig:
        with self._lock:
            effective = self.preview(candidate)
            self.store.save(effective)
            self._config = effective
            return replace(effective)

    def reset(self) -> AIConfig:
        """恢复默认配置：删除设置文件，回到「YAML / 环境变量说了算」。

        刻意不写回当前值快照：否则运维以后改 .env 会被文件里的旧快照永久盖住。
        删除失败（如权限问题）必须显式报错，不能假装成功。
        """
        with self._lock:
            if self.store.exists():
                try:
                    self.store.path.unlink()
                except OSError as exc:
                    raise AISettingsError(
                        f"无法删除 AI 设置文件（{type(exc).__name__}），请检查数据目录权限"
                    ) from exc
            self._config = validate_ai_config(replace(self._initial), strict_url=False)
            return replace(self._config)

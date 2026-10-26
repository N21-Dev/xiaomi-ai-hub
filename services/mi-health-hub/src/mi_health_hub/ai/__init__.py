"""AI 每日分析 / 健康对话 / 周期报告：OpenAI 兼容协议客户端 + 提示词。"""

from __future__ import annotations

from .client import AIClient, AIError
from .runtime import AIConfigProvider
from .settings import AISettingsError, AISettingsStore
from .prompts import (
    CHAT_SYSTEM_PROMPT,
    CHAT_USER_TEMPLATE,
    PROMPT_VERSION,
    REPORT_SYSTEM_PROMPT,
    REPORT_USER_TEMPLATE,
    SYSTEM_PROMPT,
    USER_TEMPLATE,
    workout_type_zh,
)

__all__ = [
    "AIClient",
    "AIError",
    "AIConfigProvider",
    "AISettingsError",
    "AISettingsStore",
    "PROMPT_VERSION",
    "SYSTEM_PROMPT",
    "USER_TEMPLATE",
    "CHAT_SYSTEM_PROMPT",
    "CHAT_USER_TEMPLATE",
    "REPORT_SYSTEM_PROMPT",
    "REPORT_USER_TEMPLATE",
    "workout_type_zh",
]

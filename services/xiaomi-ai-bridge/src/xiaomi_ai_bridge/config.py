from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _float_env(name: str, default: float, minimum: float) -> float:
    value = float(os.getenv(name, str(default)))
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _int_env(name: str, default: int, minimum: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


@dataclass(frozen=True)
class Settings:
    mi_user_id: str
    mi_pass_token: str
    mi_region: str
    mi_credentials_file: Path | None
    mi_c_user_id: str
    mi_service_token: str
    mi_account_device_id: str
    mi_did: str
    mi_device_name: str
    mi_hardware: str
    ai_base_url: str
    ai_api_key: str
    ai_model: str
    ai_disable_reasoning: bool
    ai_timeout_seconds: float
    ai_temperature: float
    ai_max_tokens: int
    ai_panel_max_tokens: int
    trigger_keywords: tuple[str, ...]
    start_continuous_keyword: str
    end_continuous_keyword: str
    poll_interval_seconds: float
    home_state_poll_seconds: int
    reconnect_delay_seconds: float
    max_question_age_seconds: int
    mute_xiaoai: bool
    speak_errors: bool
    log_content: bool
    answer_max_chars: int
    health_host: str
    health_port: int
    state_dir: Path
    log_level: str
    system_prompt: str
    health_service_url: str = ""
    health_web_url: str = ""
    health_api_key_file: Path | None = None
    health_ai_tools_enabled: bool = False
    ai_gateway_enabled: bool = True
    ai_gateway_auth_required: bool = True
    health_poll_seconds: int = 300
    router_url: str = ""
    router_credentials_file: Path | None = None
    router_device_key: str = ""
    router_poll_seconds: int = 60
    weather_tools_enabled: bool = False
    location_default_query: str = ""
    web_search_enabled: bool = False
    web_search_base_url: str = ""
    web_search_language: str = "zh-CN"
    panel_auth_required: bool = True
    panel_trusted_proxy_cidrs: tuple[str, ...] = ("127.0.0.0/8", "::1/128")

    @property
    def ai_configured(self) -> bool:
        return bool(self.ai_base_url and self.ai_model)

    @classmethod
    def from_env(cls) -> Settings:
        keywords = tuple(
            part.strip()
            for part in os.getenv("TRIGGER_KEYWORDS", "请问,帮我").split(",")
            if part.strip()
        )
        if not keywords:
            raise ValueError("TRIGGER_KEYWORDS must contain at least one keyword")

        region = os.getenv("MI_REGION", "cn").strip().lower()
        if region != "cn":
            raise ValueError("This version currently supports MI_REGION=cn only")

        return cls(
            # A new installation must reach the login/model setup screens
            # before the operator connects a Xiaomi account.
            mi_user_id=os.getenv("MI_USER_ID", "").strip(),
            mi_pass_token=os.getenv("MI_PASS_TOKEN", "").strip(),
            mi_region=region,
            mi_credentials_file=(
                Path(value)
                if (value := os.getenv("MI_CREDENTIALS_FILE", "").strip())
                else None
            ),
            mi_c_user_id=os.getenv("MI_CUSER_ID", "").strip(),
            mi_service_token=os.getenv("MI_SERVICE_TOKEN", "").strip(),
            mi_account_device_id=os.getenv("MI_ACCOUNT_DEVICE_ID", "").strip(),
            mi_did=os.getenv("MI_DID", "").strip(),
            mi_device_name=os.getenv("MI_DEVICE_NAME", "").strip(),
            mi_hardware=os.getenv("MI_HARDWARE", "").strip().upper(),
            ai_base_url=os.getenv("AI_BASE_URL", "").strip(),
            ai_api_key=os.getenv("AI_API_KEY", "").strip(),
            ai_model=os.getenv("AI_MODEL", "").strip(),
            ai_disable_reasoning=_bool_env("AI_DISABLE_REASONING", True),
            ai_timeout_seconds=_float_env("AI_TIMEOUT_SECONDS", 90.0, 1.0),
            ai_temperature=_float_env("AI_TEMPERATURE", 0.6, 0.0),
            ai_max_tokens=_int_env("AI_MAX_TOKENS", 220, 1),
            ai_panel_max_tokens=_int_env("AI_PANEL_MAX_TOKENS", 8192, 1),
            trigger_keywords=keywords,
            start_continuous_keyword=os.getenv(
                "START_CONTINUOUS_KEYWORD", "开始连续对话"
            ).strip(),
            end_continuous_keyword=os.getenv(
                "END_CONTINUOUS_KEYWORD", "结束连续对话"
            ).strip(),
            poll_interval_seconds=_float_env("POLL_INTERVAL_SECONDS", 0.5, 0.5),
            home_state_poll_seconds=_int_env("HOME_STATE_POLL_SECONDS", 60, 15),
            reconnect_delay_seconds=_float_env("RECONNECT_DELAY_SECONDS", 15.0, 1.0),
            max_question_age_seconds=_int_env("MAX_QUESTION_AGE_SECONDS", 90, 10),
            mute_xiaoai=_bool_env("MUTE_XIAOAI", True),
            speak_errors=_bool_env("SPEAK_ERRORS", False),
            log_content=_bool_env("LOG_CONTENT", False),
            answer_max_chars=_int_env("ANSWER_MAX_CHARS", 240, 20),
            health_host=os.getenv("HEALTH_HOST", "0.0.0.0").strip(),
            health_port=_int_env("HEALTH_PORT", 8080, 1),
            state_dir=Path(os.getenv("STATE_DIR", "/data")),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
            system_prompt=os.getenv(
                "SYSTEM_PROMPT",
                "你是家庭中枢的 AI 助手。你可以通过工具控制家中的米家设备、查询中枢数据"
                "（包含健康数据），也可以回答普通问题。严禁输出思维链、思考过程、分析过程、"
                "推理步骤、reasoning_content 或 think 标签。输出风格按入口决定：语音入口要简短、"
                "适合朗读，控制台文字入口可以完整、有条理。",
            ).strip(),
            health_service_url=os.getenv("HEALTH_SERVICE_URL", "").strip(),
            health_web_url=os.getenv("HEALTH_WEB_URL", "").strip(),
            health_api_key_file=(
                Path(value)
                if (value := os.getenv("HEALTH_API_KEY_FILE", "").strip())
                else None
            ),
            health_ai_tools_enabled=_bool_env("HEALTH_AI_TOOLS_ENABLED", False),
            ai_gateway_enabled=_bool_env("AI_GATEWAY_ENABLED", True),
            ai_gateway_auth_required=_bool_env("AI_GATEWAY_AUTH_REQUIRED", True),
            health_poll_seconds=_int_env("HEALTH_POLL_SECONDS", 300, 30),
            router_url=os.getenv("ROUTER_URL", "").strip(),
            router_credentials_file=(
                Path(value)
                if (value := os.getenv("ROUTER_CREDENTIALS_FILE", "").strip())
                else None
            ),
            router_device_key=os.getenv("ROUTER_DEVICE_KEY", "").strip(),
            router_poll_seconds=_int_env("ROUTER_POLL_SECONDS", 60, 30),
            weather_tools_enabled=_bool_env("WEATHER_TOOLS_ENABLED", False),
            location_default_query=os.getenv("LOCATION_DEFAULT_QUERY", "").strip(),
            web_search_enabled=_bool_env("WEB_SEARCH_TOOLS_ENABLED", False),
            web_search_base_url=os.getenv("WEB_SEARCH_BASE_URL", "").strip(),
            web_search_language=os.getenv("WEB_SEARCH_LANGUAGE", "").strip() or "zh-CN",
            panel_auth_required=_bool_env("PANEL_AUTH_REQUIRED", True),
            panel_trusted_proxy_cidrs=tuple(
                part.strip() for part in os.getenv("PANEL_TRUSTED_PROXY_CIDRS", "127.0.0.0/8,::1/128").split(",") if part.strip()
            ),
        )

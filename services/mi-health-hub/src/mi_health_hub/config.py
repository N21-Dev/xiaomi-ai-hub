"""配置加载：YAML 文件 + 环境变量双层覆盖。

设计原则：环境变量优先于 YAML。缺关键配置时明确报错，不用默认值静默兜底
（凭据类配置尤其如此——静默兜底会导致同步悄悄拉不到数据）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = "/etc/mi-health-hub/config.yaml"

DEFAULT_DB_PATH = Path("/var/lib/mi-health-hub/health.db")
DEFAULT_RAW_DIR = Path("/var/lib/mi-health-hub/raw")
DEFAULT_OUTPUT_DIR = Path("/var/lib/mi-health-hub/output")
DEFAULT_BACKUP_DIR = Path("/var/lib/mi-health-hub/backup")
DEFAULT_ACCESS_LOG_DIR = Path("/var/lib/mi-health-hub/access-logs")

TRUTHY = {"1", "true", "yes", "on", "y"}


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


def _env(name: str, default: Any = None) -> Any:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {name} 需要整数，实际为 {raw!r}") from exc


def _env_float(name: str, default: float | None) -> float | None:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {name} 需要数字，实际为 {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in TRUTHY


@dataclass
class Paths:
    db: Path = DEFAULT_DB_PATH
    raw: Path = DEFAULT_RAW_DIR
    output: Path = DEFAULT_OUTPUT_DIR
    backup: Path = DEFAULT_BACKUP_DIR
    access_logs: Path = DEFAULT_ACCESS_LOG_DIR

    def ensure(self) -> None:
        for path in (self.db.parent, self.raw, self.output, self.backup, self.access_logs):
            path.mkdir(parents=True, exist_ok=True)


@dataclass
class SyncConfig:
    """同步行为参数。"""

    chunk_days: int = 7
    recent_days: int = 3
    correct_days: int = 14
    request_interval_ms: int = 500
    max_requests_per_day: int = 2000
    sentinel_enabled: bool = True
    max_pages: int = 200
    request_retries: int = 3
    http_timeout_s: float = 30.0
    fetch_start_date: str = "2020-01-01"


@dataclass
class ScheduleConfig:
    sentinel_interval_hours: int = 1
    incremental_time: str = "04:30"
    correction_time: str = "05:00"
    timezone: str = "Asia/Shanghai"


@dataclass
class Goals:
    steps: int = 8000
    sleep_min: int = 480


@dataclass
class ServerConfig:
    api_host: str = "127.0.0.1"
    api_port: int = 8321
    web_port: int = 8322
    api_key: str | None = None
    serve_web: bool = True
    access_log_enabled: bool = True
    access_log_retention_days: int = 90
    # 是否把私有网段来源视为可信（默认 true）。
    # 为什么默认 true：经 docker 端口映射后客户端 IP 会变成网桥网关地址，
    # 只认回环的话连本机访问都会被拒。真正的访问控制靠宿主机端口绑定。
    trust_private: bool = True


@dataclass
class Profile:
    """个人参数，用于最大心率估算与心率区间。"""

    age: int | None = None
    max_hr: int | None = None
    weight_kg: float | None = None

    def resolved_max_hr(self) -> int:
        """Tanaka 公式估算最大心率，比 220-age 更准。手动指定时优先用指定值。"""
        if self.max_hr:
            return int(self.max_hr)
        if self.age:
            return int(round(208 - 0.7 * self.age))
        return 190  # 保守兜底，前端需提示这是估算值


AI_PROTOCOLS = frozenset({
    "openai_chat_completions",
    "openai_responses",
    "anthropic_messages",
})
AI_DETAIL_LEVELS = frozenset({"concise", "balanced", "detailed"})


@dataclass
class AIConfig:
    """AI 服务与分析偏好。

    协议层统一接收 role/content 消息，底层可切换 OpenAI Chat Completions、
    OpenAI Responses 或 Anthropic Messages。设置页会以完整对象原子替换该配置，
    避免运行中的请求读到一半新、一半旧的值。
    """

    enabled: bool = False
    protocol: str = "openai_chat_completions"
    base_url: str = "https://integrate.api.nvidia.com/v1"
    api_key: str = ""
    model: str = "nvidia/nemotron-3-super-120b-a12b"
    timeout_s: float = 120.0
    # 统一语义是「最大输出 token」；各协议适配器会映射到自己的字段名。
    max_tokens: int = 8000
    temperature: float = 0.3
    auto_after_sync: bool = True
    auto_period_reports: bool = True
    json_mode: bool = True
    allow_private_base_url: bool = False
    anthropic_version: str = "2023-06-01"
    detail_level: str = "detailed"
    include_health_notes: bool = True
    web_tools_enabled: bool = True
    custom_instructions: str = ""

    def configured(self) -> bool:
        return bool(
            self.enabled
            and self.protocol in AI_PROTOCOLS
            and self.api_key
            and self.base_url
            and self.model
        )

    def fingerprint(self) -> str:
        """不含密钥的配置指纹，用于判断 AI 缓存是否仍适用。"""
        import hashlib
        import json

        payload = {
            "protocol": self.protocol,
            "base_url": self.base_url.rstrip("/"),
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "json_mode": self.json_mode,
            "anthropic_version": self.anthropic_version,
            "detail_level": self.detail_level,
            "include_health_notes": self.include_health_notes,
            "web_tools_enabled": self.web_tools_enabled,
            "custom_instructions": self.custom_instructions,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


@dataclass
class Config:
    user_id: str = ""
    pass_token: str = ""
    region: str = "cn"
    paths: Paths = field(default_factory=Paths)
    sync: SyncConfig = field(default_factory=SyncConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    goals: Goals = field(default_factory=Goals)
    server: ServerConfig = field(default_factory=ServerConfig)
    profile: Profile = field(default_factory=Profile)
    ai: AIConfig = field(default_factory=AIConfig)
    source_config: str = "env"

    def validate_credentials(self) -> None:
        if not self.user_id or not self.pass_token:
            raise ConfigError(
                "缺少小米账号凭据（MI_USER_ID / MI_PASS_TOKEN）。\n"
                "获取方式：浏览器登录 account.xiaomi.com → F12 → Application → Cookies，"
                "复制 userId 与 passToken。"
            )


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件 {path} 顶层必须是映射结构")
    return data


def load_config(config_path: str | os.PathLike[str] | None = None) -> Config:
    """加载配置。环境变量优先，其次 YAML 文件，最后内置默认值。"""
    raw_path = config_path or _env("MI_CONFIG", DEFAULT_CONFIG_PATH)
    path = Path(raw_path)
    doc = _load_yaml(path)

    paths_doc = doc.get("paths", {}) or {}
    sync_doc = doc.get("sync", {}) or {}
    schedule_doc = doc.get("schedule", {}) or {}
    goals_doc = doc.get("goals", {}) or {}
    server_doc = doc.get("server", {}) or {}
    profile_doc = doc.get("profile", {}) or {}
    ai_doc = doc.get("ai", {}) or {}

    paths = Paths(
        db=Path(_env("MI_DB_PATH") or paths_doc.get("db") or DEFAULT_DB_PATH),
        raw=Path(_env("MI_RAW_DIR") or paths_doc.get("raw") or DEFAULT_RAW_DIR),
        output=Path(_env("MI_OUTPUT_DIR") or paths_doc.get("output") or DEFAULT_OUTPUT_DIR),
        backup=Path(_env("MI_BACKUP_DIR") or paths_doc.get("backup") or DEFAULT_BACKUP_DIR),
        access_logs=Path(
            _env("MI_ACCESS_LOG_DIR") or paths_doc.get("access_logs") or DEFAULT_ACCESS_LOG_DIR
        ),
    )

    sync_cfg = SyncConfig(
        chunk_days=_env_int("MI_CHUNK_DAYS", int(sync_doc.get("chunk_days", 7))),
        recent_days=_env_int("MI_RECENT_DAYS", int(sync_doc.get("recent_days", 3))),
        correct_days=_env_int("MI_CORRECT_DAYS", int(sync_doc.get("correct_days", 14))),
        request_interval_ms=_env_int(
            "MI_REQUEST_INTERVAL_MS", int(sync_doc.get("request_interval_ms", 500))
        ),
        max_requests_per_day=_env_int(
            "MI_MAX_REQUESTS_PER_DAY", int(sync_doc.get("max_requests_per_day", 2000))
        ),
        sentinel_enabled=_env_bool(
            "MI_SENTINEL_ENABLED", bool(sync_doc.get("sentinel_enabled", True))
        ),
        max_pages=int(sync_doc.get("max_pages", 200)),
        request_retries=int(sync_doc.get("request_retries", 3)),
        http_timeout_s=float(sync_doc.get("http_timeout_s", 30.0)),
        fetch_start_date=str(sync_doc.get("fetch_start_date", "2020-01-01")),
    )

    schedule_cfg = ScheduleConfig(
        sentinel_interval_hours=_env_int(
            "MI_SENTINEL_INTERVAL_HOURS", int(schedule_doc.get("sentinel_interval_hours", 1))
        ),
        incremental_time=str(_env("MI_T1_TIME", schedule_doc.get("incremental_time", "04:30"))),
        correction_time=str(_env("MI_T2_TIME", schedule_doc.get("correction_time", "05:00"))),
        timezone=str(_env("MI_TZ", schedule_doc.get("timezone", "Asia/Shanghai"))),
    )

    goals = Goals(
        steps=_env_int("MI_GOAL_STEPS", int(goals_doc.get("steps", 8000))),
        sleep_min=_env_int("MI_GOAL_SLEEP_MIN", int(goals_doc.get("sleep_min", 480))),
    )

    server = ServerConfig(
        api_host=str(_env("MI_API_HOST", server_doc.get("api_host", "127.0.0.1"))),
        api_port=_env_int("MI_API_PORT", int(server_doc.get("api_port", 8321))),
        web_port=_env_int("MI_WEB_PORT", int(server_doc.get("web_port", 8322))),
        api_key=_env("MI_API_KEY", server_doc.get("api_key")) or None,
        serve_web=_env_bool("MI_SERVE_WEB", bool(server_doc.get("serve_web", True))),
        access_log_enabled=_env_bool(
            "MI_ACCESS_LOG_ENABLED", bool(server_doc.get("access_log_enabled", True))
        ),
        access_log_retention_days=_env_int(
            "MI_ACCESS_LOG_RETENTION_DAYS",
            int(server_doc.get("access_log_retention_days", 90)),
        ),
        trust_private=_env_bool(
            "MI_TRUST_PRIVATE", bool(server_doc.get("trust_private", True))
        ),
    )

    profile = Profile(
        age=_env_int("MI_AGE", 0) or None,
        max_hr=_env_int("MI_MAX_HR", 0) or None,
        weight_kg=_env_float("MI_WEIGHT_KG", None),
    )

    ai_cfg = AIConfig(
        enabled=_env_bool("MI_AI_ENABLED", bool(ai_doc.get("enabled", False))),
        protocol=str(_env("MI_AI_PROTOCOL", ai_doc.get("protocol", "openai_chat_completions"))),
        base_url=str(_env("MI_AI_BASE_URL", ai_doc.get("base_url", "https://integrate.api.nvidia.com/v1"))),
        api_key=str(_env("MI_AI_API_KEY", ai_doc.get("api_key", "")) or ""),
        model=str(_env("MI_AI_MODEL", ai_doc.get("model", "nvidia/nemotron-3-super-120b-a12b"))),
        timeout_s=_env_float("MI_AI_TIMEOUT_S", float(ai_doc.get("timeout_s", 120.0))) or 120.0,
        max_tokens=_env_int("MI_AI_MAX_TOKENS", int(ai_doc.get("max_tokens", 8000))),
        temperature=_env_float("MI_AI_TEMPERATURE", float(ai_doc.get("temperature", 0.3))) or 0.0,
        auto_after_sync=_env_bool(
            "MI_AI_AUTO_AFTER_SYNC", bool(ai_doc.get("auto_after_sync", True))
        ),
        auto_period_reports=_env_bool(
            "MI_AI_AUTO_PERIOD_REPORTS", bool(ai_doc.get("auto_period_reports", True))
        ),
        json_mode=_env_bool("MI_AI_JSON_MODE", bool(ai_doc.get("json_mode", True))),
        allow_private_base_url=_env_bool(
            "MI_AI_ALLOW_PRIVATE_BASE_URL", bool(ai_doc.get("allow_private_base_url", False))
        ),
        anthropic_version=str(
            _env("MI_AI_ANTHROPIC_VERSION", ai_doc.get("anthropic_version", "2023-06-01"))
        ),
        detail_level=str(_env("MI_AI_DETAIL_LEVEL", ai_doc.get("detail_level", "detailed"))),
        include_health_notes=_env_bool(
            "MI_AI_INCLUDE_HEALTH_NOTES", bool(ai_doc.get("include_health_notes", True))
        ),
        web_tools_enabled=_env_bool(
            "MI_AI_WEB_TOOLS_ENABLED", bool(ai_doc.get("web_tools_enabled", True))
        ),
        custom_instructions=str(
            _env("MI_AI_CUSTOM_INSTRUCTIONS", ai_doc.get("custom_instructions", "")) or ""
        ),
    )
    if ai_cfg.protocol not in AI_PROTOCOLS:
        raise ConfigError(f"AI 协议不支持：{ai_cfg.protocol}")
    if ai_cfg.detail_level not in AI_DETAIL_LEVELS:
        raise ConfigError(f"AI 解读详细度不支持：{ai_cfg.detail_level}")

    return Config(
        user_id=str(_env("MI_USER_ID", doc.get("user_id", "")) or ""),
        pass_token=str(_env("MI_PASS_TOKEN", doc.get("pass_token", "")) or ""),
        region=str(_env("MI_REGION", doc.get("region", "cn")) or "cn"),
        paths=paths,
        sync=sync_cfg,
        schedule=schedule_cfg,
        goals=goals,
        server=server,
        profile=profile,
        ai=ai_cfg,
        source_config=str(path),
    )

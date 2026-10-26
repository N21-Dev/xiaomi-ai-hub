"""AI 设置持久化、校验与运行时装配。

API Key 单独保存在数据目录的 JSON 文件中，不进入 health.db 和数据库备份。
设置页保存后会原子替换完整 AIConfig；正在运行的请求继续使用自己的配置快照，
后续请求和调度任务立即读取新配置。
"""

from __future__ import annotations

import ipaddress
import json
import os
import stat
from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import urlparse

from ..config import AIConfig, AI_DETAIL_LEVELS, AI_PROTOCOLS, ConfigError

DEFAULT_AI_SETTINGS_PATH = "/var/lib/mi-health-hub/ai-settings.json"
MAX_ALLOWED_MODE = 0o640
_ENDPOINT_SUFFIXES = ("/chat/completions", "/responses", "/messages")


class AISettingsError(ConfigError):
    """AI 设置缺失、非法或无法安全保存。"""


def _check_permissions(path: Path) -> None:
    if os.name == "nt":
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & ~MAX_ALLOWED_MODE:
        raise AISettingsError(
            f"AI 设置文件 {path} 权限为 {oct(mode)}，过宽；请调整为 640 或更严"
        )


def normalize_base_url(value: str, *, allow_private: bool = False) -> str:
    """规范化 API 根地址并阻止明显的 SSRF 目标。

    Base URL 的契约是版本根地址，不包含具体操作路径。私有地址默认禁止；确需接
    Ollama/vLLM 等内网服务时，必须显式开启 allow_private_base_url。
    """
    raw = (value or "").strip().rstrip("/")
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise AISettingsError("访问地址必须是完整的 http:// 或 https:// URL")
    if parsed.username or parsed.password:
        raise AISettingsError("访问地址不能包含用户名或密码")
    path = parsed.path.rstrip("/").lower()
    if any(path.endswith(suffix) for suffix in _ENDPOINT_SUFFIXES):
        raise AISettingsError("访问地址应填写 API 根地址，不要包含具体的接口路径")
    host = parsed.hostname.strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        if not allow_private:
            raise AISettingsError("本机或局域网地址默认禁用；如确需使用，请开启允许私有地址")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None
    if addr is not None:
        dangerous = addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_multicast
        if dangerous and not allow_private:
            raise AISettingsError("本机或局域网地址默认禁用；如确需使用，请开启允许私有地址")
        if addr.is_link_local or addr.is_multicast or addr.is_unspecified:
            raise AISettingsError("不允许访问链路本地、组播或未指定地址")
    if parsed.scheme == "http" and not allow_private:
        raise AISettingsError("公网 AI 服务必须使用 HTTPS；本地服务请显式开启允许私有地址")
    return raw


def validate_ai_config(config: AIConfig, *, strict_url: bool = True) -> AIConfig:
    """校验 AI 配置。

    strict_url 区分两类来源：
    - True：设置页等**外部输入**，必须过 SSRF 检查（公网强制 HTTPS、默认禁内网）。
    - False：启动时来自 YAML / 环境变量的**运维配置**。这类地址由服务器主人决定，
      若同样强制 SSRF 规则，运维配一个内网 Ollama 就会让服务启动即崩，所以只做
      范围校验，地址仅做规范化。
    """
    if config.protocol not in AI_PROTOCOLS:
        raise AISettingsError(f"不支持的上游格式：{config.protocol}")
    if config.detail_level not in AI_DETAIL_LEVELS:
        raise AISettingsError(f"不支持的解读详细度：{config.detail_level}")
    if not config.model.strip():
        raise AISettingsError("模型名称不能为空")
    if not (5 <= float(config.timeout_s) <= 600):
        raise AISettingsError("请求超时必须在 5～600 秒之间")
    if not (64 <= int(config.max_tokens) <= 128000):
        raise AISettingsError("最大输出 Token 必须在 64～128000 之间")
    if not (0 <= float(config.temperature) <= 2):
        raise AISettingsError("Temperature 必须在 0～2 之间")
    if len(config.custom_instructions) > 4000:
        raise AISettingsError("自定义提示词补充不能超过 4000 字")
    if strict_url:
        base_url = normalize_base_url(
            config.base_url, allow_private=config.allow_private_base_url
        )
    else:
        base_url = (config.base_url or "").strip().rstrip("/")
    return replace(
        config,
        base_url=base_url,
        model=config.model.strip(),
        anthropic_version=(config.anthropic_version or "2023-06-01").strip(),
        custom_instructions=config.custom_instructions.strip(),
    )


class AISettingsStore:
    """权限受限的 JSON 设置文件；不存在时保留环境变量/YAML 配置。"""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path or os.environ.get("MI_AI_SETTINGS_FILE") or DEFAULT_AI_SETTINGS_PATH)

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self, fallback: AIConfig) -> AIConfig:
        # 读取路径一律用宽松校验：内容要么是运维配置，要么是此前已通过严格校验
        # 保存进来的值。在此再做 SSRF 检查只会在换环境时把服务挡在启动之外。
        if not self.exists():
            return validate_ai_config(fallback, strict_url=False)
        _check_permissions(self.path)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AISettingsError(f"AI 设置文件无法读取：{type(exc).__name__}") from exc
        if not isinstance(data, dict):
            raise AISettingsError("AI 设置文件顶层必须是对象")
        allowed = set(asdict(fallback))
        values = {key: value for key, value in data.items() if key in allowed}
        return validate_ai_config(replace(fallback, **values), strict_url=False)

    def save(self, config: AIConfig, *, strict_url: bool = True) -> AIConfig:
        checked = validate_ai_config(config, strict_url=strict_url)
        payload = {
            **asdict(checked),
            "note": "包含 AI API Key，请勿提交、分享或放入公开备份。",
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.exists():
            _check_permissions(self.path)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if os.name != "nt":
            os.chmod(temp, 0o600)
        temp.replace(self.path)
        return checked

    @staticmethod
    def public_view(config: AIConfig, *, source: str) -> dict[str, object]:
        key = config.api_key
        masked = ""
        if key:
            masked = (key[:4] + "…" + key[-4:]) if len(key) > 10 else "已设置"
        return {
            "enabled": config.enabled,
            "protocol": config.protocol,
            "base_url": config.base_url,
            "model": config.model,
            "timeout_s": config.timeout_s,
            "max_tokens": config.max_tokens,
            "temperature": config.temperature,
            "auto_after_sync": config.auto_after_sync,
            "auto_period_reports": config.auto_period_reports,
            "json_mode": config.json_mode,
            "allow_private_base_url": config.allow_private_base_url,
            "anthropic_version": config.anthropic_version,
            "detail_level": config.detail_level,
            "include_health_notes": config.include_health_notes,
            "web_tools_enabled": config.web_tools_enabled,
            "custom_instructions": config.custom_instructions,
            "api_key_set": bool(key),
            "api_key_masked": masked,
            "configured": config.configured(),
            "source": source,
            "config_fingerprint": config.fingerprint(),
        }

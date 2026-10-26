from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .logging_setup import scrub


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class CursorState:
    last_timestamp_ms: int = 0
    last_request_id: str = ""


class StateStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "state.json"

    def load(self) -> CursorState:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return CursorState(
                last_timestamp_ms=int(data.get("last_timestamp_ms", 0)),
                last_request_id=str(data.get("last_request_id", "")),
            )
        except FileNotFoundError:
            return CursorState()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return CursorState()

    def save(self, state: CursorState) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(asdict(state), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


@dataclass
class RuntimeOverrides:
    c_user_id: str = ""
    service_token: str = ""
    account_device_id: str = ""
    mi_did: str = ""
    mi_device_name: str = ""
    mi_hardware: str = ""
    trigger_keywords: str = ""
    answer_max_chars: str = ""
    continuous_timeout_seconds: str = ""
    mute_xiaoai: str = ""


class RuntimeConfigStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "runtime-config.json"

    def load(self) -> RuntimeOverrides:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (
            FileNotFoundError,
            OSError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ):
            return RuntimeOverrides()
        return RuntimeOverrides(
            **{
                key: str(data.get(key) or "").strip()
                for key in RuntimeOverrides.__dataclass_fields__
            }
        )

    def save(self, overrides: RuntimeOverrides) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(asdict(overrides), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)


@dataclass
class RuntimeStatus:
    started_at: str = field(default_factory=utc_now)
    xiaomi_connected: bool = False
    device_selected: bool = False
    device_hardware: str = ""
    ai_configured: bool = False
    continuous_mode: bool = False
    audio_state: str = "idle"
    active_turn: bool = False
    last_ai_latency_ms: int | None = None
    last_turn_latency_ms: int | None = None
    cancelled_turns: int = 0
    native_pauses: int = 0
    tool_calls: int = 0
    tool_successes: int = 0
    last_poll_at: str | None = None
    last_processed_at: str | None = None
    last_error: str | None = None

    @property
    def ready(self) -> bool:
        return self.xiaomi_connected and self.device_selected and self.ai_configured

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["ready"] = self.ready
        if data.get("last_error"):
            data["last_error"] = scrub(data["last_error"])[:500]
        return data

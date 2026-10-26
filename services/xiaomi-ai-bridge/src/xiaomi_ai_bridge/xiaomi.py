from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import stat
import time
import uuid
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp
from miservice import MiAccount, MiNAService, MiTokenStore

from .config import Settings
from .state import RuntimeConfigStore, RuntimeOverrides

LOGGER = logging.getLogger(__name__)

ACCOUNT_LOGIN_URL = "https://account.xiaomi.com/pass/serviceLogin"
BROWSER_USER_AGENT = (
    "Dalvik/2.1.0 (Linux; U; Android 10; RMX2111 Build/QP1A.190711.020) "
    "APP/xiaomi.mico APPV/2004040 MK/Uk1YMjExMQ== "
    "PassportSDK/3.8.3 passport-ui/3.8.3"
)


class PassTokenAccount(MiAccount):
    """MiAccount variant that authenticates with userId + passToken only."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        user_id: str,
        pass_token: str,
        token_store: MiTokenStore,
        shared_credentials_path: Path | None,
        region: str,
        c_user_id: str,
        service_token: str,
        account_device_id: str,
        recovery_credentials_path: Path | None = None,
    ) -> None:
        super().__init__(session, user_id, "", token_store)
        self.user_id = user_id
        self.env_pass_token = pass_token
        self.shared_credentials_path = shared_credentials_path
        self.region = region
        self.c_user_id = c_user_id
        self.service_token = service_token
        self.account_device_id = account_device_id
        self.recovery_credentials_path = recovery_credentials_path
        self._stored_session_attempted = False
        self._used_service_tokens: dict[str, str] = {}
        self._rejected_service_tokens: set[str] = set()

    async def login(self, sid: str) -> bool:
        if sid not in {"micoapi", "xiaomiio"}:
            raise RuntimeError(f"Unsupported Xiaomi service: {sid}")

        stored = self.token_store.load_token() if self.token_store else None
        if (
            not isinstance(stored, dict)
            or str(stored.get("userId") or "") != self.user_id
        ):
            stored = {}
        # MiAccount clears self.token before invoking login again after an auth
        # rejection. Forget only the exact service session it just rejected;
        # retain passToken, other service sessions, and a concurrently refreshed
        # token file. A restart must not replay the known-rejected cached session.
        used = self._used_service_tokens.get(sid)
        if self.token is None and used:
            self._rejected_service_tokens.add(used)
            saved = stored.get(sid)
            if isinstance(saved, (list, tuple)) and len(saved) >= 2:
                if saved[1] == used:
                    stored.pop(sid, None)
                    if self.token_store:
                        self.token_store.save_token(stored)
                elif saved[1]:
                    self._stored_session_attempted = False
        if (
            not self._stored_session_attempted
            and stored
            and isinstance(stored.get(sid), (list, tuple))
            and len(stored[sid]) >= 2
            and stored[sid][1]
        ):
            self._stored_session_attempted = True
            self.token = stored
            self._used_service_tokens[sid] = str(stored[sid][1])
            return True

        if (
            self.service_token
            and sid == "micoapi"
            and self.service_token not in self._rejected_service_tokens
        ):
            token = dict(stored or {})
            token["deviceId"] = str(
                self.account_device_id
                or token.get("deviceId")
                or f"android_{uuid.uuid4()}"
            )
            token["userId"] = self.user_id
            token["passToken"] = self.env_pass_token
            token[sid] = ["", self.service_token]
            self.token = token
            if self.token_store:
                self.token_store.save_token(token)
            self._stored_session_attempted = True
            self._used_service_tokens[sid] = self.service_token
            return True

        shared = self._load_shared_credentials()
        recovery = (
            self._load_credentials(self.recovery_credentials_path)
            if sid == "micoapi"
            else {}
        )
        if recovery and not all(
            isinstance(recovery.get(name), str) and recovery[name].strip()
            for name in ("pass_token", "device_id", "c_user_id")
        ):
            # An unpaired token must not be combined with another login's device
            # identity. Keep the existing configured candidate chain instead.
            recovery = {}
        candidates: list[tuple[str, str, str]] = []
        # The local Xiaomi Home authorization can contain a newer rotated
        # passport credential. Exchange it for micoapi with its matching device
        # identity, never copy/reuse its xiaomiio serviceToken. The recovery file
        # stays read-only; refreshed credentials are saved to this client's file.
        for source in (recovery, shared):
            candidate = (
                str(source.get("pass_token") or ""),
                str(source.get("device_id") or self.account_device_id),
                str(source.get("c_user_id") or self.c_user_id),
            )
            if candidate[0] and candidate not in candidates:
                candidates.append(candidate)
        for pass_token in (str(stored.get("passToken") or ""), self.env_pass_token):
            candidate = (pass_token, self.account_device_id, self.c_user_id)
            if candidate[0] and candidate not in candidates:
                candidates.append(candidate)

        last_error = "passToken was rejected"
        for candidate, device_id, c_user_id in candidates:
            previous_device_id, previous_c_user_id = (
                self.account_device_id,
                self.c_user_id,
            )
            self.account_device_id, self.c_user_id = device_id, c_user_id
            try:
                token = await self._exchange(sid, candidate, stored or {})
            except Exception as exc:  # noqa: BLE001 - try the next token candidate
                self.account_device_id, self.c_user_id = (
                    previous_device_id,
                    previous_c_user_id,
                )
                last_error = str(exc)
                continue
            self.token = token
            self.env_pass_token = str(token["passToken"])
            self._stored_session_attempted = True
            self._used_service_tokens[sid] = str(token[sid][1])
            if self.token_store:
                self.token_store.save_token(token)
            self._save_shared_credentials(str(token["passToken"]))
            return True

        self.token = None
        LOGGER.warning("Xiaomi passToken login failed: %s", last_error)
        return False

    def _load_shared_credentials(self) -> dict[str, Any]:
        return self._load_credentials(self.shared_credentials_path)

    def _load_credentials(self, path: Path | None) -> dict[str, Any]:
        if path is None:
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (
            FileNotFoundError,
            OSError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ):
            return {}
        if not isinstance(data, dict) or str(data.get("user_id") or "") != self.user_id:
            return {}
        return data

    def _save_shared_credentials(self, pass_token: str) -> None:
        path = self.shared_credentials_path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        payload = self._load_shared_credentials()
        payload.update(
            {
                "user_id": self.user_id,
                "pass_token": pass_token,
                "region": self.region,
                "note": "Rotating Xiaomi credential. Do not commit or share.",
            }
        )
        if self.c_user_id:
            payload["c_user_id"] = self.c_user_id
        if self.account_device_id:
            payload["device_id"] = self.account_device_id
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if os.name != "nt":
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, path)

    async def _exchange(
        self, sid: str, pass_token: str, stored: dict[str, Any]
    ) -> dict[str, Any]:
        headers = {"User-Agent": BROWSER_USER_AGENT, "Accept": "*/*"}
        cookies = {
            "passToken": pass_token,
            "userId": self.user_id,
            "cUserId": self.c_user_id,
            "deviceId": str(
                self.account_device_id
                or stored.get("deviceId")
                or f"android_{uuid.uuid4()}"
            ),
        }
        headers["Cookie"] = "; ".join(
            f"{name}={value}" for name, value in cookies.items() if value
        )
        params = {
            "_json": "true",
            "sid": sid,
            "_locale": "zh_CN",
        }
        async with self.session.get(
            ACCOUNT_LOGIN_URL,
            params=params,
            headers=headers,
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"login returned HTTP {response.status}")
            text = await response.text()

        prefix = "&&&START&&&"
        if not text.startswith(prefix):
            raise RuntimeError("login returned an unexpected payload")
        payload = json.loads(text[len(prefix) :])
        if payload.get("code") not in {0, "0", None}:
            description = str(payload.get("description") or "credential rejected")
            raise RuntimeError(description)
        location = str(payload.get("location") or "")
        ssecurity = str(payload.get("ssecurity") or "")
        if not location:
            raise RuntimeError("login response is missing location")

        # Xiaomi rotates passToken before the service-token redirect completes.
        # Persist it immediately so a later failure cannot strand both services.
        rotated_pass_token = str(payload.get("passToken") or pass_token)
        if rotated_pass_token != pass_token:
            self.env_pass_token = rotated_pass_token
            self._save_shared_credentials(rotated_pass_token)

        service_location = self._service_location(
            location,
            str(payload.get("nonce") or ""),
            ssecurity,
            sid,
        )
        async with (
            aiohttp.ClientSession(
                headers={"User-Agent": BROWSER_USER_AGENT},
                cookie_jar=aiohttp.DummyCookieJar(),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as phase_two,
            phase_two.get(service_location, allow_redirects=False) as redirect,
        ):
            if redirect.status >= 400:
                raise RuntimeError(
                    f"service token exchange returned HTTP {redirect.status}"
                )
            service_token = self._read_service_token(redirect)
        if not service_token:
            raise RuntimeError("service token exchange returned no serviceToken")

        token = dict(stored)
        token["deviceId"] = str(
            self.account_device_id or token.get("deviceId") or f"android_{uuid.uuid4()}"
        )
        token["userId"] = str(payload.get("userId") or self.user_id)
        token["passToken"] = rotated_pass_token
        token[sid] = [ssecurity, service_token]
        return token

    @staticmethod
    def _service_location(
        location: str, nonce: str, ssecurity: str, sid: str = "micoapi"
    ) -> str:
        if sid == "xiaomiio" or "clientSign=" in location:
            return location
        source = f"nonce={nonce}&{ssecurity}"
        client_sign = base64.b64encode(hashlib.sha1(source.encode()).digest()).decode()
        separator = "&" if "?" in location else "?"
        return f"{location}{separator}clientSign={quote(client_sign)}"

    @staticmethod
    def _read_service_token(response: aiohttp.ClientResponse) -> str:
        direct = response.cookies.get("serviceToken")
        if direct and direct.value:
            return direct.value
        for header in response.headers.getall("Set-Cookie", []):
            parsed = SimpleCookie()
            parsed.load(header)
            value = parsed.get("serviceToken")
            if value and value.value:
                return value.value
        return ""


@dataclass(frozen=True)
class SpeakerDevice:
    device_id: str
    miot_did: str
    name: str
    hardware: str


@dataclass(frozen=True)
class ConversationRecord:
    request_id: str
    timestamp_ms: int
    query: str


class XiaomiClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.token_path = Path(settings.state_dir) / "mi-token.json"
        self.runtime_store = RuntimeConfigStore(settings.state_dir)
        self.overrides = self.runtime_store.load()
        self.session: aiohttp.ClientSession | None = None
        self.account: MiAccount | None = None
        self.mina: MiNAService | None = None
        self.device: SpeakerDevice | None = None

    def _effective(self, override_name: str, setting_name: str) -> str:
        return str(
            getattr(self.overrides, override_name)
            or getattr(self.settings, setting_name)
            or ""
        ).strip()

    def _seed_token_store(self) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict[str, Any] = {}
        try:
            existing = json.loads(self.token_path.read_text(encoding="utf-8"))
        except (
            FileNotFoundError,
            OSError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ):
            pass

        same_user = str(existing.get("userId", "")) == self.settings.mi_user_id
        token = existing if same_user else {}
        token["deviceId"] = str(
            self._effective("account_device_id", "mi_account_device_id")
            or token.get("deviceId")
            or f"android_{uuid.uuid4()}"
        )
        token["userId"] = self.settings.mi_user_id
        token["passToken"] = str(token.get("passToken") or self.settings.mi_pass_token)
        self.token_path.write_text(
            json.dumps(token, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if os.name != "nt":
            os.chmod(self.token_path, 0o600)

    async def connect(self) -> SpeakerDevice:
        await self.close()
        if not self.settings.mi_user_id or not self.settings.mi_pass_token:
            raise RuntimeError("尚未连接小米账号，请先在面板连接设置中登录")
        self.overrides = self.runtime_store.load()
        self._seed_token_store()
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        store = MiTokenStore(str(self.token_path))
        self.account = PassTokenAccount(
            self.session,
            self.settings.mi_user_id,
            self.settings.mi_pass_token,
            store,
            self.settings.mi_credentials_file,
            self.settings.mi_region,
            self._effective("c_user_id", "mi_c_user_id"),
            self._effective("service_token", "mi_service_token"),
            self._effective("account_device_id", "mi_account_device_id"),
            recovery_credentials_path=Path(self.settings.state_dir)
            / "mi-home-credentials.json",
        )
        if not await self.account.login("micoapi"):
            raise RuntimeError(
                "小米 micoapi 拒绝当前会话；请在连接页补充 cUserId，"
                "或填写有效 serviceToken 直接连接"
            )
        self.mina = MiNAService(self.account)
        devices = await self.mina.device_list() or []
        self.device = self._select_device(devices)
        return self.device

    def _select_device(self, devices: list[dict[str, Any]]) -> SpeakerDevice:
        speakers = [item for item in devices if item.get("deviceID")]
        candidates = speakers

        mi_did = self._effective("mi_did", "mi_did")
        mi_device_name = self._effective("mi_device_name", "mi_device_name")
        mi_hardware = self._effective("mi_hardware", "mi_hardware").upper()

        if mi_did:
            wanted = mi_did
            candidates = [
                item
                for item in candidates
                if wanted
                in {
                    str(item.get("miotDID", "")),
                    str(item.get("did", "")),
                    str(item.get("deviceID", "")),
                }
            ]
        if mi_device_name:
            wanted_name = mi_device_name.casefold()
            candidates = [
                item
                for item in candidates
                if wanted_name in str(item.get("name", "")).casefold()
            ]
        if mi_hardware:
            candidates = [
                item
                for item in candidates
                if str(item.get("hardware", "")).upper() == mi_hardware
            ]

        if len(candidates) != 1:
            summary = ", ".join(
                f"{item.get('name', 'unnamed')}[{item.get('hardware', '?')}]"
                for item in speakers
            )
            if not speakers:
                raise RuntimeError("No Xiaomi speakers were returned by the account")
            raise RuntimeError(
                "Device selection is ambiguous. Set MI_DID, MI_DEVICE_NAME, or "
                f"MI_HARDWARE. Available speakers: {summary}"
            )

        selected = candidates[0]
        return SpeakerDevice(
            device_id=str(selected["deviceID"]),
            miot_did=str(selected.get("miotDID") or selected.get("did") or ""),
            name=str(selected.get("name") or "Xiaomi speaker"),
            hardware=str(selected.get("hardware") or "").upper(),
        )

    def public_config(self) -> dict[str, Any]:
        self.overrides = self.runtime_store.load()
        shared: dict[str, Any] = {}
        path = self.settings.mi_credentials_file
        if path is not None:
            try:
                shared = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
                pass
        user_id = str(shared.get("user_id") or self.settings.mi_user_id)
        masked_user = (
            user_id[:2] + "••••" + user_id[-2:] if len(user_id) > 4 else "已设置"
        )
        return {
            "user_id_masked": masked_user,
            "pass_token_set": bool(
                shared.get("pass_token") or self.settings.mi_pass_token
            ),
            "c_user_id_set": bool(self._effective("c_user_id", "mi_c_user_id")),
            "service_token_set": bool(
                self._effective("service_token", "mi_service_token")
            ),
            "account_device_id_set": bool(
                self._effective("account_device_id", "mi_account_device_id")
            ),
            "mi_did": self._effective("mi_did", "mi_did"),
            "mi_device_name": self._effective("mi_device_name", "mi_device_name"),
            "mi_hardware": self._effective("mi_hardware", "mi_hardware").upper(),
        }

    def update_connection_config(self, data: dict[str, Any]) -> dict[str, Any]:
        current = self.runtime_store.load()
        values = {
            key: str(data.get(key) or "").strip()
            for key in RuntimeOverrides.__dataclass_fields__
        }
        for key, value in values.items():
            if value:
                setattr(current, key, value)
        for key in ("service_token", "c_user_id", "account_device_id"):
            if data.get(f"clear_{key}") is True:
                setattr(current, key, "")
        if current.mi_hardware:
            current.mi_hardware = current.mi_hardware.upper()
        self.runtime_store.save(current)
        self.overrides = current

        pass_token = str(data.get("pass_token") or "").strip()
        if pass_token:
            if not (8 <= len(pass_token) <= 4096):
                raise ValueError("passToken 长度不正确")
            path = self.settings.mi_credentials_file
            if path is None:
                raise ValueError("MI_CREDENTIALS_FILE 未配置")
            user_id = str(data.get("user_id") or self.settings.mi_user_id).strip()
            if not user_id.isdigit():
                raise ValueError("小米账号 ID 必须是数字")
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "user_id": user_id,
                        "pass_token": pass_token,
                        "region": self.settings.mi_region,
                        "note": "Shared rotating Xiaomi credential. Do not commit or share.",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        return self.public_config()

    async def latest_questions(self) -> list[ConversationRecord]:
        if not self.session or not self.account or not self.device:
            raise RuntimeError("Xiaomi client is not connected")
        token = self.account.token
        if not token or "micoapi" not in token:
            raise RuntimeError("Xiaomi micoapi session is missing")

        url = "https://userprofile.mina.mi.com/device_profile/v2/conversation"
        params = {
            "source": "dialogu",
            "hardware": self.device.hardware,
            "timestamp": str(int(time.time() * 1000)),
            "limit": "5",
        }
        cookies = {
            "deviceId": self.device.device_id,
            "serviceToken": token["micoapi"][1],
            "userId": token["userId"],
        }
        headers = {"User-Agent": "MiHome/6.0.103 MICO/iOSApp/appStore/6.0.103"}
        async with self.session.get(
            url, params=params, cookies=cookies, headers=headers
        ) as response:
            if response.status in {401, 403}:
                raise RuntimeError("Xiaomi conversation session expired")
            if response.status >= 400:
                raise RuntimeError(
                    f"Xiaomi conversation API returned HTTP {response.status}"
                )
            payload = await response.json(content_type=None)

        if payload.get("code") not in {0, None}:
            raise RuntimeError(
                f"Xiaomi conversation API returned code {payload.get('code')}"
            )
        data = payload.get("data", {})
        if isinstance(data, str):
            data = json.loads(data)
        return self.parse_conversations(data)

    @staticmethod
    def parse_conversations(data: Any) -> list[ConversationRecord]:
        if not isinstance(data, dict):
            return []
        records: list[ConversationRecord] = []
        for item in data.get("records", []):
            if not isinstance(item, dict):
                continue
            query = str(item.get("query") or "").strip()
            timestamp = int(item.get("time") or item.get("timestamp") or 0)
            if not query or timestamp <= 0:
                continue
            records.append(
                ConversationRecord(
                    request_id=str(
                        item.get("requestId") or item.get("request_id") or ""
                    ),
                    timestamp_ms=timestamp,
                    query=query,
                )
            )
        return sorted(records, key=lambda record: record.timestamp_ms)

    async def is_playing(self) -> bool:
        if not self.mina or not self.device:
            return False
        try:
            result = await self.mina.player_get_status(self.device.device_id)
            data = result.get("data", {}) if isinstance(result, dict) else {}
            info = data.get("info", "{}") if isinstance(data, dict) else "{}"
            parsed = json.loads(info) if isinstance(info, str) else info
            return isinstance(parsed, dict) and int(parsed.get("status", -1)) == 1
        except (TypeError, ValueError, json.JSONDecodeError):
            return True
        except Exception as exc:  # noqa: BLE001 - status check is best effort
            LOGGER.debug(
                "Unable to read XiaoAI playback status: %s", type(exc).__name__
            )
            return True

    async def suppress_native_response(self, *, force: bool = True) -> bool:
        if not self.mina or not self.device:
            return False
        try:
            if not force and not await self.is_playing():
                return False
            await self.mina.player_pause(self.device.device_id)
            return True
        except Exception as exc:  # noqa: BLE001 - stopping audio is best effort
            LOGGER.warning("Unable to pause XiaoAI audio: %s", type(exc).__name__)
            return False

    async def stop_current_audio(self) -> None:
        await self.suppress_native_response(force=True)

    async def speak(self, text: str) -> None:
        if not self.mina or not self.device:
            raise RuntimeError("Xiaomi client is not connected")
        result = await self.mina.text_to_speech(self.device.device_id, text)
        if not result:
            raise RuntimeError("Xiaomi TTS returned an empty result")

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
        self.session = None
        self.account = None
        self.mina = None
        self.device = None

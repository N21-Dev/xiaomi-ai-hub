"""Read-only Mi Home family/room topology, projected onto known device keys."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from .miot_tools import MiotToolManager, _secure_replace
from .state import utc_now


class HomeLayout:
    def __init__(self, home: MiotToolManager):
        self.home = home
        self.path = Path(home.settings.state_dir) / "home-layout.json"
        self.owner = hashlib.sha256(home.settings.mi_user_id.encode()).hexdigest()
        self.snapshot: dict[str, Any] = {
            "available": False,
            "message": "尚未同步米家家庭和房间",
            "homes": [],
            "assignments": {},
            "updated_at": None,
        }
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if saved.get("owner") == self.owner and isinstance(
                saved.get("snapshot"), dict
            ):
                self.snapshot = saved["snapshot"]
                self.snapshot["stale"] = True
        except (OSError, ValueError, TypeError):
            pass
        self._attempted_at = 0.0
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(kind: str, *parts: object) -> str:
        raw = json.dumps([kind, *parts], ensure_ascii=False).encode()
        return kind + "_" + hashlib.sha256(raw).hexdigest()[:16]

    def public(self) -> dict[str, Any]:
        result = deepcopy(self.snapshot)
        keys = set(self.home.devices)
        result["assignments"] = {
            key: value
            for key, value in result.get("assignments", {}).items()
            if key in keys
        }
        for home in result.get("homes", []):
            for room in home.get("rooms", []):
                room["device_keys"] = [
                    key for key in room.get("device_keys", []) if key in keys
                ]
        return result

    def _parse(self, result: object) -> dict[str, Any]:
        if not isinstance(result, dict) or not isinstance(result.get("homelist"), list):
            raise TypeError("米家未返回家庭列表")
        by_did = {device.did: device.key for device in self.home.devices.values()}
        homes, assignments = [], {}
        for raw_home in result["homelist"]:
            if not isinstance(raw_home, dict) or raw_home.get("id") is None:
                continue
            home_key = self._key("home", raw_home["id"])
            home_name = str(raw_home.get("name") or "未命名家庭")[:80]
            rooms = []
            used: set[str] = set()
            for raw_room in raw_home.get("roomlist") or []:
                if not isinstance(raw_room, dict) or raw_room.get("id") is None:
                    continue
                room_key = self._key("room", raw_home["id"], raw_room["id"])
                room_name = str(raw_room.get("name") or "未命名房间")[:80]
                device_keys = list(
                    dict.fromkeys(
                        by_did[str(did)]
                        for did in raw_room.get("dids") or []
                        if str(did) in by_did
                    )
                )
                used.update(device_keys)
                rooms.append(
                    {"key": room_key, "name": room_name, "device_keys": device_keys}
                )
                for key in device_keys:
                    assignments.setdefault(
                        key,
                        {
                            "home_key": home_key,
                            "home_name": home_name,
                            "room_key": room_key,
                            "room_name": room_name,
                        },
                    )
            unassigned = list(
                dict.fromkeys(
                    by_did[str(did)]
                    for did in raw_home.get("dids") or []
                    if str(did) in by_did and by_did[str(did)] not in used
                )
            )
            if unassigned:
                room_key = self._key("room", raw_home["id"], "unassigned")
                rooms.append(
                    {"key": room_key, "name": "未分配房间", "device_keys": unassigned}
                )
                for key in unassigned:
                    assignments.setdefault(
                        key,
                        {
                            "home_key": home_key,
                            "home_name": home_name,
                            "room_key": room_key,
                            "room_name": "未分配房间",
                        },
                    )
            homes.append({"key": home_key, "name": home_name, "rooms": rooms})
        partial = bool(result.get("has_more"))
        return {
            "available": True,
            "stale": False,
            "partial": partial,
            "message": "已同步前 300 个家庭，列表尚未完整"
            if partial
            else "已同步米家家庭和房间",
            "homes": homes,
            "assignments": assignments,
            "updated_at": utc_now(),
        }

    async def describe(self, refresh: bool = False) -> dict[str, Any]:
        async with self._lock:
            age = time.monotonic() - self._attempted_at
            if self._attempted_at and age < (15 if refresh else 300):
                return self.public()
            self._attempted_at = time.monotonic()
            try:
                async with asyncio.timeout(20), self.home._lock:
                    service = await self.home._connect()
                    result = await service.miio_request(
                        "/v2/homeroom/gethome_merged",
                        {
                            "fg": True,
                            "fetch_share": True,
                            "fetch_share_dev": True,
                            "fetch_cariot": True,
                            "limit": 300,
                            "app_ver": 7,
                            "plat_form": 0,
                        },
                    )
                self.snapshot = self._parse(result)
                _secure_replace(
                    self.path, {"owner": self.owner, "snapshot": self.snapshot}
                )
            except Exception:  # noqa: BLE001 - private cloud replies stay server-side
                self.snapshot.update(
                    available=False,
                    stale=True,
                    message="米家家庭暂时无法同步，保留已有分组；设备控制仍可单独使用",
                )
            return self.public()

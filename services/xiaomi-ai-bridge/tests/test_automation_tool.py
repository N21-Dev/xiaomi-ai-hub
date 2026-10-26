from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from xiaomi_ai_bridge.automation import AutomationStore
from xiaomi_ai_bridge.bridge import Bridge
from xiaomi_ai_bridge.infrared import InfraredAdapter
from xiaomi_ai_bridge.kelvinator_ir import protocol_template
from xiaomi_ai_bridge.miot_tools import MiotToolManager, DeviceRecord, PropertySpec


class _ToolBridge:
    def __init__(self, state_dir: Path, targets: list[dict[str, object]]) -> None:
        self.automations = AutomationStore(state_dir)
        self._targets = targets
        self.home = object.__new__(MiotToolManager)
        self.home.settings = SimpleNamespace(state_dir=state_dir, mi_user_id="synthetic-user")
        self.home.devices = {}
        for target in targets:
            self.home.devices[target["device_key"]] = DeviceRecord(
                key=target["device_key"], did="synthetic-did", name=target["device"], alias=target["device"],
                model="chuangmi.ir.v2" if target["kind"] == "infrared_ac" else "synthetic.light",
                category="ir-remote-control" if target["kind"] == "infrared_ac" else "light",
                enabled=True, properties={"on": PropertySpec(2, 1, "on", "bool", True, True)},
            )
        self.infrared = InfraredAdapter(self.home)
        self.infrared._ac_template = Mock(return_value=(protocol_template(), {"ai_control_enabled": True}, {}))

    def _automation_targets(self) -> list[dict[str, object]]:
        return self._targets


class AutomationToolTests(unittest.TestCase):
    def test_definitions_and_xiaomi_create(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = _ToolBridge(
                Path(directory),
                [{
                    "kind": "xiaomi", "device": "客厅灯", "device_key": "lamp-1",
                    "actions": ["turn_on"], "room": "客厅",
                }],
            )
            definitions = Bridge.automation_tool_definitions(bridge)
            self.assertEqual(definitions[0]["function"]["name"], "manage_automations")
            result = asyncio.run(Bridge.execute_automation_tool(bridge, {
                "operation": "create", "device": "客厅灯", "action": "turn_on",
                "schedule": {"type": "delay", "delay_seconds": 60},
            }))
            self.assertTrue(result["ok"])
            self.assertEqual(result["task"]["target"]["device_key"], "lamp-1")

    def test_infrared_create_requires_profile_and_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = _ToolBridge(
                Path(directory),
                [{
                    "kind": "infrared_ac", "device": "客厅空调", "device_key": "ac-1",
                    "profile_key": "kelvinator:ac", "actions": ["set_settings"],
                }],
            )
            missing_profile = asyncio.run(Bridge.execute_automation_tool(bridge, {
                "operation": "create", "device": "客厅空调", "action": "set_settings",
                "settings": {}, "schedule": {"type": "delay", "delay_seconds": 60},
            }))
            self.assertFalse(missing_profile["ok"])
            self.assertIn("档案", missing_profile["message"])
            created = asyncio.run(Bridge.execute_automation_tool(bridge, {
                "operation": "create", "device": "客厅空调",
                "profile_key": "kelvinator:ac", "action": "set_settings",
                "settings": {"power": True},
                "schedule": {"type": "delay", "delay_seconds": 60},
            }))
            self.assertTrue(created["ok"])
            self.assertEqual(created["task"]["value"], {"power": True})


if __name__ == "__main__":
    unittest.main()

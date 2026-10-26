"""音箱动作要真的暴露给 AI 工具。

用户实测过：问「让小爱同学说肖煜宁」，模型回答「只获得了查询状态的权限」——
因为工具的 action 枚举里只有 MIoT 动作，根本没有 speaker_*。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from xiaomi_ai_bridge import speaker_control
from xiaomi_ai_bridge.miot_tools import (
    SPEAKER_ACTIONS,
    SPEAKER_MODEL_PREFIX,
    DeviceRecord,
    MiotToolManager,
)
from xiaomi_ai_bridge.speaker_control import SpeakerController


def speaker(key: str = "xiaomi-x1", alias: str = "小爱同学") -> DeviceRecord:
    return DeviceRecord(
        key=key,
        did="did-" + key,
        name="Xiaomi Sound",
        alias=alias,
        model=SPEAKER_MODEL_PREFIX + "x6a",
        category="xiaomi.wifispeaker.x6a",
        enabled=True,
        cloud_online=True,
        properties={},
    )


def light() -> DeviceRecord:
    return DeviceRecord(
        key="light-1",
        did="did-light",
        name="Bedside",
        alias="床头灯",
        model="yeelink.light.bslamp2",
        category="light",
        enabled=True,
        cloud_online=True,
        properties={},
    )


class SpeakerDefinitionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manager = MiotToolManager(
            SimpleNamespace(state_dir=Path(self.directory.name), home_state_poll_seconds=60)
        )

    def definitions(self) -> dict:
        tools = self.manager.tool_definitions()
        self.assertEqual(len(tools), 1)
        return tools[0]["function"]

    def test_actions_match_speaker_controller(self):
        """两处清单必须一致，否则改了一边另一边会悄悄失效。"""
        self.assertEqual(tuple(SPEAKER_ACTIONS), tuple(SpeakerController.ACTIONS))

    def test_speaker_actions_reach_the_model(self):
        self.manager.devices = {"xiaomi-x1": speaker()}
        function = self.definitions()
        enum = function["parameters"]["properties"]["action"]["enum"]
        self.assertIn("speaker_tts", enum, "模型必须能看到播报动作")
        self.assertIn("speaker_volume", enum)
        self.assertIn("speaker_tts", function["description"], "描述里要写清怎么用")
        self.assertIn("小爱同学", function["description"])

    def test_plain_devices_have_no_speaker_actions(self):
        self.manager.devices = {"light-1": light()}
        enum = self.definitions()["parameters"]["properties"]["action"]["enum"]
        self.assertNotIn("speaker_tts", enum)


class SpeakerDispatchTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manager = MiotToolManager(
            SimpleNamespace(state_dir=Path(self.directory.name), home_state_poll_seconds=60)
        )
        self.calls = []

    async def handler(self, device, action, value):
        self.calls.append((device.key, action, value))
        return {"ok": True, "outcome": "confirmed", "message": "已播报"}

    async def test_tts_is_routed_to_speaker_controller(self):
        self.manager.devices = {"xiaomi-x1": speaker()}
        self.manager.extension_handler = self.handler
        outcome = await self.manager.execute(
            self.manager.tool_definitions()[0]["function"]["name"],
            {"device": "小爱同学", "action": "speaker_tts", "value": "肖煜宁"},
        )
        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(self.calls, [("xiaomi-x1", "speaker_tts", "肖煜宁")])

    async def test_unknown_speaker_action_is_rejected(self):
        self.manager.devices = {"xiaomi-x1": speaker()}
        self.manager.extension_handler = self.handler
        outcome = await self.manager.execute(
            self.manager.tool_definitions()[0]["function"]["name"],
            {"device": "小爱同学", "action": "speaker_format_disk", "value": ""},
        )
        self.assertFalse(outcome["ok"])
        self.assertIn("不支持", outcome.get("error", ""))
        self.assertEqual(self.calls, [])

    async def test_speaker_action_on_non_speaker_is_rejected(self):
        self.manager.devices = {"light-1": light()}
        self.manager.extension_handler = self.handler
        outcome = await self.manager.execute(
            "home_devices", {"device": "床头灯", "action": "speaker_tts", "value": "hi"}
        )
        self.assertFalse(outcome["ok"])
        self.assertEqual(self.calls, [])

    async def test_missing_handler_reports_clearly(self):
        self.manager.devices = {"xiaomi-x1": speaker()}
        outcome = await self.manager.execute(
            self.manager.tool_definitions()[0]["function"]["name"],
            {"device": "小爱同学", "action": "speaker_tts", "value": "hi"},
        )
        self.assertFalse(outcome["ok"])
        self.assertIn("未就绪", outcome.get("error", ""))


class CoerceTest(unittest.TestCase):
    def test_volume_string_becomes_int(self):
        self.assertEqual(SpeakerController.coerce("speaker_volume", "30"), 30)
        self.assertEqual(SpeakerController.coerce("speaker_loop", "1"), 1)

    def test_text_is_untouched(self):
        self.assertEqual(SpeakerController.coerce("speaker_tts", "你好"), "你好")

    def test_non_numeric_stays_for_validate_to_reject(self):
        self.assertEqual(SpeakerController.coerce("speaker_volume", "三十"), "三十")

    def test_validate_accepts_coerced_volume(self):
        self.assertEqual(speaker_control.SpeakerController.validate("speaker_volume", 30), 30)


if __name__ == "__main__":
    unittest.main()

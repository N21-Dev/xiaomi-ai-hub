# SPDX-License-Identifier: LGPL-2.1-or-later
# See THIRD_PARTY_NOTICES.md for protocol reference attribution.
"""Offline Kelvinator-family AC codec; never accepts arbitrary RPC commands.

Frame layout/checksums: IRremoteESP8266 ir_Kelvinator.{h,cpp}.
Chuangmi A567 timing container: python-miio chuangmi_ir.py.
Templates come from validated learned frames or the documented protocol reset.
Unmapped timer/sleep-curve bits are preserved instead of guessed.
"""
from __future__ import annotations

import base64
import binascii
import struct
from typing import Any

MODE_OPTIONS = [("auto", "自动"), ("cool", "制冷"), ("dry", "除湿"), ("fan", "送风"), ("heat", "制热")]
SWING_OPTIONS = [(0, "停止"), (1, "全范围自动"), (2, "最高位置"), (3, "偏上位置"),
                 (4, "中间位置"), (5, "偏下位置"), (6, "最低位置"), (7, "下部自动"),
                 (9, "中部自动"), (11, "上部自动")]
BOOL_FIELDS = ("power", "swing_horizontal", "turbo", "quiet", "light", "ion_filter", "xfan")
# The protocol documents basic sleep flags but not the full custom sleep curve.
SETTING_SCHEMA: dict[str, dict[str, Any]] = {
    **{key: {"type": "boolean"} for key in BOOL_FIELDS},
    "mode": {"type": "string", "enum": [value for value, _ in MODE_OPTIONS]},
    "temperature": {"type": "integer", "minimum": 16, "maximum": 30},
    "fan": {"type": "integer", "minimum": 0, "maximum": 5},
    "swing_vertical": {"type": "integer", "enum": [value for value, _ in SWING_OPTIONS]},
}


def checksum(block: bytes | bytearray) -> int:
    return (10 + sum(value & 15 for value in block[:4]) + sum(value >> 4 for value in block[4:7])) & 15


def validate_state(state: bytes | bytearray) -> None:
    if len(state) != 16 or state[8:11] != state[:3]:
        raise ValueError("空调信号的双帧结构不匹配")
    if state[3] & 0xC0 != 0x40 or state[11] & 0xC0 != 0x40:
        raise ValueError("空调信号的帧标识不匹配")
    for offset in (0, 8):
        if state[offset + 7] >> 4 != checksum(state[offset:offset + 8]):
            raise ValueError("空调信号校验码不正确")
    if state[0] & 7 > 4 or state[1] & 15 > 14 or (state[14] >> 4) & 7 > 5:
        raise ValueError("空调信号包含尚未支持的设置值")
    if state[4] & 15 not in {value for value, _ in SWING_OPTIONS}:
        raise ValueError("空调信号包含尚未支持的扫风位置")


def protocol_template() -> bytes:
    """Documented Kelvinator reset, cool/25°C/off; no learned code required.

    Timer and sleep fields stay clear. This is a default command state, never a
    reading of the appliance or a claim that its brand/model has been verified.
    """
    state = bytearray(16)
    state[0:4] = bytes([1, 9, 0x20, 0x50])
    state[8:12] = bytes([1, 9, 0x20, 0x70])
    for offset in (0, 8):
        state[offset + 7] = checksum(state[offset:offset + 8]) << 4
    validate_state(state)
    return bytes(state)


def _near(value: int, expected: int) -> bool:
    return expected * 0.75 <= value <= expected * 1.25


def decode_learned(code: str) -> bytes:
    """Decode exactly one full 128-bit Kelvinator transmission, not two Gree repeats."""
    if not isinstance(code, str) or len(code) > 8192:
        raise ValueError("学习码格式不支持")
    try:
        raw = base64.b64decode(code, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("学习码不是有效编码") from exc
    if len(raw) != 208 or struct.unpack_from("<HH", raw) != (0xA567, 279):
        raise ValueError("学习码不是完整的 Kelvinator 128 位信号")
    table = struct.unpack_from("<16I", raw, 4)
    pairs = [(table[value & 15], table[value >> 4]) for value in raw[68:]]
    state = bytearray()
    for offset in (0, 70):
        if not (_near(pairs[offset][0], 9010) and _near(pairs[offset][1], 4505)):
            raise ValueError("空调信号引导脉冲不匹配")
        bits = []
        for n in [*range(offset + 1, offset + 36), *range(offset + 37, offset + 69)]:
            mark, gap = pairs[n]
            if not _near(mark, 680) or not (_near(gap, 510) or _near(gap, 1530)):
                raise ValueError("空调信号时序不匹配")
            bits.append(int(_near(gap, 1530)))
        if bits[32:35] != [0, 1, 0]:
            raise ValueError("空调信号分段标识不匹配")
        if not (_near(pairs[offset + 36][0], 680) and _near(pairs[offset + 36][1], 19975)):
            raise ValueError("空调信号分段间隔不匹配")
        if not _near(pairs[offset + 69][0], 680):
            raise ValueError("空调信号结束脉冲不匹配")
        if offset == 0 and not _near(pairs[69][1], 39950):
            raise ValueError("空调信号双帧间隔不匹配")
        bits = bits[:32] + bits[35:]
        state.extend(sum(bit << i for i, bit in enumerate(bits[n:n + 8])) for n in range(0, 64, 8))
    validate_state(state)
    return bytes(state)


def settings_from_state(state: bytes | bytearray) -> dict[str, Any]:
    validate_state(state)
    return {
        "power": bool(state[0] & 8), "mode": MODE_OPTIONS[state[0] & 7][0],
        "temperature": 16 + (state[1] & 15), "fan": (state[14] >> 4) & 7,
        "swing_vertical": state[4] & 15, "swing_horizontal": bool(state[4] & 16),
        "turbo": bool(state[2] & 16), "light": bool(state[2] & 32),
        "ion_filter": bool(state[2] & 64), "xfan": bool(state[2] & 128),
        "quiet": bool(state[12] & 128),
    }


def apply_settings(template: bytes, patch: object) -> bytes:
    validate_state(template)
    if not isinstance(patch, dict) or not patch or set(patch) - SETTING_SCHEMA.keys():
        raise ValueError("空调设置包含未知字段或为空")
    for key, value in patch.items():
        rule = SETTING_SCHEMA[key]
        expected = bool if rule["type"] == "boolean" else int if rule["type"] == "integer" else str
        if type(value) is not expected:
            raise ValueError(f"空调设置 {key} 的类型无效")
        if "enum" in rule and value not in rule["enum"]:
            raise ValueError(f"空调设置 {key} 不在支持范围内")
        if "minimum" in rule and not rule["minimum"] <= value <= rule["maximum"]:
            raise ValueError(f"空调设置 {key} 超出支持范围")
    current = settings_from_state(template)
    new = {**current, **patch}
    # Auto/dry have a fixed 25°C setpoint in this protocol.
    if new["mode"] in {"auto", "dry"}:
        new["temperature"] = 25
    if new["mode"] not in {"cool", "dry"}:
        new["xfan"] = False
    if patch.get("turbo") is True and patch.get("quiet") is True:
        raise ValueError("强劲模式和静音模式不能同时开启，请选择其中一种或使用普通风速")
    # Explicit special-mode intent wins over the fan value in a full snapshot.
    # A fan-only command exits both modes, even when selecting the same speed.
    if patch.get("turbo") is True:
        new["quiet"] = False
    elif patch.get("quiet") is True:
        new["turbo"] = False
    elif "fan" in patch or new["turbo"] and new["quiet"]:
        new["turbo"] = new["quiet"] = False
    state = bytearray(template)
    state[0] = (state[0] & 0x80) | [value for value, _ in MODE_OPTIONS].index(new["mode"])
    state[0] |= int(new["power"]) << 3 | min(new["fan"], 3) << 4
    auto_swing = new["swing_vertical"] in {1, 7, 9, 11} or new["swing_horizontal"]
    state[0] |= int(auto_swing) << 6
    state[1] = (state[1] & 0xF0) | (new["temperature"] - 16)
    for key, bit in [("turbo", 4), ("light", 5), ("ion_filter", 6), ("xfan", 7)]:
        state[2] = (state[2] & ~(1 << bit)) | int(new[key]) << bit
    state[4] = (state[4] & 0xE0) | new["swing_vertical"] | int(new["swing_horizontal"]) << 4
    state[12] = (state[12] & 0x7F) | int(new["quiet"]) << 7
    state[14] = (state[14] & 0x8F) | new["fan"] << 4
    state[8:11] = state[:3]
    for offset in (0, 8):
        state[offset + 7] = (state[offset + 7] & 15) | checksum(state[offset:offset + 8]) << 4
    validate_state(state)
    return bytes(state)


def encode_state(state: bytes) -> str:
    """Create a Chuangmi timing container using documented nominal timings."""
    validate_state(state)
    # Dictionary indices: mark, zero, one, header mark/space, short/long gap.
    table = [680, 510, 1530, 9010, 4505, 19975, 39950] + [0] * 9
    packed = bytearray()
    for offset in (0, 8):
        packed.append(4 << 4 | 3)
        for index, value in enumerate(state[offset:offset + 8]):
            for bit in range(8):
                packed.append((2 if value & (1 << bit) else 1) << 4)
            if index == 3:
                packed.extend([1 << 4, 2 << 4, 1 << 4, 5 << 4])
        packed.append(6 << 4 if offset == 0 else 0)
    assert len(packed) == 140
    raw = struct.pack("<HH16I", 0xA567, 279, *table) + packed
    return base64.b64encode(raw).decode("ascii")

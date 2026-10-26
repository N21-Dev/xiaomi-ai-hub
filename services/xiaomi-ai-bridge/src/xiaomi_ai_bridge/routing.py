from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class Action(StrEnum):
    IGNORE = "ignore"
    ASK = "ask"
    ENTER_CONTINUOUS = "enter_continuous"
    EXIT_CONTINUOUS = "exit_continuous"


@dataclass(frozen=True)
class RouteResult:
    action: Action
    prompt: str = ""


_LEADING_WAKE_WORD = re.compile(r"^小爱同学[，,。.!！?？\s]*")
_LEADING_PUNCTUATION = re.compile(r"^[，,。.!！?？:：;；\s]+")
_REASONING_BLOCK = re.compile(
    r"<(think|analysis|reasoning)(?:\s[^>]*)?>.*?</\1\s*>",
    flags=re.IGNORECASE | re.DOTALL,
)
_REASONING_BRACKET_BLOCK = re.compile(
    r"\[(think|analysis|reasoning)] .*? \[/\1]",
    flags=re.IGNORECASE | re.DOTALL | re.VERBOSE,
)
_UNCLOSED_REASONING = re.compile(
    r"<(think|analysis|reasoning)(?:\s[^>]*)?>.*$",
    flags=re.IGNORECASE | re.DOTALL,
)
_STRAY_REASONING_CLOSE = re.compile(
    r"</(?:think|analysis|reasoning)\s*>", flags=re.IGNORECASE
)
_DEVICE_TERMS = {
    "light": ("台灯", "灯"),
    "outlet": ("插座",),
    "air-conditioner": ("空调",),
    "fan": ("风扇",),
    "air-purifier": ("空气净化器", "净化器"),
}


def home_target_clarification(
    prompt: str,
    selected: str,
    devices: Sequence[dict[str, str]],
    history: Sequence[dict[str, str]] = (),
) -> str:
    """Fail closed on a guessed target; this is not a general intent parser."""
    def normalize(value: str) -> str:
        return re.sub(r"[\s的]", "", value).casefold()

    requested = normalize(prompt)
    target = next((item for item in devices if item["alias"] == selected), None)
    if target is None:
        return ""  # The executor handles invalid or unauthorized aliases.
    terms = _DEVICE_TERMS.get(target["category"], ())
    explicit = [
        item for item in devices
        if normalize(item["alias"]) in requested
        and normalize(item["alias"]) not in terms
    ]
    explicit = [
        item for item in explicit
        if not any(
            item["alias"] != other["alias"]
            and normalize(item["alias"]) in normalize(other["alias"])
            for other in explicit
        )
    ]
    if any(item["alias"] == selected for item in explicit):
        return ""
    candidates = [item for item in devices if item["category"] == target["category"]]
    if not any(term in requested for term in terms):
        if explicit:
            options = "、".join(item["alias"] for item in explicit)
            return f"请确认目标是{options}吗？这次没有操作其他设备。"
        if not any(word in requested for word in ("它", "这个", "那个")):
            return ""
        candidates = list(devices)
        previous = next((item["content"] for item in reversed(history) if item.get("role") == "user"), "")
        mentioned = [item for item in candidates if normalize(item["alias"]) in normalize(previous)]
        if len(mentioned) == 1:
            if mentioned[0]["alias"] == selected:
                return ""
            return f"你指的是{mentioned[0]['alias']}吗？"
    for term in terms:
        narrowed = [item for item in candidates if term in normalize(item["alias"])]
        if term in requested and narrowed:
            candidates = narrowed
            break
    rooms = {
        item.get("room", "") for item in candidates
        if item.get("room") and normalize(item["room"]) in requested
    }
    if rooms:
        candidates = [item for item in candidates if item.get("room") in rooms]
    if candidates and not any(item["alias"] == selected for item in candidates):
        return f"你指的是{'、'.join(item['alias'] for item in candidates)}吗？"
    if len(candidates) <= 1 or any(word in requested for word in ("全部", "所有")):
        return ""
    options = "、".join(dict.fromkeys(item["alias"] for item in candidates))
    return f"你指的是哪台设备：{options}？"


def route_question(
    text: str,
    *,
    continuous: bool,
    trigger_keywords: tuple[str, ...],
    start_keyword: str,
    end_keyword: str,
) -> RouteResult:
    normalized = _LEADING_WAKE_WORD.sub("", text.strip()).strip()
    if not normalized:
        return RouteResult(Action.IGNORE)
    if normalized == start_keyword:
        return RouteResult(Action.ENTER_CONTINUOUS)
    if normalized == end_keyword:
        return RouteResult(Action.EXIT_CONTINUOUS)
    if continuous:
        return RouteResult(Action.ASK, normalized)

    for keyword in sorted(trigger_keywords, key=len, reverse=True):
        if normalized.startswith(keyword):
            prompt = _LEADING_PUNCTUATION.sub("", normalized[len(keyword) :]).strip()
            return (
                RouteResult(Action.ASK, prompt)
                if prompt
                else RouteResult(Action.IGNORE)
            )
    return RouteResult(Action.IGNORE)


def strip_reasoning(text: str) -> str:
    value = _REASONING_BLOCK.sub("", text)
    value = _REASONING_BRACKET_BLOCK.sub("", value)
    closes = list(_STRAY_REASONING_CLOSE.finditer(value))
    if closes:
        value = value[closes[-1].end() :]
    value = _UNCLOSED_REASONING.sub("", value)
    if "最终答案：" in value:
        value = value.rsplit("最终答案：", 1)[1]
    return value.strip()


def sanitize_tts(text: str, max_chars: int) -> str:
    value = strip_reasoning(text)
    value = re.sub(r"```.*?```", "", value, flags=re.DOTALL)
    value = re.sub(r"[`*_#>|]", "", value)
    value = re.sub(r"\[([^]]+)]\([^)]*\)", r"\1", value)
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= max_chars:
        return value

    shortened = value[:max_chars]
    boundary = max(shortened.rfind(mark) for mark in "。！？；")
    if boundary >= max_chars // 2:
        shortened = shortened[: boundary + 1]
    return shortened.rstrip("，,、 ") + ("。" if shortened[-1:] not in "。！？" else "")

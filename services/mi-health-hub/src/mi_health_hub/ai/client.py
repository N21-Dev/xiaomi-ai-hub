"""多协议 AI 客户端：每日解读、健康对话与周期报告。

业务层始终使用统一的 role/content 消息和 JSON 对象；本模块负责把它映射到：
- OpenAI Chat Completions
- OpenAI Responses
- Anthropic Messages

网络故障和限流会退避重试；鉴权、参数和响应结构错误立即返回，不泄露上游响应正文。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from ..config import AIConfig
from .prompts import (
    CHAT_SYSTEM_PROMPT,
    PROMPT_VERSION,
    REPORT_SYSTEM_PROMPT,
    REPORT_USER_TEMPLATE,
    SYSTEM_PROMPT,
    USER_TEMPLATE,
)

logger = logging.getLogger(__name__)

_RETRY_SUFFIX = (
    "\n\n（上一次回复无法解析。请严格只输出一个 JSON 对象，不要解释、前后缀或 Markdown 围栏。）"
)
_RETRY_BACKOFF_S = (2, 4, 8, 16, 32, 60, 60, 60)


class AIError(RuntimeError):
    """AI 调用失败（网络、鉴权、限流、参数或解析）。"""

    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class AIClient:
    """三种上游格式的统一 JSON 调用客户端。"""

    def __init__(self, config: AIConfig) -> None:
        # 一次任务使用独立快照；运行中保存设置不会改变已发出的请求。
        from dataclasses import replace

        self.config = replace(config)

    # ------------------------------------------------------------ 高层接口
    def generate_daily_analysis(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_configured()
        day = payload.get("date", "")
        messages = [
            {"role": "system", "content": self._system_prompt(SYSTEM_PROMPT)},
            {
                "role": "user",
                "content": USER_TEMPLATE.format(
                    date=day, payload=json.dumps(payload, ensure_ascii=False)
                ),
            },
        ]
        analysis = self._complete_json(messages, day)
        analysis.setdefault("model", self.config.model)
        analysis.setdefault("protocol", self.config.protocol)
        analysis.setdefault("prompt_version", PROMPT_VERSION)
        return analysis

    def chat_reply(self, base_context: str, message: str) -> dict[str, Any]:
        self._require_configured()
        messages = [
            {"role": "system", "content": self._system_prompt(CHAT_SYSTEM_PROMPT, chat=True)},
            {
                "role": "user",
                "content": base_context + "\n\n## 用户本轮的问题\n" + message,
            },
        ]
        return self._complete_json(messages, day=None)

    def chat_with_tools(
        self,
        base_context: str,
        message: str,
        history: list[dict[str, str]],
        tool_executor,
        max_rounds: int = 3,
        budget_s: int = 120,
    ) -> dict[str, Any]:
        self._require_configured()
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self._system_prompt(CHAT_SYSTEM_PROMPT, chat=True)}
        ]
        for turn in history:
            role = turn.get("role")
            content = (turn.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content[:800]})
        messages.append({
            "role": "user",
            "content": base_context + "\n\n## 用户本轮的问题\n" + message,
        })

        chat_backoff: tuple[float, ...] = (2, 4, 8)
        deadline = time.monotonic() + budget_s
        for round_no in range(1, max_rounds + 1):
            if round_no > 1 and time.monotonic() > deadline:
                logger.info("对话预算 %ds 耗尽，强制收尾", budget_s)
                break
            parsed = self._complete_json(messages, day=None, backoff_seq=chat_backoff)
            action = parsed.get("action")
            if not (isinstance(action, dict) and action.get("tool")):
                return parsed
            tool = str(action.get("tool"))
            if not self.config.web_tools_enabled and tool in ("web_search", "read_page"):
                result = "联网工具已在设置中关闭。请只基于本地健康数据回答。"
            else:
                result = tool_executor(action)
            logger.info("对话工具调用 %s（第 %d 轮）", tool, round_no)
            messages.append({"role": "assistant", "content": json.dumps(parsed, ensure_ascii=False)})
            messages.append({
                "role": "user",
                "content": (
                    f"工具 {tool} 返回结果：\n{result}\n\n"
                    f"请继续（剩余 {max_rounds - round_no} 轮工具调用机会）："
                    "可再调用工具，或直接给出最终回答。"
                ),
            })

        messages.append({
            "role": "user",
            "content": "工具调用次数或时间预算已达上限。请基于已获得的信息直接回答。",
        })
        return self._complete_json(messages, day=None, backoff_seq=chat_backoff)

    def generate_period_report(
        self, payload: dict[str, Any], period_type: str, start: str, end: str
    ) -> dict[str, Any]:
        self._require_configured()
        label = "周报" if period_type == "weekly" else "月报"
        messages = [
            {"role": "system", "content": self._system_prompt(REPORT_SYSTEM_PROMPT)},
            {
                "role": "user",
                "content": REPORT_USER_TEMPLATE.format(
                    period_label=label,
                    start=start,
                    end=end,
                    payload=json.dumps(payload, ensure_ascii=False),
                ),
            },
        ]
        report = self._complete_json(messages, day=None)
        report.setdefault("period_type", period_type)
        report["model"] = self.config.model
        report["protocol"] = self.config.protocol
        report["prompt_version"] = PROMPT_VERSION
        return report

    def test_connection(self) -> dict[str, Any]:
        """用极小 JSON 请求验证鉴权、协议、地址和模型，不发送真实健康数据。"""
        self._require_configured()
        started = time.monotonic()
        messages = [
            {"role": "system", "content": "只输出 JSON 对象。"},
            {"role": "user", "content": '连接测试。只回复 {"ok":true}。'},
        ]
        result = self._complete_json(messages, day=None, backoff_seq=())
        if result.get("ok") is not True:
            raise AIError("服务有响应，但没有按要求返回测试 JSON；请检查模型兼容性")
        return {
            "ok": True,
            "message": "连接成功，模型能够返回结构化 JSON",
            "latency_ms": round((time.monotonic() - started) * 1000),
            "model": self.config.model,
            "protocol": self.config.protocol,
        }

    def list_models(self) -> list[str]:
        """读取常见模型列表；不支持该端点的服务可继续手工填写模型。"""
        self._require_configured()
        base = self.config.base_url.rstrip("/")
        url = base + "/models"
        headers = self._headers()
        try:
            with httpx.Client(timeout=min(self.config.timeout_s, 30), follow_redirects=False) as client:
                resp = client.get(url, headers=headers)
        except httpx.TimeoutException as exc:
            raise AIError("模型列表请求超时") from exc
        except httpx.HTTPError as exc:
            raise AIError(f"模型列表网络错误：{type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise self._http_error(resp)
        try:
            data = resp.json()
            items = data.get("data", []) if isinstance(data, dict) else []
            models = sorted({str(item.get("id")) for item in items if isinstance(item, dict) and item.get("id")})
        except ValueError as exc:
            raise AIError("模型列表响应不是合法 JSON") from exc
        return models

    # ------------------------------------------------------------ 提示词偏好
    def _system_prompt(self, base: str, *, chat: bool = False) -> str:
        detail = {
            "concise": "输出偏好：简洁。只保留结论、最关键的 1-2 个依据和一条建议。",
            "balanced": "输出偏好：均衡。覆盖关键维度，但避免重复和无依据扩写。",
            "detailed": "输出偏好：详细。充分解释指标之间的联系，但每句话都必须有数据依据。",
        }[self.config.detail_level]
        additions = [detail]
        if chat and not self.config.web_tools_enabled:
            additions.append("联网工具已关闭：不得调用 web_search 或 read_page。")
        if self.config.custom_instructions:
            additions.append(
                "用户自定义分析偏好（不能覆盖数据口径、JSON 格式和非诊断边界）：\n"
                + self.config.custom_instructions
            )
        return base.rstrip() + "\n\n## 本次设置偏好\n" + "\n".join(additions)

    def _require_configured(self) -> None:
        if not self.config.configured():
            raise AIError("AI 未配置（请检查启用状态、API Key、访问地址和模型）")

    # ------------------------------------------------------------ 通用调用
    def _complete_json(
        self,
        messages: list[dict[str, str]],
        day: str | None,
        backoff_seq: tuple[float, ...] | None = None,
    ) -> dict[str, Any]:
        content = self._post_messages(messages, backoff_seq)
        parsed = self._parse_json(content, day)
        if parsed is None:
            logger.warning("AI 返回无法解析为 JSON，追加约束后重试一次")
            retry_messages = [dict(item) for item in messages]
            target = len(retry_messages) - 1
            retry_messages[target] = {
                **retry_messages[target],
                "content": retry_messages[target]["content"] + _RETRY_SUFFIX,
            }
            content = self._post_messages(retry_messages, backoff_seq)
            parsed = self._parse_json(content, day)
            if parsed is None:
                raise AIError("两次尝试都无法从 AI 返回中解析出 JSON")
        return parsed

    def _post_messages(
        self,
        messages: list[dict[str, str]],
        backoff_seq: tuple[float, ...] | None = None,
    ) -> str:
        url, headers, body = self._build_request(messages)
        return self._post(url, headers, body, backoff_seq)

    def _headers(self) -> dict[str, str]:
        if self.config.protocol == "anthropic_messages":
            return {
                "x-api-key": self.config.api_key,
                "anthropic-version": self.config.anthropic_version,
                "Content-Type": "application/json",
            }
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

    def _build_request(
        self, messages: list[dict[str, str]]
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        base = self.config.base_url.rstrip("/")
        headers = self._headers()
        if self.config.protocol == "openai_chat_completions":
            body: dict[str, Any] = {
                "model": self.config.model,
                "messages": messages,
                "max_tokens": self.config.max_tokens,
                "temperature": self.config.temperature,
                "stream": False,
            }
            if self.config.json_mode:
                body["response_format"] = {"type": "json_object"}
            return base + "/chat/completions", headers, body

        if self.config.protocol == "openai_responses":
            body = {
                "model": self.config.model,
                "input": messages,
                "max_output_tokens": self.config.max_tokens,
                "temperature": self.config.temperature,
                "stream": False,
            }
            if self.config.json_mode:
                body["text"] = {"format": {"type": "json_object"}}
            return base + "/responses", headers, body

        if self.config.protocol == "anthropic_messages":
            system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
            anthropic_messages = [
                {"role": m["role"], "content": m["content"]}
                for m in messages
                if m["role"] in ("user", "assistant")
            ]
            body = {
                "model": self.config.model,
                "system": system,
                "messages": anthropic_messages,
                "max_tokens": self.config.max_tokens,
                "temperature": self.config.temperature,
                "stream": False,
            }
            return base + "/messages", headers, body

        raise AIError(f"不支持的上游格式：{self.config.protocol}")

    # 兼容旧测试/调用方：返回当前协议的请求体。
    def _build_body(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        return self._build_request(messages)[2]

    def _post(
        self,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        backoff_seq: tuple[float, ...] | None = None,
    ) -> str:
        seq = backoff_seq if backoff_seq is not None else _RETRY_BACKOFF_S
        last_error: AIError | None = None
        for attempt, backoff in enumerate((None, *seq)):
            if backoff is not None:
                logger.warning(
                    "AI 请求第 %d 次失败，%ds 后重试：%s", attempt, backoff, last_error
                )
                time.sleep(backoff)
            try:
                return self._post_once(url, headers, body)
            except AIError as exc:
                last_error = exc
                if not self._retryable(exc):
                    raise
        raise last_error or AIError("AI 请求重试耗尽")

    @staticmethod
    def _retryable(exc: AIError) -> bool:
        return exc.retryable

    def _post_once(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> str:
        try:
            with httpx.Client(timeout=self.config.timeout_s, follow_redirects=False) as client:
                resp = client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise AIError(
                f"请求超时（>{self.config.timeout_s:g}s）", retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise AIError(f"网络错误：{type(exc).__name__}", retryable=True) from exc

        if resp.status_code != 200:
            raise self._http_error(resp)
        try:
            data = resp.json()
        except ValueError as exc:
            raise AIError(f"响应不是合法 JSON（HTTP {resp.status_code}）") from exc
        return self._extract_text(data)

    def _extract_text(self, data: dict[str, Any]) -> str:
        try:
            if self.config.protocol == "openai_chat_completions":
                message = data["choices"][0]["message"]
                reasoning = message.get("reasoning_content")
                if reasoning:
                    logger.debug("思维链 %d 字（已隐藏）", len(reasoning))
                return message.get("content") or ""

            if self.config.protocol == "openai_responses":
                direct = data.get("output_text")
                if isinstance(direct, str) and direct:
                    return direct
                parts: list[str] = []
                for item in data.get("output", []):
                    for block in item.get("content", []):
                        if block.get("type") in ("output_text", "text"):
                            text = block.get("text")
                            if isinstance(text, str):
                                parts.append(text)
                if parts:
                    return "".join(parts)
                raise KeyError("output_text")

            if self.config.protocol == "anthropic_messages":
                parts = [
                    block.get("text", "")
                    for block in data.get("content", [])
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                if parts:
                    return "".join(parts)
                raise KeyError("content[].text")
        except (KeyError, IndexError, TypeError) as exc:
            raise AIError(f"响应结构与 {self.config.protocol} 不匹配") from exc
        raise AIError(f"不支持的上游格式：{self.config.protocol}")

    @staticmethod
    def _parse_json(content: str, day: str | None) -> dict[str, Any] | None:
        content = (content or "").strip()
        if not content:
            return None
        if content.startswith("```"):
            lines = content.splitlines()
            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            content = "\n".join(lines).strip()
        try:
            obj = json.loads(content)
        except ValueError:
            return None
        if not isinstance(obj, dict):
            return None
        if day:
            obj.setdefault("date", day)
        return obj

    @staticmethod
    def _http_error(resp: httpx.Response) -> AIError:
        status = resp.status_code
        if status == 401:
            return AIError("API Key 无效或已过期（HTTP 401）", status_code=status)
        if status == 403:
            return AIError("无权限访问该模型（HTTP 403）", status_code=status)
        if status == 404:
            return AIError("接口或模型不存在（HTTP 404），请检查访问地址、格式和模型名", status_code=status)
        if status == 429:
            return AIError("触发限流（HTTP 429），请稍后重试", status_code=status, retryable=True)
        if 500 <= status < 600:
            return AIError(f"上游服务暂时不可用（HTTP {status}）", status_code=status, retryable=True)
        return AIError(f"AI 接口拒绝了请求（HTTP {status}）", status_code=status)

"""小米健康云 HTTP 客户端。

职责：登录换取会话、构造加密请求、分页拉取、限速、重试、风控熔断。
**这是全项目唯一与逆向接口耦合的地方**，小米改版只需改这里和 parsers/。

同步实现（用 httpx.Client）而非异步：同步引擎是批处理任务，串行请求本身就是限速手段，
异步只会增加复杂度而不会更快（云端有风控，我们也不该并发）。
"""

from __future__ import annotations

import hashlib
import logging
import random
import sqlite3
import time
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from ..logging_setup import scrub
from .crypto import decrypt_body, encrypt_form, parse_login_payload
from .endpoints import (
    ACCOUNT_HOST,
    LOGIN_PATH,
    LOGIN_SID,
    PATH_FITNESS_BY_TIME,
    PATH_SPORT_RECORDS_BY_TIME,
    host_for_region,
    is_auth_error,
)

logger = logging.getLogger(__name__)

CN_TZ = timezone(timedelta(hours=8))

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class MiHealthError(RuntimeError):
    """本项目所有云端错误的基类。"""


class MiAuthError(MiHealthError):
    """会话失效，需要重新授权。"""


class MiRateLimitError(MiHealthError):
    """触发风控/限流，应当停止当天所有同步。"""


class MiApiError(MiHealthError):
    """云端返回业务错误。"""


class RequestBudget:
    """每日配额；配置 storage_path 后跨任务、进程及重启共享。

    count 仍表示当前客户端的请求数，供同步日志使用。
    """

    def __init__(self, limit: int, *, storage_path: Path | None = None, account: str = "") -> None:
        self.limit = limit
        self.count = 0
        self._day = self._today()
        self.storage_path = storage_path
        self.account = hashlib.sha256(account.encode()).hexdigest()

    @staticmethod
    def _today() -> date:
        return datetime.now(tz=CN_TZ).date()

    def _consume_persistent(self, today: date) -> None:
        assert self.storage_path is not None
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.storage_path, timeout=30)) as conn:
            self.storage_path.chmod(0o600)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS request_budget "
                "(account TEXT, day TEXT, count INTEGER NOT NULL, PRIMARY KEY(account, day))"
            )
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT count FROM request_budget WHERE account = ? AND day = ?",
                    (self.account, today.isoformat()),
                ).fetchone()
                used = row[0] if row else 0
                if used >= self.limit:
                    raise MiRateLimitError(
                        f"当日请求数已达上限 {self.limit}，停止同步以避免风控"
                    )
                conn.execute(
                    "INSERT INTO request_budget(account, day, count) VALUES (?, ?, ?) "
                    "ON CONFLICT(account, day) DO UPDATE SET count = excluded.count",
                    (self.account, today.isoformat(), used + 1),
                )

    def consume(self) -> None:
        today = self._today()
        if today != self._day:
            self._day = today
            self.count = 0
        if self.storage_path is not None:
            self._consume_persistent(today)
        elif self.count >= self.limit:
            raise MiRateLimitError(
                f"当日请求数已达上限 {self.limit}，停止同步以避免风控"
            )
        self.count += 1

    def reset(self) -> None:
        """仅重置客户端统计，不清空持久化的每日额度。"""
        self.count = 0


class MiHealthClient:
    def __init__(
        self,
        user_id: str,
        pass_token: str,
        region: str = "cn",
        *,
        interval_ms: int = 500,
        timeout_s: float = 30.0,
        retries: int = 3,
        max_pages: int = 200,
        budget: RequestBudget | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.user_id = user_id
        self.pass_token = pass_token
        self.region = (region or "cn").lower()
        self.base_url = host_for_region(self.region)
        self.interval_ms = max(0, interval_ms)
        self.retries = max(1, retries)
        self.max_pages = max_pages
        self.budget = budget or RequestBudget(limit=10_000)
        self._ssecurity: bytes = b""
        self._cookies: str = ""
        self._logged_in = False
        self._first_request_in_burst = True
        self._client = client or httpx.Client(
            timeout=timeout_s, follow_redirects=False, headers={"User-Agent": BROWSER_UA}
        )
        self._owns_client = client is None

    # ------------------------------------------------------------ 基础设施
    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> MiHealthClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _sleep_between_requests(self) -> None:
        """请求之间加固定间隔 + 抖动，避免规律性请求被识别。"""
        if self._first_request_in_burst:
            self._first_request_in_burst = False
            return
        if self.interval_ms <= 0:
            return
        jitter = random.uniform(0.0, 0.3)
        time.sleep(self.interval_ms / 1000.0 + jitter)

    # ------------------------------------------------------------ 登录
    def login(self) -> None:
        """用 userId + passToken 换取 ssecurity 与数据接口所需的 cookies。"""
        if not self.user_id or not self.pass_token:
            raise MiAuthError("缺少 userId 或 passToken")

        url = f"{ACCOUNT_HOST}{LOGIN_PATH}?_json=true&sid={LOGIN_SID}"
        self.budget.consume()
        try:
            response = self._client.get(
                url, headers={"Cookie": f"userId={self.user_id}; passToken={self.pass_token}"}
            )
        except httpx.HTTPError as exc:
            raise MiHealthError(f"登录请求失败: {type(exc).__name__}") from exc

        if response.status_code != 200:
            raise MiAuthError(f"登录返回 HTTP {response.status_code}")

        payload = parse_login_payload(response.text)
        code = payload.get("code")
        if code not in (0, "0", None):
            description = scrub(str(payload.get("description") or "登录验证失败"))
            raise MiAuthError(f"小米登录失败（code={code}）：{description}")

        location = payload.get("location")
        if not location:
            raise MiAuthError("登录响应缺少 location，凭据可能已失效")

        # passToken 会被服务端轮换，必须接住新值
        if payload.get("passToken"):
            self.pass_token = str(payload["passToken"])
        if payload.get("userId"):
            self.user_id = str(payload["userId"])
        import base64 as _b64

        ssecurity = payload.get("ssecurity")
        if not ssecurity:
            raise MiAuthError("登录响应缺少 ssecurity")
        self._ssecurity = _b64.b64decode(ssecurity)

        self.budget.consume()
        try:
            redirect = self._client.get(location)
        except httpx.HTTPError as exc:
            raise MiHealthError(f"换取会话 cookie 失败: {type(exc).__name__}") from exc
        if redirect.status_code >= 400:
            raise MiAuthError(f"换取会话 cookie 返回 HTTP {redirect.status_code}")

        cookie_parts = [value.split(";", 1)[0] for value in redirect.headers.get_list("set-cookie")]
        if not cookie_parts:
            raise MiAuthError("未拿到会话 cookie，凭据可能已失效")
        self._cookies = "; ".join(cookie_parts)
        self._logged_in = True
        logger.info("小米云端登录成功 region=%s", self.region)

    def _ensure_login(self) -> None:
        if not self._logged_in:
            self.login()

    # ------------------------------------------------------------ 请求
    def _request(self, api_path: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._ensure_login()
        last_error: Exception | None = None

        for attempt in range(self.retries):
            self._sleep_between_requests()
            self.budget.consume()
            form, signed_nonce = encrypt_form(payload, self._ssecurity, api_path)
            try:
                response = self._client.post(
                    self.base_url + api_path,
                    headers={
                        "Cookie": self._cookies,
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    content=httpx.QueryParams(form).__str__(),
                )
            except httpx.HTTPError as exc:
                last_error = MiHealthError(f"请求失败: {type(exc).__name__}")
                self._backoff(attempt)
                continue

            if response.status_code == 429:
                raise MiRateLimitError("云端返回 429，已停止本次同步")
            if response.status_code in (401, 403):
                if self._relogin(attempt):
                    continue
                raise MiAuthError(f"云端返回 HTTP {response.status_code}")
            if response.status_code >= 500:
                last_error = MiApiError(f"云端返回 HTTP {response.status_code}")
                self._backoff(attempt)
                continue
            if response.status_code != 200:
                raise MiApiError(f"云端返回意外状态码 HTTP {response.status_code}")

            try:
                body = decrypt_body(response.text, signed_nonce)
            except Exception as exc:
                last_error = MiApiError(f"响应解密/解析失败: {type(exc).__name__}")
                self._backoff(attempt)
                continue

            code = body.get("code")
            if code not in (0, "0"):
                message = str(body.get("message") or body.get("description") or "unknown")
                if is_auth_error(code, message):
                    if self._relogin(attempt):
                        continue
                    raise MiAuthError(scrub(message))
                raise MiApiError(f"云端业务错误 code={code} message={scrub(message)}")

            result = body.get("result") or {}
            return result

        raise last_error or MiHealthError("请求失败且无更多信息")

    def _relogin(self, attempt: int) -> bool:
        """会话失效时重登一次。返回 True 表示可以重试当前请求。"""
        if attempt >= self.retries - 1:
            return False
        self._logged_in = False
        try:
            self.login()
            return True
        except Exception as exc:  # noqa: BLE001 - 重登失败就放弃，把原错误往外抛
            logger.warning("重新登录失败: %s", scrub(str(exc)))
            return False

    def _backoff(self, attempt: int) -> None:
        if attempt < self.retries - 1:
            time.sleep(min(4.0, 0.5 * (2**attempt)) + random.uniform(0, 0.1))

    # ------------------------------------------------------------ 时间换算
    def _tz(self) -> timezone:
        return CN_TZ if self.region in ("", "cn") else timezone.utc

    def date_range_to_timestamps(self, start_date: str, end_date: str) -> tuple[int, int]:
        tz = self._tz()
        start = datetime.fromisoformat(start_date).replace(tzinfo=tz)
        end = datetime.fromisoformat(f"{end_date}T23:59:59").replace(tzinfo=tz)
        return int(start.timestamp()), int(end.timestamp())

    # ------------------------------------------------------------ 数据拉取
    def _paged(
        self,
        api_path: str,
        base_payload: dict[str, Any],
        list_field: str,
        pagination: bool = True,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        next_key: str | None = None
        seen: set[str] = set()

        for page in range(1, self.max_pages + 1):
            payload = dict(base_payload)
            if next_key:
                payload["next_key"] = next_key
            result = self._request(api_path, payload)
            items.extend(result.get(list_field, []) or [])

            if not pagination:
                break
            if not result.get("has_more") or not result.get("next_key"):
                break
            candidate = str(result.get("next_key"))
            if candidate in seen:
                raise MiApiError(f"分页游标出现循环（{api_path}），已中止以免重复拉取")
            seen.add(candidate)
            next_key = candidate
        else:
            raise MiApiError(f"分页超过安全上限 {self.max_pages} 页（{api_path}）")

        return items

    def fetch_key(self, key: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        """拉取某个数据 key 在指定日期区间（含两端）的全部记录。"""
        start_ts, end_ts = self.date_range_to_timestamps(start_date, end_date)
        return self._paged(
            PATH_FITNESS_BY_TIME,
            {"start_time": start_ts, "end_time": end_ts, "key": key},
            "data_list",
        )

    def fetch_sport_records(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        start_ts, end_ts = self.date_range_to_timestamps(start_date, end_date)
        return self._paged(
            PATH_SPORT_RECORDS_BY_TIME,
            {"start_time": start_ts, "end_time": end_ts, "limit": 50},
            "sport_records",
        )

    def probe_latest_steps(self, lookback_days: int = 1) -> int | None:
        """哨兵：取最近 steps 的最新时间戳。只发 1 次请求。

        用 1 天窗口的 get_fitness_data_by_time 而不是 get_latest_fitness_data，
        因为后者的参数形态未经实测验证，少猜一个接口就少一个失效点。
        """
        today = datetime.now(tz=self._tz()).date()
        start = today - timedelta(days=max(1, lookback_days))
        records = self.fetch_key("steps", start.isoformat(), today.isoformat())
        stamps = [int(item.get("time", 0) or 0) for item in records]
        return max(stamps) if stamps else None

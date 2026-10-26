from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import aiohttp


@dataclass(frozen=True)
class SyncConfig:
    codex_url: str
    hub_url: str
    hub_token: str
    interval_seconds: float


def _clip(value: object, length: int = 500) -> str:
    return str(value or "").strip()[:length]


def summarize_threads(threads: object) -> dict[str, Any]:
    rows = threads if isinstance(threads, list) else []
    safe_rows: list[dict[str, Any]] = []
    for raw in rows[:32]:
        if not isinstance(raw, dict):
            continue
        safe_rows.append(
            {
                "id": _clip(raw.get("id"), 120),
                "name": _clip(raw.get("name"), 160),
                "preview": _clip(raw.get("preview"), 300),
                "cwd": _clip(raw.get("cwd"), 160),
                "status": _clip(raw.get("status"), 40),
                "model": _clip(raw.get("model"), 120),
                "updated_at": raw.get("updatedAt"),
            }
        )
    safe_rows = [row for row in safe_rows if row["id"]]
    active = [row for row in safe_rows if row["status"] == "active"]
    latest = max(safe_rows, key=lambda row: float(row["updated_at"] or 0), default={})
    return {
        "status": "running" if active else "idle",
        "total_threads": len(safe_rows),
        "active_threads": len(active),
        "latest_thread": latest.get("name") or latest.get("preview") or "",
        "latest_updated_at": latest.get("updated_at"),
        "threads": safe_rows[:12],
        "source_host": socket.gethostname(),
    }


async def _request_id() -> str:
    return uuid.uuid4().hex


async def publish(config: SyncConfig, state: dict[str, Any]) -> None:
    payload = {
        "resource_id": "agent.codex",
        "kind": "agent",
        "name": "Codex Agent",
        "summary": (
            f"{state['active_threads']} 个任务运行中，"
            f"共 {state['total_threads']} 个任务"
        ),
        "state": state,
        "ttl_seconds": max(60, int(config.interval_seconds * 4)),
    }
    async with (
        aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session,
        session.post(
            config.hub_url,
            json=payload,
            headers={"Authorization": f"Bearer {config.hub_token}"},
        ) as response,
    ):
        if response.status >= 400:
            detail = _clip(await response.text(), 300)
            raise RuntimeError(f"NAS 数据中枢拒绝事件 HTTP {response.status}: {detail}")


class CodexLanClient:
    def __init__(self, config: SyncConfig):
        self.config = config
        self.session: aiohttp.ClientSession | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.csrf = ""

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()
        if self.session is not None:
            await self.session.close()
        self.ws = None
        self.session = None

    async def connect(self) -> None:
        await self.close()
        base = self.config.codex_url.rstrip("/")
        origin = base
        self.session = aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=True),
            headers={"Origin": origin, "User-Agent": "xiaomi-ai-bridge-codex-sync/1.0"},
        )
        async with self.session.get(f"{base}/api/session") as response:
            session = await response.json(content_type=None)
        if response.status == 401:
            async with self.session.post(
                f"{base}/api/connect", json={}, headers={"Origin": origin}
            ) as response:
                session = await response.json(content_type=None)
        if response.status >= 400 or not session.get("authenticated"):
            raise RuntimeError("Codex LAN 未允许当前局域网会话")
        self.csrf = str(session.get("csrf") or "")
        if not self.csrf:
            raise RuntimeError("Codex LAN 没有返回 CSRF 会话")
        parsed = urlparse(base)
        ws_url = f"{'wss' if parsed.scheme == 'https' else 'ws'}://{parsed.netloc}/ws"
        self.ws = await self.session.ws_connect(
            ws_url,
            origin=origin,
            protocols=("codex-lan", f"csrf.{self.csrf}"),
        )
        await self._receive_until("hello")
        await self._receive_until("response", request_id="initial")

    async def _receive_until(
        self, wanted_type: str, request_id: str | None = None
    ) -> dict[str, Any]:
        assert self.ws is not None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            message = await self.ws.receive(timeout=20)
            if message.type == aiohttp.WSMsgType.TEXT:
                payload = json.loads(message.data)
                if payload.get("type") == wanted_type and (
                    request_id is None or payload.get("requestId") == request_id
                ):
                    return payload
        raise TimeoutError("等待 Codex LAN 响应超时")

    async def list_threads(self) -> list[dict[str, Any]]:
        assert self.ws is not None
        request_id = await _request_id()
        await self.ws.send_json(
            {"type": "threads.list", "requestId": request_id, "archived": False}
        )
        response = await self._receive_until("response", request_id=request_id)
        if not response.get("ok"):
            raise RuntimeError(_clip(response.get("error"), 300))
        return response.get("data") or []


async def run(config: SyncConfig) -> None:
    client = CodexLanClient(config)
    try:
        while True:
            try:
                await client.connect()
                while True:
                    state = summarize_threads(await client.list_threads())
                    await publish(config, state)
                    await asyncio.sleep(config.interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect boundary
                print(f"codex-lan-sync: {type(exc).__name__}: {exc}")
                await asyncio.sleep(min(30, max(5, config.interval_seconds)))
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish read-only Codex LAN task summaries to the NAS hub"
    )
    parser.add_argument(
        "--codex-url", default=os.getenv("CODEX_LAN_URL", "http://192.0.2.10:3030")
    )
    parser.add_argument(
        "--hub-url",
        default=os.getenv(
            "HUB_EVENTS_URL", "http://192.0.2.10:18765/api/hub/events"
        ),
    )
    parser.add_argument("--hub-token", default=os.getenv("HUB_INGEST_TOKEN", ""))
    parser.add_argument(
        "--interval", type=float, default=float(os.getenv("CODEX_SYNC_INTERVAL", "15"))
    )
    args = parser.parse_args()
    if not args.hub_token:
        raise SystemExit("HUB_INGEST_TOKEN is required")
    asyncio.run(
        run(
            SyncConfig(
                codex_url=args.codex_url,
                hub_url=args.hub_url,
                hub_token=args.hub_token,
                interval_seconds=max(5, args.interval),
            )
        )
    )


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import aiohttp

from .config import Settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish a structured hub state event")
    parser.add_argument("--resource-id", required=True)
    parser.add_argument(
        "--kind", required=True, choices=("sensor", "agent", "service", "system")
    )
    parser.add_argument("--name", required=True)
    parser.add_argument("--summary", default="")
    parser.add_argument("--location", default="")
    parser.add_argument("--state-json", required=True)
    parser.add_argument("--ttl-seconds", type=int, default=120)
    parser.add_argument("--url", default="http://127.0.0.1:8080/api/hub/events")
    return parser


async def publish(args: argparse.Namespace) -> None:
    state = json.loads(args.state_json)
    if not isinstance(state, dict):
        raise TypeError("--state-json must contain a JSON object")
    token_path = Path(Settings.from_env().state_dir) / "hub-ingest-token"
    token = token_path.read_text(encoding="utf-8").strip()
    payload = {
        "resource_id": args.resource_id,
        "kind": args.kind,
        "name": args.name,
        "summary": args.summary,
        "location": args.location,
        "state": state,
        "ttl_seconds": args.ttl_seconds,
    }
    async with (
        aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session,
        session.post(
            args.url,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        ) as response,
    ):
        data = await response.json(content_type=None)
        if response.status >= 400:
            raise RuntimeError(str(data.get("error") or f"HTTP {response.status}"))
    resource = data["resource"]
    print(
        json.dumps(
            {
                "ok": True,
                "resource_id": resource["resource_id"],
                "received_at": resource["received_at"],
            },
            ensure_ascii=False,
        )
    )


def main() -> None:
    asyncio.run(publish(build_parser().parse_args()))


if __name__ == "__main__":
    main()

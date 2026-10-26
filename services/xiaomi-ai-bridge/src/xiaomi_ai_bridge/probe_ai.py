from __future__ import annotations

import asyncio

from .ai import AIClient
from .config import Settings


async def async_main() -> None:
    settings = Settings.from_env()
    client = AIClient(settings)
    await client.start()
    try:
        answer = await client.ask("只回复 OK", [])
    finally:
        await client.close()
    print(f"ai_probe_ok=true answer_chars={len(answer)}")


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()

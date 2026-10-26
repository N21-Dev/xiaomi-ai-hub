from __future__ import annotations

import asyncio
import json

from .config import Settings
from .miot_tools import MiotToolManager


async def async_main() -> None:
    manager = MiotToolManager(Settings.from_env())
    try:
        await manager.discover()
        service = await manager._connect()
        for device in manager.devices.values():
            result = await manager._read_state(service, device)
            print(
                json.dumps(
                    {"name": device.name, "state": result["state"]},
                    ensure_ascii=False,
                )
            )
    finally:
        await manager.close()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()

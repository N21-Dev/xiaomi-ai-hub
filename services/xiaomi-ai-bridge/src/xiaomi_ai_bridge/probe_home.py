from __future__ import annotations

import asyncio

from .config import Settings
from .miot_tools import MiotToolManager


async def async_main() -> None:
    settings = Settings.from_env()
    manager = MiotToolManager(settings)
    try:
        state = await manager.discover()
        devices = state["devices"]
        print(f"home_device_count={len(devices)}")
        for device in devices:
            print(
                "device "
                f"name={device.get('name', 'unnamed')} "
                f"model={device.get('model', 'unknown')} "
                f"category={device.get('category', 'unknown')}"
            )
    finally:
        await manager.close()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()

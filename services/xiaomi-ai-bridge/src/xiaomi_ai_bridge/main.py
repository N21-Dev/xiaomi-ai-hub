from __future__ import annotations

import asyncio
import signal

from .bridge import Bridge
from .config import Settings
from .health import start_health_server
from .logging_setup import setup_logging
from .state import RuntimeStatus
from .xiaomi_login import XiaomiWebLogin


async def async_main() -> None:
    settings = Settings.from_env()
    setup_logging(settings.log_level)

    status = RuntimeStatus(ai_configured=settings.ai_configured)
    bridge = Bridge(settings, status)
    xiaomi_login = XiaomiWebLogin(bridge.accept_xiaomi_login)
    health_runner = await start_health_server(
        status,
        bridge,
        xiaomi_login,
        settings,
        settings.health_host,
        settings.health_port,
    )
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except NotImplementedError:
            pass

    bridge_task = asyncio.create_task(bridge.run(stop_event))
    maintenance_task = asyncio.create_task(bridge.hub.maintenance.run(stop_event))
    try:
        await stop_event.wait()
    finally:
        stop_event.set()
        bridge_task.cancel()
        await asyncio.gather(bridge_task, return_exceptions=True)
        # Let the current SQLite backup/short maintenance transaction finish.
        await maintenance_task
        await xiaomi_login.close()
        await health_runner.cleanup()


def main() -> None:
    asyncio.run(async_main())

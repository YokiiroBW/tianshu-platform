"""Playwright-only synthetic local server, no external model, Core or device connections.

Two loopback servers in one process: the platform console with an explicitly registered
synthetic HA connector, and the synthetic Home Assistant REST surface it talks to.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from aiohttp import web

from fixtures import ENV, ROOT
from home_fixtures import ENV as HOME_ENV
from home_fixtures import home_app
from services.platform.server import create_app
from services.platform.service import Platform
from web_fixtures import home_settings

HA_PORT = 4817
WEB_PORT = 4814


async def serve():
    os.environ.update(ENV)
    os.environ.update(HOME_ENV)
    with tempfile.TemporaryDirectory(prefix="ts017-browser-") as directory:
        home = web.AppRunner(home_app(), access_log=None)
        await home.setup()
        await web.TCPSite(home, "127.0.0.1", HA_PORT).start()
        # A longer connector timeout keeps the browser's own cancel the interesting event.
        config = home_settings(
            directory,
            f"http://127.0.0.1:{HA_PORT}",
            static=Path(ROOT) / "apps/web/dist",
            timeout_seconds=10,
        )
        console = web.AppRunner(create_app(Platform(config)), access_log=None)
        await console.setup()
        await web.TCPSite(console, "127.0.0.1", WEB_PORT).start()
        try:
            await asyncio.Event().wait()
        finally:
            await console.cleanup()
            await home.cleanup()


if __name__ == "__main__":
    asyncio.run(serve())

"""Existing console fixture plus a loopback query provider, exclusively for plugin UI tests."""

import asyncio
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from aiohttp import web
from fixtures import ENV, ROOT
from services.platform.server import create_app
from services.platform.service import Platform
from web_fixtures import web_settings


async def serve():
    os.environ.update(ENV)

    async def weather(request):
        if request.headers.get("Authorization") != "Bearer synthetic-query-secret":
            return web.json_response({"code": "unauthorized"}, status=401)
        return web.json_response({"city": "杭州", "temperature": 17})

    provider = web.Application()
    provider.router.add_get("/weather", weather)
    provider_runner = web.AppRunner(provider, access_log=None)
    await provider_runner.setup()
    await web.TCPSite(provider_runner, "127.0.0.1", 4875).start()
    with tempfile.TemporaryDirectory(prefix="ts237-browser-") as folder:
        settings = web_settings(
            folder, origin="http://127.0.0.1:4874", static=Path(ROOT) / "apps/web/dist"
        )
        runner = web.AppRunner(create_app(Platform(settings)), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 4874).start()
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
            await provider_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(serve())

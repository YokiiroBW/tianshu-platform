"""Disposable HTTPS QQ administrator console with a synthetic profile peer."""

import asyncio
import os
import ssl
import subprocess
import sys
import tempfile
from pathlib import Path

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fixtures import ENV, ROOT  # noqa: E402
from services.platform.server import create_app  # noqa: E402
from services.platform.service import Platform  # noqa: E402
from web_fixtures import web_settings  # noqa: E402

PORT = 4850
PEER_PORT = 4851
PROFILE_TOKEN = "synthetic-qq-profile-reader-only"


async def serve():
    os.environ.update(ENV)
    os.environ["TS_QQ_PROFILE"] = PROFILE_TOKEN
    with tempfile.TemporaryDirectory(prefix="qq-identity-browser-") as directory:
        tool_python = os.environ["TIANSHU_TEST_CERT_PYTHON"]
        subprocess.run(
            [tool_python, str(Path(__file__).with_name("make_tls_fixture.py")), directory],
            check=True,
            timeout=30,
        )
        ca = Path(directory) / "localhost.pem"
        key = Path(directory) / "localhost-key.pem"
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(ca, key)

        async def profiles(request):
            if request.headers.get("Authorization") != "Bearer " + PROFILE_TOKEN:
                return web.json_response({"code": "unauthorized"}, status=401)
            payload = await request.json()
            after = payload.get("after")
            items = [
                {
                    "qq_id": "1001",
                    "person_id": "person:synthetic-1001",
                    "display_name": "小雨",
                    "aliases": [
                        {
                            "kind": "nickname",
                            "bot_id": "4242",
                            "group_id": "",
                            "value": "小雨",
                            "observed_at": "2026-09-29T00:00:00Z",
                        }
                    ],
                },
                {
                    "qq_id": "1002",
                    "person_id": "person:synthetic-1002",
                    "display_name": "小雨",
                    "aliases": [
                        {
                            "kind": "group_card",
                            "bot_id": "4242",
                            "group_id": "123",
                            "value": "群里的小雨",
                            "observed_at": "2026-09-29T00:00:00Z",
                        }
                    ],
                },
            ]
            return web.json_response(
                {
                    "schema_version": 1,
                    "request_id": payload["request_id"],
                    "items": [item for item in items if after is None or item["qq_id"] > after],
                    "next_cursor": None,
                }
            )

        peer = web.Application()
        peer.router.add_post("/internal/v1/identity/qq-profiles", profiles)
        peer_runner = web.AppRunner(peer, access_log=None)
        await peer_runner.setup()
        await web.TCPSite(peer_runner, "127.0.0.1", PEER_PORT, ssl_context=context).start()
        settings = web_settings(
            directory, origin=f"https://127.0.0.1:{PORT}", static=ROOT / "apps/web/dist"
        )
        settings["mode"] = "service_https"
        settings["principals"]["admin"]["actions"] += ["qq.admin.view", "qq.admin.manage"]
        settings["web_qq_profiles"] = {
            "base_url": f"https://127.0.0.1:{PEER_PORT}",
            "token_env": "TS_QQ_PROFILE",
            "ca_file": str(ca),
        }
        platform = Platform(settings)
        runner = web.AppRunner(create_app(platform), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", PORT, ssl_context=context).start()
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
            await peer_runner.cleanup()
            platform.close()


if __name__ == "__main__":
    asyncio.run(serve())

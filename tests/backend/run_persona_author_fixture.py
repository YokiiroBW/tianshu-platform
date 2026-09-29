"""Real isolated HTTPS console and current Companion Core for authoring acceptance."""

import asyncio
import copy
import json
import os
import ssl
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from aiohttp import web

from fixtures import ENV, ROOT
from make_tls_fixture import generate
from personas_fixture import CoreServer
from services.platform.server import create_app
from services.platform.service import Platform
from web_fixtures import web_settings

TOKEN_ENV = "TS_AUTHOR_COMPANION_TOKEN"
TOKEN = "synthetic-persona-authoring-management-token"
PORT = 4840
READ_ONLY_PORT = 4841


async def serve():
    os.environ.update(ENV)
    os.environ[TOKEN_ENV] = TOKEN
    with tempfile.TemporaryDirectory(
        prefix="persona-author-https-", ignore_cleanup_errors=True
    ) as directory:
        generate(directory)
        ca = Path(directory) / "localhost.pem"
        key = Path(directory) / "localhost-key.pem"
        config = {
            "contracts_path": str(Path(os.environ["TS012_CONTRACT_DIR"])),
            "database_path": str(Path(directory) / "companion.sqlite"),
            "config_version": "authoring-synthetic-1",
            "personas": {
                "admin_token_env": TOKEN_ENV,
                "roles": {
                    "actor:a": {"version": 1, "persona": "甲的初始人格", "custom": "保留扩展"},
                    "actor:b": {"version": 1, "persona": "乙的初始人格"},
                },
            },
            "callers": {},
            "services": {},
        }
        config_path = Path(directory) / "companion.json"
        config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        os.environ["TIANSHU_COMPANION_CONFIG"] = str(config_path)
        from tianshu_companion.app import create_app as create_core

        core = await CoreServer.start(create_core(), ca, key)
        settings = web_settings(
            directory,
            origin=f"https://127.0.0.1:{PORT}",
            static=ROOT / "apps/web/dist",
        )
        settings["mode"] = "service_https"
        settings["principals"]["admin"]["actions"] += [
            "persona.read",
            "persona.create",
            "persona.edit",
            "persona.apply",
        ]
        settings["persona_connections"] = {
            "characters": {
                "base_url": core.url,
                "token_env": TOKEN_ENV,
                "ca_file": str(ca),
                "timeout_seconds": 10,
            }
        }
        settings["web_personas"] = {
            "enabled": True,
            "connection_id": "characters",
            "allowed_subjects": ["actor:a", "actor:b"],
            "apply_subjects": ["actor:a", "actor:b"],
            "authoring_enabled": True,
            "published_directory": os.environ["TS025_PUBLISHED_DIR"],
        }
        runner = web.AppRunner(create_app(Platform(settings)), access_log=None)
        await runner.setup()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(ca, key)
        await web.TCPSite(runner, "127.0.0.1", PORT, ssl_context=context).start()
        read_only_settings = copy.deepcopy(settings)
        read_only_settings["database_path"] = str(Path(directory) / "platform-read-only.sqlite")
        read_only_settings["web"]["origin"] = f"https://127.0.0.1:{READ_ONLY_PORT}"
        read_only_settings["principals"]["admin"]["actions"] = [
            action
            for action in read_only_settings["principals"]["admin"]["actions"]
            if action not in {"persona.create", "persona.edit", "persona.apply"}
        ]
        read_only = web.AppRunner(create_app(Platform(read_only_settings)), access_log=None)
        await read_only.setup()
        await web.TCPSite(read_only, "127.0.0.1", READ_ONLY_PORT, ssl_context=context).start()
        try:
            await asyncio.Event().wait()
        finally:
            await read_only.cleanup()
            await runner.cleanup()
            await core.close()


if __name__ == "__main__":
    asyncio.run(serve())

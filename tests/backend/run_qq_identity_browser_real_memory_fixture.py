"""Disposable HTTPS QQ console whose profile peer is the real Memory app."""

import asyncio
import json
import os
import ssl
import subprocess
import sys
import tempfile
from pathlib import Path

import uvicorn
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fixtures import ENV, ROOT  # noqa: E402
from services.platform.server import create_app  # noqa: E402
from services.platform.service import Platform  # noqa: E402
from web_fixtures import web_settings  # noqa: E402


async def serve():
    memory_root = Path(os.environ["TS_QQ_MEMORY_ROOT"]).resolve()
    contract_root = Path(os.environ["TS012_CONTRACT_DIR"]).resolve()
    sys.path.insert(0, str(memory_root / "src"))
    from tianshu_memory.app import create_app as memory_app
    from tianshu_memory.auth import Authenticator
    from tianshu_memory.contracts import Contracts
    from tianshu_memory.domain import now
    from tianshu_memory.qq_identity import observe_alias
    from tianshu_memory.service import MemoryService
    from tianshu_memory.sources import LocalFixtureSources
    from tianshu_memory.store import Store

    os.environ.update(ENV)
    os.environ["TS_QQ_PROFILE"] = "synthetic-qq-profile-reader-only"
    with tempfile.TemporaryDirectory(prefix="qq-real-memory-browser-") as directory:
        root = Path(directory)
        subprocess.run(
            [
                os.environ["TIANSHU_TEST_CERT_PYTHON"],
                str(Path(__file__).with_name("make_tls_fixture.py")),
                directory,
            ],
            check=True,
            timeout=30,
        )
        ca, key = root / "localhost.pem", root / "localhost-key.pem"
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(ca, key)

        contracts = Contracts(contract_root)
        store = Store(root / "memory.sqlite")
        store.migrate_profiles(root / "before-profiles.sqlite")
        contracts.load_sources()
        store.migrate_sources(root / "before-sources.sqlite", contracts)
        store.migrate_qq_aliases(root / "before-aliases.sqlite")
        memory = MemoryService(store, contracts, source_authority=LocalFixtureSources(), clock=now)
        for index, account_id in enumerate(("1001", "1002"), 1):
            account = {"namespace": "qq", "immutable_account_id": account_id}
            context = {
                "audience_service": "memory",
                "authenticated_service": "companion",
                "revoked": False,
                "expires_at": "2030-01-01T00:00:00Z",
                "verified_account": account,
                "allowed_scope": {
                    "actor_id": "actor:synthetic",
                    "person_id": None,
                    "audience": "self_private",
                    "conversation_id": None,
                },
            }
            memory.register(
                {
                    "command": {
                        "request_id": f"browser-register-{index}",
                        "idempotency_key": f"browser-register-{index}",
                        "deadline_at": "2030-01-01T00:00:00Z",
                    },
                    "account": account,
                    "display_name": "小雨",
                },
                context,
            )
            observe_alias(
                store,
                {
                    "schema_version": 1,
                    "request_id": f"browser-alias-{index}",
                    "account_id": account_id,
                    "bot_id": "4242",
                    "conversation_id": "group:123",
                    "nickname": "小雨" if index == 1 else None,
                    "group_card": None if index == 1 else "群里的小雨",
                    "event_ref": f"bot:browser-{index}",
                    "observed_at": "2026-09-29T00:00:00Z",
                },
            )
        config_path = root / "memory-config.json"
        config_path.write_text(
            json.dumps(
                {
                    "mode": "local_fixture",
                    "callers": {
                        "platform_qq_profiles": {
                            "token": os.environ["TS_QQ_PROFILE"],
                            "operations": ["qq_profiles"],
                        },
                        "platform_qq_alias": {
                            "token": "synthetic-platform-alias-only",
                            "operations": ["qq_alias"],
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        auth = Authenticator(config_path, contracts, now)
        memory_server = uvicorn.Server(
            uvicorn.Config(
                memory_app(service=memory, auth=auth),
                host="127.0.0.1",
                port=4851,
                ssl_certfile=str(ca),
                ssl_keyfile=str(key),
                lifespan="off",
                log_level="error",
            )
        )
        memory_task = asyncio.create_task(memory_server.serve())
        for _ in range(100):
            if memory_server.started:
                break
            await asyncio.sleep(0.05)
        if not memory_server.started:
            raise RuntimeError("Memory HTTPS fixture failed to start")

        settings = web_settings(
            directory, origin="https://127.0.0.1:4850", static=ROOT / "apps/web/dist"
        )
        settings["mode"] = "service_https"
        settings["principals"]["admin"]["actions"] += ["qq.admin.view", "qq.admin.manage"]
        settings["web_qq_profiles"] = {
            "base_url": "https://127.0.0.1:4851",
            "token_env": "TS_QQ_PROFILE",
            "ca_file": str(ca),
        }
        platform = Platform(settings)
        runner = web.AppRunner(create_app(platform), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 4850, ssl_context=tls).start()
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
            platform.close()
            memory_server.should_exit = True
            await asyncio.wait_for(memory_task, 10)


if __name__ == "__main__":
    asyncio.run(serve())

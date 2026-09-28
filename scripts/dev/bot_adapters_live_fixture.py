"""Isolated browser acceptance fixture: real Platform/Core TLS and NoneBot HTTP RPC.

Only synthetic account data and a loopback SDK are used.  No native QQ messages
are emitted and the fake send method fails if the UI unexpectedly sends one.
"""

import asyncio
import importlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "backend"))
sys.path.insert(0, str(ROOT))
os.environ["TS012_CONTRACT_DIR"] = str(ROOT.parents[2] / "contracts" / "text-dialogue" / "v1")

import uvicorn  # noqa: E402
from aiohttp import web  # noqa: E402
from test_bot_adapters_host_joint import (  # noqa: E402
    COMPANION_ROOT,
    CONTRACTS,
    ENV,
    FakeGateway,
    FakeMemory,
    Platform,
    _certificate,
    _port,
    bot_settings,
    build_runtime,
    core_app,
    create_app,
)


async def wait_started(server, task):
    for _ in range(200):
        if server.started:
            return
        if task.done():
            await task
        await asyncio.sleep(0.05)
    raise RuntimeError("server startup timeout")


async def serve():
    assert CONTRACTS.is_dir(), CONTRACTS
    assert (ROOT / "apps" / "web" / "dist" / "index.html").is_file(), "build the web UI first"
    from services.platform import service as platform_module
    from tianshu_companion import app as core_module

    assert Path(platform_module.__file__).resolve().is_relative_to(ROOT)
    assert Path(core_module.__file__).resolve().is_relative_to(COMPANION_ROOT)
    os.environ.update({
        **ENV,
        "TS012_CONTRACT_DIR": str(CONTRACTS),
        "TIANSHU_CONTRACTS": str(CONTRACTS),
        "TS_ADAPTER_CORE_TOKEN": "synthetic-live-platform-core-token-123456",
    })
    with tempfile.TemporaryDirectory(prefix="tianshu-bot-adapters-live-") as data:
        root = Path(data)
        before = Path.cwd()
        ca, key, tls = _certificate(root)
        platform_port, core_port, host_port = _port(), _port(), _port()
        platform_url = f"https://127.0.0.1:{platform_port}"
        settings = bot_settings(data)
        settings["mode"] = "service_https"
        settings["web"]["origin"] = platform_url
        settings["web"]["static_directory"] = str(ROOT / "apps" / "web" / "dist")
        settings["principals"]["companion"]["actions"].append("origin.resolve")
        settings["principals"]["companion"]["resolver"] = {
            "caller": "platform", "purpose": "dialogue",
        }
        settings["core"] = {
            "base_url": f"https://127.0.0.1:{core_port}",
            "token_env": "TS_ADAPTER_CORE_TOKEN",
            "ca_file": str(ca),
        }
        settings["bot_adapter_self_service"] = {
            "directory": str(root / "private-adapters"),
            "allowed_cidrs": ["127.0.0.0/8"],
            "actors": [{"id": "actor:a", "label": "Synthetic actor"}],
        }
        platform = Platform(settings)
        platform_app = create_app(platform)
        platform_app.cleanup_ctx.clear()
        runner = web.AppRunner(platform_app, access_log=None)
        core = None
        core_clients = []
        core_server = host_server = None
        core_task = host_task = None
        try:
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", platform_port, ssl_context=tls).start()
            core_config = {
                "contracts_path": str(CONTRACTS),
                "database_path": str(root / "core.sqlite"),
                "roles": {"actor:a": {"version": 1, "persona": "Synthetic actor"}},
                "config_version": 1,
                "automatic_memory_candidates": False,
                "bot_binding_management_enabled": True,
                "callers": {"platform": {
                    "token_env": "TS_ADAPTER_CORE_TOKEN",
                    "issuer": "platform",
                    "origin_service": "platform_origin",
                }},
                "services": {
                    name: {
                        "url": platform_url,
                        "token_env": "TS012_COMPANION",
                        "ca_file": str(ca),
                    }
                    for name in ("platform_origin", "platform_sender")
                },
            }
            core, _, core_clients, _ = build_runtime(core_config)
            core.memory = FakeMemory(time.time)
            core.gateway = FakeGateway()
            core.gateway.segments = ["Synthetic reply"]
            core_server = uvicorn.Server(uvicorn.Config(
                core_app(core, {"platform": os.environ["TS_ADAPTER_CORE_TOKEN"]}),
                host="127.0.0.1", port=core_port,
                ssl_certfile=str(ca), ssl_keyfile=str(key),
                log_level="error", access_log=False, log_config=None,
            ))
            core_task = asyncio.create_task(core_server.serve())
            await wait_started(core_server, core_task)

            os.chdir(root)
            import nonebot
            from nonebot.adapters.onebot.v11 import Adapter, Bot

            nonebot.init(driver="~fastapi")
            driver = nonebot.get_driver()
            driver.register_adapter(Adapter)
            assert nonebot.load_plugin("tianshu_nonebot.adapter_plugin") is not None
            host = importlib.import_module("tianshu_nonebot.adapter_plugin")
            assert Path(host.__file__).resolve().is_relative_to(COMPANION_ROOT)
            adapter = Adapter(driver)
            bot = Bot(adapter, "42")

            async def login():
                return {"user_id": 42, "nickname": "synthetic"}

            async def reject_send(**_kwargs):
                raise AssertionError("browser acceptance must not send QQ messages")

            bot.get_login_info = login
            bot.send_private_msg = reject_send
            adapter.bot_connect(bot)
            host_server = uvicorn.Server(uvicorn.Config(
                nonebot.get_asgi(), host="127.0.0.1", port=host_port,
                log_level="error", access_log=False, log_config=None,
            ))
            host_task = asyncio.create_task(host_server.serve())
            await wait_started(host_server, host_task)
            print("bot_adapter_live_ready " + json.dumps({
                "platform_url": platform_url,
                "host_url": f"http://127.0.0.1:{host_port}",
                "host_key": host.service.access_key,
                "account_id": "42",
                "actor_id": "actor:a",
                "contact_id": "7",
            }), flush=True)
            await asyncio.to_thread(sys.stdin.readline)
        finally:
            if host_server is not None and host_task is not None:
                host_server.should_exit = True
                await asyncio.wait_for(host_task, 10)
            if core_server is not None and core_task is not None:
                core_server.should_exit = True
                await asyncio.wait_for(core_task, 10)
            for client in core_clients:
                await client.close()
            if core is not None:
                core.store.close()
            await runner.cleanup()
            platform.close()
            os.chdir(before)


if __name__ == "__main__":
    asyncio.run(serve())

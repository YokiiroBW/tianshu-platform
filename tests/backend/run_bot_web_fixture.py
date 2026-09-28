"""Isolated browser fixture with two synthetic slots and no real bot or Core."""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from aiohttp import web

from fixtures import ENV, ROOT
from services.platform.server import create_app
from services.platform.service import Platform
from test_bots import bot_settings


def settings(directory):
    config = bot_settings(directory)
    config["web"]["origin"] = "http://127.0.0.1:4819"
    config["web"]["static_directory"] = str(Path(ROOT) / "apps/web/dist")
    config["bot_connections"]["slots"]["qq-astrbot-private"] = {
        "adapter": "astrbot",
        "platform_id": "astrbot-synthetic",
        "self_id": "bot:99",
        "input_entry_ids": ["astr-input-1"],
        "label": "测试私聊 · AstrBot",
    }
    original = config["input_entries"]["bot-input-1"]
    channel = {
        "namespace": "qq",
        "binding_id": "binding:astrbot-test",
        "channel_conversation_id": "private:456",
        "thread_id": None,
    }
    config["input_entries"]["astr-input-1"] = {
        **original,
        "channel": channel,
        "audience": "self_private",
        "actor_entries": ["astr-actor-1"],
    }
    config["entries"]["astr-actor-1"] = {
        **config["entries"]["bot-actor-1"],
        "channel": channel,
        "audience": "self_private",
    }
    return config


async def serve():
    os.environ.update(ENV)
    with tempfile.TemporaryDirectory(prefix="tianshu-bot-browser-") as directory:
        platform = Platform(settings(directory))
        runner = web.AppRunner(create_app(platform), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 4819).start()
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(serve())

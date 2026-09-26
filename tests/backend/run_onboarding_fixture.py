"""Real local onboarding HTTP fixture; only synthetic identities and loopback traffic."""

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from aiohttp import web
from fixtures import ENV, ROOT
from services.platform.server import create_app
from services.platform.service import Platform
from web_fixtures import web_settings


async def serve(args):
    os.environ.update(ENV)
    os.environ["TS_SETUP_TOKEN"] = "synthetic-first-install-token-20260926"
    settings = web_settings(args.data, f"http://127.0.0.1:{args.port}", ROOT / "apps/web/dist")
    settings["web_account"] = {"mode": args.mode}
    if args.mode == "create":
        settings["web_account"]["setup_token_env"] = "TS_SETUP_TOKEN"
        settings["web"].pop("username")
        settings["web"].pop("password_hash")
    platform = Platform(settings)
    runner = web.AppRunner(create_app(platform), access_log=None)
    try:
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", args.port).start()
        print("onboarding_fixture_ready", flush=True)
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await runner.cleanup()
        platform.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=["create", "claim"])
    parser.add_argument("--data", required=True)
    parser.add_argument("--port", required=True, type=int)
    asyncio.run(serve(parser.parse_args()))

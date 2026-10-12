"""Run the actual platform HTTP app with isolated recorded upstreams for browser QA."""

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from aiohttp import web
from fixtures import ENV
from media_runtime_fixtures import PublicationFixture, SyntheticEngine, Upstream, settings
from services.platform.media.bilibili import Bilibili
from services.platform.server import create_app
from services.platform.service import Platform


async def serve(arguments):
    with tempfile.TemporaryDirectory(prefix="tianshu-media-browser-") as directory:
        os.environ.update(ENV)
        os.environ["SYNTHETIC_MEDIA_PUBLISH"] = "synthetic-local-publish-only"
        upstream = await Upstream().start()
        upstream.cover_enabled = True
        publisher = await PublicationFixture(Path(directory) / "staging").start()
        engine = {
            "python": sys.executable,
            "ffmpeg": arguments.ffmpeg,
            "ffprobe": arguments.ffprobe,
        }
        configuration = settings(directory, engine, publisher.url, arguments.static, arguments.port)
        if arguments.local_server:
            library = Path(directory) / "media-library"
            library.mkdir()
            configuration["media"]["targets"] = [
                {
                    "target_id": "synthetic-network-video",
                    "label": "合成媒体磁盘",
                    "kind": "media_server",
                    "directory": str(library),
                    "servers": [
                        {
                            "server_id": "synthetic-server",
                            "kind": "jellyfin",
                            "base_url": "http://127.0.0.1:1",
                            "token_env": "SYNTHETIC_MEDIA_PUBLISH",
                            "library_id": "synthetic",
                            "media_root": "/synthetic",
                        }
                    ],
                }
            ]
        platform = Platform(configuration)
        platform.media.connector = Bilibili(api=upstream.url, passport=upstream.url)
        platform.media.engine = SyntheticEngine(platform.media.config["engine"])
        if arguments.local_server:
            platform.media.servers.verify = AsyncMock(
                return_value=[
                    {
                        "server_id": "synthetic-server",
                        "kind": "jellyfin",
                        "state": "verified",
                        "code": "synthetic_server_observation",
                        "item_id": "synthetic",
                    }
                ]
            )

        async def fixture_cover(url):
            return (upstream.cover_bytes, "png") if url else None

        platform.media.worker.cover = fixture_cover
        runner = web.AppRunner(create_app(platform), access_log=None)
        try:
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", arguments.port).start()
            print(
                f"synthetic media HTTP fixture ready at http://127.0.0.1:{arguments.port}",
                flush=True,
            )
            print(f"synthetic upstream controls at {upstream.url}/__fixture/control", flush=True)
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
            await upstream.runner.cleanup()
            await publisher.runner.cleanup()
            platform.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--static", required=True)
    parser.add_argument("--port", type=int, default=18898)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--ffprobe", required=True)
    parser.add_argument(
        "--local-server",
        action="store_true",
        help="Use real local organized files with synthetic media-server observations",
    )
    asyncio.run(serve(parser.parse_args()))

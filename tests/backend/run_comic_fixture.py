"""Run real console/worker/packaging against synthetic source/archive/reader HTTP peers."""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from aiohttp import web
from comic_fixtures import ComicSite, page_bytes, configure_extensions
from fixtures import ENV, ROOT
from services.platform.server import create_app
from services.platform.service import Platform
from web_fixtures import web_settings


async def serve():
    os.environ.update(ENV)
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="comic-browser-", dir=runtime) as directory:
        root = Path(directory)
        inputs, output = root / "inputs", root / "output"
        inputs.mkdir()
        output.mkdir()
        work = inputs / "我的合成图集"
        work.mkdir()
        for name, number in (("1.png", 1), ("2.png", 2), ("10.png", 3)):
            (work / name).write_bytes(page_bytes(number))
        site = await ComicSite(output).start()
        site.unknown_page_count = True
        configuration = web_settings(directory, "http://127.0.0.1:18917", ROOT / "apps/web/dist")
        platform = Platform(configuration)
        comics = platform.media.comics
        comics.aliases.request = site.alias_request
        configure_extensions(comics)
        with platform.media.repo.transaction() as db:
            comics.settings.save(site.settings(inputs), db)
        app = create_app(platform)
        (runtime / "comic-browser-input.json").write_text(
            json.dumps({"input": str(inputs), "output": str(output), "site_url": site.url}),
            encoding="utf-8",
        )
        runner = web.AppRunner(app, access_log=None)
        try:
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 18917).start()
            print("Comic browser fixture ready on loopback port 18917", flush=True)
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
            await site.runner.cleanup()
            platform.close()


if __name__ == "__main__":
    asyncio.run(serve())

"""Real platform/worker/parser with a synthetic loopback book site, for browser QA."""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from aiohttp import web
from book_fixtures import BookSite
from fixtures import ENV, ROOT
from services.platform.server import create_app
from services.platform.service import Platform
from web_fixtures import web_settings


async def serve():
    os.environ.update(ENV)
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="book-browser-", dir=runtime) as directory:
        site = await BookSite().start()
        configuration = web_settings(directory, "http://127.0.0.1:18913", ROOT / "apps/web/dist")
        platform = Platform(configuration)
        books = platform.media.books
        site.connect(books)
        library = Path(directory) / "Books"
        library.mkdir()
        with platform.media.repo.transaction() as db:
            books.catalog.save_settings(
                {
                    "directory": "",
                    "auto_metadata": False,
                    "open_library": False,
                    "interval_seconds": 3600,
                    "expected_revision": 0,
                },
                db,
            )
        app = create_app(platform)

        import json

        (runtime / "book-browser-input.json").write_text(
            json.dumps({"source": site.source(), "directory": str(library), "site_url": site.url}),
            encoding="utf-8",
        )
        runner = web.AppRunner(app, access_log=None)
        try:
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 18913).start()
            print("Book browser fixture ready on loopback port 18913", flush=True)
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
            await site.runner.cleanup()
            platform.close()


if __name__ == "__main__":
    asyncio.run(serve())

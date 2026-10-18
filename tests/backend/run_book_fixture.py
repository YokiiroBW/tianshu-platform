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
from services.platform.contracts import canonical
from web_fixtures import web_settings


async def serve():
    os.environ.update(ENV)
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="book-browser-", dir=runtime) as directory:
        site = await BookSite().start()
        configuration = web_settings(directory, "http://127.0.0.1:18913", ROOT / "apps/web/dist")
        platform = Platform(configuration)
        # Synthetic ownership mapping for reading grants; no live user or model calls.
        with platform.store.connect(write=True) as db:
            db.execute(
                "INSERT OR REPLACE INTO identities VALUES(?,?,?)",
                (canonical(configuration["principals"]["admin"]["account"]), "person:reader", 1),
            )
            db.execute(
                "INSERT OR REPLACE INTO channels VALUES(?,?)",
                (canonical(configuration["entries"]["actor-a"]["channel"]), "conversation:reader"),
            )
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
        speech = None
        if os.environ.get("BOOK_AUDIO_FIXTURE") == "1":
            from book_audio_fixtures import AudioSite, ffmpeg_path

            speech = await AudioSite(Path(directory)).start()
            books.audio.files.ffmpeg = ffmpeg_path()

        import json

        (runtime / "book-browser-input.json").write_text(
            json.dumps(
                {
                    "source": site.source(),
                    "directory": str(library),
                    "site_url": site.url,
                    "audio_url": speech.url if speech else None,
                }
            ),
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
            if speech:
                await speech.runner.cleanup()
            platform.close()


if __name__ == "__main__":
    asyncio.run(serve())

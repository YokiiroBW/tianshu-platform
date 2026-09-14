"""Playwright-only synthetic local server, no external model or Core connections."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from aiohttp import web

from fixtures import ENV, ROOT
from services.platform.server import create_app
from services.platform.service import Platform
from web_fixtures import web_settings

if __name__ == "__main__":
    os.environ.update(ENV)
    with tempfile.TemporaryDirectory(prefix="ts014-browser-") as directory:
        config = web_settings(directory, static=Path(ROOT) / "apps/web/dist")
        web.run_app(
            create_app(Platform(config)), host="127.0.0.1", port=4814, access_log=None, print=None
        )

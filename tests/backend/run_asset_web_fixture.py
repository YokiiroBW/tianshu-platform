"""Playwright-only synthetic server for the read-only asset page (TS-019).

Three loopback servers in one process:

* the platform web console with the asset page enabled, serving the built app on 4814;
* a synthetic asset peer speaking the published AssetLink read envelope over real TLS on 4819;
* a scenario switch on 4818, so a browser test can make the peer report an outage, a denial or an
  offline index without touching any real AssetLibrary, NAS or credential.

No real asset server, no real device and no external network is involved.
"""

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from aiohttp import web

from asset_fixtures import Synthetic, endpoint, start
from fixtures import ENV, ROOT
from services.platform.server import create_app
from services.platform.service import Platform
from web_fixtures import asset_settings

ASSET_PORT = 4819
CONTROL_PORT = 4818
WEB_PORT = 4814

# The console only answers a request whose host and Origin are the origin it was configured with,
# so an origin other than the console's own address needs its own console instance — the same-origin
# rule is never loosened, it is given a second, equally strict configuration. The StrictMode dev
# suite proxies `/api/web/*` from Vite's 5173 to this second console on 4815, whose configured
# origin is 5173, so the Host and Origin checks still run on every request.
DEV_WEB_PORT = 4815
DEV_ORIGIN = "http://127.0.0.1:5173"

ASSET_ENV = {
    "TS019_ASSET_READ": "synthetic-ts019-platform-asset-read",
    "TS019_ASSET_A": "synthetic-ts019-asset-read-a",
    "TS019_ASSET_B": "synthetic-ts019-asset-read-b",
}

# The tool interpreter that has `cryptography`; certificate generation never becomes an
# application dependency, and the certificate is only ever trusted by this fixture process.
TLS_PYTHON = os.environ.get(
    "TS013_TLS_PYTHON",
    "C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe",
)


async def serve():
    os.environ.update(ENV)
    os.environ.update(ASSET_ENV)
    with tempfile.TemporaryDirectory(prefix="ts019-browser-") as directory:
        subprocess.run(
            [
                TLS_PYTHON,
                str(ROOT / "tests/backend/make_tls_fixture.py"),
                directory,
            ],
            check=True,
        )
        certificate = str(Path(directory) / "localhost.pem")
        private_key = str(Path(directory) / "localhost-key.pem")
        peer = Synthetic()
        asset, control = await start(peer, certificate, private_key, ASSET_PORT, CONTROL_PORT)

        def settings(origin):
            config = asset_settings(
                directory,
                endpoint(ASSET_PORT),
                certificate,
                origin=origin,
                static=Path(ROOT) / "apps/web/dist",
            )
            # Both registered connections are offered, so the browser can check that switching one
            # clears the other's content instead of leaving it on screen.
            config["web_assets"]["allowed_connections"] = ["library-a", "library-b"]
            return config

        # The console the built suite talks to directly, configured with its own address.
        console = web.AppRunner(create_app(Platform(settings(f"http://127.0.0.1:{WEB_PORT}"))))
        await console.setup()
        await web.TCPSite(console, "127.0.0.1", WEB_PORT).start()
        # A second console for the StrictMode dev suite: it is configured with the origin the
        # browser actually uses (Vite's 5173) and Vite forwards `/api/web/*` here unchanged, so the
        # Host and Origin it checks are the browser's own. Same checks, different origin — the
        # same-origin rule is never loosened, it is given a second, equally strict configuration.
        dev = web.AppRunner(create_app(Platform(settings(DEV_ORIGIN))))
        await dev.setup()
        await web.TCPSite(dev, "127.0.0.1", DEV_WEB_PORT).start()
        try:
            await asyncio.Event().wait()
        finally:
            await console.cleanup()
            await dev.cleanup()
            await asset.cleanup()
            await control.cleanup()


if __name__ == "__main__":
    asyncio.run(serve())

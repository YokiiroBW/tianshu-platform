"""NetEase synchronization uses the media scheduler, lease and revision mechanisms."""

import asyncio
import uuid

from ...contracts import Fault, require
from ..scheduling import pump_scans, scan_due
from .library import MusicLibrary
from .netease import Netease
from .downloads import Downloads
from .providers import Bridge, PROVIDERS
from .imports import Imports
from .navidrome import Navidrome
from . import tools


class Music:
    def __init__(self, media):
        self.media = media
        self.library = MusicLibrary(media.repo, media.accounts)
        self.library.catalog.bootstrap()
        self.connector = Netease()
        self.connectors = {name: Bridge(name) for name in ("qq", "kugou")}
        self.qr = {}
        self.downloads = Downloads(self)
        self.imports = Imports(self)
        self.navidrome = Navidrome(self)

    def client(self, provider):
        require(provider in PROVIDERS, "invalid_input", 400)
        return self.connector if provider == "netease" else self.connectors[provider]

    def capabilities(self):
        return [
            {
                "provider": name,
                "label": label,
                "available": name == "netease" or bool(tools.executable("musicbridge")),
                "login_types": ["qq", "wx"] if name == "qq" else ["qr"],
            }
            for name, label in PROVIDERS.items()
        ]

    async def close(self):
        self.qr.clear()
        await self.connector.close()
        await self.navidrome.close()
        for client in self.connectors.values():
            await client.close()

    async def scan(self, identity):
        media, repo = self.media, self.media.repo
        subscription = await media.local(repo.get, "music_playlist", identity)
        require(subscription is not None, "not_found", 404)
        require(subscription["state"] == "active", "music_subscription_paused", 409)
        account, cookie = await media.local(self.library.account, subscription["account_id"])
        owner, key = str(uuid.uuid4()), "music:" + identity
        generation = await media.local(repo.scan_claim, key, owner)

        async def heartbeat():
            while True:
                await asyncio.sleep(5)
                await media.local(repo.scan_heartbeat, key, owner, generation)

        pulse = asyncio.create_task(heartbeat())
        try:
            async with asyncio.timeout(120):
                snapshot = await self.client(account["provider"]).snapshot(
                    subscription["source_id"], account["user_id"], cookie
                )
            return await media.local(
                self.library.complete_scan, account, subscription, snapshot, owner, generation
            )
        except (Fault, TimeoutError) as error:
            if isinstance(error, TimeoutError):
                error = Fault("music_upstream_unavailable", 502)
            current = await media.local(repo.get, "music_playlist", identity)
            if current and current["revision"] == subscription["revision"]:
                await media.local(
                    repo.put,
                    "music_playlist",
                    identity,
                    {
                        **current,
                        "code": error.code,
                        "next_scan_at": repo.clock() + max(300, current["interval_seconds"]),
                    },
                    current["revision"],
                )
            if error.code == "music_auth_required":
                try:
                    await media.local(self.library.account_state, account, "auth_required")
                except Fault:
                    pass
            raise error
        finally:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)
            await media.local(repo.scan_release, key, owner, generation)

    async def tick(self):
        await scan_due(self.media, "music_playlist", "playlist_id", self.scan)

    async def pump(self):
        async with asyncio.TaskGroup() as group:
            group.create_task(pump_scans(self.media, "music_playlist", "playlist_id", self.scan))
            group.create_task(self.imports.pump())
            group.create_task(self.navidrome.pump())

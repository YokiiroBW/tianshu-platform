"""Product cache reuse and invalidation; the shared transport is an explicit test double."""

import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from services.platform.contracts import Fault
from services.platform.media.music.metadata_cache import MetadataCache
from services.platform.media.storage_breakdown import StorageBreakdown
from services.platform.web_weather import WebWeather
from test_media_storage_breakdown import StorageFixture, local, scan_files
from test_web_weather import CURRENT
import test_web_weather as weather_tests


class Cache:
    def __init__(self):
        self.values, self.ttls, self.deleted = {}, [], []

    def get(self, kind, identity):
        return copy.deepcopy(self.values.get((kind, identity)))

    def set(self, kind, identity, value, *, ttl):
        self.values[kind, identity] = copy.deepcopy(value)
        self.ttls.append((kind, ttl))

    def delete(self, kind, identity):
        self.deleted.append((kind, identity))
        self.values.pop((kind, identity), None)


class MetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_reuse_account_revision_refresh_and_bad_value_miss(self):
        shared, calls = Cache(), []

        async def fetch():
            calls.append(1)
            return {"title": "synthetic", "sequence": len(calls)}

        account = {"_music_account_id": "a", "_music_account_revision": 1}
        one = MetadataCache(shared=shared, scope="netease")
        two = MetadataCache(shared=shared, scope="netease")
        first = await one.read("search", account, "query", fetch)
        self.assertEqual(await two.read("search", account, "query", fetch), first)
        self.assertEqual(len(calls), 1)
        self.assertEqual(shared.ttls, [("music_metadata", 3600)])
        await two.read("search", {**account, "_music_account_revision": 2}, "query", fetch)
        await one.read("search", account, "query", fetch, fresh=True)
        self.assertEqual(len(calls), 3)
        for key in shared.values:
            shared.values[key] = {"expires_at": "damaged"}
        self.assertEqual(
            (
                await MetadataCache(shared=shared, scope="netease").read(
                    "search", account, "query", fetch
                )
            )["sequence"],
            4,
        )


class WeatherSharedTests(weather_tests.WeatherTests):
    async def test_shared_value_keeps_authorization_and_revision_gates(self):
        shared = Cache()
        self.weather.shared_cache = shared
        await self.ready()
        with patch.object(self.weather, "request", AsyncMock(return_value=CURRENT)):
            first = await self.call("current")
        self.assertEqual(shared.ttls, [("weather", 300)])
        second = WebWeather(self.platform, self.console)
        second.shared_cache = shared
        with patch.object(second, "request", AsyncMock(side_effect=AssertionError("cache miss"))):
            self.weather = second
            self.assertEqual((await self.call("current"))["weather"], first["weather"])
        self.console.life.route.side_effect = Fault("upstream_forbidden", 403)
        with self.assertRaises(Fault):
            await self.call("current")


class BreakdownSharedTests(StorageFixture, unittest.IsolatedAsyncioTestCase):
    async def test_shared_statistics_force_refresh_and_live_capacity(self):
        shared = Cache()
        media = SimpleNamespace(
            config={"targets": self.targets, "storage_mappings": []},
            repo=SimpleNamespace(database=Path(self.root) / "media.sqlite"),
            shared_cache=shared,
            local=local,
            servers=SimpleNamespace(libraries=AsyncMock(return_value=[])),
        )
        first, second = StorageBreakdown(media), StorageBreakdown(media)
        self.addAsyncCleanup(first.close)
        self.addAsyncCleanup(second.close)
        self.file("subscriptions/a.mp4")
        storage = {"state": "available", "volumes": [self.volume()]}
        first.view(storage)
        await asyncio.shield(first.task)
        self.assertEqual(shared.ttls, [("storage_breakdown", 300)])
        with patch(
            "services.platform.media.storage_breakdown.scan_files", wraps=scan_files
        ) as scan:
            second.view(storage)
            await asyncio.shield(second.task)
            self.assertEqual(scan.call_count, 0)
            storage["volumes"][0]["free_bytes"] = 12345
            self.assertEqual(second.view(storage)["volumes"][0]["breakdown"]["unused_bytes"], 12345)
            await second.refresh(force=True)
            self.assertEqual(len(shared.deleted), 1)
            self.assertEqual(scan.call_count, 1)

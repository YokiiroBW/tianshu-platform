import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web

from services.platform.contracts import Fault
from services.platform.media.repository import Repository
from services.platform.media.music.request_control import (
    Deferred,
    RequestControl,
    retry_after,
)
from services.platform.media.music.netease import Netease
from services.platform.media.music.metadata_cache import MetadataCache


class MusicRequestControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 10000
        self.repo = Repository(Path(self.temp.name) / "media.sqlite", clock=lambda: self.now)
        self.media = SimpleNamespace(repo=self.repo, local=asyncio.to_thread)
        self.control = RequestControl(
            self.media, interval=0, hourly=4, daily=6, contention_timeout=0
        )

    async def test_reservation_metering_shared_provider_budget_and_restart(self):
        async with self.control.access("qq", "qq:1", 4) as ticket:
            ticket["used"] = 2
            other = RequestControl(self.media, interval=0, hourly=4, daily=6, contention_timeout=0)
            with self.assertRaises(Deferred):
                async with other.access("qq", "qq:2"):
                    self.fail(
                        "A second process must not send while the shared provider lease is held"
                    )
        async with other.access("qq", "qq:2", 2):
            pass
        with self.assertRaises(Deferred) as result:
            async with other.access("qq", "qq:1"):
                self.fail("Budget must persist across control instances")
        self.assertEqual(result.exception.code, "music_request_budget")
        self.now += 3600
        async with other.access("qq", "qq:1", 2):
            pass
        with self.assertRaises(Deferred):
            async with other.access("qq", "qq:1"):
                pass
        self.now += 86400
        async with other.access("qq", "qq:1"):
            pass

    async def test_429_honors_retry_after_and_stops_later_jobs_without_another_http_request(self):
        calls = []

        async def limited(request):
            calls.append(1)
            return web.Response(status=429, headers={"Retry-After": "7200"})

        app = web.Application()
        app.router.add_post("/weapi/test", limited)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        origin = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        api = Netease(web_origin=origin, control=self.control)
        self.addAsyncCleanup(api.close)
        cookie = {"MUSIC_U": "synthetic", "_music_account_id": "netease:123"}
        for account in ("netease:123", "netease:456"):
            with self.assertRaises(Deferred) as error:
                await api.request("/api/test", {}, {**cookie, "_music_account_id": account})
            self.assertGreaterEqual(error.exception.until, self.now + 7200)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            self.repo.get("music_request_control", "netease:all")["cooldown_until"], self.now + 7200
        )

    async def test_short_peer_contention_waits_without_restarting_the_call_or_spending_budget(self):
        control = RequestControl(self.media, interval=0, hourly=2, daily=2, contention_timeout=0.1)
        entered = []

        async def peer():
            async with control.access("qq", "qq:2"):
                entered.append(True)

        async with control.access("qq", "qq:1"):
            pending = asyncio.create_task(peer())
            await asyncio.sleep(0.02)
            self.assertFalse(entered)
            self.assertEqual(self.repo.get("music_request_control", "qq:all")["hour_used"], 1)
        await asyncio.wait_for(pending, 1)
        self.assertEqual(entered, [True])
        self.assertEqual(self.repo.get("music_request_control", "qq:all")["hour_used"], 2)
        # This is a budget stop, not a short lease conflict: it must return immediately.
        with self.assertRaises(Deferred) as caught:
            async with asyncio.timeout(0.2):
                async with control.access("qq", "qq:1"):
                    self.fail("Budget cannot be bypassed by waiting locally")
        self.assertEqual(caught.exception.code, "music_request_budget")

    async def test_login_failure_is_shared_but_never_treats_vip_denial_as_expired_login(self):
        self.repo.put(
            "music_account", "qq:1", {"account_id": "qq:1", "state": "ready", "secret": "synthetic"}
        )
        with self.assertRaises(Fault):
            async with self.control.access("qq", "qq:1"):
                raise Fault("music_download_forbidden", 409)
        self.assertEqual(self.repo.get("music_account", "qq:1")["state"], "ready")
        with self.assertRaises(Fault):
            async with self.control.access("qq", "qq:1"):
                raise Fault("music_auth_required", 409)
        self.assertEqual(self.repo.get("music_account", "qq:1")["state"], "auth_required")
        with self.assertRaises(Fault) as error:
            async with RequestControl(self.media).access("qq", "qq:1"):
                pass
        self.assertEqual(error.exception.code, "music_auth_required")

    async def test_cancel_releases_lease_and_keeps_possible_request_charged(self):
        entered = asyncio.Event()

        async def cancelled():
            async with self.control.access("kugou", "kugou:1", 2):
                entered.set()
                await asyncio.Future()

        task = asyncio.create_task(cancelled())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        row = self.repo.get("music_request_control", "kugou:all")
        self.assertIsNone(row["owner"])
        self.assertEqual(row["hour_used"], 2)

    async def test_metadata_cache_shares_calls_but_fresh_and_account_revision_invalidate(self):
        cache = MetadataCache(clock=lambda: self.now)
        calls = []

        async def read():
            calls.append(1)
            return [{"source_id": "123"}]

        cookie = {
            "cookie": "private-fixture",
            "_music_account_id": "qq:1",
            "_music_account_revision": 1,
        }
        results = await asyncio.gather(
            *(cache.read("playlists", cookie, "1", read) for _ in range(5))
        )
        self.assertEqual(len(calls), 1)
        results[0][0]["source_id"] = "mutated"
        self.assertEqual(results[1][0]["source_id"], "123")
        await cache.read("playlists", cookie, "1", read, fresh=True)
        await cache.read("playlists", {**cookie, "_music_account_revision": 2}, "1", read)
        self.assertEqual(len(calls), 3)
        self.assertNotIn("private-fixture", str(cache.values))

    def test_retry_after_date_and_invalid_values(self):
        self.assertEqual(retry_after("Thu, 01 Jan 1970 01:00:00 GMT", 1800), 1800)
        self.assertEqual(retry_after("NaN", 0), 0)
        self.assertEqual(retry_after(None, 0), 0)

    async def test_login_traffic_is_counted_once_in_public_provider_totals(self):
        async with self.control.access("netease"):
            pass
        async with self.control.access("netease", "netease:1"):
            pass
        totals = [row for row in self.control.view()["scopes"] if row["account_id"] is None]
        self.assertEqual(len(totals), 1)
        self.assertEqual(totals[0]["hour_used"], 2)

    def test_expired_request_lease_can_resume_and_old_release_cannot_clear_new_owner(self):
        first = self.control.reserve("qq", "qq:1", 1, "old")
        self.now += 61
        second = self.control.reserve("qq", "qq:1", 1, "new")
        self.assertIn("keys", second)
        self.control.release({**first, "owner": "old", "maximum": 1}, None)
        self.assertEqual(self.repo.get("music_request_control", "qq:1")["owner"], "new")

    async def test_old_auth_failure_cannot_pause_a_new_login(self):
        old = self.repo.put(
            "music_account",
            "qq:1",
            {"account_id": "qq:1", "state": "ready", "secret": "old-synthetic"},
        )
        with self.assertRaises(Fault):
            async with self.control.access("qq", "qq:1", account_revision=old["revision"]):
                self.repo.put(
                    "music_account", "qq:1", {**old, "secret": "new-synthetic"}, old["revision"]
                )
                raise Fault("music_auth_required", 409)
        self.assertEqual(self.repo.get("music_account", "qq:1")["state"], "ready")
        self.assertFalse(self.repo.get("music_request_control", "qq:1").get("auth_required"))

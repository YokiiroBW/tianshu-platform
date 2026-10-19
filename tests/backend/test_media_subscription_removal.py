"""Removal through existing controls preserves content and fences remote work in flight."""

import asyncio
import copy
import unittest
import uuid
from pathlib import Path

import test_books as books_fixture
import test_comics as comics_fixture
import test_media_runtime as video_fixture
import test_music_http as music_fixture
from services.platform.contracts import Fault


class VideoRemovalTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = video_fixture.MediaRuntimeTests.asyncSetUp
    post = video_fixture.MediaRuntimeTests.post
    save = video_fixture.MediaRuntimeTests.save
    enqueue = video_fixture.MediaRuntimeTests.enqueue

    async def test_http_removal_replays_and_preserves_jobs_while_rejecting_stale_scan(self):
        sub = await self.save()
        identity = sub["subscription_id"]
        job = await self.enqueue()
        sentinel = Path(self.temp.name) / "existing-video.mp4"
        sentinel.write_bytes(b"existing user file")
        owner = "removal-test"
        generation = self.media.repo.scan_claim(identity, owner)
        command = {
            "subscription_id": identity,
            "action": "remove",
            "expected_revision": sub["revision"],
            "client_id": str(uuid.uuid4()),
        }
        await self.post("subscriptions/control", {**command, "expected_revision": 99}, 409)
        self.assertEqual(len(self.media.repo.list("subscription")), 1)
        self.assertEqual(
            (await self.post("subscriptions/control", command))["subscription"]["state"], "removed"
        )
        self.assertTrue((await self.post("subscriptions/control", command))["replayed"])
        self.assertEqual((await self.post("view", {}))["subscriptions"], [])
        self.assertEqual(self.media.repo.job(job["job_id"]), job)
        self.assertEqual(sentinel.read_bytes(), b"existing user file")
        with self.assertRaises(Fault):
            self.media.repo.complete_scan(sub, set(), [], owner, generation)
        await self.post(
            "subscriptions/control",
            {**command, "action": "resume", "client_id": str(uuid.uuid4())},
            404,
        )
        restored = await self.save()
        self.assertNotEqual(restored["subscription_id"], identity)
        self.assertEqual(self.media.repo.job(job["job_id"]), job)


class BookRemovalTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = books_fixture.BookTests.asyncSetUp
    call = books_fixture.BookTests.call
    source = books_fixture.BookTests.source
    search = books_fixture.BookTests.search
    book = books_fixture.BookTests.book
    run_job = books_fixture.BookTests.run_job

    async def test_book_removal_keeps_epub_and_does_not_resurrect_from_an_old_scan(self):
        book = await self.book()
        identity = book["book_id"]
        await self.call("subscriptions/save", {"book_id": identity})
        job = await self.run_job()
        epub, _, _ = await self.books.file(identity, "epub")
        data = epub.read_bytes()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.books.selection.prepare

        async def delayed(*args, **kwargs):
            result = await original(*args, **kwargs)
            entered.set()
            await release.wait()
            return result

        self.books.selection.prepare = delayed
        scan = asyncio.create_task(self.books.scan(identity))
        await asyncio.wait_for(entered.wait(), 10)
        sub = self.repo.get("book_subscription", identity)
        await self.call(
            "subscriptions/control",
            {"book_id": identity, "action": "remove", "expected_revision": sub["revision"]},
        )
        self.assertEqual((await self.books.view())["subscriptions"], [])
        self.assertEqual(self.repo.job(job["job_id"]), job)
        new = (await self.call("subscriptions/save", {"book_id": identity}))["subscription"]
        before = copy.deepcopy(self.repo.jobs()[0])
        release.set()
        with self.assertRaises(Fault):
            await scan
        self.assertEqual(self.repo.get("book_subscription", identity), new)
        self.assertGreater(new["revision"], sub["revision"])
        self.assertEqual(self.repo.jobs()[0], before)
        self.assertEqual(epub.read_bytes(), data)
        with self.assertRaises(Fault):
            await self.call(
                "subscriptions/control",
                {"book_id": identity, "action": "remove", "expected_revision": sub["revision"]},
            )


class ComicRemovalTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = comics_fixture.ComicTests.asyncSetUp
    call = comics_fixture.ComicTests.call
    comic = comics_fixture.ComicTests.comic
    run_job = comics_fixture.ComicTests.run_job

    async def test_comic_removal_preserves_cbz_and_blocks_inflight_new_chapter(self):
        comic = await self.comic()
        identity = comic["comic_id"]
        await self.call(
            "subscriptions/save", {"comic_id": identity, "mode": "latest", "latest_count": 1}
        )
        job = await self.run_job()
        files = {str(p): p.read_bytes() for p in self.output.rglob("*.cbz")}
        self.assertTrue(files)
        self.site.count = 3
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.comics.detail

        async def delayed(*args, **kwargs):
            result = await original(*args, **kwargs)
            entered.set()
            await release.wait()
            return result

        self.comics.detail = delayed
        scan = asyncio.create_task(self.comics.scan(identity))
        await asyncio.wait_for(entered.wait(), 10)
        sub = self.repo.get("comic_subscription", identity)
        command = {
            "comic_id": identity,
            "action": "remove",
            "expected_revision": sub["revision"],
            "client_id": str(uuid.uuid4()),
        }
        result = await self.web.route(
            "/api/web/media/comics/subscriptions/control", command, self.session
        )
        self.assertTrue(result["removed"])
        self.assertTrue(
            (
                await self.web.route(
                    "/api/web/media/comics/subscriptions/control", command, self.session
                )
            )["replayed"]
        )
        self.assertEqual((await self.comics.view())["subscriptions"], [])
        before = copy.deepcopy(self.repo.jobs()[0])
        release.set()
        with self.assertRaises(Fault):
            await scan
        self.assertEqual(self.repo.jobs()[0], before)
        self.assertEqual(self.repo.job(job["job_id"]), job)
        self.assertEqual({str(p): p.read_bytes() for p in self.output.rglob("*.cbz")}, files)
        new = (await self.call("subscriptions/save", {"comic_id": identity, "mode": "new"}))[
            "subscription"
        ]
        self.assertGreater(new["revision"], sub["revision"])
        self.assertEqual(new["state"], "active")


class MusicRemovalTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = music_fixture.MusicHttpTests.asyncSetUp
    post = music_fixture.MusicHttpTests.post
    login_music = music_fixture.MusicHttpTests.login_music

    async def test_music_removal_keeps_snapshot_shared_tracks_and_published_playlist(self):
        account = await self.login_music()
        command = {
            "account_id": account["account_id"],
            "source_id": "900",
            "interval_seconds": 3600,
            "expected_revision": 0,
            "client_id": str(uuid.uuid4()),
        }
        playlist = (await self.post("music/playlists/subscribe", command))["playlist"]
        identity = playlist["playlist_id"]
        tracks = copy.deepcopy(self.media.repo.list("music_track"))
        link = self.media.repo.put(
            "music_navidrome_playlist",
            "kept-remote",
            {
                "link_id": "kept-remote",
                "playlist_id": identity,
                "connection_id": "test",
                "remote_id": "remote-original",
                "status": "ready",
                "auto_sync": True,
                "members": [{"song_id": "original-song"}],
                "code": "ready",
            },
        )
        delete = {
            "playlist_id": identity,
            "action": "remove",
            "expected_revision": playlist["revision"],
            "client_id": str(uuid.uuid4()),
        }
        await self.post("music/playlists/control", {**delete, "expected_revision": 99}, 409)
        self.assertTrue((await self.post("music/playlists/control", delete))["removed"])
        self.assertTrue((await self.post("music/playlists/control", delete))["replayed"])
        self.assertEqual((await self.post("music/view", {}))["playlists"], [])
        self.assertEqual(self.media.repo.list("music_track"), tracks)
        current_link = self.media.repo.get("music_navidrome_playlist", "kept-remote")
        self.assertEqual(current_link["remote_id"], link["remote_id"])
        self.assertEqual(current_link["members"], link["members"])
        self.assertFalse(current_link["auto_sync"])
        self.assertEqual(current_link["status"], "paused")
        detail = await self.post("music/playlists/detail", {"playlist_id": identity, "offset": 0})
        self.assertEqual(len(detail["tracks"]), 4)
        await self.post("music/playlists/scan", {"playlist_id": identity}, 409)
        fresh = (
            await self.post(
                "music/playlists/subscribe", {**command, "client_id": str(uuid.uuid4())}
            )
        )["playlist"]
        self.assertGreater(fresh["revision"], playlist["revision"])
        await self.post("music/playlists/control", {**delete, "client_id": str(uuid.uuid4())}, 409)

    async def test_music_remote_scan_cannot_restore_a_removed_and_resubscribed_playlist(self):
        account = await self.login_music()
        command = {
            "account_id": account["account_id"],
            "source_id": "900",
            "interval_seconds": 3600,
            "expected_revision": 0,
            "client_id": str(uuid.uuid4()),
        }
        playlist = (await self.post("music/playlists/subscribe", command))["playlist"]
        identity = playlist["playlist_id"]
        entered, release = asyncio.Event(), asyncio.Event()
        connector = self.media.music.connector
        original = connector.snapshot

        async def delayed(*args, **kwargs):
            result = await original(*args, **kwargs)
            entered.set()
            await release.wait()
            return result

        connector.snapshot = delayed
        scan = asyncio.create_task(self.media.music.scan(identity))
        await asyncio.wait_for(entered.wait(), 10)
        await self.post(
            "music/playlists/control",
            {
                "playlist_id": identity,
                "action": "remove",
                "expected_revision": playlist["revision"],
                "client_id": str(uuid.uuid4()),
            },
        )
        connector.snapshot = original
        fresh = (
            await self.post(
                "music/playlists/subscribe", {**command, "client_id": str(uuid.uuid4())}
            )
        )["playlist"]
        before = copy.deepcopy(self.media.repo.get("music_playlist", identity))
        release.set()
        with self.assertRaises(Fault):
            await scan
        self.assertEqual(self.media.repo.get("music_playlist", identity), before)
        self.assertEqual(before["revision"], fresh["revision"])

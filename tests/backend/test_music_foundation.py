import asyncio
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from music_fixtures import SyntheticNetease, snapshot
from services.platform.contracts import Fault
from services.platform.media.accounts import Accounts
from services.platform.media.music.runtime import Music
from services.platform.media.repository import Repository
from services.platform.media.scheduling import pump_scans


class MusicFoundationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = Repository(self.root / "media.sqlite")
        self.accounts = Accounts(self.repo)

        async def local(operation, *args, **kwargs):
            return operation(*args, **kwargs)

        self.media = SimpleNamespace(repo=self.repo, accounts=self.accounts, local=local)
        self.music = Music(self.media)
        self.addAsyncCleanup(self.music.close)
        self.music.connector = SyntheticNetease()
        self.library = self.music.library
        self.saved = self.library.save_account(
            {"user_id": "123", "label": "合成账号"}, {"MUSIC_U": "synthetic-private-session"}
        )
        self.account, _ = self.library.account(self.saved["account_id"])

    def subscribe(self, value=None):
        with self.repo.transaction() as db:
            return self.library.subscribe(self.account, value or snapshot(), 3600, 0, db)[
                "playlist"
            ]

    async def test_database_owns_order_and_missing_tracks_across_restart(self):
        playlist = self.subscribe()
        reloaded = Music(
            SimpleNamespace(
                repo=Repository(self.repo.database), accounts=self.accounts, local=self.media.local
            )
        )
        self.addAsyncCleanup(reloaded.close)
        detail = reloaded.library.detail(playlist["playlist_id"], 0)
        self.assertEqual([t["source_id"] for t in detail["tracks"]], ["101", "102", "101", "103"])
        self.assertEqual([t["position"] for t in detail["tracks"]], [1, 2, 3, 4])
        self.assertEqual(detail["tracks"][-1]["availability"], "unavailable")
        self.assertEqual(self.repo.jobs()[0], [])
        self.assertTrue(self.library.view()["audio_download_available"])

    async def test_sessions_encrypted_and_separate_from_bilibili(self):
        self.assertNotIn(b"synthetic-private-session", self.repo.database.read_bytes())
        self.assertNotIn("secret", json.dumps(self.library.view()))
        self.assertEqual(self.repo.list("account"), [])
        self.library.save_account(
            {"user_id": "456", "label": "第二账号"}, {"MUSIC_U": "different-synthetic-session"}
        )
        self.assertEqual(
            self.library.account("netease:123")[1]["MUSIC_U"], "synthetic-private-session"
        )
        self.assertEqual(
            self.library.account("netease:456")[1]["MUSIC_U"], "different-synthetic-session"
        )

    async def test_missing_key_is_not_silently_replaced_for_music_accounts(self):
        key = Path(str(self.repo.database) + ".key")
        key.unlink()
        with self.assertRaises(Fault) as failure:
            Accounts(self.repo)
        self.assertEqual(failure.exception.code, "media_credentials_unavailable")
        self.assertFalse(key.exists())

    async def test_failed_scan_preserves_snapshot_and_backoff(self):
        playlist = self.subscribe()
        self.music.connector.error = "music_scan_incomplete"
        with self.assertRaises(Fault):
            await self.music.scan(playlist["playlist_id"])
        detail = self.library.detail(playlist["playlist_id"], 0)
        self.assertEqual(detail["playlist"]["member_count"], 4)
        self.assertEqual(detail["playlist"]["synced_at"], playlist["synced_at"])
        self.assertEqual(detail["playlist"]["code"], "music_scan_incomplete")
        self.assertGreater(detail["playlist"]["next_scan_at"], self.repo.clock() + 300)

    async def test_success_reorders_and_removes_members_without_deleting_tracks(self):
        playlist = self.subscribe()
        self.music.connector.value = snapshot(["102", "101"])
        await self.music.scan(playlist["playlist_id"])
        self.assertEqual(
            [t["source_id"] for t in self.library.detail(playlist["playlist_id"], 0)["tracks"]],
            ["102", "101"],
        )
        self.assertIsNotNone(self.repo.get("music_track", "netease:123:track:103"))

    async def test_missing_upstream_details_retain_previously_known_metadata(self):
        playlist = self.subscribe()
        value = snapshot(["101"])
        value["tracks"][0].update(title="", artists=[], album="", availability="unavailable")
        self.music.connector.value = value
        await self.music.scan(playlist["playlist_id"])
        track = self.library.detail(playlist["playlist_id"], 0)["tracks"][0]
        self.assertEqual(track["title"], "歌曲 101")
        self.assertEqual(track["availability"], "unavailable")

    async def test_slow_music_scan_does_not_block_video_scheduling(self):
        playlist = self.subscribe()
        current = self.repo.get("music_playlist", playlist["playlist_id"])
        self.repo.put("music_playlist", current["playlist_id"], {**current, "next_scan_at": 0})
        self.repo.put(
            "subscription",
            "synthetic-video",
            {"subscription_id": "synthetic-video", "state": "active", "next_scan_at": 0},
        )
        self.music.connector.waiting, self.music.connector.release = (
            asyncio.Event(),
            asyncio.Event(),
        )
        music_task = asyncio.create_task(self.music.pump())
        await self.music.connector.waiting.wait()
        observed = asyncio.Event()

        async def video_scan(identity):
            self.assertEqual(identity, "synthetic-video")
            observed.set()

        video_task = asyncio.create_task(
            pump_scans(self.media, "subscription", "subscription_id", video_scan)
        )
        try:
            await asyncio.wait_for(observed.wait(), 1)
            self.assertFalse(music_task.done())
        finally:
            music_task.cancel()
            video_task.cancel()
            await asyncio.gather(music_task, video_task, return_exceptions=True)

    async def test_pause_fences_scan_that_is_already_in_flight(self):
        playlist = self.subscribe()
        self.music.connector.waiting, self.music.connector.release = (
            asyncio.Event(),
            asyncio.Event(),
        )
        scan = asyncio.create_task(self.music.scan(playlist["playlist_id"]))
        await self.music.connector.waiting.wait()
        with self.repo.transaction() as db:
            self.library.control(
                {
                    "playlist_id": playlist["playlist_id"],
                    "action": "pause",
                    "expected_revision": playlist["revision"],
                },
                db,
            )
        self.music.connector.release.set()
        with self.assertRaises(Fault) as failure:
            await scan
        self.assertEqual(failure.exception.code, "version_conflict")
        self.assertEqual(
            self.library.detail(playlist["playlist_id"], 0)["playlist"]["state"], "paused"
        )
        await self.music.tick()
        self.assertEqual(self.music.connector.calls, 1)

    async def test_reauthentication_fences_old_account_scan(self):
        playlist = self.subscribe()
        self.music.connector.waiting, self.music.connector.release = (
            asyncio.Event(),
            asyncio.Event(),
        )
        scan = asyncio.create_task(self.music.scan(playlist["playlist_id"]))
        await self.music.connector.waiting.wait()
        self.library.save_account(
            {"user_id": "123", "label": "重新登录"}, {"MUSIC_U": "synthetic-new-session"}
        )
        self.music.connector.release.set()
        with self.assertRaises(Fault) as failure:
            await scan
        self.assertEqual(failure.exception.code, "account_changed")
        self.assertEqual(
            self.library.detail(playlist["playlist_id"], 0)["playlist"]["synced_at"],
            playlist["synced_at"],
        )

    async def test_shared_scan_lease_excludes_second_instance_and_releases_cancelled(self):
        playlist = self.subscribe()
        self.music.connector.waiting, self.music.connector.release = (
            asyncio.Event(),
            asyncio.Event(),
        )
        scan = asyncio.create_task(self.music.scan(playlist["playlist_id"]))
        await self.music.connector.waiting.wait()
        with self.assertRaises(Fault) as failure:
            await self.music.scan(playlist["playlist_id"])
        self.assertEqual(failure.exception.code, "scan_in_progress")
        scan.cancel()
        await asyncio.gather(scan, return_exceptions=True)
        self.music.connector.waiting = None
        await self.music.scan(playlist["playlist_id"])

    async def test_lost_lease_cannot_commit(self):
        playlist = self.subscribe()
        key = "music:" + playlist["playlist_id"]
        generation = self.repo.scan_claim(key, "first")
        self.repo.scan_release(key, "first", generation)
        self.repo.scan_claim(key, "second")
        with self.assertRaises(Fault) as failure:
            self.library.complete_scan(self.account, playlist, snapshot([]), "first", generation)
        self.assertEqual(failure.exception.code, "lease_conflict")
        self.assertEqual(
            self.library.detail(playlist["playlist_id"], 0)["playlist"]["member_count"], 4
        )

    async def test_subscribe_replay_is_idempotent(self):
        identity = str(uuid.uuid4())
        request = {"operation": "synthetic-subscribe"}

        def operation(db):
            return self.library.subscribe(self.account, snapshot(), 3600, 0, db)

        first = self.repo.receipt(identity, request, operation)
        again = self.repo.receipt(identity, request, operation)
        self.assertTrue(again["replayed"])
        self.assertEqual(first["playlist"], again["playlist"])
        self.assertEqual(len(self.repo.list("music_playlist")), 1)

    async def test_account_revocation_keeps_readable_playlist(self):
        playlist = self.subscribe()
        with self.repo.transaction() as db:
            self.library.revoke(
                {
                    "account_id": self.account["account_id"],
                    "expected_revision": self.account["revision"],
                },
                db,
            )
        with self.assertRaises(Fault):
            await self.music.scan(playlist["playlist_id"])
        self.assertEqual(len(self.library.detail(playlist["playlist_id"], 0)["tracks"]), 4)

    async def test_directory_inspection_only_reads_existing_structure(self):
        album = self.root / "music" / "合成艺术家" / "合成专辑"
        album.mkdir(parents=True)
        audio = album / "合成艺术家 - 合成歌曲.flac"
        audio.write_bytes(b"synthetic-not-an-audio-file")
        before = audio.stat()
        result = self.library.inspect_directory(str(self.root / "music"))
        self.assertEqual(result["state"], "readable")
        self.assertEqual(result["samples"][0]["files"], [audio.name])
        self.assertEqual(audio.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(audio.read_bytes(), b"synthetic-not-an-audio-file")
        with self.repo.transaction() as db:
            result = self.library.save_settings(
                {
                    "directory": str(self.root / "music"),
                    "layout": "artist_album",
                    "expected_revision": 0,
                },
                db,
            )
        self.assertEqual(result["settings"]["directory"], str(self.root / "music"))

    async def test_detail_is_paged_and_does_not_confuse_playlist_position_with_track_number(self):
        playlist = self.subscribe(snapshot([str(i + 1) for i in range(105)]))
        first = self.library.detail(playlist["playlist_id"], 0)
        last = self.library.detail(playlist["playlist_id"], 100)
        self.assertTrue(first["more"])
        self.assertEqual(len(last["tracks"]), 5)
        self.assertEqual(last["tracks"][0]["position"], 101)
        self.assertNotIn("track_number", last["tracks"][0])

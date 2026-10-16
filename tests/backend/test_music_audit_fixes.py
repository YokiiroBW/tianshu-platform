import copy
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import test_music_downloads as downloads
import test_music_navidrome as navidrome
import test_music_selection as selection
from services.platform.contracts import Fault
from services.platform.media.music.audio import write_tags
from services.platform.media.music.catalog import Catalog
from services.platform.media.music.queue import enqueue_playlist
from services.platform.media.music.navidrome.paths import NativePaths
from services.platform.media.music.navidrome.state import LINK


class MusicAuditFixTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(downloads.MusicDownloadTests.setUpClass.__func__)
    asyncSetUp = downloads.MusicDownloadTests.asyncSetUp
    fixture_url = downloads.MusicDownloadTests.fixture_url
    value = downloads.MusicDownloadTests.value
    subscribe = downloads.MusicDownloadTests.subscribe
    queue = downloads.MusicDownloadTests.queue
    run_job = downloads.MusicDownloadTests.run_job
    file = downloads.MusicDownloadTests.file
    channels = selection.MusicSelectionTests.channels

    async def test_exports_only_stat_audio_and_do_not_rewrite_unchanged_manifests(self):
        self.queue()
        job = await self.run_job()
        folder = Path(
            self.music.downloads.storage.export(self.playlist["playlist_id"])["directory"]
        )
        before = {p.name: p.stat().st_mtime_ns for p in folder.iterdir()}
        with patch(
            "services.platform.media.music.storage.record",
            side_effect=AssertionError("Export must not read audio content"),
        ):
            result = self.music.downloads.storage.export(self.playlist["playlist_id"])
            self.assertEqual(result["exported"], 2)
            self.assertEqual({p.name: p.stat().st_mtime_ns for p in folder.iterdir()}, before)
            self.file(job).unlink()
            self.assertEqual(
                self.music.downloads.storage.export(self.playlist["playlist_id"])["exported"], 0
            )

    async def test_merged_jobs_serialize_and_existing_job_handles_new_playlist_requests(self):
        self.queue()
        other = self.value(["102"])
        other["playlist"]["source_id"] = "902"
        self.queue(self.subscribe(other))
        jobs = self.repo.jobs()[0]
        left, right = jobs
        with self.repo.transaction() as db:
            self.library.catalog.merge(left["recording_id"], right["recording_id"], db)
        first = self.repo.claim("first")
        self.assertIsNone(self.repo.claim("second"))
        await self.media.worker.run(first, "first")
        second = self.repo.claim("second")
        before = self.audio_transfers
        await self.media.worker.run(second, "second")
        self.assertEqual(self.audio_transfers, before)
        third = self.value(["101"])
        third["playlist"]["source_id"] = "903"
        playlist = self.subscribe(third)
        with self.repo.transaction() as db:
            enqueue_playlist(self.library, playlist["playlist_id"], db)
        self.assertEqual(self.repo.job_count(), 2)
        self.assertIsNone(self.repo.claim("third"))

    def test_bootstrap_normalizes_legacy_alias_jobs_without_changing_lease(self):
        self.queue()
        original = self.repo.jobs()[0][0]
        with self.repo.transaction() as db:
            other = self.library.catalog.register(
                "qq", {**original["track"], "source_id": "other", "album": "Other album"}, db
            )
            row = self.repo._document(db, "music_recording", original["recording_id"])
            self.repo.put(
                "music_recording",
                original["recording_id"],
                {**row, "merged_into": other["recording_id"]},
                db=db,
            )
            self.repo.put("music_catalog", "legacy_registered", {"version": 2}, db=db)
        claimed = self.repo.claim("legacy")
        with self.repo.transaction() as db:
            before = tuple(
                db.execute(
                    "SELECT owner,generation,lease FROM jobs WHERE id=?", (original["job_id"],)
                ).fetchone()
            )
        Catalog(self.repo).bootstrap()
        current = self.repo.job(original["job_id"])
        self.assertEqual(current["recording_id"], other["recording_id"])
        self.assertEqual(current["generation"], claimed["generation"])
        with self.repo.transaction() as db:
            self.assertEqual(
                tuple(
                    db.execute(
                        "SELECT owner,generation,lease FROM jobs WHERE id=?", (original["job_id"],)
                    ).fetchone()
                ),
                before,
            )

    async def test_cover_and_login_revision_changes_do_not_requeue_archived_audio(self):
        self.queue()
        completed = await self.run_job()
        changed = copy.deepcopy(self.api.value)
        changed["tracks"][0]["cover_url"] = self.origin + "/new-cover"
        with self.repo.transaction() as db:
            current = self.repo._document(db, "music_playlist", self.playlist["playlist_id"])
            self.library.install_snapshot(
                self.account,
                self.playlist["playlist_id"],
                changed,
                {**current, "auto_download": True},
                db,
            )
        self.library.save_account(
            {"user_id": "123", "label": "new login"}, {"MUSIC_U": "new-synthetic"}
        )
        with self.repo.transaction() as db:
            enqueue_playlist(self.library, self.playlist["playlist_id"], db)
        current = self.repo.job(completed["job_id"])
        self.assertEqual(current["state"], "completed")
        self.assertEqual(current["request_version"], completed["request_version"])
        self.assertIsNone(self.repo.claim("no-redundant-comparison"))

    async def test_relogin_resumes_only_auth_blocked_jobs_even_when_snapshot_unchanged(self):
        self.queue()
        job = self.repo.jobs()[0][0]
        self.repo.update_job(
            job["job_id"], {"state": "auth_required", "code": "music_auth_required"}
        )
        self.library.account_state(self.account, "auth_required")
        self.library.save_account(
            {"user_id": "123", "label": "restored"}, {"MUSIC_U": "new-synthetic"}
        )
        self.api.snapshot_since = AsyncMock(
            return_value={
                "playlist": self.api.value["playlist"],
                "entries": self.api.value["entries"],
                "unchanged": True,
            }
        )
        await self.music.scan(self.playlist["playlist_id"])
        self.assertEqual(self.repo.job(job["job_id"])["state"], "queued")
        finished = await self.run_job()
        self.assertEqual(finished["job_id"], job["job_id"])
        self.assertEqual(finished["state"], "completed")

    async def test_failed_best_download_stays_pending_then_upgrades_once(self):
        _, transfers = self.channels(
            {"netease": (self.audio["flac"], "lossless"), "qq": (self.audio["hires"], "hires")}
        )
        acquire = self.music.downloads.acquire

        async def failure(job, candidate, update):
            if candidate["provider"] == "qq":
                raise Fault("music_download_unavailable", 502)
            return await acquire(job, candidate, update)

        self.music.downloads.acquire = failure
        self.queue()
        first = await self.run_job()
        self.assertEqual(first["actual_quality"], "lossless")
        self.assertFalse(first["audio_routing"]["complete"])
        row = self.repo.list("music_quality_check")[0]
        self.assertEqual(row["outcome"], "partial")
        self.assertEqual(row["pending_channels"][0]["provider"], "qq")
        self.assertLess(row["next_check_at"] - row["checked_at"], 600)
        with self.repo.transaction() as db:
            playlist = self.repo._document(db, "music_playlist", self.playlist["playlist_id"])
            self.repo.put(
                "music_playlist",
                playlist["playlist_id"],
                {**playlist, "auto_download": True},
                db=db,
            )
            key, _ = self.music.maintenance.key(first, db)
            self.repo.put(
                "music_quality_check", key, {**row, "next_check_at": self.repo.clock() - 1}, db=db
            )
        self.music.downloads.acquire = acquire
        self.assertEqual(self.music.maintenance.plan()["state"], "reviewing")
        upgraded = await self.run_job()
        self.assertEqual(upgraded["actual_quality"], "hires")
        self.assertEqual(self.repo.list("music_quality_check")[0]["pending_channels"], [])
        self.playlist = self.repo.get("music_playlist", self.playlist["playlist_id"])
        self.queue()
        again = await self.run_job()
        self.assertTrue(again["audio_reused"])
        self.assertEqual(transfers, ["netease", "qq"])
        self.assertEqual(len(list(self.library_root.rglob("*.flac"))), 1)

    async def test_local_hires_becomes_preferred_and_originals_remain_intact(self):
        self.file_bytes, self.level = self.audio["mp3"], "standard"
        self.queue()
        old = await self.run_job()
        old_path = self.file(old)
        new = self.library_root / "external-hires.flac"
        new.write_bytes(self.audio["hires"])
        write_tags(new, self.api.value["tracks"][0], None, "")
        original = new.read_bytes()
        found = await self.music.imports.index(self.library_root, {"path": new.name})
        artifact = self.library.catalog.file(found)
        self.assertEqual(artifact["quality"]["level"], "hires")
        self.assertEqual(artifact["files"][0]["path"], new.name)
        self.music.downloads.storage.cleanup_retired()
        self.assertFalse(old_path.exists())
        self.assertEqual(new.read_bytes(), original)
        before = self.audio_transfers
        self.queue()
        repeated = await self.run_job()
        self.assertTrue(repeated["audio_reused"])
        self.assertEqual(self.audio_transfers, before)


class NavidromeAuditFixTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUpFoundation = navidrome.NavidromeTests.asyncSetUpFoundation
    asyncSetUp = navidrome.NavidromeTests.asyncSetUp
    subscribe = navidrome.NavidromeTests.subscribe
    archive = navidrome.NavidromeTests.archive
    link = navidrome.NavidromeTests.link
    control = navidrome.NavidromeTests.control
    prepare_ambiguous = navidrome.NavidromeTests.test_real_path_disambiguates_old_copies_and_repeat_sync_reuses_cached_read

    async def test_transient_native_failure_keeps_verified_members_then_recovers(self):
        await self.prepare_ambiguous()
        row = self.repo.get(LINK, self.link_id)
        before = copy.deepcopy(self.server.playlists[row["remote_id"]]["entry"])
        self.nav.client.paths.cache.clear()
        self.server.native_unavailable = True
        self.control("sync")
        await self.nav.tick()
        self.assertEqual(self.server.playlists[row["remote_id"]]["entry"], before)
        pending = self.repo.get(LINK, self.link_id)
        self.assertEqual(pending["members"][0]["code"], "verification_pending")
        self.assertEqual(pending["problem_count"], 3)
        self.server.native_unavailable = False
        self.nav.client.paths.retry_at = 0
        self.control("sync")
        await self.nav.tick()
        self.assertEqual(self.repo.get(LINK, self.link_id)["members"][0]["code"], "matched")
        self.assertEqual(self.server.creates, 1)

    async def test_changed_archive_does_not_retain_old_match_on_native_failure(self):
        await self.prepare_ambiguous()
        row = self.repo.get(LINK, self.link_id)
        recording = row["members"][0]["recording_id"]
        artifact = self.library.catalog.file(recording)
        artifact["files"][0]["sha256"] = "1" * 64
        with self.repo.transaction() as db:
            self.library.catalog.store_file(recording, artifact, db)
        self.nav.client.paths.cache.clear()
        self.server.native_unavailable = True
        self.control("sync")
        await self.nav.tick()
        self.assertEqual(
            [r["id"] for r in self.server.playlists[row["remote_id"]]["entry"]], ["102"]
        )


class NativePathBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_first_page_does_not_starve_later_identities(self):
        paths = NativePaths()
        calls = []

        async def request(connection, method, path, **kwargs):
            if method == "POST":
                return {"token": "synthetic"}
            identity = path.rsplit("/", 1)[1]
            calls.append(identity)
            return {
                "id": identity,
                "libraryId": 1,
                "libraryPath": "/music",
                "path": identity + ".flac",
                "size": 1,
            }

        paths.request = request
        connection = {
            "base_url": "http://127.0.0.1",
            "username": "synthetic",
            "password": "synthetic",
            "music_folder_id": "1",
        }
        identities = [f"{i:04}" for i in range(1101)]
        for _ in range(12):
            await paths.resolve(connection, identities)
        self.assertEqual(set(calls), set(identities))
        smaller = NativePaths()
        smaller.request = request
        await smaller.resolve(connection, identities[:101])
        result = await smaller.resolve(connection, identities[:101])
        self.assertEqual(len(result["paths"]), 101)
        self.assertEqual(result["pending"], set())

import hashlib
import json
import unittest

import test_music_downloads as fixtures
from services.platform.media.music.maintenance import CHECK, Maintenance, WEEK
from services.platform.media.music.request_control import Deferred


class MusicMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(fixtures.MusicDownloadTests.setUpClass.__func__)
    asyncSetUp = fixtures.MusicDownloadTests.asyncSetUp
    fixture_url = fixtures.MusicDownloadTests.fixture_url
    value = fixtures.MusicDownloadTests.value
    subscribe = fixtures.MusicDownloadTests.subscribe
    queue = fixtures.MusicDownloadTests.queue
    run_job = fixtures.MusicDownloadTests.run_job
    file = fixtures.MusicDownloadTests.file

    def enabled(self, enabled=True):
        with self.repo.transaction() as db:
            row = self.repo._document(db, "music_playlist", self.playlist["playlist_id"])
            self.repo.put(
                "music_playlist", row["playlist_id"], {**row, "auto_download": True}, db=db
            )
            return self.music.maintenance.save(
                {
                    "enabled": enabled,
                    "playlist_interval_seconds": 43200,
                    "expected_revision": self.music.maintenance.settings(db)["revision"],
                },
                db,
            )

    def due(self):
        for row in self.repo.list(CHECK):
            job = self.repo.job(row["job_id"])
            with self.repo.transaction() as db:
                key, _ = self.music.maintenance.key(job, db)
                self.repo.put(CHECK, key, {**row, "next_check_at": self.repo.clock() - 1}, db=db)

    async def test_weekly_check_reuses_job_across_playlists_and_does_not_download_equal_audio(self):
        self.queue()
        original = await self.run_job()
        path = self.file(original)
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        duplicate = self.value(["101"])
        duplicate["playlist"]["source_id"] = "901"
        second = self.subscribe(duplicate)
        with self.repo.transaction() as db:
            row = self.repo._document(db, "music_playlist", second["playlist_id"])
            self.repo.put(
                "music_playlist", row["playlist_id"], {**row, "auto_download": True}, db=db
            )
        self.enabled()
        self.due()
        self.assertEqual(self.music.maintenance.plan()["state"], "reviewing")
        self.assertEqual(Maintenance(self.music).plan()["state"], "reviewing")
        transfers = self.audio_transfers
        checked = await self.run_job()
        self.assertEqual(checked["job_id"], original["job_id"])
        self.assertTrue(checked["audio_reused"])
        self.assertEqual(self.audio_transfers, transfers)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(len(self.repo.list(CHECK)), 1)
        row = self.repo.list(CHECK)[0]
        self.assertAlmostEqual(row["next_check_at"] - row["checked_at"], WEEK)
        self.assertEqual(row["outcome"], "unchanged")
        self.assertEqual(self.repo.job_count(), 1)

    async def test_weekly_higher_quality_uses_existing_archive_replacement(self):
        self.queue()
        original = await self.run_job()
        old_digest = hashlib.sha256(self.file(original).read_bytes()).hexdigest()
        self.enabled()
        self.due()
        self.music.maintenance.plan()
        self.level, self.file_bytes = "hires", self.audio["hires"]
        upgraded = await self.run_job()
        self.assertEqual(upgraded["state"], "completed")
        self.assertFalse(upgraded["audio_reused"])
        self.assertEqual(upgraded["audio_quality"]["level"], "hires")
        self.assertNotEqual(
            hashlib.sha256(self.file(upgraded).read_bytes()).hexdigest(), old_digest
        )
        self.assertEqual(len(list(self.library_root.rglob("*.flac"))), 1)
        self.assertEqual(self.repo.list(CHECK)[0]["outcome"], "upgraded")
        self.assertEqual(self.repo.list(CHECK)[0]["upgrades"], 1)

    async def test_cooling_keeps_archive_and_schedules_channel_completion_independent_of_weekly_pause(
        self,
    ):
        self.queue()
        original = await self.run_job()
        before = self.file(original).read_bytes()
        self.enabled()
        self.due()
        self.music.maintenance.plan()
        until = self.repo.clock() + 1800

        async def deferred(*args):
            raise Deferred(until, "music_account_cooling")

        self.api.download = deferred
        job = await self.run_job()
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["attempts"], 0)
        self.assertEqual(self.file(job).read_bytes(), before)
        self.assertTrue(self.repo.list(CHECK)[0]["pending_channels"])
        self.enabled(False)
        self.assertEqual(Maintenance(self.music).plan()["state"], "scheduled")
        self.assertEqual(self.repo.job(job["job_id"])["state"], "completed")
        self.due()
        self.assertEqual(Maintenance(self.music).plan()["state"], "reviewing")
        self.assertTrue(self.repo.job(job["job_id"])["quality_catchup"])

    async def test_channel_completion_runs_with_backlog_and_reuses_equal_archive(self):
        self.queue()
        original = await self.run_job()
        before = (self.file(original).read_bytes(), self.audio_transfers)
        row = self.repo.list(CHECK)[0]
        with self.repo.transaction() as db:
            key, _ = self.music.maintenance.key(original, db)
            self.repo.put(
                CHECK,
                key,
                {
                    **row,
                    "next_check_at": self.repo.clock() - 1,
                    "pending_channels": [
                        {
                            "provider": "netease",
                            "account_id": self.account["account_id"],
                            "retry_at": self.repo.clock() - 1,
                            "code": "music_request_budget",
                            "stage": "entitlement",
                        }
                    ],
                },
                db=db,
            )
        later = self.value(["102"])
        later["playlist"]["source_id"] = "902"
        self.queue(self.subscribe(later))
        # An unfinished download comparison resumes even while weekly maintenance
        # is disabled and other new songs remain queued.
        with self.repo.transaction() as db:
            p = self.repo._document(db, "music_playlist", self.playlist["playlist_id"])
            self.repo.put("music_playlist", p["playlist_id"], {**p, "auto_download": True}, db=db)
        self.assertEqual(Maintenance(self.music).plan()["state"], "reviewing")
        result = await self.run_job()
        self.assertEqual(result["job_id"], original["job_id"])
        self.assertTrue(result["audio_reused"])
        self.assertEqual((self.file(result).read_bytes(), self.audio_transfers), before)
        self.assertEqual(self.repo.list(CHECK)[0]["pending_channels"], [])

    async def test_12_hour_sync_is_staggered_and_foreground_jobs_precede_reviews(self):
        self.queue()
        original = await self.run_job()
        second = self.value(["102"])
        second["playlist"]["source_id"] = "902"
        self.subscribe(second)
        self.enabled()
        self.due()
        playlists = self.repo.list("music_playlist")
        self.assertTrue(all(p["interval_seconds"] == 43200 for p in playlists))
        self.assertEqual(len({p["next_scan_at"] for p in playlists}), 2)
        self.assertTrue(all(0 < p["next_scan_at"] - self.repo.clock() <= 43200 for p in playlists))
        self.music.maintenance.plan()
        fresh = {
            **original,
            "job_id": "new-song",
            "recording_id": "new-recording",
            "state": "queued",
            "quality_review": False,
            "created_at": self.repo.clock() + 1,
        }
        with self.repo.transaction() as db:
            db.execute(
                "INSERT INTO jobs(id,dedupe,state,created,updated,body) VALUES(?,?,?,?,?,?)",
                (
                    "new-song",
                    "new-song",
                    "queued",
                    fresh["created_at"],
                    fresh["created_at"],
                    json.dumps(fresh),
                ),
            )
        self.assertEqual(self.music.maintenance.plan()["state"], "waiting_downloads")
        self.assertEqual(self.repo.claim("new-first")["job_id"], "new-song")

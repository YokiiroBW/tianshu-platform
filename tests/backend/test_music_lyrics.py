import asyncio
import unittest
from unittest.mock import patch

import test_music_downloads as fixtures
from services.platform.contracts import Fault
from services.platform.media.music.lyrics_storage import KIND, archive_key
from services.platform.media.music.lyrics_text import aligned, coverage, lines, normalize, render
from services.platform.media.music.request_control import Deferred


class LyricTextTests(unittest.TestCase):
    def test_timestamps_offset_repeated_lines_and_bilingual_render(self):
        value = {
            "original": "[offset:100]\n[00:01.100][00:02.100]hello",
            "translated": "[00:01.00]你好\n[00:02.00]你好",
        }
        self.assertEqual(lines(value["original"]), [(1000, "hello"), (2000, "hello")])
        self.assertEqual(render(value), "[00:01.000]hello ／ 你好\n[00:02.000]hello ／ 你好")
        self.assertTrue(coverage(value)["translation_synced"])

    def test_misaligned_translation_is_retained_but_not_guessed(self):
        value = {"original": "[00:01.00]hello", "translated": "[00:20.00]你好"}
        self.assertFalse(aligned(value["original"], value["translated"]))
        self.assertEqual(render(value), value["original"])
        self.assertFalse(normalize({"original": "[03:00.00]late"}, 3000))
        self.assertFalse(normalize({"original": "[by:credits]"}))


class MusicLyricsTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(fixtures.MusicDownloadTests.setUpClass.__func__)
    asyncSetUp = fixtures.MusicDownloadTests.asyncSetUp
    fixture_url = fixtures.MusicDownloadTests.fixture_url
    value = fixtures.MusicDownloadTests.value
    subscribe = fixtures.MusicDownloadTests.subscribe
    queue = fixtures.MusicDownloadTests.queue
    run_job = fixtures.MusicDownloadTests.run_job
    file = fixtures.MusicDownloadTests.file

    async def test_plain_lyrics_are_upgraded_from_another_channel_without_changing_audio(self):
        async def plain(*args):
            return {"original": "hello"}

        self.api.lyrics = plain
        job = await self.archive()
        audio = self.file(job)
        before = (audio.read_bytes(), audio.stat().st_mtime_ns)

        async def search(query, cookie):
            return [{**job["track"], "source_id": "qq-test", "provider": "qq"}]

        async def timed(*args):
            return {"original": "[00:01.00]hello"}

        self.qq(search, timed)
        result = await self.music.lyrics.tick(job["recording_id"])
        self.assertTrue(result["coverage"]["synced"])
        self.assertEqual(result["source"]["provider"], "qq")
        self.assertEqual((audio.read_bytes(), audio.stat().st_mtime_ns), before)

    async def test_empty_results_wait_a_week_and_audio_upgrade_invalidates_lyric_check(self):
        async def empty(*args):
            return {}

        self.api.lyrics = empty
        job = await self.archive()
        result = await self.music.lyrics.tick(job["recording_id"])
        self.assertEqual(result["state"], "missing")
        self.assertGreaterEqual(result["next_check_at"], self.repo.clock() + 7 * 86400 - 1)
        artifact = self.library.catalog.file(job["recording_id"])
        artifact["files"][0]["sha256"] = "f" * 64
        with self.repo.transaction() as db:
            self.library.catalog.store_file(job["recording_id"], artifact, db)
        self.assertEqual(self.music.lyrics.plan()["recording_id"], job["recording_id"])

    async def archive(self, missing=False):
        if missing:

            async def no_lyrics(*args):
                raise Fault("music_upstream_unavailable", 502)

            self.api.lyrics = no_lyrics
        self.queue()
        job = await self.run_job()
        self.assertEqual(job["state"], "completed", job)
        return job

    def qq(self, search, lyrics):
        from types import SimpleNamespace

        self.library.save_account(
            {"user_id": "321", "label": "Synthetic QQ"}, {"cookie": "synthetic"}, "qq"
        )

        async def close():
            pass

        self.music.connectors["qq"] = SimpleNamespace(
            search=search, lyrics_track=lyrics, close=close
        )

    async def test_new_download_bilingual_tags_and_idempotent_sidecars(self):
        job = await self.archive()
        audio = self.file(job)
        before = (audio.read_bytes(), audio.stat().st_mtime_ns, self.audio_transfers)
        first = await self.music.lyrics.tick(job["recording_id"])
        self.assertTrue(first["coverage"]["translation_synced"])
        lrc = audio.with_suffix(".lrc")
        self.assertIn("合成歌词 ／ 合成译文", lrc.read_text(encoding="utf8"))
        stamp = lrc.stat().st_mtime_ns
        second = await self.music.lyrics.tick(job["recording_id"])
        self.assertFalse(second["changed"])
        self.assertEqual(lrc.stat().st_mtime_ns, stamp)
        self.assertEqual(
            (audio.read_bytes(), audio.stat().st_mtime_ns, self.audio_transfers), before
        )
        self.assertEqual(len(self.repo.list(KIND)), 1)
        self.assertFalse(self.repo.list("music_retired_file"))

    async def test_existing_missing_lyrics_falls_back_without_audio_calls(self):
        job = await self.archive(missing=True)
        audio = self.file(job)
        before = (audio.read_bytes(), audio.stat().st_mtime_ns, self.audio_transfers)
        calls = []

        async def search(query, cookie):
            calls.append("search")
            return [{**job["track"], "source_id": "qq-test", "provider": "qq"}]

        async def lyrics(track, cookie):
            calls.append("lyrics")
            return {"original": "[00:01.00]hello", "translated": "[00:01.00]你好"}

        self.qq(search, lyrics)
        result = await self.music.lyrics.tick(job["recording_id"])
        self.assertEqual(result["source"]["provider"], "qq")
        self.assertEqual(calls, ["search", "lyrics"])
        self.assertTrue(result["coverage"]["translation_synced"])
        self.assertEqual(
            (audio.read_bytes(), audio.stat().st_mtime_ns, self.audio_transfers), before
        )
        self.assertEqual(self.repo.job_count(), 1)
        self.assertTrue(
            self.music.downloads.storage.matches(self.library.catalog.file(job["recording_id"]))
        )
        self.assertIsNotNone(self.music.downloads.storage.available(job))

    async def test_failure_is_durable_low_frequency_and_other_provider_continues(self):
        job = await self.archive(missing=True)

        async def limited(*args):
            raise Deferred(self.repo.clock() + 900, "music_request_budget")

        self.api.lyrics = limited

        async def search(query, cookie):
            return [{**job["track"], "source_id": "qq-test", "provider": "qq"}]

        async def lyrics(*args):
            return {"original": "[00:01.00]hello"}

        self.qq(search, lyrics)
        result = await self.music.lyrics.tick(job["recording_id"])
        self.assertTrue(result["coverage"]["synced"])
        self.assertEqual(result["state"], "retry_wait")
        self.assertGreaterEqual(result["next_check_at"], self.repo.clock() + 21599)
        self.assertIsNone(self.music.lyrics.plan())
        self.assertEqual(self.repo.job_count(), 1)
        self.assertEqual(self.audio_transfers, 1)

    async def test_old_catalog_backfill_and_duplicate_subscription_share_one_check(self):
        await self.archive()
        with self.repo.transaction() as db:
            db.execute("DELETE FROM documents WHERE kind=?", (KIND,))
        duplicate = self.value(["101"])
        duplicate["playlist"]["source_id"] = "901"
        self.subscribe(duplicate)
        await self.music.lyrics.tick()
        self.assertEqual(len(self.repo.list(KIND)), 1)
        self.assertIsNone(self.music.lyrics.plan())
        self.assertEqual(self.audio_transfers, 1)

    async def test_ambiguous_match_and_other_recording_lyrics_are_rejected(self):
        job = await self.archive(missing=True)

        async def search(query, cookie):
            return [{**job["track"], "source_id": str(i), "provider": "qq"} for i in (3, 4)]

        async def lyrics(*args):
            self.fail("ambiguous lyrics must not be fetched")

        self.qq(search, lyrics)
        result = await self.music.lyrics.tick(job["recording_id"])
        self.assertFalse(result["coverage"]["original"])
        self.assertTrue(any(r["code"] == "music_match_ambiguous" for r in result["providers"]))

    async def test_interrupted_sidecar_publication_recovers_without_cloud_or_audio(self):
        job = await self.archive()
        with patch(
            "services.platform.media.music.lyrics_storage.sync_directory",
            side_effect=OSError("synthetic crash"),
        ):
            # Remove the newly generated sidecar to exercise a real replacement.
            self.file(job).with_suffix(".lrc").unlink()
            self.assertIsNone(await self.music.lyrics.tick(job["recording_id"]))
        self.assertIn("pending", self.repo.get(KIND, job["recording_id"]))

        async def forbidden(*args):
            self.fail("recovery must not fetch cloud lyrics")

        self.api.lyrics = forbidden
        result = await self.music.lyrics.tick(job["recording_id"])
        self.assertTrue(result["coverage"]["synced"])
        self.assertNotIn("pending", self.repo.get(KIND, job["recording_id"]))
        self.assertEqual(self.audio_transfers, 1)

    async def test_concurrent_lyrics_workers_are_serialized(self):
        job = await self.archive(missing=True)
        entered, release = asyncio.Event(), asyncio.Event()

        async def lyrics(*args):
            entered.set()
            await release.wait()
            return {"original": "[00:01.00]hello"}

        self.api.lyrics = lyrics
        first = asyncio.create_task(self.music.lyrics.tick(job["recording_id"]))
        try:
            await asyncio.wait_for(entered.wait(), 10)
            self.assertIsNone(await self.music.lyrics.tick(job["recording_id"]))
        finally:
            release.set()
            await first
        self.assertEqual(len(self.repo.list(KIND)), 1)

    async def test_missing_sidecar_never_invalidates_reusable_audio(self):
        job = await self.archive()
        self.file(job).with_suffix(".lrc").unlink()
        self.assertIsNotNone(self.music.downloads.storage.available(job))
        await self.music.lyrics.tick(job["recording_id"])
        self.assertTrue(self.file(job).with_suffix(".lrc").is_file())
        self.assertEqual(
            archive_key(self.library.catalog.file(job["recording_id"])),
            self.repo.get(KIND, job["recording_id"])["archive_key"],
        )

import asyncio
import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from PIL import Image

from music_fixtures import SyntheticNetease, snapshot
from services.platform.contracts import Fault
from services.platform.media.accounts import Accounts
from services.platform.media.music.audio import record
from services.platform.media.music.downloads import settled
from services.platform.media.music.runtime import Music
from services.platform.media.music.storage import component, stem
from services.platform.media.music.transfer import Transfer, cdn_url
from services.platform.media.repository import Repository
from services.platform.media.worker import Worker


class MusicDownloadTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = os.environ.get("MEDIA_TEST_FFMPEG") or shutil.which("ffmpeg")
        cls.ffprobe = os.environ.get("MEDIA_TEST_FFPROBE") or shutil.which("ffprobe")
        if not cls.ffmpeg or not cls.ffprobe:
            raise unittest.SkipTest("Real audio validation requires ffmpeg and ffprobe")
        cls.fixture = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.fixture.cleanup)
        cls.audio = {}
        for extension, codec in (("flac", "flac"), ("mp3", "libmp3lame"), ("m4a", "aac")):
            target = Path(cls.fixture.name) / ("tone." + extension)
            subprocess.run(
                [
                    cls.ffmpeg,
                    "-nostdin",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=3",
                    "-c:a",
                    codec,
                    str(target),
                ],
                check=True,
            )
            cls.audio[extension] = target.read_bytes()
        for name, extension, codec, options in (
            ("alac", "m4a", "alac", []),
            ("hires", "flac", "flac", ["-sample_fmt", "s32", "-ar", "96000"]),
        ):
            target = Path(cls.fixture.name) / (name + "." + extension)
            subprocess.run(
                [
                    cls.ffmpeg,
                    "-nostdin",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=3",
                    "-c:a",
                    codec,
                    *options,
                    str(target),
                ],
                check=True,
            )
            cls.audio[name] = target.read_bytes()
        output = io.BytesIO()
        Image.new("RGB", (32, 32), "blue").save(output, format="PNG")
        cls.image = output.getvalue()

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library_root = self.root / "library"
        self.library_root.mkdir()
        self.repo = Repository(self.root / "media.sqlite")
        self.received = []
        self.file_bytes = self.audio["flac"]
        self.level = "lossless"
        self.offered_md5 = None

        async def serve(request):
            self.received.append(dict(request.headers))
            return web.Response(body=self.image if request.path == "/cover" else self.file_bytes)

        app = web.Application()
        app.router.add_get("/file", serve)
        app.router.add_get("/cover", serve)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.addAsyncCleanup(self.runner.cleanup)
        self.origin = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        self.media = SimpleNamespace(
            repo=self.repo,
            accounts=Accounts(self.repo),
            local=asyncio.to_thread,
            config={
                "staging_directory": str(self.root / "staging"),
                "targets": {},
                "engine": {"ffmpeg": self.ffmpeg, "ffprobe": self.ffprobe, "max_bytes": 1024**3},
            },
            wake=asyncio.Event(),
        )
        self.music = self.media.music = Music(self.media)
        self.media.worker = Worker(self.media)
        self.addAsyncCleanup(self.music.close)
        self.api = self.music.connector = SyntheticNetease()
        self.api.download_calls = 0

        async def download(identity, level, cookie):
            self.api.download_calls += 1
            return {
                "url": self.origin + "/file",
                "level": self.level,
                "size": len(self.file_bytes),
                "md5": self.offered_md5 or hashlib.md5(self.file_bytes).hexdigest(),
            }

        async def lyrics(identity, cookie):
            return {
                "original": "[00:00.00]合成歌词",
                "translated": "[00:00.00]合成译文",
                "romanized": "",
            }

        async def album(identity, cookie):
            return {"album_artists": ["专辑艺术家"], "date": "2026-01-02"}

        self.api.download, self.api.lyrics, self.api.album = download, lyrics, album
        self.music.downloads.transfer = Transfer(validate_url=self.fixture_url)
        self.library = self.music.library
        saved = self.library.save_account(
            {"user_id": "123", "label": "合成账号"}, {"MUSIC_U": "synthetic-secret"}
        )
        self.account, _ = self.library.account(saved["account_id"])
        with self.repo.transaction() as db:
            self.library.save_settings(
                {
                    "directory": str(self.library_root),
                    "layout": "artist_album",
                    "expected_revision": 0,
                },
                db,
            )
        self.api.value = self.value(["101", "101", "103"])
        self.playlist = self.subscribe(self.api.value)

    def fixture_url(self, url):
        self.assertIn(url, {self.origin + "/file", self.origin + "/cover"})
        return url

    def value(self, entries):
        value = snapshot(entries)
        for track in value["tracks"]:
            track.update(
                duration_ms=3000,
                track_number=4,
                disc="1",
                album_id="11",
                cover_url=self.origin + "/cover",
            )
        return value

    def subscribe(self, value):
        with self.repo.transaction() as db:
            return self.library.subscribe(self.account, value, 3600, 0, db)["playlist"]

    def queue(self, playlist=None):
        with self.repo.transaction() as db:
            return self.library.download(
                {
                    "playlist_id": (playlist or self.playlist)["playlist_id"],
                    "expected_revision": (playlist or self.playlist)["revision"],
                },
                db,
            )

    async def run_job(self):
        job = self.repo.claim("fixture")
        self.assertIsNotNone(job)
        await self.media.worker.run(job, "fixture")
        return self.repo.job(job["job_id"])

    def file(self, job, kind="audio"):
        return self.library_root / next(
            f["path"] for f in job["audio_receipt"]["files"] if f["kind"] == kind
        )

    async def test_real_flac_tags_sidecars_ordered_export_and_cleanup(self):
        result = self.queue()
        self.assertEqual((result["total"], result["unavailable"]), (1, 1))
        job = await self.run_job()
        self.assertEqual(job["state"], "completed", job)
        path = self.file(job)
        self.assertEqual(
            path.relative_to(self.library_root).as_posix(),
            "合成艺术家/合成专辑/合成艺术家 - 歌曲 101.flac",
        )
        tags = FLAC(path)
        self.assertEqual(tags["title"], ["歌曲 101"])
        self.assertEqual(tags["albumartist"], ["专辑艺术家"])
        self.assertEqual(tags["tracknumber"], ["4"])
        self.assertEqual(tags["date"], ["2026-01-02"])
        self.assertEqual(tags["lyrics"], ["[00:00.00]合成歌词"])
        self.assertEqual(len(tags.pictures), 1)
        self.assertEqual(job["audio_quality"]["codec"], "flac")
        self.assertTrue(job["audio_quality"]["lossless"])
        self.assertEqual(self.repo.get("music_stage", job["job_id"])["state"], "cleaned")
        self.assertFalse(list((self.root / "staging").rglob("*.flac")))
        self.assertTrue(all("Cookie" not in headers for headers in self.received))
        exported = self.music.downloads.storage.export(self.playlist["playlist_id"])
        self.assertEqual((exported["exported"], exported["total"]), (2, 3))
        folder = Path(exported["directory"])
        data = json.loads((folder / "playlist.json").read_bytes())
        self.assertEqual([t["source_id"] for t in data["tracks"]], ["101", "101", "103"])
        self.assertIsNone(data["tracks"][-1]["file"])
        self.assertEqual((folder / "playlist.m3u8").read_text("utf-8").count(".flac"), 2)
        self.assertNotIn("synthetic-secret", self.file(job, "metadata").read_text("utf-8"))
        self.assertNotIn(
            self.origin + "/file", self.repo.database.read_bytes().decode("utf-8", errors="ignore")
        )

    async def test_mp3_and_m4a_tags_keep_actual_lossy_quality(self):
        for index, extension in enumerate(("mp3", "m4a")):
            with self.subTest(extension=extension):
                value = self.value([str(201 + index)])
                value["playlist"]["source_id"] = str(901 + index)
                playlist = self.subscribe(value)
                self.file_bytes, self.level = self.audio[extension], "exhigh"
                self.queue(playlist)
                job = await self.run_job()
                self.assertEqual(job["state"], "completed", job)
                self.assertTrue(job["audio_quality"]["fallback"])
                self.assertFalse(job["audio_quality"]["lossless"])
                audio = MP3(self.file(job)) if extension == "mp3" else MP4(self.file(job))
                self.assertIn("TIT2" if extension == "mp3" else "\u00a9nam", audio)
                self.assertIn("APIC:" if extension == "mp3" else "covr", audio)

    async def test_strict_quality_refuses_lossy_instead_of_transcoding(self):
        self.file_bytes, self.level = self.audio["mp3"], "exhigh"
        with self.repo.transaction() as db:
            self.library.downloads(
                {
                    "playlist_id": self.playlist["playlist_id"],
                    "expected_revision": 1,
                    "auto_download": True,
                    "quality": {"level": "lossless", "allow_fallback": False},
                },
                db,
            )
        job = await self.run_job()
        self.assertEqual(job["code"], "music_quality_unavailable")
        self.assertFalse(list(self.library_root.rglob("*.mp3")))

    async def test_hires_and_alac_keep_their_original_stream_properties(self):
        for index, name in enumerate(("hires", "alac")):
            value = self.value([str(401 + index)])
            value["playlist"]["source_id"] = str(991 + index)
            playlist = self.subscribe(value)
            self.file_bytes = self.audio[name]
            self.level = "hires" if name == "hires" else "lossless"
            self.queue(playlist)
            job = await self.run_job()
            self.assertEqual(job["state"], "completed", job)
            self.assertTrue(job["audio_quality"]["lossless"])
            if name == "hires":
                self.assertGreater(job["audio_quality"]["bits_per_sample"], 16)
                self.assertEqual(job["audio_quality"]["sample_rate"], 96000)
            else:
                self.assertEqual(job["audio_quality"]["codec"], "alac")
                self.assertIn("covr", MP4(self.file(job)))

    async def test_allowed_fallback_resolves_a_lower_level_when_high_level_is_denied(self):
        self.file_bytes, self.level = self.audio["mp3"], "exhigh"
        original = self.api.download
        levels = []

        async def download(identity, level, cookie):
            levels.append(level)
            if level == "lossless":
                raise Fault("music_download_forbidden", 409)
            return await original(identity, level, cookie)

        self.api.download = download
        self.queue()
        job = await self.run_job()
        self.assertEqual(job["state"], "completed", job)
        self.assertEqual(levels, ["lossless", "exhigh"])
        self.assertTrue(job["audio_quality"]["fallback"])

    async def test_false_lossless_claim_and_preview_rejected(self):
        self.file_bytes = self.audio["mp3"]
        self.queue()
        job = await self.run_job()
        self.assertEqual(job["code"], "music_quality_mismatch")
        self.file_bytes = self.audio["flac"]
        job["track"]["duration_ms"] = 120000
        self.repo.update_job(job["job_id"], {"state": "queued", "track": job["track"]})
        job = await self.run_job()
        self.assertEqual(job["code"], "music_preview_rejected")

    async def test_corrupt_download_md5_never_published(self):
        self.offered_md5 = "0" * 32
        self.queue()
        job = await self.run_job()
        self.assertEqual(job["code"], "music_download_incomplete")
        self.assertFalse(list(self.library_root.rglob("*.flac")))

    async def test_reuse_across_playlists_and_manual_retry_no_network(self):
        self.queue()
        job = await self.run_job()
        before = record(self.file(job))
        value = self.value(["101"])
        value["playlist"]["source_id"] = "901"
        playlist = self.subscribe(value)
        self.queue(playlist)
        self.assertEqual(len(self.repo.jobs()[0]), 1)
        job = await self.run_job()
        self.assertTrue(job["audio_reused"])
        self.assertEqual(self.api.download_calls, 1)
        self.assertEqual(record(self.file(job)), before)
        detail = self.library.detail(playlist["playlist_id"], 0)
        self.assertEqual(detail["tracks"][0]["audio_state"], "completed")

    async def test_original_files_untouched_on_name_collision(self):
        track = self.api.value["tracks"][0]
        original = self.library_root / (stem(track, "artist_album") + ".flac")
        original.parent.mkdir(parents=True)
        original.write_bytes(b"original-not-managed")
        self.queue()
        job = await self.run_job()
        self.assertEqual(job["state"], "completed", job)
        self.assertNotEqual(self.file(job), original)
        self.assertIn("网易云101", self.file(job).name)
        self.assertEqual(original.read_bytes(), b"original-not-managed")

    async def test_lost_publication_ack_recovers_without_redownload(self):
        self.queue()
        with patch.object(
            self.music.downloads.storage,
            "finish_job",
            side_effect=Fault("music_publication_unverified", 409),
        ):
            job = await self.run_job()
        self.assertEqual(job["state"], "failed")
        self.queue()
        job = await self.run_job()
        self.assertEqual(job["state"], "completed", job)
        self.assertEqual(self.api.download_calls, 1)
        self.assertEqual(len(list(self.library_root.rglob("*.flac"))), 1)

    async def test_partial_publication_recovers_without_duplicate_audio(self):
        self.queue()
        link = os.link
        calls = 0

        def fail_once(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic interruption")
            return link(source, target)

        with patch("services.platform.media.music.storage.os.link", side_effect=fail_once):
            job = await self.run_job()
        self.assertEqual(job["state"], "failed")
        self.assertEqual(len(list(self.library_root.rglob("*.flac"))), 1)
        self.queue()
        job = await self.run_job()
        self.assertEqual(job["state"], "completed", job)
        self.assertEqual(self.api.download_calls, 1)

    async def test_tampered_or_missing_published_file_gets_new_copy_without_overwrite(self):
        self.queue()
        job = await self.run_job()
        original = self.file(job)
        original.write_bytes(b"externally-changed")
        self.queue()
        job = await self.run_job()
        self.assertEqual(job["state"], "completed", job)
        self.assertNotEqual(original, self.file(job))
        self.assertEqual(original.read_bytes(), b"externally-changed")
        self.assertEqual(self.api.download_calls, 2)

    async def test_auto_sync_only_adds_new_jobs_and_preserves_audio_facts(self):
        with self.repo.transaction() as db:
            self.playlist = self.library.downloads(
                {
                    "playlist_id": self.playlist["playlist_id"],
                    "expected_revision": 1,
                    "auto_download": True,
                    "quality": {"level": "lossless", "allow_fallback": True},
                },
                db,
            )["playlist"]
        first = await self.run_job()
        self.api.value = self.value(["101", "102"])
        await self.music.scan(self.playlist["playlist_id"])
        self.assertEqual(len(self.repo.jobs()[0]), 2)
        detail = self.library.detail(self.playlist["playlist_id"], 0)
        self.assertEqual([t["audio_state"] for t in detail["tracks"]], ["completed", "queued"])
        self.api.value["tracks"][0]["availability"] = "unavailable"
        await self.music.scan(self.playlist["playlist_id"])
        detail = self.library.detail(self.playlist["playlist_id"], 0)
        self.assertEqual(detail["tracks"][0]["audio_state"], "completed")
        self.api.value = self.value(["102"])
        await self.music.scan(self.playlist["playlist_id"])
        self.assertTrue(self.file(first).exists())

    async def test_changed_root_blocks_pending_download(self):
        self.queue()
        with self.repo.transaction() as db:
            self.library.save_settings(
                {
                    "directory": str(self.root / "other"),
                    "layout": "artist_album",
                    "expected_revision": 1,
                },
                db,
            )
        job = await self.run_job()
        self.assertEqual(job["code"], "scope_changed")
        self.assertEqual(self.api.download_calls, 0)

    async def test_revoked_account_blocks_pending_download(self):
        self.queue()
        with self.repo.transaction() as db:
            self.library.revoke(
                {
                    "account_id": self.account["account_id"],
                    "expected_revision": self.account["revision"],
                },
                db,
            )
        job = await self.run_job()
        self.assertEqual(job["state"], "auth_required")
        self.assertEqual(self.api.download_calls, 0)

    async def test_optional_lyrics_failure_is_explicit_and_does_not_lose_audio(self):
        async def failed(*args):
            raise Fault("music_upstream_unavailable", 502)

        self.api.lyrics = failed
        self.queue()
        job = await self.run_job()
        self.assertEqual(job["state"], "completed", job)
        self.assertIn("music_lyrics_unavailable", job["audio_receipt"]["warnings"])
        self.assertFalse(list(self.library_root.rglob("*.lrc")))

    async def test_deleted_job_can_reuse_durable_artifact(self):
        self.queue()
        first = await self.run_job()
        with self.repo.transaction() as db:
            self.repo.delete_job(first["job_id"], first["revision"], db=db)
        self.queue()
        second = await self.run_job()
        self.assertNotEqual(first["job_id"], second["job_id"])
        self.assertTrue(second["audio_reused"])
        self.assertEqual(self.api.download_calls, 1)

    async def test_mixed_queue_claims_do_not_use_fake_bvid(self):
        self.queue()
        first = self.repo.claim("first")
        self.assertNotIn("bvid", first)
        self.repo.enqueue(
            {
                "bvid": "BV1SYNTHETIC",
                "selected_cids": ["1"],
                "target_id": "video",
                "quality": {"mode": "best"},
                "layout": "single",
            }
        )
        video = self.repo.claim("second")
        self.assertEqual(video["bvid"], "BV1SYNTHETIC")
        other = copy.deepcopy(first)
        other.update(
            audio_key="other-policy", quality={"level": "standard", "allow_fallback": True}
        )
        self.repo.enqueue(other)
        self.assertIsNone(self.repo.claim("third"))

    async def test_executor_is_settled_even_after_two_cancellations(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def operation():
            entered.set()
            release.wait(5)
            finished.set()

        task = asyncio.create_task(settled(asyncio.to_thread, operation))
        await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.01)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set())

    async def test_cancel_during_publication_settles_copy_and_can_recover(self):
        from services.platform.media.video_storage import copy_file

        self.queue()
        job = self.repo.claim("fixture")
        entered, release = threading.Event(), threading.Event()

        def delayed(source, target):
            entered.set()
            release.wait(5)
            copy_file(source, target)

        with patch("services.platform.media.music.storage.copy_file", side_effect=delayed):
            task = asyncio.create_task(self.media.worker.run(job, "fixture"))
            self.assertTrue(await asyncio.to_thread(entered.wait, 4))
            self.repo.update_job(job["job_id"], {"cancel_requested": True})
            task.cancel()
            await asyncio.sleep(0.01)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        cancelled = self.repo.job(job["job_id"])
        self.assertEqual(cancelled["state"], "cancelled")
        self.queue()
        final = await self.run_job()
        self.assertEqual(final["state"], "completed", final)
        self.assertEqual(self.api.download_calls, 1)

    def test_portable_names_and_only_netease_cdn(self):
        self.assertEqual(component("CON"), "_CON")
        self.assertNotIn("/", component("../bad/name:*"))
        self.assertEqual(
            cdn_url("http://m10.music.126.net/song.mp3"), "https://m10.music.126.net/song.mp3"
        )
        for value in (
            "https://music.126.net.evil.example/a",
            "http://127.0.0.1/a",
            "https://u:p@m10.music.126.net/a",
            "https://m10.music.126.net:8443/a",
        ):
            with self.subTest(value=value), self.assertRaises(Fault):
                cdn_url(value)

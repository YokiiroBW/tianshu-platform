"""Actual download counts over real SQLite/files, including separate worker processes."""

import asyncio
import hashlib
import json
import multiprocessing
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.platform.contracts import Fault, digest
from services.platform.media.destination import (
    head_key,
    snapshot,
    source_subdirectory,
    subdirectory,
)
from services.platform.media.download_cache import DownloadCache
from services.platform.media.repository import Repository
from services.platform.media.organizer import library_lock
from services.platform.media.retention import source_directory

BV = "BV1xx411c7mD"
CID = "123"
PART = {"cid": CID, "index": 1, "duration_seconds": 2}
EXACT = {"mode": "exact", "quality_id": "80", "allow_fallback": False}


class CountingEngine:
    def __init__(self, root, *, delay=0):
        self.root, self.delay = Path(root), delay
        self.calls = []
        self.available = ["80", "64"]
        self.entered = asyncio.Event()
        self.release = None
        self.status = {"state": "ready", "code": "ready"}

    async def check(self):
        return self.status

    async def formats(self, *args):
        return [{"quality_id": value} for value in self.available]

    async def download(self, job, part, cookie, directory):
        quality = job["quality"]["quality_id"]
        self.calls.append(quality)
        with (self.root / "actual-calls.txt").open("a") as out:
            out.write(quality + "\n")
        self.entered.set()
        if self.release:
            await self.release.wait()
        await asyncio.sleep(self.delay)
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "download.mp4"
        path.write_bytes(("fixture-video-" + quality).encode())
        return {"file": str(path), "actual_quality": quality, "extension": "mp4"}


async def local(function, *args, **kwargs):
    return await asyncio.to_thread(function, *args, **kwargs)


async def resolve(*args, **kwargs):
    return {"snapshot": {"parts": [PART], "captured_at": "2026-10-12T00:00:00Z"}}


def media_for(root, *, clock=time.time, delay=0):
    root = Path(root)
    return SimpleNamespace(
        repo=Repository(root / "media.sqlite", clock=clock),
        engine=CountingEngine(root, delay=delay),
        connector=SimpleNamespace(resolve=resolve),
        config={"staging_directory": str(root / "staging")},
        local=local,
    )


def enqueue(media, target, quality=None, destination=None):
    return media.repo.enqueue(
        {
            "bvid": BV,
            "cid": CID,
            "selected_cids": [CID],
            "target_id": target,
            "quality": quality or dict(EXACT),
            "layout": "single",
            "snapshot": {"parts": [PART]},
            "account_id": None,
            **(destination or {}),
        }
    )


def process_download(root, identity, owner):
    async def run():
        media = media_for(root, delay=0.6)
        job = media.repo.job(identity)
        result, reused = await DownloadCache(media).obtain(job, PART, None, owner, "80")
        (Path(root) / (owner + ".result")).write_text(str(reused) + ":" + result["actual_quality"])

    asyncio.run(run())


class GlobalDownloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = media_for(self.root)

    def claimed(self, target, owner, quality=None):
        enqueue(self.media, target, quality)
        return self.media.repo.claim(owner)

    async def get(self, job, owner, media=None):
        media = media or self.media
        cache = DownloadCache(media)
        quality = await cache.selected_quality(job, PART, None)
        return await cache.obtain(job, PART, None, owner, quality)

    async def test_concurrent_independent_repositories_download_once(self):
        first = self.claimed("one", "one")
        second = self.claimed("two", "two")
        other = media_for(self.root, delay=0.3)
        self.media.engine.delay = 0.3
        results = await asyncio.gather(self.get(first, "one"), self.get(second, "two", other))
        self.assertEqual(sorted(reused for _, reused in results), [False, True])
        self.assertEqual((self.root / "actual-calls.txt").read_text().splitlines(), ["80"])
        self.assertNotEqual(results[0][0]["file"], results[1][0]["file"])
        self.assertTrue(Path(results[0][0]["file"]).samefile(results[1][0]["file"]))
        self.assertNotIn("attempts", self.media.repo.job(second["job_id"]))

    async def test_separate_processes_download_once(self):
        first = self.claimed("one", "one")
        second = self.claimed("two", "two")
        context = multiprocessing.get_context("spawn")
        processes = [
            context.Process(target=process_download, args=(str(self.root), j["job_id"], o))
            for j, o in [(first, "one"), (second, "two")]
        ]
        for process in processes:
            process.start()
        for process in processes:
            await asyncio.to_thread(process.join, 15)
            if process.is_alive():
                process.terminate()
                process.join()
            self.assertEqual(process.exitcode, 0)
        self.assertEqual((self.root / "actual-calls.txt").read_text().splitlines(), ["80"])
        self.assertEqual(
            sorted((self.root / (o + ".result")).read_text() for o in ["one", "two"]),
            ["False:80", "True:80"],
        )

    async def test_restart_and_legacy_publication_failure_reuse(self):
        first = self.claimed("old", "old")
        download, reused = await self.get(first, "old")
        self.assertFalse(reused)
        self.media.repo.update_job(
            first["job_id"], {"downloads": {CID: download}, "state": "failed"}
        )
        with self.media.repo.transaction() as db:
            db.execute("DELETE FROM download_sources")
            db.execute("PRAGMA user_version=1")
        restarted = media_for(self.root)
        second = self.claimed("new", "new")
        result, reused = await self.get(second, "new", restarted)
        self.assertTrue(reused)
        self.assertNotEqual(result["file"], download["file"])
        self.assertTrue(Path(result["file"]).samefile(download["file"]))
        self.assertEqual(restarted.engine.calls, [])
        self.assertTrue(list(self.root.glob("media.sqlite.pre-media-*")))

    async def test_missing_corrupt_same_length_and_different_quality_never_hit(self):
        first = self.claimed("first", "first")
        initial, _ = await self.get(first, "first")
        Path(initial["file"]).write_bytes(b"x" * initial["size_bytes"])
        second = self.claimed("second", "second")
        repaired, reused = await self.get(second, "second")
        self.assertFalse(reused)
        self.assertNotEqual(initial["file"], repaired["file"])
        Path(repaired["file"]).unlink()
        source = self.media.repo.download_source(BV, CID, "80")
        Path(source["download"]["file"]).unlink()
        third = self.claimed("third", "third")
        _, reused = await self.get(third, "third")
        self.assertFalse(reused)
        low = self.claimed("low", "low", {**EXACT, "quality_id": "64"})
        downloaded, reused = await self.get(low, "low")
        self.assertFalse(reused)
        self.assertEqual(downloaded["actual_quality"], "64")
        self.assertEqual(self.media.engine.calls, ["80", "80", "80", "64"])

    async def test_best_and_fallback_recheck_available_quality_without_downgrade_hit(self):
        self.media.engine.available = ["64"]
        fallback = self.claimed("fallback", "fallback", {**EXACT, "allow_fallback": True})
        low, _ = await self.get(fallback, "fallback")
        self.assertEqual(low["actual_quality"], "64")
        self.media.engine.available = ["80", "64"]
        exact = self.claimed("exact", "exact")
        high, reused = await self.get(exact, "exact")
        self.assertFalse(reused)
        self.assertEqual(high["actual_quality"], "80")
        best = self.claimed(
            "best", "best", {"mode": "best", "quality_id": None, "allow_fallback": False}
        )
        high, reused = await self.get(best, "best")
        self.assertTrue(reused)
        self.media.engine.available = ["112", "80", "64"]
        better = self.claimed("better", "better", best["quality"])
        newest, reused = await self.get(better, "better")
        self.assertFalse(reused)
        self.assertEqual(newest["actual_quality"], "112")
        self.assertEqual(self.media.engine.calls, ["64", "80", "112"])

    async def test_cancelled_owner_releases_source_and_waiter_does_not_retry_failures(self):
        self.media.engine.release = asyncio.Event()
        first = self.claimed("first", "first")
        second = self.claimed("second", "second")
        owned = asyncio.create_task(self.get(first, "first"))
        await self.media.engine.entered.wait()
        waiting = asyncio.create_task(self.get(second, "second"))
        await asyncio.sleep(0.05)
        owned.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await owned
        self.media.engine.release.set()
        result, reused = await asyncio.wait_for(waiting, 3)
        self.assertFalse(reused)
        self.assertEqual(result["actual_quality"], "80")
        self.assertNotIn("attempts", self.media.repo.job(second["job_id"]))

    async def test_stalled_live_owner_keeps_os_lock_after_lease_expiry(self):
        now = [100.0]
        self.media = media_for(self.root, clock=lambda: now[0])
        self.media.engine.release = asyncio.Event()
        first = self.claimed("first", "first")
        second = self.claimed("second", "second")
        owned = asyncio.create_task(self.get(first, "first"))
        await self.media.engine.entered.wait()
        now[0] += 31
        waiting = asyncio.create_task(self.get(second, "second"))
        await asyncio.sleep(0.35)
        self.assertEqual(self.media.engine.calls, ["80"])
        self.media.engine.release.set()
        results = await asyncio.gather(owned, waiting)
        self.assertEqual(sorted(reused for _, reused in results), [False, True])
        self.assertEqual(self.media.engine.calls, ["80"])

    async def test_source_directories_keep_independent_jobs_and_one_global_download(self):
        target = {"kind": "media_server", "directory": str(self.root / "library")}
        downloads = []
        jobs = []
        for kind, identity in (("uploader", "123"), ("favorite", "456")):
            destination = snapshot(
                target, source_subdirectory({"kind": kind, "id": identity}, "同名")
            )
            created = enqueue(self.media, "same-library", destination=destination)
            job = self.media.repo.claim(kind)
            self.assertEqual(created["job_id"], job["job_id"])
            downloads.append(await self.get(job, kind))
            jobs.append(job)
        self.assertNotEqual(jobs[0]["job_id"], jobs[1]["job_id"])
        self.assertNotEqual(head_key(jobs[0]), head_key(jobs[1]))
        self.assertFalse(downloads[0][1])
        self.assertTrue(downloads[1][1])
        self.assertTrue(Path(downloads[0][0]["file"]).samefile(downloads[1][0]["file"]))
        self.assertEqual(self.media.engine.calls, ["80"])

    async def test_expired_crashed_lease_can_be_taken_over_and_stale_finish_is_fenced(self):
        now = [100.0]
        self.media = media_for(self.root, clock=lambda: now[0])
        old = self.media.repo.download_claim(BV, CID, "80", "crashed")
        now[0] += 31
        job = self.claimed("new", "new")
        _, reused = await self.get(job, "new")
        self.assertFalse(reused)
        with self.assertRaises(Fault):
            self.media.repo.download_finish(BV, CID, "80", "crashed", old["generation"])

    async def test_cancelled_caller_keeps_source_lock_until_threaded_pin_finishes(self):
        job = self.claimed("one", "one")
        cache = DownloadCache(self.media)
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        pin = self.media.retention.pin

        def slow_pin(*args):
            entered.set()
            release.wait(5)
            try:
                return pin(*args)
            finally:
                finished.set()

        self.media.retention.pin = slow_pin
        running = asyncio.create_task(cache.obtain(job, PART, None, "one", "80"))
        self.assertTrue(await asyncio.to_thread(entered.wait, 3))
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        directory = source_directory(self.media.config["staging_directory"], BV, CID, "80")
        try:
            with self.assertRaises(Fault) as error:
                with library_lock(directory):
                    pass
            self.assertEqual(error.exception.code, "busy")
        finally:
            release.set()
        self.assertTrue(await asyncio.to_thread(finished.wait, 3))
        await asyncio.sleep(0.05)
        with library_lock(directory):
            self.assertTrue(self.media.repo.job_inputs(job["job_id"]))

    async def test_advanced_policy_variants_are_serialized_and_old_matching_bytes_are_reusable(
        self,
    ):
        from services.platform.media.config import quality as normalize_quality

        original = self.media.engine.download

        async def selected(job, part, cookie, directory):
            result = await original(job, part, cookie, directory)
            codec = job["quality"].get("stream_options", {}).get("codecs", ["avc"])[0]
            Path(result["file"]).write_bytes(("actual-media-" + codec).encode())
            result["stream_evidence"] = {
                "video_codec": codec,
                "audio_quality": "30280",
                "hdr": False,
                "dolby_vision": False,
                "dolby_audio": False,
                "hires": False,
            }
            return result

        self.media.engine.download = selected
        self.media.engine.delay = 0.1
        avc = normalize_quality({**EXACT, "stream_options": {"codecs": ["avc"]}})
        hevc = normalize_quality({**EXACT, "stream_options": {"codecs": ["hevc"]}})
        a = self.claimed("avc-one", "a", avc)
        b = self.claimed("avc-two", "b", avc)
        results = await asyncio.gather(self.get(a, "a"), self.get(b, "b"))
        self.assertEqual(sorted(reused for _, reused in results), [False, True])
        first = Path(results[0][0]["file"])
        first_bytes = first.read_bytes()
        c = self.claimed("hevc", "c", hevc)
        result, reused = await self.get(c, "c")
        self.assertFalse(reused)
        self.assertNotEqual(result["sha256"], results[0][0]["sha256"])
        self.assertEqual(first.read_bytes(), first_bytes)
        # The active QN pointer is HEVC, while the archived AVC remains valid.
        d = self.claimed("avc-again", "d", avc)
        result, reused = await self.get(d, "d")
        self.assertTrue(reused)
        self.assertTrue(Path(result["file"]).samefile(first))
        # Ordering is a preference for new downloads, never forced transcoding.
        e = self.claimed(
            "compatible",
            "e",
            normalize_quality({**EXACT, "stream_options": {"codecs": ["hevc", "avc"]}}),
        )
        result, reused = await self.get(e, "e")
        self.assertTrue(reused)
        self.assertEqual(self.media.engine.calls, ["80", "80"])
        with self.media.repo.transaction() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)

    async def test_missing_policy_evidence_does_not_hit_but_video_only_range_can_reuse(self):
        first = self.claimed("old", "first")
        old, _ = await self.get(first, "first")
        ranged = {**EXACT, "stream_options": {"video_min_quality": "64", "video_max_quality": "80"}}
        job = self.claimed("range", "range", ranged)
        _, reused = await self.get(job, "range")
        self.assertTrue(reused)
        job = self.claimed("codec", "codec", {**EXACT, "stream_options": {"codecs": ["hevc"]}})
        with self.assertRaises(Fault):
            await self.get(job, "codec")
        self.assertEqual(self.media.engine.calls, ["80", "80"])
        self.assertTrue(Path(old["file"]).exists())


class DestinationTests(unittest.TestCase):
    def test_chinese_normalization_and_rejections(self):
        self.assertEqual(subdirectory(" 学习\\收藏 "), "学习/收藏")
        for value in [
            "/absolute",
            "../parent",
            "a//b",
            "a/./b",
            "C:/bad",
            "CON",
            "aux.mp4",
            "a/b.",
            "a /b",
            "a\x00b",
            "中" * 86,
            "a?b",
        ]:
            with self.subTest(value=value), self.assertRaises(Fault) as error:
                subdirectory(value)
            self.assertEqual(error.exception.code, "invalid_destination_subdirectory")
        self.assertEqual(head_key({"target_id": "old", "bvid": BV}), "old:" + BV)
        with self.assertRaises(Fault):
            snapshot({"kind": "assetlibrary"}, "folder")

    def test_symlinked_destination_is_rejected_without_writing_outside(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = {"kind": "media_server", "directory": str(root / "library")}
            (root / "library").mkdir()
            (root / "outside").mkdir()
            link = root / "library" / "linked"
            try:
                link.symlink_to(root / "outside", target_is_directory=True)
            except OSError:
                self.skipTest("Host cannot create a directory symlink")
            with self.assertRaises(Fault) as error:
                snapshot(target, "linked/nested")
            self.assertEqual(error.exception.code, "unsafe_library_path")
            self.assertFalse((root / "outside" / "nested").exists())


class PolicyPublicationTests(unittest.IsolatedAsyncioTestCase):
    """Use the existing real HTTP/FFmpeg publication fixture without duplicating its suite."""

    async def asyncSetUp(self):
        from test_media_standalone import StandaloneMediaTests

        for name in ("post", "run_job", "enqueue", "verify_output"):
            setattr(self, name, getattr(StandaloneMediaTests, name).__get__(self))
        await StandaloneMediaTests.asyncSetUp(self)
        self.mixed = False
        self.mov_text = False

        async def synthetic_formats(*args, **kwargs):
            return [{"quality_id": "80", "label": "Synthetic 1080P"}]

        self.media.engine.formats = synthetic_formats

        async def actual_download(job, part, cookie, directory):
            from services.platform.media.engine import run_process
            from services.platform.media.probe_details import media_details
            from services.platform.media.stream_policy import output_evidence

            self.media.engine.calls.append(part["cid"])
            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=True)
            hires = self.mixed and part["cid"] == self.upstream.parts[job["bvid"]][-1]
            extension, audio_codec = ("mkv", "flac") if hires else ("mp4", "aac")
            path = directory / ("download." + extension)
            command = [
                self.media.engine.config["ffmpeg"],
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=160x90:d=2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=2",
            ]
            if self.mov_text and not hires:
                subtitle = directory / "subtitle.srt"
                subtitle.write_text(
                    "1\n00:00:00,200 --> 00:00:00,800\n第一条字幕\n\n2\n00:00:01,100 --> 00:00:01,700\nSecond subtitle\n",
                    encoding="utf-8",
                )
                command += [
                    "-i",
                    str(subtitle),
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-map",
                    "2:s:0",
                    "-c:s",
                    "mov_text",
                ]
            command += [
                "-c:v",
                "libx264",
                "-c:a",
                audio_codec,
                "-shortest",
                "-y",
                str(path),
            ]
            code, _, _ = await run_process(command)
            self.assertEqual(code, 0)
            media = media_details(await self.media.engine.verify(path, 2))
            formats = [
                {"quality": 80, "vcodec": "avc1.640032", "acodec": "none"},
                {
                    "format_id": "30251" if hires else "30280",
                    "vcodec": "none",
                    "acodec": "flac" if hires else "mp4a.40.2",
                },
            ]
            return {
                "file": str(path),
                "actual_quality": "80",
                "extension": extension,
                "media": media,
                "stream_evidence": output_evidence(formats, media, strict=True),
            }

        self.media.engine.download = actual_download

    async def payload(self, folder, *, options=None):
        from media_runtime_fixtures import BV, QUALITY
        from services.platform.media.subscriptions import job_payload

        resolved = await self.media.connector.resolve(BV, with_formats=False)
        quality = {**QUALITY, "stream_options": options or {"video_min_quality": "64"}}
        return job_payload(
            resolved,
            self.upstream.parts[BV],
            None,
            "test-server",
            quality,
            **snapshot(self.media.config["targets"]["test-server"], folder),
        )

    async def test_receipted_sources_keep_actual_policy_evidence_after_cache_cleanup(self):
        from media_runtime_fixtures import BV
        from services.platform.media.retention import Retention

        self.upstream.parts[BV] = ["111111111"]
        options = {
            "codecs": ["avc"],
            "audio_max_quality": "30280",
            "allow_hdr": False,
            "allow_dolby_vision": False,
        }
        first = await self.run_job(
            self.media.repo.enqueue(await self.payload("one", options=options))["job_id"]
        )
        original = self.verify_output(first) / "video.mp4"
        original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
        self.assertFalse(Path(first["downloads"]["111111111"]["file"]).exists())
        self.media.repo = Repository(self.media.repo.database)
        self.media.publisher.organizer.repo = self.media.repo
        self.media.retention = Retention(self.media)
        incompatible = {"quality": {"stream_options": {"codecs": ["hevc"]}}}
        self.assertIsNone(self.media.retention.source(BV, "111111111", "80", incompatible))
        second = await self.run_job(
            self.media.repo.enqueue(await self.payload("two", options=options))["job_id"]
        )
        destination = self.verify_output(second) / "video.mp4"
        self.assertEqual(self.media.engine.calls, ["111111111"])
        self.assertTrue(original.samefile(destination))
        self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(), original_hash)
        self.assertEqual(second["downloads"]["111111111"]["source"]["kind"], "library")

    async def test_multipart_mixed_containers_only_remux_private_inputs_and_retain_real_result(
        self,
    ):
        from media_runtime_fixtures import BV
        from services.platform.media.engine import file_record
        from services.platform.media.engine import run_process

        self.upstream.parts[BV] = ["111111111", "222222222"]
        self.mixed = True
        self.mov_text = True
        sources = {}
        remux = self.media.engine.remux

        async def packets(path, stream):
            code, raw, _ = await run_process(
                [
                    self.media.engine.config["ffprobe"],
                    "-v",
                    "error",
                    "-select_streams",
                    stream,
                    "-show_packets",
                    "-show_data_hash",
                    "sha256",
                    "-show_entries",
                    "packet=pts_time,data_hash",
                    "-of",
                    "json",
                    str(path),
                ]
            )
            self.assertEqual(code, 0)
            values = json.loads(raw)["packets"]
            return [value["data_hash"] for value in values], float(values[0]["pts_time"])

        async def subtitles(path, video_start):
            code, raw, _ = await run_process(
                [
                    self.media.engine.config["ffmpeg"],
                    "-v",
                    "error",
                    "-i",
                    str(path),
                    "-map",
                    "0:s:0",
                    "-f",
                    "srt",
                    "-",
                ]
            )
            self.assertEqual(code, 0)
            text = raw.decode("utf-8").replace("\r\n", "\n")

            def seconds(value):
                hours, minutes, tail = value.split(":")
                return (
                    int(hours) * 3600
                    + int(minutes) * 60
                    + float(tail.replace(",", "."))
                    - video_start
                )

            return [
                (seconds(start), seconds(end), re.sub(r"<[^>]+>", "", content).strip())
                for start, end, content in re.findall(
                    r"(\d\d:\d\d:\d\d,\d\d\d) --> (\d\d:\d\d:\d\d,\d\d\d)\n(.*?)(?:\n\n|$)",
                    text,
                    re.S,
                )
            ]

        async def observe_remux(download, destination, expected_duration=None):
            source = Path(download["file"])
            before = source.read_bytes()
            sources["original"] = (source, before, source.stat().st_ino)
            sources["cache"] = self.media.repo.download_source(BV, "111111111", "80")
            video_before, video_start = await packets(source, "v:0")
            audio_before, _ = await packets(source, "a:0")
            text_before = await subtitles(source, video_start)
            converted = await remux(download, destination, expected_duration)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(source.stat().st_ino, sources["original"][2])
            self.assertNotEqual(converted["sha256"], download["sha256"])
            video_after, video_start = await packets(converted["file"], "v:0")
            audio_after, _ = await packets(converted["file"], "a:0")
            self.assertEqual(video_before, video_after)
            self.assertEqual(audio_before, audio_after)
            text_after = await subtitles(converted["file"], video_start)
            self.assertEqual([value[2] for value in text_before], ["第一条字幕", "Second subtitle"])
            self.assertEqual(
                [value[2] for value in text_before], [value[2] for value in text_after]
            )
            for old, new in zip(text_before, text_after, strict=True):
                self.assertAlmostEqual(old[0], new[0], delta=0.011)
                self.assertAlmostEqual(old[1], new[1], delta=0.011)
            self.assertEqual(converted["media"]["subtitle_tracks"][0]["codec_name"], "ass")
            self.assertEqual(
                converted["remux_source"]["subtitle_conversions"],
                [{"stream": 0, "from": "mov_text", "to": "ass"}],
            )
            self.assertEqual(
                file_record(Path(converted["file"]), "cache", "video", "111111111")["sha256"],
                converted["sha256"],
            )
            return converted

        self.media.engine.remux = observe_remux
        with patch.object(
            self.media.retention, "cleanup", side_effect=PermissionError("defer cleanup")
        ):
            first = await self.run_job(
                self.media.repo.enqueue(await self.payload("mixed"))["job_id"]
            )
        root = self.verify_output(first)
        cache = sources["cache"]["download"]
        cache_path = Path(cache["file"])
        self.assertTrue(cache_path.exists())
        converted = first["downloads"]["111111111"]
        self.assertEqual(converted["remux_source"]["sha256"], cache["sha256"])
        self.assertNotEqual(converted["sha256"], cache["sha256"])
        ready = next(
            record
            for record in self.media.repo.job_inputs(first["job_id"])
            if record["file"] == converted["file"]
        )
        self.assertEqual(ready["state"], "ready")
        self.assertEqual(ready["remux_source"], converted["remux_source"])
        # A recorded conversion never authorizes deleting changed current bytes.
        cache_path.write_bytes(b"x" * cache["size_bytes"])
        self.media.retention.finish(first)
        self.assertEqual(
            self.media.repo.get("media_retention", first["job_id"])["code"],
            "download_cache_changed",
        )
        self.assertTrue(cache_path.exists())
        cache_path.write_bytes(sources["original"][1])
        # A prepared/incomplete conversion cannot stand in for a ready result.
        identity = digest({"job": first["job_id"], "file": converted["file"]})
        self.media.repo.put("media_input", identity, {**ready, "state": "prepared"})
        self.media.retention.finish(first)
        self.assertEqual(
            self.media.repo.get("media_retention", first["job_id"])["code"], "source_changed"
        )
        self.assertTrue(cache_path.exists())
        self.media.repo.put("media_input", identity, ready)
        self.media.retention.finish(
            {**first, "asset_receipt": {**first["asset_receipt"], "files": []}}
        )
        self.assertEqual(
            self.media.repo.get("media_retention", first["job_id"])["code"],
            "publication_unverified",
        )
        self.assertTrue(cache_path.exists())
        self.media.retention.finish(first)
        self.assertFalse(cache_path.exists())
        self.assertIsNone(self.media.repo.download_source(BV, "111111111", "80"))
        manifest = self.media.publisher.organizer.operation(
            self.media.config["targets"]["test-server"], first["asset_receipt"]["operation_id"]
        )["manifest"]
        self.assertEqual(manifest["media_extension"], "mkv")
        self.assertEqual({d["extension"] for d in first["downloads"].values()}, {"mkv"})
        for cid, expected_codec in (("111111111", "aac"), ("222222222", "flac")):
            entry = next(e for e in manifest["files"] if e["kind"] == "video" and e["cid"] == cid)
            self.assertEqual(
                hashlib.sha256((root / entry["path"]).read_bytes()).hexdigest(), entry["sha256"]
            )
            self.assertEqual(
                first["downloads"][cid]["media"]["audio_tracks"][0]["codec_name"], expected_codec
            )
            self.assertTrue(
                all(
                    not Path(record["file"]).exists()
                    for record in self.media.repo.job_inputs(first["job_id"])
                )
            )
        self.assertEqual(
            self.media.repo.get("media_retention", first["job_id"])["state"], "cleaned"
        )
        second = await self.run_job(
            self.media.repo.enqueue(await self.payload("reuse-mixed"))["job_id"]
        )
        self.verify_output(second)
        self.assertEqual(self.media.engine.calls, ["111111111", "222222222"])
        self.assertEqual({d["extension"] for d in second["downloads"].values()}, {"mkv"})
        self.assertTrue(all(d["source"]["kind"] == "library" for d in second["downloads"].values()))

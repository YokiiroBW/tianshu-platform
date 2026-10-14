"""Actual download counts over real SQLite/files, including separate worker processes."""

import asyncio
import multiprocessing
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from services.platform.contracts import Fault
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

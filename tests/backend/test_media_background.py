import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services.platform.media.background import Background
from services.platform.media.destination import safe_path
from services.platform.media.music.storage import stem
from services.platform.media.repository import Repository
from services.platform.media.types import MetadataValidationError
from services.platform.media.worker import Worker


class MediaBackgroundTests(unittest.IsolatedAsyncioTestCase):
    def test_music_ellipsis_is_valid_but_parent_segments_remain_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            track = {
                "source_id": "101",
                "title": "春泥",
                "album": "我...真的受伤了",
                "artists": ["庾澄庆"],
            }
            relative = stem(track, "artist_album") + ".flac"
            self.assertEqual(safe_path(Path(directory), relative), Path(directory) / relative)
            for relative in (
                "../song.flac",
                "artist/../song.flac",
                "artist/./song.flac",
                "artist\\song.flac",
                "/song.flac",
            ):
                with self.subTest(relative=relative), self.assertRaises(MetadataValidationError):
                    safe_path(Path(directory), relative)

    async def test_unexpected_item_error_keeps_worker_and_next_job(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Repository(Path(directory) / "ledger.sqlite")
            jobs = []
            for key in ("bad", "good"):
                jobs.append(
                    repo.enqueue(
                        {
                            "media_kind": "audio",
                            "audio_key": key,
                            "provider": "local",
                            "source_id": key,
                            "title": key,
                            "target_id": "music-library",
                            "destination_directory": directory,
                        }
                    )
                )

            async def local(operation, *args, **kwargs):
                return operation(*args, **kwargs)

            media = SimpleNamespace(repo=repo, local=local, wake=asyncio.Event())
            worker = Worker(media)
            finished = asyncio.Event()

            async def run(job, owner):
                if job["title"] == "bad":
                    raise ValueError("synthetic source failure")
                repo.update_job(
                    job["job_id"],
                    {"state": "completed", "code": "music_archived"},
                    owner=owner,
                    generation=job["generation"],
                )
                finished.set()

            worker.run = run
            task = asyncio.create_task(worker.pump(0))
            try:
                await asyncio.wait_for(finished.wait(), 2)
                self.assertFalse(task.done())
                self.assertEqual(repo.job(jobs[0]["job_id"])["code"], "media_worker_failed")
                self.assertEqual(repo.job(jobs[1]["job_id"])["state"], "completed")
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_background_restarts_failed_loop_and_observes_cancelled_loop(self):
        owner = Background()
        self.addAsyncCleanup(owner.close)
        entered = asyncio.Event()
        calls = 0

        async def loop():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("synthetic")
            entered.set()
            await asyncio.Event().wait()

        with patch("services.platform.media.background.asyncio.sleep", new=AsyncMock()):
            owner.start("worker_0", loop)
            await asyncio.wait_for(entered.wait(), 2)
        self.assertEqual(owner.view()[0]["restarts"], 1)
        self.assertTrue(owner.healthy())
        owner.tasks["worker_0"].cancel()
        await asyncio.gather(owner.tasks["worker_0"], return_exceptions=True)
        self.assertFalse(owner.healthy())

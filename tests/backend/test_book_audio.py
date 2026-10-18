import asyncio
import json
import unittest

from book_audio_fixtures import AudioSite, ffmpeg_path
import test_books
from services.platform.contracts import Fault, digest
from services.platform.media.books.audio.config import profile
from services.platform.media.books.audio.providers import Providers
from services.platform.media.books.audio.segments import segments


class BookAudioTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_books.BookTests.asyncSetUp
    call = test_books.BookTests.call
    source = test_books.BookTests.source
    search = test_books.BookTests.search
    book = test_books.BookTests.book
    run_job = test_books.BookTests.run_job

    async def prepare_audio(self):
        self.audio = self.books.audio
        self.audio.files.ffmpeg = ffmpeg_path()
        self.speech = await AudioSite(self.folder).start()
        self.addAsyncCleanup(self.speech.runner.cleanup)
        await self.call(
            "audio/settings/save",
            {
                "provider": "local",
                "endpoint": self.speech.url,
                "model": "synthetic",
                "voice": "tone",
                "expected_revision": 0,
            },
        )
        self.current_book = await self.book()
        self.book_id = self.current_book["book_id"]
        await self.call("subscriptions/save", {"book_id": self.book_id})
        self.assertEqual((await self.run_job())["state"], "completed")
        return {"book_id": self.book_id, "provider": "local"}

    async def enqueue(self, body, **changes):
        estimate = await self.call("audio/estimate", body)
        return await self.call(
            "audio/generate", {**body, "profile_id": estimate["profile_id"], **changes}
        )

    async def drain(self):
        for _ in range(20):
            if not await self.audio.run_once():
                break
        return self.repo.list("book_audio_job")

    async def test_real_http_audio_files_dedupe_and_incremental_follow(self):
        body = await self.prepare_audio()
        self.assertEqual((await self.enqueue(body, auto_new=True))["queued"], 2)
        await asyncio.gather(self.audio.run_once(), self.audio.run_once())
        rows = self.repo.list("book_audio_job")
        self.assertTrue(all(r["state"] == "ready" for r in rows), rows)
        self.assertEqual(self.speech.hits["/v1/audio/speech"], 2)
        for row in rows:
            manifest = await self.call("audio/manifest", {"job_id": row["id"]})
            self.assertEqual(manifest["sync"], "segment")
            self.assertEqual(manifest["text"], row["text"])
            path, _, mime = await self.books.file(self.book_id, "audio-" + row["id"])
            self.assertEqual(mime, "audio/mpeg")
            self.assertGreater(path.stat().st_size, 1000)
            self.assertTrue((self.library / row["artifact"]["relative_path"]).is_file())
        self.assertEqual((await self.enqueue(body, auto_new=True))["queued"], 0)
        self.site.count = 3
        await self.books.scan(self.book_id)
        await self.run_job()
        await self.audio.reconcile()
        await self.drain()
        self.assertEqual(self.speech.hits["/v1/audio/speech"], 3)
        await self.audio.reconcile()
        await self.drain()
        self.assertEqual(self.speech.hits["/v1/audio/speech"], 3)
        self.assertEqual(len(self.repo.list("book_audio_job")), 3)

    async def test_uncertain_result_never_auto_retries_and_explicit_retry(self):
        body = await self.prepare_audio()
        body["chapter_ids"] = [self.books.catalog.directory(self.book_id)[0]["chapter_id"]]
        await self.enqueue(body)
        self.speech.disconnect = True
        await self.drain()
        row = self.repo.list("book_audio_job")[0]
        self.assertEqual(row["state"], "unknown")
        self.assertFalse(await self.audio.run_once())
        self.assertEqual(self.speech.hits["/v1/audio/speech"], 1)
        self.speech.disconnect = False
        await self.call(
            "audio/retry",
            {"job_id": row["id"], "expected_revision": row["revision"], "allow_charge": True},
        )
        await self.drain()
        self.assertEqual(self.speech.hits["/v1/audio/speech"], 2)
        self.assertEqual(self.repo.list("book_audio_job")[0]["state"], "ready")

    async def test_published_chunk_recovered_after_crash_before_receipt(self):
        body = await self.prepare_audio()
        body["chapter_ids"] = [self.books.catalog.directory(self.book_id)[0]["chapter_id"]]
        await self.enqueue(body)
        job = self.audio.ledger.claim()
        part = segments(job["text"])[0]
        key = digest({"part": part["text"], "profile": job["profile"]})
        self.audio.ledger.begin(job, part, key)
        self.audio.files.publish(job["root"], key, part["text"], self.speech.raw, [])
        self.audio.ledger.update(job, lease=0)
        await self.drain()
        self.assertEqual(self.speech.hits["/v1/audio/speech"], 0)
        self.assertEqual(self.repo.list("book_audio_job")[0]["state"], "ready")

    async def test_quota_reserved_before_request_and_rejection_releases(self):
        body = await self.prepare_audio()
        config = self.audio.config.get("local")
        await self.call(
            "audio/settings/save",
            {"provider": "local", "daily_characters": 1, "expected_revision": config["revision"]},
        )
        await self.enqueue(body)
        await self.drain()
        self.assertEqual(self.speech.hits["/v1/audio/speech"], 0)
        self.assertTrue(
            all(r["code"] == "book_audio_budget" for r in self.repo.list("book_audio_job"))
        )
        config = self.audio.config.get("local")
        await self.call(
            "audio/settings/save",
            {
                "provider": "local",
                "daily_characters": 10000,
                "expected_revision": config["revision"],
            },
        )
        row = self.repo.list("book_audio_job")[0]
        await self.call("audio/retry", {"job_id": row["id"], "expected_revision": row["revision"]})
        self.speech.status = 401
        await self.drain()
        self.assertEqual(self.audio.settings()["usage"]["local"], 0)

    async def test_content_change_retains_audio_and_voice_change_new_cache(self):
        body = await self.prepare_audio()
        await self.enqueue(body, auto_new=True)
        await self.drain()
        original = self.repo.list("book_audio_job")
        chapter = self.books.catalog.directory(self.book_id)[0]
        self.books.catalog.save_chapter(self.book_id, chapter, "修订的正文。" * 40)
        await self.audio.reconcile()
        await self.drain()
        estimate = await self.call("audio/estimate", body)
        self.assertEqual(estimate["changed_text"], 1)
        self.assertEqual(self.speech.hits["/v1/audio/speech"], 2)
        manifest = await self.audio.manifest(original[0]["id"])
        self.assertEqual(manifest["text"], original[0]["text"])
        config = self.audio.config.get("local")
        await self.call(
            "audio/settings/save",
            {"provider": "local", "voice": "tone-two", "expected_revision": config["revision"]},
        )
        self.assertEqual((await self.enqueue(body))["queued"], 2)
        await self.drain()
        self.assertEqual(len(self.repo.list("book_audio_job")), 4)

    async def test_fish_sse_cumulative_alignment_and_fixed_model(self):
        await self.prepare_audio()
        providers = Providers(fish_endpoint=self.speech.url)
        selected = profile(self.audio.config.get("fish"))
        selected["voice"] = "a" * 32
        raw, boundaries = await providers.synthesize(selected, "合成协议测试。", "synthetic-key")
        self.assertEqual(raw, self.speech.raw)
        self.assertEqual(len(boundaries), 1)
        self.assertEqual(boundaries[0]["text"], "合成协议测试。")
        with self.assertRaises(Fault):
            await self.call(
                "audio/settings/save", {"provider": "fish", "model": "unknown-paid-fallback"}
            )

    async def test_credentials_redaction_and_endpoint_change(self):
        await self.prepare_audio()
        config = self.audio.config.get("local")
        await self.call(
            "audio/settings/save",
            {
                "provider": "local",
                "api_key": "synthetic-private",
                "expected_revision": config["revision"],
            },
        )
        view = await self.call("audio/settings")
        self.assertNotIn("synthetic-private", json.dumps(view))
        self.assertTrue(next(c for c in view["providers"] if c["provider"] == "local")["has_key"])
        selected = profile(self.audio.config.get("local"))
        await self.call(
            "audio/settings/save", {"provider": "local", "endpoint": self.speech.url + "/other"}
        )
        with self.assertRaises(Fault):
            self.audio.config.credentials(selected)
        self.assertNotIn("secret", self.audio.config.get("local"))

    async def test_pause_does_not_block_video_or_music_queue(self):
        body = await self.prepare_audio()
        await self.enqueue(body)
        plan = self.repo.get("book_audio_plan", self.book_id)
        await self.call(
            "audio/control",
            {"book_id": self.book_id, "expected_revision": plan["revision"], "action": "pause"},
        )
        self.assertFalse(await self.audio.run_once())
        jobs, _ = self.repo.jobs()
        self.assertEqual(jobs[0]["media_kind"], "book")
        self.assertEqual(jobs[0]["state"], "completed")

    async def test_saving_same_speed_and_rotating_key_does_not_resynthesize(self):
        body = await self.prepare_audio()
        await self.enqueue(body)
        await self.drain()
        before = await self.call("audio/estimate", body)
        config = self.audio.config.get("local")
        await self.call(
            "audio/settings/save",
            {
                "provider": "local",
                "speed": 1,
                "api_key": "rotated-synthetic-key",
                "expected_revision": config["revision"],
            },
        )
        after = await self.call("audio/estimate", body)
        self.assertEqual(before["profile_id"], after["profile_id"])
        self.assertEqual((await self.enqueue(body))["queued"], 0)
        await self.drain()
        self.assertEqual(self.speech.hits["/v1/audio/speech"], 2)

    async def test_partial_chapter_retry_reuses_completed_segments(self):
        body = await self.prepare_audio()
        chapter = self.books.catalog.directory(self.book_id)[0]
        content = "甲" * 600 + "。\n" + "乙" * 600 + "。\n" + "丙" * 600 + "。"
        self.books.catalog.save_chapter(self.book_id, chapter, content)
        body["chapter_ids"] = [chapter["chapter_id"]]
        self.speech.disconnect_at = 2
        await self.enqueue(body)
        await self.drain()
        row = self.repo.list("book_audio_job")[0]
        self.assertEqual(row["state"], "unknown")
        self.assertEqual(row["completed_parts"], 1)
        await self.call(
            "audio/retry",
            {"job_id": row["id"], "expected_revision": row["revision"], "allow_charge": True},
        )
        await self.drain()
        row = self.repo.list("book_audio_job")[0]
        self.assertEqual(row["state"], "ready")
        self.assertEqual(len(self.speech.requests), 4)
        self.assertEqual(
            sum(request["input"].startswith("甲") for request in self.speech.requests), 1
        )
        manifest = await self.audio.manifest(row["id"])
        self.assertEqual(len(manifest["cues"]), 3)
        self.assertGreater(manifest["duration"], 4)
        self.assertEqual(manifest["cues"][-1]["text_end"], len(content))

    async def test_missing_chapter_file_is_rebuilt_from_cached_parts_without_speech(self):
        body = await self.prepare_audio()
        body["chapter_ids"] = [self.books.catalog.directory(self.book_id)[0]["chapter_id"]]
        await self.enqueue(body)
        await self.drain()
        row = self.repo.list("book_audio_job")[0]
        (self.audio.files.cache(row["root"], row["key"]) / "audio.mp3").unlink()
        with self.assertRaises(Fault):
            await self.audio.manifest(row["id"])
        row = self.repo.get("book_audio_job", row["id"])
        self.assertEqual(row["state"], "failed")
        await self.call(
            "audio/retry",
            {"job_id": row["id"], "expected_revision": row["revision"], "allow_charge": True},
        )
        await self.drain()
        self.assertEqual(self.speech.hits["/v1/audio/speech"], 1)
        self.assertEqual(self.repo.get("book_audio_job", row["id"])["state"], "ready")


if __name__ == "__main__":
    unittest.main()

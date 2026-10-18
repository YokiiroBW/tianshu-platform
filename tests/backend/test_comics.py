import asyncio
import hashlib
import os
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path

from book_fixtures import LiveConsole
from comic_fixtures import ComicSite, page_bytes
from services.platform.contracts import Fault
from services.platform.media.accounts import Accounts
from services.platform.media.comics.packages import (
    extract_images,
    folder_pages,
    image_record,
    package_cbz,
)
from services.platform.media.comics.publication import publish
from services.platform.media.runtime import Media, public_job
from services.platform.media.web import WebMedia


class ComicTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        runtime = Path(__file__).resolve().parents[2] / ".runtime"
        runtime.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="comics-test-", dir=runtime)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inputs, self.output = self.root / "inputs", self.root / "output"
        self.inputs.mkdir()
        self.output.mkdir()
        self.site = await ComicSite(self.output).start()
        self.addAsyncCleanup(self.site.runner.cleanup)
        self.now = 1800000000
        self.media = Media(
            {"database_path": str(self.root / "state.sqlite")},
            clock=lambda: self.now,
            local=asyncio.to_thread,
        )
        self.addAsyncCleanup(self.media.close)
        self.repo, self.comics = self.media.repo, self.media.comics
        self.web, self.session = WebMedia(self.media, LiveConsole()), {"live": True}
        await self.call("settings/save", self.site.settings(self.inputs))

    async def call(self, operation, body=None):
        return await self.web.route(
            "/api/web/media/comics/" + operation,
            {**(body or {}), "client_id": str(uuid.uuid4())},
            self.session,
        )

    async def comic(self):
        result = await self.call("search/start", {"query": "星际"})
        await self.comics.sources.searches[result["search_id"]]["task"]
        result = await self.call("search/results", {"search_id": result["search_id"]})
        self.assertEqual(result["sources"][0]["filtered"], 1)
        self.assertEqual(len(result["comics"]), 1)
        return (await self.call("detail", {"comic_id": result["comics"][0]["comic_id"]}))["comic"]

    async def run_job(self):
        self.now += 60
        job = self.repo.claim("comic-test", kinds=("comic",))
        self.assertIsNotNone(job)
        await self.media.worker.run(job, "comic-test")
        return self.repo.job(job["job_id"])

    async def test_subscription_incremental_identity_lossless_archive_and_reader(self):
        comic = await self.comic()
        self.assertEqual(comic["source_id"], "9007199254740993")
        subscribed = await self.call("subscriptions/save", {"comic_id": comic["comic_id"]})
        self.assertEqual(len(subscribed["job_ids"]), 2)
        first, second = await self.run_job(), await self.run_job()
        for job in (first, second):
            self.assertEqual(job["state"], "completed", job["code"])
            self.assertTrue(job["comic_cache_cleaned"])
            self.assertFalse((self.comics.data_root / job["job_id"]).exists())
            self.assertEqual(job["library_results"][0]["page_count"], 3)
            self.assertEqual(job["asset_receipt"]["sha256"], job["comic_artifact"]["sha256"])
            with zipfile.ZipFile(self.output / job["comic_relative_path"]) as package:
                for index in range(1, 4):
                    self.assertEqual(package.read(f"{index:06d}.png"), page_bytes(index))
                self.assertIn(b"ComicInfo", package.read("ComicInfo.xml"))
        original = {p.relative_to(self.output): p.read_bytes() for p in self.output.rglob("*.cbz")}
        self.site.count = 3
        added = await self.call("subscriptions/scan", {"comic_id": comic["comic_id"]})
        self.assertEqual(len(added["job_ids"]), 1)
        third = await self.run_job()
        self.assertEqual(third["state"], "completed", third["code"])
        repeated = await self.call("subscriptions/scan", {"comic_id": comic["comic_id"]})
        self.assertEqual(repeated["job_ids"], [])
        self.assertEqual(self.site.hits["/image/1/1"], 1)
        for path, content in original.items():
            self.assertEqual((self.output / path).read_bytes(), content)
        self.assertNotIn("comic_cache", public_job(third))

    async def test_restart_resumes_pages_and_lost_asset_reply_reconciles_same_key(self):
        comic = await self.comic()
        chapter = self.comics.catalog.chapters(comic["comic_id"])[0]
        await self.call(
            "downloads/enqueue",
            {"comic_id": comic["comic_id"], "chapter_ids": [chapter["chapter_id"]]},
        )
        self.site.fail_pages.add("/image/1/2")
        first = await self.run_job()
        self.assertEqual(first["state"], "retry_wait")
        self.assertEqual(first["comic_pages_done"], 1)
        self.site.fail_pages.clear()
        self.site.lose_observe = True
        self.repo.update_job(first["job_id"], {"state": "queued"})
        second = await self.run_job()
        self.assertEqual(second["code"], "comic_archive_unavailable")
        self.assertEqual(self.site.hits["/image/1/1"], 1)
        key = second["comic_artifact"]["archive_key"]
        await self.media.close()
        self.media = Media(
            {"database_path": str(self.root / "state.sqlite")},
            clock=lambda: self.now,
            local=asyncio.to_thread,
        )
        self.addAsyncCleanup(self.media.close)
        self.repo, self.comics = self.media.repo, self.media.comics
        self.repo.update_job(first["job_id"], {"state": "queued"})
        third = await self.run_job()
        self.assertEqual(third["state"], "completed", third["code"])
        self.assertEqual(third["comic_artifact"]["archive_key"], key)
        self.assertEqual(
            self.site.hits["POST /assetlink/v1/service-imports/" + key + "/observe"], 1
        )
        self.assertEqual(len(list(self.output.rglob("*.cbz"))), 1)

    async def test_local_folder_natural_order_preserves_original_and_duplicate_enqueue(self):
        work = self.inputs / "合成图集"
        work.mkdir()
        originals = {"10.png": page_bytes(3), "2.png": page_bytes(2), "1.png": page_bytes(1)}
        for name, content in originals.items():
            (work / name).write_bytes(content)
        inspected = await self.call("imports/inspect", {"relative_path": work.name})
        body = {"relative_path": work.name, "input_digest": inspected["input_digest"]}
        first = await self.call("imports/enqueue", body)
        again = await self.call("imports/enqueue", body)
        self.assertEqual(first["job_ids"], again["job_ids"])
        job = await self.run_job()
        self.assertEqual(job["state"], "completed", job["code"])
        with zipfile.ZipFile(self.output / job["comic_relative_path"]) as package:
            for index, name in enumerate(("1.png", "2.png", "10.png"), 1):
                self.assertEqual(package.read(f"{index:06d}.png"), originals[name])
        for name, content in originals.items():
            self.assertEqual((work / name).read_bytes(), content)

    async def test_changed_input_and_unsafe_archive_do_not_publish(self):
        folder = self.inputs / "pages"
        folder.mkdir()
        (folder / "1.png").write_bytes(page_bytes(1))
        inspected = await self.call("imports/inspect", {"relative_path": "pages"})
        (folder / "2.png").write_bytes(page_bytes(2))
        with self.assertRaises(Fault) as error:
            await self.call(
                "imports/enqueue",
                {"relative_path": "pages", "input_digest": inspected["input_digest"]},
            )
        self.assertEqual(error.exception.code, "comic_input_changed")
        archive = self.inputs / "bad.cbz"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("../1.png", page_bytes(1))
        with self.assertRaises(Fault):
            extract_images(archive, self.root / "extract")
        self.assertFalse(list(self.output.rglob("*.cbz")))
        (folder / "01.png").write_bytes(page_bytes(1))
        with self.assertRaises(Fault) as order:
            folder_pages(folder)
        self.assertEqual(order.exception.code, "comic_page_order_ambiguous")

    async def test_corrupt_page_never_becomes_a_cbz(self):
        image = self.inputs / "1.png"
        image.write_bytes(page_bytes(1)[:30])
        with self.assertRaises(Fault):
            image_record(image)
        image.write_bytes(page_bytes(1))
        expected = image_record(image)
        image.write_bytes(page_bytes(2))
        with self.assertRaises(Fault):
            package_cbz(
                [(image, expected)], self.output / "unsafe.cbz", {"title": "test"}, {"title": "1"}
            )
        self.assertFalse((self.output / "unsafe.cbz").exists())

    async def test_repacking_same_images_is_independent_of_download_timestamps(self):
        image = self.inputs / "1.png"
        image.write_bytes(page_bytes(1))
        expected = image_record(image)
        first = package_cbz(
            [(image, expected)], self.output / "first.cbz", {"title": "作品"}, {"title": "章节"}
        )
        os.utime(image, (1600000000, 1600000000))
        second = package_cbz(
            [(image, expected)], self.output / "second.cbz", {"title": "作品"}, {"title": "章节"}
        )
        self.assertEqual(first["sha256"], second["sha256"])

    async def test_deleted_task_reimport_uses_original_file_and_asset_intent(self):
        comic = await self.comic()
        chapter = self.comics.catalog.chapters(comic["comic_id"])[0]
        body = {"comic_id": comic["comic_id"], "chapter_ids": [chapter["chapter_id"]]}
        await self.call("downloads/enqueue", body)
        first = await self.run_job()
        with self.repo.transaction() as db:
            self.repo.delete_job(first["job_id"], first["revision"], db=db)
        await self.call("downloads/enqueue", body)
        second = await self.run_job()
        self.assertEqual(second["state"], "completed", second["code"])
        self.assertEqual(
            first["comic_artifact"]["archive_key"], second["comic_artifact"]["archive_key"]
        )
        self.assertEqual(len(list(self.output.rglob("*.cbz"))), 1)
        self.assertEqual(first["asset_receipt"], second["asset_receipt"])

    async def test_credentials_redacted_and_key_loss_rejected(self):
        settings = (await self.call("view"))["settings"]
        self.assertNotIn("isolated-archive", str(settings))
        self.assertNotIn("isolated-komga", str(settings))
        self.assertTrue(settings["connections"]["komga"]["has_credentials"])
        Path(str(self.repo.database) + ".key").unlink()
        with self.assertRaises(Fault) as failure:
            Accounts(self.repo)
        self.assertEqual(failure.exception.code, "media_credentials_unavailable")

    async def test_catalogue_only_source_is_filtered_and_marked(self):
        self.site.no_search = True
        result = await self.call("search/start", {"query": "星际"})
        await self.comics.sources.searches[result["search_id"]]["task"]
        result = await self.call("search/results", {"search_id": result["search_id"]})
        self.assertEqual([r["metadata"]["title"] for r in result["comics"]], ["星际邮差"])
        self.assertEqual(result["sources"][0]["search_mode"], "catalogue")

    async def test_scan_facts_do_not_invalidate_subscription_controls(self):
        comic = await self.comic()
        saved = await self.call(
            "subscriptions/save", {"comic_id": comic["comic_id"], "mode": "new"}
        )
        revision = saved["subscription"]["revision"]
        await self.call("subscriptions/scan", {"comic_id": comic["comic_id"]})
        paused = await self.call(
            "subscriptions/control",
            {"comic_id": comic["comic_id"], "action": "pause", "expected_revision": revision},
        )
        self.assertEqual(paused["subscription"]["state"], "paused")

    async def test_removed_source_chapter_keeps_its_archive_and_reader_link(self):
        comic = await self.comic()
        chapters = self.comics.catalog.chapters(comic["comic_id"])
        removed = chapters[-1]
        await self.call(
            "downloads/enqueue",
            {"comic_id": comic["comic_id"], "chapter_ids": [removed["chapter_id"]]},
        )
        job = await self.run_job()
        self.assertEqual(job["state"], "completed")
        self.site.count = 1
        detail = await self.call("detail", {"comic_id": comic["comic_id"], "refresh": True})
        retained = next(c for c in detail["chapters"] if c["chapter_id"] == removed["chapter_id"])
        self.assertTrue(retained["source_missing"])
        self.assertTrue(retained["artifact"]["komga"]["reader_url"])
        with self.assertRaises(Fault) as failure:
            await self.call(
                "downloads/enqueue",
                {"comic_id": comic["comic_id"], "chapter_ids": [removed["chapter_id"]]},
            )
        self.assertEqual(failure.exception.code, "comic_chapter_missing")
        self.assertTrue((self.output / job["comic_relative_path"]).is_file())

    async def test_scoped_search_pause_and_mixed_job_claims(self):
        comic = await self.comic()
        await self.call("subscriptions/save", {"comic_id": comic["comic_id"], "mode": "new"})
        self.assertIsNone(self.repo.claim("no-history", kinds=("comic",)))
        subscription = self.repo.get("comic_subscription", comic["comic_id"])
        await self.call(
            "subscriptions/control",
            {
                "comic_id": comic["comic_id"],
                "action": "pause",
                "expected_revision": subscription["revision"],
            },
        )
        with self.assertRaises(Fault) as failure:
            await self.call("subscriptions/scan", {"comic_id": comic["comic_id"]})
        self.assertEqual(failure.exception.code, "comic_subscription_paused")
        search = await self.call("search/start", {"query": "星际"})
        with self.assertRaises(Fault):
            self.comics.sources.results(search["search_id"], {"live": True})
        await self.comics.sources.searches[search["search_id"]]["task"]
        with self.repo.transaction() as db:
            comic_job = self.comics.catalog.enqueue(
                self.comics.catalog.comic(comic["comic_id"], db),
                self.comics.catalog.chapters(comic["comic_id"], db)[:1],
                db=db,
            )[0]
            video_job = self.repo.enqueue(
                {
                    "bvid": "BVfixture",
                    "selected_cids": ["1"],
                    "target_id": "fixture",
                    "quality": {"mode": "best"},
                },
                db=db,
            )
        self.assertFalse(self.repo.jobs_overlap(comic_job, video_job))
        self.assertEqual(self.repo.claim("video", kinds=("video",))["job_id"], video_job["job_id"])
        self.assertEqual(self.repo.claim("comic", kinds=("comic",))["job_id"], comic_job["job_id"])

    async def test_existing_different_file_never_overwritten(self):
        comic = await self.comic()
        chapter = self.comics.catalog.chapters(comic["comic_id"])[0]
        await self.call(
            "downloads/enqueue",
            {"comic_id": comic["comic_id"], "chapter_ids": [chapter["chapter_id"]]},
        )
        job = self.repo.claim("publisher", kinds=("comic",))
        source = self.root / "test.cbz"
        source.write_bytes(b"synthetic-package")
        job["comic_artifact"] = {
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "size_bytes": source.stat().st_size,
        }
        destination = self.output / job["comic_relative_path"]
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"original-file")
        with self.assertRaises(Fault):
            publish(self.repo, job, source, "publisher")
        self.assertEqual(destination.read_bytes(), b"original-file")


if __name__ == "__main__":
    unittest.main()

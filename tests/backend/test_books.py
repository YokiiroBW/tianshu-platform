import asyncio
import base64
import io
import json
import tempfile
import time
import unittest
import uuid
import zipfile
from pathlib import Path

from PIL import Image

from book_fixtures import BookSite, LiveConsole
from services.platform.contracts import Fault
from services.platform.media.books.documents import (
    build_epub,
    import_file,
    validate_epub,
    OPF,
    DC,
    ET,
)
from services.platform.media.books.engine import RuleEngine
from services.platform.media.books.network import Network
from services.platform.media.runtime import Media, public_job
from services.platform.media.web import WebMedia


class BookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        runtime = Path(__file__).resolve().parents[2] / ".runtime"
        runtime.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="books-test-", dir=runtime)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.library = self.folder / "library"
        self.library.mkdir()
        self.site = await BookSite().start()
        self.addAsyncCleanup(self.site.runner.cleanup)
        self.media = Media(
            {"database_path": str(self.folder / "state.sqlite")},
            clock=time.time,
            local=asyncio.to_thread,
        )
        self.addAsyncCleanup(self.media.close)
        self.books, self.repo = self.media.books, self.media.repo
        self.site.connect(self.books)
        self.web = WebMedia(self.media, LiveConsole())
        self.session = {"live": True}
        await self.call(
            "settings/save",
            {
                "directory": str(self.library),
                "interval_seconds": 3600,
                "auto_metadata": False,
                "open_library": False,
                "expected_revision": 0,
            },
        )

    async def call(self, operation, body=None):
        return await self.web.route(
            "/api/web/media/books/" + operation,
            {**(body or {}), "client_id": str(uuid.uuid4())},
            self.session,
        )

    async def source(self, rules=None):
        result = await self.call("sources/import", {"sources": rules or self.site.source()})
        return result["source_ids"][0]

    async def search(self, query="山海"):
        result = await self.call("search/start", {"query": query})
        await self.books.sources.searches[result["search_id"]]["task"]
        return await self.call("search/results", {"search_id": result["search_id"]})

    async def book(self):
        await self.source()
        result = await self.search()
        self.assertEqual(result["sources"][0]["code"], "ready")
        self.assertEqual(len(result["books"]), 1)
        identity = result["books"][0]["book_id"]
        await self.call("detail", {"book_id": identity})
        return self.books.catalog.book(identity)

    async def run_job(self):
        job = self.repo.claim("books-test", kinds=("book",))
        self.assertIsNotNone(job)
        await self.media.worker.run(job, "books-test")
        return self.repo.job(job["job_id"])

    async def test_real_rule_subscription_incremental_and_metadata_rebuild(self):
        book = await self.book()
        directory = self.books.catalog.directory(book["book_id"])
        preview = await self.call(
            "preview", {"book_id": book["book_id"], "chapter_id": directory[0]["chapter_id"]}
        )
        self.assertIn("清晨", preview["chapter"]["content"])
        await self.call("subscriptions/save", {"book_id": book["book_id"]})
        job = await self.run_job()
        self.assertEqual(job["state"], "completed", job["code"])
        self.assertEqual(public_job(job)["progress"], 1)
        path, _, _ = await self.books.file(book["book_id"], "epub")
        validate_epub(path)
        self.assertEqual(self.site.hits["/chapter/1"], 1)
        self.site.count = 3
        await self.books.scan(book["book_id"])
        job = await self.run_job()
        self.assertEqual(job["book_artifact"]["chapters"], 3)
        self.assertEqual(self.site.hits["/chapter/1"], 1)
        self.assertEqual(self.site.hits["/chapter/2"], 1)
        current = self.books.catalog.book(book["book_id"])
        await self.call(
            "metadata/apply",
            {
                "book_id": book["book_id"],
                "expected_revision": current["metadata_revision"],
                "fields": {
                    "title": "人工书名",
                    "isbn": "9781234567897",
                    "tags": ["旅行"],
                    "series": "旅途",
                },
                "source": "manual",
            },
        )
        job = await self.run_job()
        with zipfile.ZipFile(path) as archive:
            package = ET.fromstring(archive.read("EPUB/package.opf"))
            self.assertEqual(package.find(f"{{{OPF}}}metadata/{{{DC}}}title").text, "人工书名")
        await self.books.detail(book["book_id"], refresh=True)
        self.assertEqual(self.books.catalog.book(book["book_id"])["metadata"]["title"], "人工书名")
        self.assertEqual(len((await self.search("9781234567897"))["books"]), 1)
        self.assertEqual(self.site.hits["/chapter/3"], 1)
        summary = self.repo.dashboard_details()
        self.assertEqual(summary["books"]["active"], 1)
        self.assertEqual(summary["books"]["files"], 1)
        self.assertEqual(summary["recent"]["book"][0]["job_id"], job["job_id"])

    async def test_failed_update_preserves_complete_epub_and_retry_fills_only_missing(self):
        book = await self.book()
        await self.call("subscriptions/save", {"book_id": book["book_id"]})
        await self.run_job()
        path, _, _ = await self.books.file(book["book_id"], "epub")
        previous = path.read_bytes()
        self.site.count, self.site.failed = 3, {3}
        await self.books.scan(book["book_id"])
        job = await self.run_job()
        self.assertEqual(job["code"], "book_source_unavailable")
        self.assertEqual(job["state"], "retry_wait")
        self.assertEqual(path.read_bytes(), previous)
        self.site.failed.clear()
        now = self.repo.clock() + job["retry_delay"] + 1
        self.repo.clock = lambda: now
        job = await self.run_job()
        self.assertEqual(job["state"], "completed")
        self.assertEqual(self.site.hits["/chapter/1"], 1)
        self.assertEqual(self.site.hits["/chapter/3"], 2)

    async def test_rate_limited_partial_download_resumes_automatically_from_cache(self):
        book = await self.book()
        identity = book["book_id"]
        self.site.rate_limited = {2}
        await self.call("subscriptions/save", {"book_id": identity})
        first = await self.run_job()
        self.assertEqual(first["state"], "retry_wait")
        self.assertEqual(first["code"], "book_source_rate_limited")
        self.assertIsNotNone(
            self.books.catalog.chapter(
                identity, self.books.catalog.directory(identity)[0]["chapter_id"]
            )
        )
        self.assertIsNone(self.repo.claim("too-early", kinds=("book",)))
        self.site.rate_limited.clear()
        now = self.repo.clock() + first["retry_delay"] + 1
        self.repo.clock = lambda: now
        await self.media.start()
        async with asyncio.timeout(20):
            while True:
                job = await self.media.local(self.repo.job, first["job_id"])
                if job["state"] == "completed":
                    break
                await asyncio.sleep(0.05)
        self.assertTrue(job["book_artifact"]["complete"])
        self.assertEqual(job["book_artifact"]["chapters"], 2)
        self.assertEqual(self.site.hits["/chapter/1"], 1)
        self.assertEqual(self.site.hits["/chapter/2"], 2)

    async def test_transient_failures_stop_after_bounded_automatic_retries(self):
        book = await self.book()
        self.site.rate_limited = {2}
        await self.call("subscriptions/save", {"book_id": book["book_id"]})
        for attempt in range(1, 5):
            job = await self.run_job()
            self.assertEqual(job["attempts"], attempt)
            self.assertEqual(job["state"], "retry_wait" if attempt < 4 else "failed")
            now = self.repo.clock() + job["retry_delay"] + 1
            self.repo.clock = lambda: now
        self.assertIsNone(self.repo.claim("exhausted", kinds=("book",)))
        self.assertEqual(self.site.hits["/chapter/1"], 1)
        self.assertEqual(self.site.hits["/chapter/2"], 4)

    async def test_successful_search_does_not_claim_body_readable_and_notices_are_not_cached(self):
        book = await self.book()
        self.assertFalse(book.get("readability"))
        chapter = self.books.catalog.directory(book["book_id"])[1]
        for notice, code in (
            ("", "book_content_empty"),
            ("暂无正文。", "book_content_empty"),
            ("访问过于频繁，请稍后重试。", "book_source_rate_limited"),
            ("请先登录后阅读本章正文。", "book_source_auth_required"),
            ("本章为VIP章节，请订阅后阅读。", "book_content_purchase_required"),
            ("请完成人机验证后继续阅读。", "book_content_verification_required"),
        ):
            with self.subTest(code=code):
                self.site.notices = {2: notice}
                with self.assertRaises(Fault) as caught:
                    await self.call(
                        "preview", {"book_id": book["book_id"], "chapter_id": chapter["chapter_id"]}
                    )
                self.assertEqual(caught.exception.code, code)
                self.assertIsNone(
                    self.books.catalog.chapter(book["book_id"], chapter["chapter_id"])
                )
                self.assertEqual(
                    self.books.catalog.book(book["book_id"])["readability"]["code"], code
                )
        self.site.notices = {
            2: "旅人拿着地图登陆了小岛。他记起朋友曾经说过账号登录的事，但这里没有网络。本章完，感谢订阅支持。"
        }
        preview = await self.call(
            "preview", {"book_id": book["book_id"], "chapter_id": chapter["chapter_id"]}
        )
        self.assertIn("小岛", preview["chapter"]["content"])
        self.assertEqual(preview["readability"]["state"], "previewed")

    async def test_subscription_automatically_checks_middle_before_bulk_download(self):
        self.site.count = 9
        self.site.notices = {5: "请先登录后查看章节正文。"}
        book = await self.book()
        await self.call("subscriptions/save", {"book_id": book["book_id"]})
        job = await self.run_job()
        self.assertEqual(job["code"], "book_source_auth_required")
        self.assertEqual(job["state"], "failed")
        self.assertIsNone(self.books.catalog.book(book["book_id"])["artifact"])
        self.assertEqual(self.site.hits["/chapter/2"], 0)
        self.assertEqual(self.site.hits["/chapter/5"], 1)

    async def test_late_paywall_after_sample_pass_is_never_saved_as_chapter(self):
        self.site.count = 9
        self.site.notices = {3: "请购买本章后阅读正文。"}
        book = await self.book()
        await self.call("subscriptions/save", {"book_id": book["book_id"]})
        job = await self.run_job()
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["book_artifact"]["chapters"], 8)
        self.assertFalse(job["book_artifact"]["complete"])
        chapter = self.books.catalog.directory(book["book_id"])[2]
        self.assertIsNone(self.books.catalog.chapter(book["book_id"], chapter["chapter_id"]))
        self.assertEqual(
            self.books.catalog.book(book["book_id"])["readability"]["code"],
            "book_content_purchase_required",
        )
        path, _, _ = await self.books.file(book["book_id"], "epub")
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if name.endswith(".xhtml"):
                    self.assertNotIn("请购买本章", archive.read(name).decode())

    async def test_redirect_to_login_form_is_reported_as_auth_required(self):
        book = await self.book()
        self.site.login_chapters = {1}
        chapter = self.books.catalog.directory(book["book_id"])[0]
        with self.assertRaises(Fault) as caught:
            await self.call(
                "preview", {"book_id": book["book_id"], "chapter_id": chapter["chapter_id"]}
            )
        self.assertEqual(caught.exception.code, "book_source_auth_required")
        self.assertIsNone(self.books.catalog.chapter(book["book_id"], chapter["chapter_id"]))

    async def test_source_bundle_partial_validation_feed_failure_and_encryption(self):
        rules = self.site.source()
        rules["header"] = json.dumps({"Cookie": "synthetic-secret", "User-Agent": "Test"})
        result = await self.call(
            "sources/import", {"sources": [None, rules, {"bookSourceName": 5}]}
        )
        self.assertEqual((result["imported"], result["invalid"]), (1, 2))
        source_id = result["source_ids"][0]
        view = await self.call("view")
        self.assertNotIn("synthetic-secret", json.dumps(view))
        self.assertNotIn("synthetic-secret", json.dumps(self.repo.list("book_source")))
        self.assertNotIn(
            "synthetic-secret",
            json.dumps(await self.call("sources/export", {"source_id": source_id})),
        )
        self.assertEqual((await self.call("sources/list", {"query": "测试文学"}))["total"], 1)
        saved = await self.call("feeds/save", {"url": self.site.url + "/rules"})
        feed_id = saved["feed"]["feed_id"]
        await self.books.sources.refresh_feed(feed_id)
        sources = self.repo.list("book_source")
        self.site.bad_feed = True
        with self.assertRaises(Fault):
            await self.books.sources.refresh_feed(feed_id)
        self.assertEqual(self.repo.list("book_source"), sources)

    async def test_bad_source_revision_recovers_last_successful_body_rules(self):
        book = await self.book()
        chapter = self.books.catalog.directory(book["book_id"])[0]
        await self.books.preview(book["book_id"], chapter["chapter_id"])
        rules = self.site.source()
        rules["ruleContent"]["content"] = "@js:throw new Error('broken')"
        await self.source(rules)
        result = await self.books.preview(book["book_id"], chapter["chapter_id"], refresh=True)
        self.assertIn("清晨", result["chapter"]["content"])
        self.assertEqual(
            self.repo.get("book_source", book["source_id"])["code"], "book_source_previous_restored"
        )

    async def test_css_xpath_javascript_and_gb18030(self):
        source = self.site.source()
        source["searchUrl"] = self.site.url + "/html"
        source["ruleSearch"] = {
            "bookList": "class.entry",
            "name": "tag.a@text",
            "author": "tag.em@text",
            "bookUrl": "tag.a@href",
        }
        result = await self.books.engine.call(source, "search", query="山", page=1)
        self.assertEqual(result[0]["name"], "山海之间")
        source["ruleSearch"] = {
            "bookList": "//div[@class='entry']",
            "name": ".//a/text()",
            "author": ".//em/text()",
            "bookUrl": ".//a/@href",
        }
        result = await self.books.engine.call(source, "search", query="山", page=1)
        self.assertEqual(result[0]["author"], "合成作者")
        source = self.site.source()
        source["ruleContent"]["content"] = "$.content@js:result.replace('清晨','傍晚')"
        result = await self.books.engine.call(source, "content", url=self.site.url + "/chapter/1")
        self.assertIn("傍晚", result["content"])

    async def test_cancel_and_hard_timeout_release_rule_processes(self):
        self.site.delay = 3
        task = asyncio.create_task(self.books.engine.call(self.site.source(), "search", query="山"))
        for _ in range(100):
            if self.books.engine.processes:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.books.engine.processes)
        engine = RuleEngine(allowed_private_origins=[self.site.url], timeout=0.1)
        with self.assertRaises(Fault) as caught:
            await engine.call(self.site.source(), "search", query="山")
        self.assertEqual(caught.exception.code, "book_rule_timeout")
        self.assertFalse(engine.processes)

    async def test_opds_auth_and_selected_fields(self):
        book = await self.book()
        current = self.books.catalog.settings()
        await self.call(
            "settings/save",
            {
                **current,
                "expected_revision": current["revision"],
                "opds_url": self.site.url + "/opds",
                "opds_username": "synthetic",
                "opds_password": "secret",
            },
        )
        result = await self.call("metadata/search", {"book_id": book["book_id"], "query": "山海"})
        candidate = result["candidates"][0]
        self.assertEqual(candidate["metadata"]["publisher"], "合成出版社")
        result = await self.call(
            "metadata/apply",
            {
                "book_id": book["book_id"],
                "expected_revision": book["metadata_revision"],
                "fields": {"publisher": candidate["metadata"]["publisher"]},
                "source": candidate["provider"],
            },
        )
        self.assertEqual(result["book"]["metadata"]["description"], book["metadata"]["description"])
        self.assertNotIn("secret", json.dumps(await self.call("view")))

    async def test_local_import_epub_roundtrip_and_metadata_conflict(self):
        content = "第一章 起点\n清晨的书页。\n第二章 归途\n海边的风。".encode("gb18030")
        result = await self.call(
            "library/import",
            {"filename": "合成小说.txt", "content_base64": base64.b64encode(content).decode()},
        )
        book = result["book"]
        job = await self.run_job()
        self.assertEqual(job["state"], "completed", job["code"])
        path, _, _ = await self.books.file(book["book_id"], "epub")
        original = path.read_bytes()
        metadata, chapters, _ = import_file("roundtrip.epub", original)
        self.assertEqual(len(chapters), 2)
        image = io.BytesIO()
        Image.new("RGB", (40, 60), "#336699").save(image, "JPEG")
        revised = self.folder / "revised.epub"
        updated = {
            **book,
            "metadata": {**metadata, "title": "新书名", "tags": ["合成"], "publisher": "测试出版"},
        }
        build_epub(revised, updated, chapters, cover=image.getvalue(), original=original)
        validate_epub(revised)
        self.assertEqual(import_file("revised.epub", revised.read_bytes())[0]["title"], "新书名")
        with zipfile.ZipFile(revised) as archive:
            self.assertIn(
                image.getvalue(),
                [archive.read(name) for name in archive.namelist() if name.endswith(".jpg")],
            )
        # Publication/progress revisions do not invalidate an unchanged metadata editor.
        await self.call(
            "metadata/apply",
            {
                "book_id": book["book_id"],
                "expected_revision": book["metadata_revision"],
                "fields": {"title": "已保存"},
            },
        )
        with self.assertRaises(Fault) as caught:
            await self.call(
                "metadata/apply",
                {
                    "book_id": book["book_id"],
                    "expected_revision": book["metadata_revision"],
                    "fields": {"title": "过期输入"},
                },
            )
        self.assertEqual(caught.exception.code, "version_conflict")

    async def test_publication_restart_adopts_own_file_but_rejects_external_changes(self):
        book = await self.book()
        await self.call("downloads/create", {"book_id": book["book_id"]})
        job = self.repo.claim("books-test", kinds=("book",))
        for chapter in self.books.catalog.directory(book["book_id"]):
            await self.books.preview(book["book_id"], chapter["chapter_id"])
        chapters = [
            self.books.catalog.chapter(book["book_id"], chapter["chapter_id"])
            for chapter in self.books.catalog.directory(book["book_id"])
        ]
        first = self.books.downloads.publish(book, chapters, [], job, "books-test", None)
        self.assertIsNone(self.books.catalog.book(book["book_id"])["artifact"])
        recovered = self.books.downloads.publish(book, chapters, [], job, "books-test", None)
        self.assertEqual(first["sha256"], recovered["sha256"])
        self.assertTrue(recovered["reused"])
        target = self.library / first["relative_path"]
        target.write_bytes(b"external edits")
        with self.assertRaises(Fault):
            self.books.downloads.publish(book, chapters, [], job, "books-test", None)

    async def test_shared_worker_claim_isolated_from_other_media_and_session_scope(self):
        for index in range(6):
            self.repo.enqueue(
                {
                    "media_kind": "audio",
                    "audio_key": str(index),
                    "target_id": "music",
                    "title": "合成音乐",
                }
            )
        result = await self.call(
            "library/import",
            {
                "filename": "本地.txt",
                "content_base64": base64.b64encode("合成正文".encode()).decode(),
            },
        )
        job = await self.run_job()
        self.assertEqual(job["book_id"], result["book"]["book_id"])
        self.assertEqual(len([row for row in self.repo.jobs()[0] if row["state"] == "queued"]), 6)
        self.session["live"] = False
        with self.assertRaises(Fault) as caught:
            await self.call("view")
        self.assertEqual(caught.exception.code, "session_expired")

    async def test_network_rejects_unconfigured_private_sources(self):
        with self.assertRaises(Fault) as caught:
            await asyncio.to_thread(Network().fetch, self.site.url + "/search")
        self.assertEqual(caught.exception.code, "book_source_address_denied")

    async def test_completed_search_cache_does_not_block_continued_use(self):
        for index in range(35):
            result = await self.search(str(index))
            self.assertEqual(result["state"], "completed")
        self.assertLessEqual(len(self.books.sources.searches), 32)

    async def test_webview_hint_http_script_helpers_and_browser_script_rejection(self):
        rules = self.site.source()
        rules["searchUrl"] = self.site.url + '/search?key={{java.encodeURI(key)}},{"webView":true}'
        rules["ruleToc"]["chapterList"] = (
            '@js:java.setContent(result); java.getElements("$.chapters[*]");'
        )
        rules["ruleContent"]["content"] = '@js:java.setContent(src); java.getString("$.content");'
        await self.source(rules)
        found = await self.search()
        identity = found["books"][0]["book_id"]
        await self.call("subscriptions/save", {"book_id": identity})
        job = await self.run_job()
        self.assertEqual(job["state"], "completed", job["code"])
        self.assertEqual(job["book_artifact"]["chapters"], 2)
        path, _, _ = await self.books.file(identity, "epub")
        validate_epub(path)
        rules["searchUrl"] = self.site.url + '/search,{"webJs":"document.body.innerText"}'
        with self.assertRaises(Fault) as caught:
            await self.books.engine.call(rules, "search", query="山")
        self.assertEqual(caught.exception.code, "book_rule_capability_unsupported")
        source_id = found["sources"][0]["source_id"]
        source = self.repo.get("book_source", source_id)
        self.repo.put("book_source", source_id, {**source, "enabled": False, "capabilities": {}})
        rules["searchUrl"] = self.site.url + '/search?key={{java.encodeURI(key)}},{"webView":true}'
        await self.source(rules)
        refreshed = self.repo.get("book_source", source_id)
        self.assertFalse(refreshed["enabled"])
        self.assertTrue(refreshed["capabilities"]["webview_http_fallback"])

    async def test_background_due_scan_downloads_only_new_chapters(self):
        book = await self.book()
        identity = book["book_id"]
        await self.call("subscriptions/save", {"book_id": identity})
        first = await self.run_job()
        self.assertEqual(first["state"], "completed")
        now = self.repo.clock() + 3601
        self.repo.clock = lambda: now
        self.site.count = 3
        await self.media.start()
        async with asyncio.timeout(20):
            while True:
                current = await self.media.local(self.books.catalog.book, identity)
                if current.get("artifact", {}).get("chapters") == 3:
                    break
                await asyncio.sleep(0.05)
        subscription = self.repo.get("book_subscription", identity)
        self.assertEqual(subscription["last_scan_at"], now)
        self.assertGreater(subscription["next_scan_at"], now)
        self.assertEqual(self.site.hits["/chapter/1"], 1)
        self.assertEqual(self.site.hits["/chapter/2"], 1)
        self.assertEqual(self.site.hits["/chapter/3"], 1)
        self.assertTrue(current["artifact"]["complete"])
        self.assertEqual(current["artifact"]["version"], first["book_artifact"]["version"] + 1)

    async def test_update_during_long_download_queues_new_chapters_immediately(self):
        book = await self.book()
        identity = book["book_id"]
        await self.call("subscriptions/save", {"book_id": identity})
        chapters_saved, continue_publication = asyncio.Event(), asyncio.Event()
        enrich = self.books.metadata.enrich

        async def wait_before_publication(current):
            chapters_saved.set()
            await continue_publication.wait()
            return await enrich(current)

        self.books.metadata.enrich = wait_before_publication
        downloading = asyncio.create_task(self.run_job())
        async with asyncio.timeout(10):
            await chapters_saved.wait()
        self.site.count = 3
        await self.books.scan(identity)
        continue_publication.set()
        first = await downloading
        self.assertEqual(
            first["state"],
            "queued",
            (
                first.get("code"),
                first.get("book_chapters_total"),
                len(self.books.catalog.directory(identity)),
            ),
        )
        self.assertEqual(first["code"], "book_directory_changed")
        self.books.metadata.enrich = enrich
        caught_up = await self.run_job()
        self.assertEqual(caught_up["state"], "completed")
        self.assertEqual(caught_up["book_artifact"]["chapters"], 3)
        self.assertEqual(self.site.hits["/chapter/1"], 1)
        self.assertEqual(self.site.hits["/chapter/2"], 1)
        self.assertEqual(self.site.hits["/chapter/3"], 1)

    async def test_unicode_remote_paths_preserve_existing_escapes(self):
        for suffix in (
            "/古籍/中文.html?书名=大学",
            "/%E5%8F%A4%E7%B1%8D/%E4%B8%AD%E6%96%87.html?%E4%B9%A6%E5%90%8D=%E5%A4%A7%E5%AD%A6",
        ):
            response = await asyncio.to_thread(self.books.network.fetch, self.site.url + suffix)
            self.assertEqual(
                json.loads(response.text), {"path": "/古籍/中文.html", "query": "大学"}
            )

    async def test_category_reorganization_and_new_root_preserve_previous_files(self):
        book = await self.book()
        await self.call("subscriptions/save", {"book_id": book["book_id"]})
        first = await self.run_job()
        old_path, _, _ = await self.books.file(book["book_id"], "epub")
        old_bytes = old_path.read_bytes()
        new_root = self.library.parent / "新书库"
        new_root.mkdir()
        settings = self.books.catalog.settings()
        await self.call(
            "settings/save",
            {**settings, "directory": str(new_root), "expected_revision": settings["revision"]},
        )
        current = self.books.catalog.book(book["book_id"])
        await self.call(
            "metadata/apply",
            {
                "book_id": book["book_id"],
                "expected_revision": current["metadata_revision"],
                "fields": {"category": "文学诗词"},
                "source": "manual",
            },
        )
        moved = await self.run_job()
        self.assertEqual(moved["state"], "completed", moved["code"])
        path, _, _ = await self.books.file(book["book_id"], "epub")
        self.assertTrue(path.is_relative_to(new_root / "文学诗词"))
        self.assertEqual(old_path.read_bytes(), old_bytes)
        self.assertEqual(moved["book_artifact"]["version"], first["book_artifact"]["version"] + 1)
        with zipfile.ZipFile(path) as archive:
            package = ET.fromstring(archive.read("EPUB/package.opf"))
            self.assertIn(
                "文学诗词", [x.text for x in package.findall(f"{{{OPF}}}metadata/{{{DC}}}subject")]
            )
        await self.call("downloads/create", {"book_id": book["book_id"]})
        repeated = await self.run_job()
        self.assertTrue(repeated["book_reused"])
        self.assertEqual(self.site.hits["/chapter/1"], 1)

    async def test_hidden_web_style_is_not_published_as_book_text(self):
        rules = self.site.source()
        rules["ruleContent"]["content"] = (
            '@js:"<style>.hidden {color:red}</style><p>可读正文</p><script>doNotRun()</script>"'
        )
        content = await self.books.engine.call(rules, "content", url=self.site.url + "/chapter/1")
        self.assertIn("可读正文", content["content"])
        self.assertNotIn("color:red", content["content"])
        self.assertNotIn("doNotRun", content["content"])

    async def test_download_cancel_keeps_cached_chapters_and_retry_resumes(self):
        book = await self.book()
        chapters = self.books.catalog.directory(book["book_id"])
        await self.books.preview(book["book_id"], chapters[0]["chapter_id"])
        await self.call("downloads/create", {"book_id": book["book_id"]})
        job = self.repo.claim("books-test", kinds=("book",))
        self.site.content_delay = 2
        task = asyncio.create_task(self.media.worker.run(job, "books-test"))
        self.media.worker.active[job["job_id"]] = task
        try:
            async with asyncio.timeout(10):
                while not self.site.hits["/chapter/2"]:
                    await asyncio.sleep(0.02)
            await self.web.route(
                "/api/web/media/jobs/control",
                {"job_id": job["job_id"], "action": "cancel", "client_id": str(uuid.uuid4())},
                self.session,
            )
            await asyncio.gather(task, return_exceptions=True)
        finally:
            self.media.worker.active.pop(job["job_id"], None)
        self.assertEqual(self.repo.job(job["job_id"])["state"], "cancelled")
        self.assertTrue(self.books.catalog.chapter(book["book_id"], chapters[0]["chapter_id"]))
        self.assertFalse(self.books.engine.processes)
        self.site.content_delay = 0
        await self.web.route(
            "/api/web/media/jobs/control",
            {"job_id": job["job_id"], "action": "retry", "client_id": str(uuid.uuid4())},
            self.session,
        )
        finished = await self.run_job()
        self.assertEqual(finished["state"], "completed", finished["code"])
        self.assertEqual(self.site.hits["/chapter/1"], 1)


if __name__ == "__main__":
    unittest.main()

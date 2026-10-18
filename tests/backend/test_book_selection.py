import asyncio
import json
import tempfile
import time
import unittest
import uuid
import zipfile
from pathlib import Path

from aiohttp import web

from book_fixtures import BookSite, LiveConsole
from services.platform.media.books.documents import ET, OPF, validate_epub
from services.platform.media.books.engine import RuleEngine
from services.platform.media.books.matching import ChapterIndex, chapter_label
from services.platform.media.books.network import Network
from services.platform.media.runtime import Media
from services.platform.media.web import WebMedia


class NovelSite(BookSite):
    def __init__(self, name, count=6):
        super().__init__()
        self.name, self.count = name, count
        self.author = "合成作者"
        self.title = "山海之间"
        self.split = set()
        self.omitted = set()
        self.wrong_body = False
        self.reverse = False
        self.directory_failed = False
        self.rename = {}

    def source(self):
        return {**super().source(), "bookSourceName": self.name}

    def text(self, number):
        subject = "另一部不相干的城市故事" if self.wrong_body else f"山海之间第{number}章的旅程"
        return "\n\n".join(
            f"{subject}。旅人展开第{index}封信，沿着山路寻找第{number}座灯塔，记录潮水与岩石的变化。"
            for index in range(14)
        )

    async def handle(self, request):
        path = request.path
        if path == "/search":
            self.hits[path] += 1
            return web.json_response(
                {"books": [{"name": self.title, "author": self.author, "url": "/book/1"}]}
            )
        if path == "/book/1":
            self.hits[path] += 1
            if self.directory_failed:
                raise web.HTTPServiceUnavailable()
            return web.json_response({"name": self.title, "author": self.author, "toc": "/toc"})
        if path == "/toc":
            self.hits[path] += 1
            entries = []
            for number in range(1, self.count + 1):
                if number in self.omitted:
                    continue
                title = self.rename.get(number, f"山间第{number}封信")
                parts = ("上", "下") if number in self.split else ("",)
                for part in parts:
                    entries.append(
                        {
                            "name": f"第{number}章 {title}" + (f"【{part}】" if part else ""),
                            "url": f"/chapter/{number}" + (f"/{part}" if part else ""),
                        }
                    )
            return web.json_response(
                {"chapters": list(reversed(entries)) if self.reverse else entries}
            )
        if path.startswith("/chapter/"):
            self.hits[path] += 1
            number = int(path.split("/")[2])
            if number in self.failed:
                raise web.HTTPServiceUnavailable()
            if number in self.notices:
                return web.json_response({"content": self.notices[number]})
            content = self.text(number)
            if number in self.split:
                paragraphs = content.split("\n\n")
                content = "\n\n".join(paragraphs[:7] if path.endswith("上") else paragraphs[7:])
            return web.json_response({"content": content})
        return await super().handle(request)


class SelectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.library = self.folder / "library"
        self.library.mkdir()
        self.media = Media(
            {"database_path": str(self.folder / "state.sqlite")},
            clock=time.time,
            local=asyncio.to_thread,
        )
        self.addAsyncCleanup(self.media.close)
        self.books, self.repo = self.media.books, self.media.repo
        self.web = WebMedia(self.media, LiveConsole())
        self.session = {"live": True}
        self.sites = []
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

    async def site(self, name, count=6, *, discover=True):
        site = await NovelSite(name, count).start()
        self.sites.append(site)
        self.addAsyncCleanup(site.runner.cleanup)
        await self.books.engine.close()
        self.books.engine = RuleEngine(allowed_private_origins=[s.url for s in self.sites])
        self.books.network = Network(allowed_private_origins=[s.url for s in self.sites])
        imported = await self.call("sources/import", {"sources": site.source()})
        site.source_id = imported["source_ids"][0]
        if not discover:
            return site
        source = self.repo.get("book_source", site.source_id)
        rows, _ = await self.books.sources.find(source, "山海之间")
        site.book_id = rows[0]["book_id"]
        await self.books.detail(site.book_id)
        return site

    async def run_job(self):
        job = self.repo.claim("selection-test", kinds=("book",))
        self.assertIsNotNone(job)
        await self.media.worker.run(job, "selection-test")
        return self.repo.job(job["job_id"])

    def chapter_hits(self, site):
        return {key: value for key, value in site.hits.items() if key.startswith("/chapter/")}

    async def test_lagging_source_adds_split_tail_and_reuses_original_chapters(self):
        slow = await self.site("慢源", 6)
        await self.call("subscriptions/save", {"book_id": slow.book_id})
        first = await self.run_job()
        self.assertEqual(first["state"], "completed")
        original = self.books.catalog.directory(slow.book_id)
        old_hits = self.chapter_hits(slow)
        fast = await self.site("快源", 9)
        fast.split = {7}
        await self.books.scan(slow.book_id)
        job = await self.run_job()
        self.assertEqual(job["state"], "completed", job["code"])
        final = self.books.catalog.directory(slow.book_id)
        self.assertEqual([c["chapter_id"] for c in final[:6]], [c["chapter_id"] for c in original])
        self.assertEqual([chapter_label(c["title"])[0] for c in final], list(range(1, 10)))
        self.assertEqual(self.chapter_hits(slow), old_hits)
        self.assertEqual(fast.hits["/chapter/7/上"], 1)
        self.assertEqual(fast.hits["/chapter/7/下"], 1)
        self.assertEqual(
            "".join(
                self.books.catalog.chapter(slow.book_id, final[6]["chapter_id"])["content"].split()
            ),
            "".join(fast.text(7).split()),
        )
        self.assertEqual(
            self.books.catalog.book(slow.book_id)["source_selection"]["repaired_chapters"], 3
        )
        hits = self.chapter_hits(fast)
        await self.books.scan(slow.book_id)
        repeated = await self.run_job()
        self.assertEqual(repeated["state"], "completed", repeated["code"])
        self.assertTrue(repeated["book_reused"])
        self.assertEqual(self.chapter_hits(fast), hits)
        refreshed = await self.books.preview(slow.book_id, final[-1]["chapter_id"], refresh=True)
        self.assertFalse(refreshed["cached"])
        self.assertEqual(fast.hits["/chapter/9"], hits["/chapter/9"] + 1)
        aliased = await self.call("subscriptions/save", {"book_id": fast.book_id})
        self.assertEqual(aliased["subscription"]["book_id"], slow.book_id)
        self.assertEqual(len(self.repo.list("book_subscription")), 1)
        path, _, _ = await self.books.file(slow.book_id, "epub")
        validate_epub(path)
        provenance = json.loads((path.parent / "source.json").read_bytes())
        self.assertEqual(len(provenance["chapter_sources"]), 9)
        self.assertEqual(
            sum(
                row["source"]["source_id"] == fast.source_id
                for row in provenance["chapter_sources"]
            ),
            3,
        )
        with zipfile.ZipFile(path) as archive:
            package = ET.fromstring(archive.read("EPUB/package.opf"))
            self.assertEqual(len(package.find(f"{{{OPF}}}spine")), 9)

    async def test_failed_body_uses_matching_source_without_dropping_order(self):
        current = await self.site("当前源")
        fallback = await self.site("备用源")
        current.failed = {2}
        await self.call("subscriptions/save", {"book_id": current.book_id})
        job = await self.run_job()
        self.assertEqual(job["state"], "completed", job["code"])
        directory = self.books.catalog.directory(current.book_id)
        self.assertEqual([chapter_label(c["title"])[0] for c in directory], list(range(1, 7)))
        chapter = self.books.catalog.chapter(current.book_id, directory[1]["chapter_id"])
        self.assertEqual(chapter["origin"]["source_id"], fallback.source_id)
        self.assertEqual(fallback.hits["/chapter/2"], 1)
        self.assertEqual(current.hits["/chapter/2"], 1)
        self.assertEqual(
            self.books.catalog.book(current.book_id)["source_selection"]["last_repair"][
                "source_name"
            ],
            fallback.name,
        )

    async def test_dead_directory_preserves_existing_book_and_catches_up(self):
        current = await self.site("旧源", 5)
        await self.call("subscriptions/save", {"book_id": current.book_id})
        await self.run_job()
        fallback = await self.site("备用源", 7)
        current.directory_failed = True
        await self.books.scan(current.book_id)
        job = await self.run_job()
        self.assertEqual(job["state"], "completed", job["code"])
        self.assertEqual(job["book_artifact"]["chapters"], 7)
        self.assertEqual(
            self.books.catalog.book(current.book_id)["source_selection"]["preferred_source_name"],
            fallback.name,
        )

    async def test_wrong_author_body_and_reversed_directory_are_rejected(self):
        current = await self.site("原书", 6)
        await self.call("subscriptions/save", {"book_id": current.book_id})
        await self.run_job()
        wrong_author = await self.site("同名异作者", 9)
        wrong_author.author = "另一作者"
        wrong_body = await self.site("冒名正文", 10)
        wrong_body.wrong_body = True
        reversed_site = await self.site("乱序目录", 11)
        reversed_site.reverse = True
        await self.books.scan(current.book_id)
        job = await self.run_job()
        self.assertEqual(job["state"], "completed", job["code"])
        self.assertEqual(job["book_artifact"]["chapters"], 6)
        codes = {
            row["source_name"]: row["code"]
            for row in self.books.catalog.book(current.book_id)["source_selection"]["candidates"]
        }
        self.assertEqual(codes["同名异作者"], "book_identity_mismatch")
        self.assertEqual(codes["冒名正文"], "book_identity_mismatch")
        self.assertEqual(codes["乱序目录"], "book_chapter_mapping_ambiguous")

    async def test_middle_gap_is_inserted_and_conflicting_tail_is_not_guessed(self):
        current = await self.site("有缺目录", 6)
        current.omitted = {3}
        await self.books.source_detail(current.book_id, refresh=True)
        donor = await self.site("完整目录", 8)
        conflict = await self.site("冲突目录", 8)
        conflict.rename[7] = "完全不同的新章节"
        await self.call("subscriptions/save", {"book_id": current.book_id})
        job = await self.run_job()
        self.assertEqual(job["state"], "completed", job["code"])
        directory = self.books.catalog.directory(current.book_id)
        self.assertEqual([chapter_label(c["title"])[0] for c in directory], list(range(1, 7)))
        self.assertEqual(
            self.books.catalog.book(current.book_id)["source_selection"]["code"],
            "book_chapter_mapping_ambiguous",
        )
        self.assertEqual(donor.hits["/chapter/3"] + conflict.hits["/chapter/3"], 1)

    async def test_background_discovers_new_source_and_recovers_dead_source(self):
        current = await self.site("后台旧源", 5)
        saved = await self.call("subscriptions/save", {"book_id": current.book_id})
        await self.run_job()
        previous = self.chapter_hits(current)
        donor = await self.site("后台新源", 8, discover=False)
        current.directory_failed = True
        advanced = self.repo.clock() + 3601
        started = time.monotonic()
        self.repo.clock = lambda: advanced + time.monotonic() - started
        await self.media.start()
        async with asyncio.timeout(40):
            while True:
                job = await self.media.local(self.repo.job, saved["job_id"])
                if (
                    job["state"] == "completed"
                    and job.get("book_artifact", {}).get("chapters") == 8
                ):
                    break
                await asyncio.sleep(0.1)
        self.assertGreaterEqual(
            self.repo.get("book_subscription", current.book_id)["last_scan_at"], advanced
        )
        self.assertEqual(self.chapter_hits(current), previous)
        self.assertEqual(donor.hits["/search"], 1)
        self.assertEqual(donor.hits["/chapter/8"], 1)

    async def test_fresh_primary_split_chapter_is_combined_without_changing_native_cache(self):
        current = await self.site("分段主源", 6)
        current.split = {2}
        await self.books.source_detail(current.book_id, refresh=True)
        await self.call("subscriptions/save", {"book_id": current.book_id})
        job = await self.run_job()
        self.assertEqual(job["state"], "completed", job["code"])
        self.assertEqual(job["book_artifact"]["chapters"], 6)
        self.assertEqual(len(self.books.catalog.source_directory(current.book_id)), 7)
        chapter = self.books.catalog.directory(current.book_id)[1]
        cached = self.books.catalog.chapter(current.book_id, chapter["chapter_id"])
        self.assertEqual("".join(cached["content"].split()), "".join(current.text(2).split()))
        self.assertEqual(len(cached["origin"]["chapter_ids"]), 2)
        self.assertEqual(current.hits["/chapter/2/上"], 1)
        self.assertEqual(current.hits["/chapter/2/下"], 1)

    async def test_missing_opening_chapter_is_inserted_before_preserved_ids(self):
        current = await self.site("缺开头", 6)
        current.omitted = {1}
        await self.books.source_detail(current.book_id, refresh=True)
        await self.call("subscriptions/save", {"book_id": current.book_id})
        await self.run_job()
        original = self.books.catalog.directory(current.book_id)
        donor = await self.site("完整开头", 6)
        await self.books.scan(current.book_id)
        job = await self.run_job()
        self.assertEqual(job["state"], "completed", job["code"])
        final = self.books.catalog.directory(current.book_id)
        self.assertEqual([chapter_label(row["title"])[0] for row in final], list(range(1, 7)))
        self.assertEqual(
            [row["chapter_id"] for row in final[1:]], [row["chapter_id"] for row in original]
        )
        self.assertEqual(donor.hits["/chapter/1"], 1)

    async def test_newer_locked_source_cannot_replace_complete_epub(self):
        current = await self.site("可读旧源", 6)
        await self.call("subscriptions/save", {"book_id": current.book_id})
        await self.run_job()
        path, _, _ = await self.books.file(current.book_id, "epub")
        original = path.read_bytes()
        donor = await self.site("新章需登录", 9)
        donor.notices[9] = "请先登录后阅读本章"
        await self.books.scan(current.book_id)
        job = await self.run_job()
        self.assertEqual(job["state"], "completed", job["code"])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(job["book_artifact"]["chapters"], 6)
        selection = self.books.catalog.book(current.book_id)["source_selection"]
        notice = next(row for row in selection["candidates"] if row["book_id"] == donor.book_id)
        self.assertFalse(notice["readable"])
        self.assertEqual(notice["code"], "book_source_auth_required")
        latest = self.books.catalog.source_directory(donor.book_id)[-1]
        self.assertIsNone(self.books.catalog.chapter(donor.book_id, latest["chapter_id"]))


class ChapterMatchingTests(unittest.TestCase):
    def test_chinese_numbers_split_parts_and_conflicts(self):
        self.assertEqual(chapter_label("第八百四十七章 误闯天家")[0], 847)
        entries = [
            {"title": name, "order": i, "chapter_id": str(i), "url": str(i)}
            for i, name in enumerate(
                ["第1章 春风【上】", "第1章 春风【下】", "第2章 山雨", "第3章 青山", "第3章 海潮"]
            )
        ]
        index = ChapterIndex(entries)
        self.assertEqual(len(index.by_number[1][0].parts), 2)
        self.assertEqual([unit.number for unit in index.extension(1, 1)], [2])
        incomplete = ChapterIndex(entries[:1])
        self.assertTrue(incomplete.ambiguous)

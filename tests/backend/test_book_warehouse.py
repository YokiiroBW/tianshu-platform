import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
import uuid

from book_fixtures import BookSite, LiveConsole
from services.platform.contracts import Fault
from services.platform.media.runtime import Media
from services.platform.media.scheduling import scan_due
from services.platform.media.web import WebMedia


class WarehouseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        runtime = Path(__file__).resolve().parents[2] / ".runtime"
        runtime.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="warehouse-test-", dir=runtime)
        self.addCleanup(self.temp.cleanup)
        self.now = time.time()
        self.configuration = {"database_path": str(Path(self.temp.name) / "state.sqlite")}
        self.media = Media(self.configuration, clock=lambda: self.now, local=asyncio.to_thread)
        self.addAsyncCleanup(self.media.close)
        self.site = await BookSite().start()
        self.addAsyncCleanup(self.site.runner.cleanup)
        self.books, self.repo = self.media.books, self.media.repo
        self.site.connect(self.books)
        self.web = WebMedia(self.media, LiveConsole())
        self.session = {"live": True}

    async def call(self, operation, body=None, client=None):
        return await self.web.route(
            "/api/web/media/books/" + operation,
            {**(body or {}), "client_id": client or str(uuid.uuid4())},
            self.session,
        )

    async def subscribe(self, kind="source", identity="101"):
        return (
            await self.call(
                "warehouse/subscribe",
                {"entries": [{"kind": kind, "entry_id": identity, "title": "所选测试条目"}]},
            )
        )["feeds"][0]

    async def due(self, media=None):
        media = media or self.media
        await scan_due(media, "book_feed", "feed_id", media.books.sources.refresh_feed)

    async def test_browse_search_pagination_detail_and_inert_projection(self):
        first = await self.call("warehouse/browse")
        self.assertEqual([row["entry_id"] for row in first["entries"]], ["101", "102"])
        self.assertTrue(first["has_next"])
        second = await self.call("warehouse/browse", {"page": 2})
        self.assertEqual([row["entry_id"] for row in second["entries"]], ["103"])
        self.assertFalse(second["has_next"])
        found = await self.call("warehouse/browse", {"query": "林间", "order": "down"})
        self.assertEqual([row["title"] for row in found["entries"]], ["林间书源"])
        empty = await self.call("warehouse/browse", {"query": "<script>"})
        self.assertEqual(empty["entries"], [])
        groups = await self.call("warehouse/browse", {"kind": "collection"})
        self.assertEqual(groups["entries"][0]["entry_id"], "201")
        detail = await self.call("warehouse/detail", {"kind": "collection", "entry_id": "201"})
        self.assertEqual(detail["title"], "合成精选合集")
        self.assertIn("合成说明", detail["description"])
        self.assertNotIn("warehouseUnsafe", json.dumps(detail))
        self.assertNotIn("private rule", json.dumps(detail))
        self.site.warehouse_page_failure = True
        for operation, body in [
            ("warehouse/browse", {}),
            ("warehouse/detail", {"kind": "source", "entry_id": "101"}),
        ]:
            with self.assertRaises(Fault) as failed:
                await self.call(operation, body)
            self.assertEqual(failed.exception.code, "book_warehouse_changed")

    async def test_durable_auto_updates_preserve_local_switch_good_rules_and_failures(self):
        feed = await self.subscribe()
        await self.due()
        source = self.repo.list("book_source")[0]
        self.books.catalog.source_result(source, good=True)
        source = self.repo.get("book_source", source["source_id"])
        await self.call(
            "sources/control",
            {
                "source_id": source["source_id"],
                "enabled": False,
                "expected_revision": source["revision"],
            },
        )
        initial = self.repo.get("book_feed", feed["feed_id"])
        self.assertEqual(initial["result"]["added"], 1)
        self.site.warehouse_version = 2
        await self.due()
        self.assertEqual(self.site.hits["/yuedu/shuyuan/json/id/101.json"], 1)
        # A restarted instance reads the persisted next due time and refreshes without UI calls.
        restarted = Media(self.configuration, clock=lambda: self.now, local=asyncio.to_thread)
        self.addAsyncCleanup(restarted.close)
        self.site.connect(restarted.books)
        self.now = initial["next_scan_at"] + 1
        await self.due(restarted)
        updated = self.repo.get("book_source", source["source_id"])
        self.assertIn("v2", updated["name"])
        self.assertFalse(updated["enabled"])
        self.assertEqual(updated["last_good_digest"], source["digest"])
        feed = self.repo.get("book_feed", feed["feed_id"])
        self.assertEqual(feed["result"]["updated"], 1)
        self.site.warehouse_failure = True
        self.now = feed["next_scan_at"] + 1
        await self.due()
        self.assertEqual(self.repo.get("book_source", source["source_id"]), updated)
        failed = self.repo.get("book_feed", feed["feed_id"])
        self.assertEqual(failed["code"], "book_source_unavailable")
        self.assertEqual(failed["next_scan_at"], self.now + 3600)

    async def test_repeat_selection_receipts_pause_interval_and_resume(self):
        body = {"entries": [{"kind": "source", "entry_id": "101", "title": "书源101"}] * 2}
        client = str(uuid.uuid4())
        saved = await self.call("warehouse/subscribe", body, client)
        replay = await self.call("warehouse/subscribe", body, client)
        self.assertEqual(replay["feeds"], saved["feeds"])
        self.assertTrue(replay["replayed"])
        feed = saved["feeds"][0]
        self.assertEqual(len(saved["feeds"]), 1)
        await self.call(
            "feeds/control",
            {"feed_id": feed["feed_id"], "expected_revision": feed["revision"], "action": "pause"},
        )
        again = await self.subscribe()
        self.assertEqual(again["state"], "paused")
        self.assertEqual(again["revision"], feed["revision"] + 1)
        await self.due()
        self.assertEqual(len(self.repo.list("book_source")), 0)
        interval = (
            await self.call(
                "feeds/control",
                {
                    "feed_id": feed["feed_id"],
                    "expected_revision": again["revision"],
                    "action": "interval",
                    "interval_seconds": 21600,
                },
            )
        )["feed"]
        self.assertEqual(interval["state"], "paused")
        self.assertEqual(interval["next_scan_at"], self.now)
        await self.call(
            "feeds/control",
            {
                "feed_id": feed["feed_id"],
                "expected_revision": interval["revision"],
                "action": "resume",
            },
        )
        await self.due()
        self.assertEqual(len(self.repo.list("book_source")), 1)
        current = self.repo.get("book_feed", feed["feed_id"])
        self.assertEqual(current["next_scan_at"], self.now + 21600)
        with self.assertRaises(Fault):
            await self.call(
                "feeds/control",
                {
                    "feed_id": feed["feed_id"],
                    "expected_revision": feed["revision"],
                    "action": "remove",
                },
            )

    async def test_detail_protocol_json_links_deduplicate_and_invalid_urls_rejected(self):
        prefix = "https://www.yckceo.com:443/yuedu/shuyuan/"
        urls = [
            prefix + "content/id/101.html",
            prefix + "json/id/101.json",
            "yuedu://booksource/importonline?src=" + prefix + "json/id/101.json",
            "http://yckceo.com/yuedu/shuyuan/json/id/101.json",
        ]
        ids = [(await self.call("feeds/save", {"url": url}))["feed"]["feed_id"] for url in urls]
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(self.repo.list("book_feed")), 1)
        self.assertEqual((await self.subscribe())["feed_id"], ids[0])
        batch = await self.call(
            "feeds/save", {"url": "https://www.yckceo.com:443/yuedu/shuyuan/jsons?id=101-102"}
        )
        self.assertEqual(batch["feed"]["url"], self.site.url + "/yuedu/shuyuan/jsons?id=101-102")
        for value in [
            "https://www.yckceo.com/",
            "https://www.yckceo.com/yuedu/rss/json/id/101.json",
            "javascript:alert(1)",
            "https://[bad",
            "yuedu://booksource/importonline?src=bad",
        ]:
            with self.assertRaises(Fault):
                await self.call("feeds/save", {"url": value})
        with self.assertRaises(Fault):
            await self.call("warehouse/detail", {"url": "http://127.0.0.1/private"})
        self.session["live"] = False
        before = sum(self.site.hits.values())
        with self.assertRaises(Fault):
            await self.call("warehouse/browse")
        self.assertEqual(sum(self.site.hits.values()), before)

    async def test_collection_partial_report_and_reappearing_source(self):
        feed = await self.subscribe("collection", "201")
        await self.due()
        result = self.repo.get("book_feed", feed["feed_id"])
        self.assertEqual(result["code"], "book_feed_partial")
        self.assertEqual(result["result"]["imported"], 2)
        self.assertEqual(result["result"]["reasons"], {"book_source_type_unsupported": 1})
        source = self.repo.list("book_source")[0]
        self.repo.put("book_source", source["source_id"], {**source, "upstream_removed": True})
        await self.call("feeds/refresh", {"feed_id": feed["feed_id"]})
        self.assertFalse(self.repo.get("book_source", source["source_id"])["upstream_removed"])

    async def test_empty_reader_rules_and_nonempty_array_validation(self):
        rules = self.site.source()
        rules.update(ruleSearch=[], ruleBookInfo=None, ruleExplore=[])
        result = await self.call("sources/import", {"sources": rules})
        self.assertEqual(result["imported"], 1)
        source, stored = self.books.catalog.source(result["source_ids"][0])
        self.assertEqual(stored["ruleSearch"], {})
        self.assertEqual(stored["ruleBookInfo"], {})
        self.assertEqual(rules["ruleSearch"], [])
        rejected = await self.call(
            "sources/import", {"sources": {**rules, "ruleContent": ["invalid"]}}
        )
        self.assertEqual(rejected["invalid"], 1)
        self.assertEqual(
            self.repo.get("book_source", source["source_id"])["digest"], source["digest"]
        )

    async def test_large_feed_uses_separate_budget_from_chapter_fetches(self):
        url = self.site.url + "/large-rules.json"
        with self.assertRaises(Fault) as failed:
            await asyncio.to_thread(self.books.network.fetch, url)
        self.assertEqual(failed.exception.code, "book_response_too_large")
        feed = (await self.call("feeds/save", {"url": url}))["feed"]
        await self.due()
        self.assertEqual(self.repo.get("book_feed", feed["feed_id"])["result"]["imported"], 1)
        self.assertEqual(self.books.network.max_bytes, 8 * 1024 * 1024)

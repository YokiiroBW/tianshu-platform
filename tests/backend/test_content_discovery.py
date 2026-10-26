"""Actual Media ledger/catalog pages and isolated SearXNG protocol fixtures."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from services.platform.content_discovery import Discovery, MediaCatalog
from services.platform.content_discovery.candidates import public_url
from services.platform.content_discovery.schemas import INPUT, OUTPUT
from services.platform.contracts import Fault
from services.platform.media.runtime import Media
from services.platform.media.music.runtime import Music
from services.platform.plugins.schema import check


class CatalogFixture:
    def prepare_catalog(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ts318-")
        self.addCleanup(self.temp.cleanup)
        self.settings = {"database_path": str(Path(self.temp.name) / "platform.db")}
        self.media = Media(self.settings, clock=lambda: 1800000000, local=asyncio.to_thread)
        self.media.music = Music(self.media)
        self.addAsyncCleanup(self.media.music.close)
        self.repo = self.media.repo
        self.discovery = Discovery(MediaCatalog(self.media), clock=lambda: 1800000000)

    def book(self, identity, title="A real catalog title"):
        return self.repo.put(
            "book",
            identity,
            {
                "book_id": identity,
                "metadata": {
                    "title": title,
                    "author": "Author",
                    "description": "Stored description",
                },
                "artifact": {"path": "C:/private/library/book.epub"},
                "updated_at": 1700000000,
            },
        )

    async def discover(self, config, value=None, response=None):
        async def request(arguments):
            self.request_arguments = arguments
            return response or {"status": 200, "body": {"results": []}}

        value = value or {}
        check(INPUT, value)
        result = await self.discovery.discover(config, value, request=request)
        check(OUTPUT, result)
        return result


class ContentDiscoveryTests(CatalogFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.prepare_catalog()

    async def test_real_catalog_pagination_does_not_require_known_content_ids(self):
        self.book("a" * 24, "Not matching")
        self.book("b" * 24, "Wanted book")
        config = {"include_books": True}
        value = {"sources": ["books"], "query": "wanted", "limit": 1}
        first = await self.discover(config, value)
        self.assertEqual(first["candidates"], [])
        cursor = first["sources"][0]["next_cursor"]
        self.assertIsNotNone(cursor)
        second = await self.discover(config, {**value, "cursors": {"books": cursor}})
        self.assertEqual(second["candidates"][0]["title"], "Wanted book")
        self.assertIsNone(second["sources"][0]["next_cursor"])
        self.assertNotIn("private/library", json.dumps(second))
        with self.assertRaises(Fault):
            await self.discover(
                config, {**value, "query": "different", "cursors": {"books": cursor}}
            )
        with self.assertRaises(Fault):
            await self.discover({"include_books": False}, {**value, "cursors": {"books": cursor}})

    async def test_private_music_requires_explicit_original_scope_including_empty(self):
        for index, scope in enumerate(["", "account:a", "account:b"]):
            self.repo.put(
                "music_recording",
                "r" + str(index),
                {"recording_id": "r" + str(index), "metadata": {"title": "Wrong merged metadata"}},
            )
            self.repo.put(
                "music_source",
                "source" + str(index),
                {
                    "source_key": "source" + str(index),
                    "recording_id": "r" + str(index),
                    "private_scope": scope,
                    "metadata": {"title": "Allowed " + str(index), "artists": ["Singer"]},
                },
            )
        value = {"sources": ["music"]}
        self.assertEqual((await self.discover({"include_music": True}, value))["candidates"], [])
        result = await self.discover({"include_music": True, "music_scopes": ["account:a"]}, value)
        self.assertEqual([r["title"] for r in result["candidates"]], ["Allowed 1"])
        self.assertEqual(result["candidates"][0]["read_locator"]["source_key"], "source1")
        self.assertNotIn("account:", json.dumps(result))
        result = await self.discover({"include_music": True, "music_scopes": [""]}, value)
        self.assertEqual([r["title"] for r in result["candidates"]], ["Allowed 0"])

    async def test_matching_authorized_music_source_survives_other_source_title(self):
        self.repo.put("music_recording", "recording", {"recording_id": "recording", "metadata": {}})
        for key, title in [("a", "Original language title"), ("b", "Matching translated title")]:
            self.repo.put(
                "music_source",
                key,
                {
                    "source_key": key,
                    "recording_id": "recording",
                    "private_scope": "scope",
                    "metadata": {"title": title, "artists": []},
                },
            )
        result = await self.discover(
            {"include_music": True, "music_scopes": ["scope"]},
            {"sources": ["music"], "query": "translated"},
        )
        self.assertEqual(
            [item["title"] for item in result["candidates"]], ["Matching translated title"]
        )

    async def test_comics_library_scope_and_source_version_not_user_progress(self):
        for index, kind in enumerate(["general", "private"]):
            self.repo.put(
                "comic",
                str(index) * 24,
                {
                    "comic_id": str(index) * 24,
                    "library_kind": kind,
                    "metadata": {"title": kind, "author": "Creator"},
                },
            )
        self.repo.put("book_reading_progress", "user:book", {"offset": 19})
        result = await self.discover({"include_comics": True}, {"sources": ["comics"]})
        self.assertEqual([r["title"] for r in result["candidates"]], ["general"])
        old = result["candidates"][0]
        self.repo.put(
            "comic",
            "0" * 24,
            {"comic_id": "0" * 24, "library_kind": "general", "metadata": {"title": "new"}},
        )
        result = await self.discover(
            {"include_comics": True},
            {"sources": ["comics"], "exclude_keys": [old["candidate_key"]]},
        )
        self.assertEqual(result["candidates"][0]["title"], "new")
        result = await self.discover(
            {"include_comics": True}, {"sources": ["comics"], "exclude_keys": [old["content_key"]]}
        )
        self.assertEqual(result["candidates"], [])
        self.assertEqual(self.repo.get("book_reading_progress", "user:book")["offset"], 19)

    async def test_searx_json_403_and_invalid_source_fall_back_to_actual_books(self):
        self.book("a" * 24, "Search topic")
        config = {"base_url": "http://127.0.0.1:1234/searx", "include_books": True}
        value = {"query": "Search", "sources": ["web", "books"]}
        result = await self.discover(config, value, {"status": 403, "body": {"results": []}})
        self.assertEqual(result["sources"][0]["state"], "unavailable")
        self.assertEqual(result["candidates"][0]["source"], "books")
        self.assertIn("/searx/search?q=Search&format=json&pageno=1", self.request_arguments["url"])
        self.assertEqual(result["seen_state"], "unknown")
        self.assertNotIn("content_hash", json.dumps(result))

    async def test_invalid_optional_search_time_does_not_discard_local_candidates(self):
        self.book("a" * 24, "Search topic")
        result = await self.discover(
            {"base_url": "https://search.invalid", "include_books": True},
            {"query": "Search", "sources": ["web", "books"]},
            {
                "status": 200,
                "body": {
                    "results": [
                        {"url": "https://example.org/a", "title": "Search", "publishedDate": False},
                        {
                            "url": "https://example.org/b",
                            "title": "Search",
                            "publishedDate": "not a timestamp",
                        },
                    ]
                },
            },
        )
        self.assertEqual([row["source"] for row in result["candidates"]], ["web", "web", "books"])
        self.assertTrue(all(row["published_at"] is None for row in result["candidates"]))

    async def test_search_transport_preserves_explicit_default_port_for_vault_origin(self):
        for base in ("http://search.invalid:80", "https://search.invalid:443/searx"):
            await self.discover({"base_url": base}, {"query": "topic", "sources": ["web"]})
            self.assertTrue(self.request_arguments["url"].startswith(base + "/search?"))

    async def test_url_dedup_preserves_query_fragment_and_equal_title_is_not_identity(self):
        rows = [
            {"url": url, "title": "Same title", "content": "<b>Snippet</b>"}
            for url in [
                "HTTPS://Example.org:443/article",
                "https://example.org/article",
                "https://example.org/article?edition=2",
                "https://example.org/article#part2",
            ]
        ]
        result = await self.discover(
            {"base_url": "https://search.invalid"},
            {"query": "topic", "sources": ["web"]},
            {"status": 200, "body": {"results": rows}},
        )
        self.assertEqual(len(result["candidates"]), 3)
        self.assertTrue(
            all(
                r["summary_kind"] == "search_snippet" and r["summary"] == "Snippet"
                for r in result["candidates"]
            )
        )
        self.assertTrue(all(r["owner_version"] is None for r in result["candidates"]))
        with self.assertRaises(Fault):
            public_url("https://name:secret@example.org/")

    async def test_invalid_search_rows_report_partial_and_do_not_invent_links(self):
        result = await self.discover(
            {"base_url": "https://search.invalid"},
            {"query": "topic", "sources": ["web"]},
            {
                "status": 200,
                "body": {
                    "results": [
                        {"title": "Missing URL"},
                        {"url": "file:///private/a"},
                        {"title": "Real", "url": "https://example.org", "content": "x" * 2100},
                    ]
                },
            },
        )
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["sources"][0]["state"], "partial")
        self.assertTrue(result["candidates"][0]["text_truncated"])

    async def test_search_continuation_does_not_skip_rest_of_upstream_page(self):
        rows = [
            {"url": "https://example.org/" + str(index), "title": str(index)} for index in range(3)
        ]
        config = {"base_url": "https://search.invalid"}
        value = {"query": "topic", "sources": ["web"], "limit": 1}
        response = {"status": 200, "body": {"results": rows}}
        first = await self.discover(config, value, response)
        second = await self.discover(
            config, {**value, "cursors": {"web": first["sources"][0]["next_cursor"]}}, response
        )
        self.assertEqual(first["candidates"][0]["title"], "0")
        self.assertEqual(second["candidates"][0]["title"], "1")
        changed = await self.discover(
            config,
            {**value, "cursors": {"web": second["sources"][0]["next_cursor"]}},
            {"status": 200, "body": {"results": list(reversed(rows))}},
        )
        self.assertEqual(changed["candidates"], [])
        self.assertEqual(changed["sources"][0]["state"], "unavailable")

    async def test_source_timeout_isolated_and_oversized_provider_result_rejected(self):
        self.book("a" * 24)

        async def timeout(arguments):
            raise TimeoutError()

        result = await self.discovery.discover(
            {"base_url": "https://search.invalid", "include_books": True},
            {"query": "title", "sources": ["web", "books"]},
            request=timeout,
        )
        self.assertEqual(result["sources"][0]["code"], "source_timeout")
        self.assertEqual(result["candidates"][0]["source"], "books")
        row = result["candidates"][0]

        class Oversized:
            def read(self, *args):
                return [{**row, "summary": "x" * (600 * 1024)}], "ok", None

        provider = Discovery(Oversized())
        with self.assertRaises(Fault) as caught:
            await provider.discover({}, {"sources": ["books"]}, request=timeout)
        self.assertEqual(caught.exception.code, "budget_exceeded")

    def test_document_page_preserves_payload_reader_removed_filter_and_empty_cursor(self):
        self.repo.put("subscription", "a", {"state": "removed"})
        self.repo.put("subscription", "b", {"state": "active"})
        with patch.object(self.repo.payloads, "read", wraps=self.repo.payloads.read) as read:
            rows, cursor = self.repo.document_page("subscription", limit=1)
            self.assertEqual(rows, [])
            self.assertEqual(cursor, "a")
            self.assertEqual(read.call_count, 1)
            rows, cursor = self.repo.document_page("subscription", after_id=cursor, limit=1)
            self.assertEqual(rows[0]["state"], "active")
            self.assertIsNone(cursor)

    async def test_members_are_historical_and_missing_parts_stay_unknown(self):
        self.repo.put("subscription", "sub", {"state": "active", "last_scan_at": 1700000000})
        with self.repo.transaction() as db:
            db.executemany(
                "INSERT INTO members VALUES(?,?)",
                [("sub", "BV1xx411c7mD"), ("sub", "BV1xx411c7mE")],
            )
            db.execute("INSERT INTO member_parts VALUES(?,?,?)", ("sub", "BV1xx411c7mE", "123"))
        rows, cursor = self.repo.member_page("sub", limit=1)
        self.assertIsNone(rows[0]["cid"])
        rows, cursor = self.repo.member_page("sub", limit=1, after=cursor)
        self.assertEqual(rows[0]["cid"], "123")
        self.assertIsNone(cursor)
        result = await self.discover({"subscription_ids": ["sub"]}, {"sources": ["bilibili"]})
        self.assertEqual(len(result["candidates"]), 2)
        self.assertTrue(
            all(r["title"] is None and r["seen_state"] == "unknown" for r in result["candidates"])
        )
        self.assertIsNone(result["candidates"][1]["read_locator"]["url"])
        self.assertTrue(all(item["source_observed_at"] is None for item in result["candidates"]))

    async def test_subscription_metadata_beyond_latest_jobs_is_reachable_without_global_grant(self):
        self.repo.clock = iter(range(1000, 2000)).__next__
        self.repo.put("subscription", "allowed", {"state": "active", "last_scan_at": 1900})
        bvid = "BV1xx411c7mD"
        with self.repo.transaction() as db:
            db.execute("INSERT INTO members VALUES(?,?)", ("allowed", bvid))
            db.execute("INSERT INTO member_parts VALUES(?,?,?)", ("allowed", bvid, "123"))
            self.repo.insert_job(
                {
                    "subscription_id": "allowed",
                    "bvid": bvid,
                    "selected_cids": ["123"],
                    "snapshot": {
                        "title": "Rare old title",
                        "captured_at": 100,
                        "parts": [{"cid": "123", "index": 1}],
                    },
                },
                "old",
                db,
            )
            for index in range(130):
                self.repo.insert_job(
                    {
                        "subscription_id": "other",
                        "bvid": bvid,
                        "selected_cids": ["123"],
                        "snapshot": {
                            "title": "Unrelated private Rare title",
                            "parts": [{"cid": "123", "index": 1}],
                        },
                    },
                    str(index),
                    db,
                )
        config = {"subscription_ids": ["allowed"]}
        value = {"query": "Rare", "sources": ["bilibili"], "limit": 1}
        observed = []
        for _ in range(140):
            result = await self.discover(config, value)
            observed.extend(result["candidates"])
            following = result["sources"][0]["next_cursor"]
            if following is None:
                break
            value["cursors"] = {"bilibili": following}
        else:
            self.fail("bounded metadata continuation did not finish")
        self.assertEqual([item["title"] for item in observed], ["Rare old title"])
        self.assertEqual(observed[0]["source_observed_at"], 100)

    async def test_video_library_reads_multipart_snapshot_with_bounded_continuation(self):
        bvid = "BV1xx411c7mD"
        with self.repo.transaction() as db:
            job = self.repo.insert_job(
                {
                    "bvid": bvid,
                    "selected_cids": ["1", "2"],
                    "snapshot": {
                        "title": "Recorded title",
                        "parts": [{"cid": "1", "index": 1}, {"cid": "2", "index": 2}],
                        "creators": [{"name": "Creator"}],
                    },
                },
                "video-test",
                db,
            )
        config, value = {"include_video_library": True}, {"sources": ["bilibili"], "limit": 1}
        first = await self.discover(config, value)
        self.assertTrue(first["candidates"][0]["content_key"].endswith(":1"))
        value["cursors"] = {"bilibili": first["sources"][0]["next_cursor"]}
        second = await self.discover(config, value)
        self.assertTrue(second["candidates"][0]["content_key"].endswith(":2"))
        self.assertIsNone(second["sources"][0]["next_cursor"])
        self.assertEqual(self.repo.job(job["job_id"])["state"], "queued")

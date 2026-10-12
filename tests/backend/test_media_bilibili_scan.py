"""Recorded response shapes over isolated HTTP; no real account or favorite contents."""

import unittest
from unittest.mock import patch

from media_runtime_fixtures import BV, OTHER_BV, Upstream, favorite_page
from services.platform.contracts import Fault
from services.platform.media.bilibili import Bilibili


class BilibiliScanTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.upstream = await Upstream().start()
        self.addAsyncCleanup(self.upstream.runner.cleanup)
        self.connector = Bilibili(api=self.upstream.url, passport=self.upstream.url)
        self.addAsyncCleanup(self.connector.close)

    async def favorite(self, **limits):
        return await self.connector.scan(
            {"kind": "favorite", "id": "123"}, "SESSDATA=synthetic-favorite-only", **limits
        )

    async def test_favorite_terminal_page_confirms_visible_set_below_media_count(self):
        members = [f"BV1{index:09d}" for index in range(23)]
        self.upstream.favorite_pages = [
            favorite_page(24, members[:19], True),
            favorite_page(24, members[19:]),
        ]
        self.assertEqual(await self.favorite(), set(members))
        self.assertEqual(self.upstream.favorite_requests, [1, 2])
        self.assertTrue(
            all(
                cookie == "SESSDATA=synthetic-favorite-only"
                for _, cookie in self.upstream.cookie_seen
            )
        )

    async def test_explicit_empty_visible_terminal_page_accepts_list_or_null(self):
        for members in ([], None):
            with self.subTest(members=members):
                self.upstream.favorite_pages = [favorite_page(4, members)]
                self.assertEqual(await self.favorite(), set())

    async def test_favorite_requires_boolean_pagination_and_valid_page_shape(self):
        invalid = []
        for value in (None, 0, 1, "false", [], {}):
            invalid.append({**favorite_page(1, [BV]), "has_more": value})
        for value in (False, 0, "", {}):
            invalid.append({**favorite_page(1, [BV]), "medias": value})
        for value in (None, [], 1):
            invalid.append({**favorite_page(1, [BV]), "info": value})
        for missing in ("has_more", "medias", "info"):
            page = favorite_page(1, [BV])
            del page[missing]
            invalid.append(page)
        for value in (None, True, -1, "1"):
            invalid.append({**favorite_page(1, [BV]), "info": {"media_count": value}})
        invalid.append(favorite_page(21, [f"BV1{index:09d}" for index in range(21)]))
        for index, page in enumerate(invalid):
            with self.subTest(index=index):
                self.upstream.favorite_pages = [page]
                with self.assertRaises(Fault) as caught:
                    await self.favorite()
                self.assertEqual(caught.exception.code, "source_invalid_response")

    async def test_favorite_total_changes_still_reject_the_scan(self):
        self.upstream.favorite_pages = [
            favorite_page(24, [BV], True),
            favorite_page(25, [OTHER_BV]),
        ]
        with self.assertRaises(Fault) as caught:
            await self.favorite()
        self.assertEqual(caught.exception.code, "source_changed_during_scan")

    async def test_repeated_or_overlapping_pages_do_not_confirm_a_partial_scan(self):
        for second_page in ([BV], [BV, OTHER_BV]):
            with self.subTest(second_page=second_page):
                self.upstream.favorite_pages = [
                    favorite_page(24, [BV], True),
                    favorite_page(24, second_page),
                ]
                with self.assertRaises(Fault) as caught:
                    await self.favorite()
                self.assertEqual(caught.exception.code, "source_scan_incomplete")

    async def test_favorite_budgets_and_missing_members_still_reject(self):
        cases = [
            ([favorite_page(24, [BV], True)], {"page_limit": 1}, "source_budget_exceeded"),
            ([favorite_page(24, [BV, OTHER_BV])], {"item_limit": 1}, "source_budget_exceeded"),
            ([favorite_page(24, [], True)], {}, "source_scan_incomplete"),
            ([favorite_page(1, [BV, OTHER_BV])], {}, "source_scan_incomplete"),
            ([{**favorite_page(24, []), "medias": [{}]}], {}, "source_member_unavailable"),
        ]
        for pages, limits, code in cases:
            with self.subTest(code=code, limits=limits):
                self.upstream.favorite_pages = pages
                with self.assertRaises(Fault) as caught:
                    await self.favorite(**limits)
                self.assertEqual(caught.exception.code, code)

    async def test_other_source_kinds_keep_exact_total_validation(self):
        original = self.connector.request

        async def counted_response(path, **kwargs):
            data = await original(path, **kwargs)
            if path == "/x/space/wbi/arc/search":
                data["page"]["count"] += 1
            elif path in {"/x/polymer/web-space/seasons_archives_list", "/x/series/archives"}:
                data["page"]["total"] += 1
            return data

        with patch.object(self.connector, "request", counted_response):
            for kind in ("uploader", "collection", "series"):
                with self.subTest(kind=kind):
                    identity = "123" if kind == "uploader" else "123:456"
                    with self.assertRaises(Fault) as caught:
                        await self.connector.scan({"kind": kind, "id": identity}, None)
                    self.assertEqual(caught.exception.code, "source_scan_incomplete")

import unittest
import uuid

import test_comics as comic_tests
from services.platform.contracts import Fault
from services.platform.media.comics.search_text import (
    match_score,
    normalize,
    query_variants,
    search_names,
)


class ChineseTextTests(unittest.TestCase):
    def test_traditional_variants_punctuation_and_exact_title_rank(self):
        self.assertEqual(normalize("臺灣漫畫　ＡＢＣ"), normalize("台湾漫画 ABC"))
        self.assertEqual(match_score(["西 游 记"], {"title": "西遊記"}), 3)
        self.assertEqual(match_score(["西游记"], {"title": "西遊記外傳"}), 2)
        self.assertEqual(match_score(["西游记"], {"title": "其他作品", "author": "西游记"}), 1)
        self.assertEqual(match_score(["西游记"], {"title": "西游纪"}), 0)
        self.assertEqual(match_score(["one-piece"], {"title": "ＯＮＥ ＰＩＥＣＥ"}), 3)

    def test_explicit_aliases_are_alternatives_without_inventing_translations(self):
        item = {"title": "航海王"}
        self.assertEqual(match_score(["海贼王"], item), 0)
        self.assertEqual(match_score(["海贼王", "航海王"], item), 3)
        self.assertEqual(query_variants(["西游记", "西遊記"]), ["西游记", "西遊記"])
        with self.assertRaises(Fault):
            search_names("漫画", ["a", "b", "c"])


class ChineseSearchTests(unittest.IsolatedAsyncioTestCase):
    # Reuse the existing real HTTP/queue/archive fixture without inheriting its test methods.
    asyncSetUp = comic_tests.ComicTests.asyncSetUp
    call = comic_tests.ComicTests.call
    comic = comic_tests.ComicTests.comic
    run_job = comic_tests.ComicTests.run_job

    async def search(self, body):
        result = await self.call("search/start", body)
        await self.comics.sources.searches[result["search_id"]]["task"]
        return await self.call("search/results", {"search_id": result["search_id"]})

    async def test_default_chinese_scope_preserves_foreign_sources_and_source_preferences(self):
        await self.comics.sources.refresh()
        before = await self.comics.sources.list()
        engine = await self.comics.sources.engine_id()
        self.comics.catalog.save_sources(
            [
                {
                    "id": "9007199254740993",
                    "name": "合成漫画源",
                    "lang": "zh-Hant",
                    "contentWarning": "SAFE",
                },
                {"id": "55", "name": "English fixture", "lang": "en", "contentWarning": "SAFE"},
            ],
            engine,
        )
        result = await self.search({"query": "星際"})
        self.assertEqual(result["language"], "zh")
        self.assertEqual([s["source_id"] for s in result["sources"]], ["9007199254740993"])
        self.assertEqual(len(result["comics"]), 1)
        rows = await self.comics.sources.list()
        self.assertTrue(next(s for s in rows if s["source_id"] == "55")["enabled"])
        self.assertEqual(
            next(s for s in rows if s["source_id"] == before[0]["source_id"])["revision"],
            before[0]["revision"],
        )
        explicit = await self.search({"query": "星际", "language": "en"})
        self.assertEqual([s["source_id"] for s in explicit["sources"]], ["55"])

    async def test_upstream_traditional_query_dedup_ranking_and_page_forwarding(self):
        await self.comics.sources.refresh()
        requests = []

        async def engine_search(source, query, page):
            requests.append((source, query, page))
            return {
                "mangas": []
                if query == "西游记"
                else [
                    self.site.manga(number=3, title="西遊記外傳"),
                    self.site.manga(number=4, title="西遊記"),
                    self.site.manga(number=4, title="西遊記"),
                ],
                "hasNextPage": True,
            }

        self.comics.engine.search = engine_search
        result = await self.search({"query": "西游记", "page": 2})
        self.assertEqual([r[1:] for r in requests], [("西游记", 2), ("西遊記", 2)])
        self.assertEqual(
            [c["metadata"]["title"] for c in result["comics"]], ["西遊記", "西遊記外傳"]
        )
        self.assertEqual(result["sources"][0]["count"], 2)
        self.assertTrue(result["sources"][0]["has_next_page"])
        self.assertEqual(self.repo.job_count(), 0)

    async def test_partial_query_error_keeps_matches_and_safe_failure_facts(self):
        await self.comics.sources.refresh()

        async def engine_search(source, query, page):
            if query == "西遊記":
                raise Fault("comic_source_server_error", 503)
            return {"mangas": [self.site.manga(title="西游记")], "hasNextPage": False}

        self.comics.engine.search = engine_search
        result = await self.search({"query": "西游记"})
        self.assertEqual(len(result["comics"]), 1)
        self.assertEqual(result["sources"][0]["code"], "comic_search_partial")
        record = (await self.comics.sources.list())[0]["health"]["search"]
        self.assertEqual(record["count"], 1)
        self.assertEqual(record["query_errors"], ["comic_source_server_error"])

    async def test_alias_result_and_private_scope_are_kept_separate(self):
        result = await self.search(
            {"query": "另一译名", "aliases": ["星際"], "library_kind": "private"}
        )
        self.assertEqual(len(result["comics"]), 1)
        self.assertEqual(result["comics"][0]["library_kind"], "private")
        self.assertEqual(result["sources"][0]["source_id"], "9007199254740994")

    async def test_chapter_retry_reuses_job_and_preserved_pages(self):
        comic = await self.comic()
        detail = await self.call("detail", {"comic_id": comic["comic_id"]})
        chapter = detail["chapters"][0]
        queued = await self.call(
            "downloads/enqueue",
            {"comic_id": comic["comic_id"], "chapter_ids": [chapter["chapter_id"]]},
        )
        self.site.fail_pages.add("/image/1/2")
        failed = await self.run_job()
        self.assertEqual(failed["state"], "retry_wait")
        shown = (await self.call("detail", {"comic_id": comic["comic_id"]}))["chapters"][0]["job"]
        self.assertEqual(shown["job_id"], queued["job_ids"][0])
        self.assertTrue(shown["can_retry"])
        self.assertEqual(shown["comic_pages_done"], 1)
        self.assertNotIn("comic_target", shown)
        first_hits = self.site.hits["/image/1/1"]
        self.site.fail_pages.clear()
        await self.web.route(
            "/api/web/media/jobs/control",
            {"job_id": shown["job_id"], "action": "retry", "client_id": str(uuid.uuid4())},
            self.session,
        )
        done = await self.run_job()
        self.assertEqual(done["state"], "completed")
        self.assertEqual(done["job_id"], shown["job_id"])
        self.assertEqual(self.repo.job_count(), 1)
        self.assertEqual(self.site.hits["/image/1/1"], first_hits)
        shown = (await self.call("detail", {"comic_id": comic["comic_id"]}))["chapters"][0]
        self.assertIsNotNone(shown["artifact"])
        self.assertFalse(shown["job"]["can_retry"])

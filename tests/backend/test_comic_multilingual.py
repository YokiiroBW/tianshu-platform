import copy
import asyncio
import json

import aiohttp
from aiohttp import web

import unittest
import test_comics as fixtures

from services.platform.contracts import Fault
from services.platform.media.comics.aliases import Aliases
from services.platform.media.comics.versions import evidence, chinese_filter_plans


class MultilingualTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ComicTests.asyncSetUp
    call = fixtures.ComicTests.call

    async def search(self, body):
        result = await self.call("search/start", body)
        await self.comics.sources.searches[result["search_id"]]["task"]
        return await self.call("search/results", {"search_id": result["search_id"]})

    async def test_public_metadata_transport_reads_all_chunks_and_stops_oversized_responses(self):
        payload = json.dumps({"title": "Star Courier", "padding": "x" * 9000}).encode()

        async def stream(request):
            response = web.StreamResponse(headers={"Content-Type": "application/json"})
            await response.prepare(request)
            try:
                for offset in range(0, len(payload), 2000):
                    await response.write(payload[offset : offset + 2000])
                    await asyncio.sleep(0.001)
                await response.write_eof()
            except ConnectionResetError:
                pass
            return response

        app = web.Application()
        app.router.add_get("/metadata", stream)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))

        class LocalTransport:
            def request(self, method, url, **kwargs):
                assert url == "https://api.bgm.tv/v0/subjects/1"
                return client.request(method, f"http://127.0.0.1:{port}/metadata", **kwargs)

            async def close(self):
                await client.close()

        self.comics.aliases.session = LocalTransport()
        result = await Aliases.request(self.comics.aliases, "bangumi", "/v0/subjects/1")
        self.assertEqual(result["title"], "Star Courier")
        payload = b"x" * (1024 * 1024 + 1)
        with self.assertRaises(Fault):
            await Aliases.request(self.comics.aliases, "bangumi", "/v0/subjects/1")

    async def test_english_and_japanese_names_find_chinese_title_without_manual_aliases(self):
        for query in ("Star Courier", "星間郵便"):
            result = await self.search({"query": query})
            self.assertEqual(result["alias_resolution"]["state"], "ready")
            self.assertIn("星际邮差", result["queries"])
            self.assertEqual([c["metadata"]["title"] for c in result["comics"]], ["星际邮差"])
            self.assertEqual(
                result["comics"][0]["language_evidence"], {"language": "zh", "basis": "source"}
            )
        calls = self.site.hits["alias:bangumi"]
        await self.search({"query": "Star Courier"})
        self.assertEqual(self.site.hits["alias:bangumi"], calls)
        self.assertEqual(self.repo.job_count(), 0)

    async def test_anilist_bridge_only_connects_an_exact_work_not_sequels_or_novels(self):
        async def provider(name, path, body=None):
            if name == "anilist":
                return {
                    "data": {
                        "Page": {
                            "media": [
                                {
                                    "id": 1,
                                    "title": {"english": "Star Courier", "native": "STAR COURIER"},
                                    "synonyms": ["ほしのゆうびん"],
                                },
                                {
                                    "id": 2,
                                    "title": {"english": "Star Courier Sequel"},
                                    "synonyms": [],
                                },
                            ]
                        }
                    }
                }
            if body and body["keyword"] == "ほしのゆうびん":
                return {"data": []}
            return await self.site.alias_request(name, path, body)

        self.comics.aliases.request = provider
        result = await self.search({"query": "ほしのゆうびん"})
        self.assertEqual(result["alias_resolution"]["selected"]["id"], "bangumi:10001")
        self.assertEqual(result["alias_resolution"]["selected"]["via"], "AniList")
        self.assertNotIn("Star Courier Sequel", result["queries"])
        self.assertEqual(len(result["comics"]), 1)

    async def test_ambiguous_names_wait_for_selected_work_and_do_not_merge_aliases(self):
        async def provider(name, path, body=None):
            records = [
                {
                    "id": i,
                    "type": 1,
                    "platform": "漫画",
                    "name": "Same Title",
                    "name_cn": title,
                    "infobox": [{"key": "作者", "value": author}],
                }
                for i, title, author in ((1, "星际邮差", "甲"), (2, "另外作品", "乙"))
            ]
            return {"data": records} if body else records[int(path.rsplit("/", 1)[-1]) - 1]

        self.comics.aliases.request = provider
        result = await self.search({"query": "Same Title"})
        self.assertEqual(result["alias_resolution"]["state"], "ambiguous")
        self.assertNotIn("星际邮差", result["queries"])
        chosen = await self.search({"query": "Same Title", "alias_subject": "bangumi:1"})
        self.assertEqual(len(chosen["comics"]), 1)
        self.assertNotIn("另外作品", chosen["queries"])
        invalid = await self.search({"query": "Same Title", "alias_subject": "bangumi:99"})
        self.assertEqual(invalid["code"], "comic_alias_choice_expired")

    async def test_provider_failure_keeps_original_and_manual_search(self):
        async def unavailable(*args, **kwargs):
            raise Fault("comic_alias_provider_unavailable", 503)

        self.comics.aliases.request = unavailable
        result = await self.search({"query": "Star Courier", "aliases": ["星际"]})
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["alias_resolution"]["state"], "unavailable")
        self.assertEqual(len(result["comics"]), 1)

    async def multi_source(self):
        rows = self.comics.catalog.save_sources(
            [{"id": "55", "name": "Multilingual fixture", "lang": "all", "contentWarning": "SAFE"}],
            await self.comics.sources.engine_id(),
        )
        return rows[0]

    async def test_multilingual_search_keeps_chinese_edition_with_english_title_and_unknown_is_distinct(
        self,
    ):
        source = await self.multi_source()
        requests = []

        async def filters(identity):
            return [
                {
                    "__typename": "SelectFilter",
                    "name": "Original language",
                    "values": ["Japanese", "Chinese"],
                },
                {
                    "__typename": "SelectFilter",
                    "name": "Translated language",
                    "values": ["All", "Chinese (Simplified)"],
                },
            ]

        async def search(identity, query, page, **kwargs):
            requests.append(kwargs)
            return {
                "mangas": [
                    {
                        **self.site.manga("55", 1, "Star Courier"),
                        "description": "Language: Chinese (Simplified)",
                    },
                    {**self.site.manga("55", 2, "Star Courier [English]"), "description": ""},
                    {**self.site.manga("55", 3, "Star Courier"), "description": ""},
                ],
                "filters_applied": bool(kwargs.get("filters")),
                "hasNextPage": False,
            }

        self.comics.engine.filters, self.comics.engine.search = filters, search
        result = await self.search({"query": "Star Courier"})
        self.assertEqual(result["total"], 1)
        self.assertEqual(len(result["comics"]), 2)
        self.assertEqual(result["comics"][0]["language_evidence"]["language"], "zh-Hans")
        self.assertEqual(result["comics"][1]["language_evidence"]["language"], "unknown")
        self.assertTrue(result["comics"][1]["chinese_available"])
        self.assertTrue(all(r["filters"] == [{"position": 1, "selectState": 1}] for r in requests))
        self.assertEqual(
            self.repo.get("comic_source", source["source_key"])["revision"], source["revision"]
        )

    async def test_filter_unavailable_still_checks_item_language_and_preserves_source_controls(
        self,
    ):
        await self.multi_source()

        async def filters(identity):
            raise Fault("comic_engine_invalid_response", 503)

        async def search(identity, query, page):
            return {
                "mangas": [{**self.site.manga("55", 1, "星际邮差 [汉化]"), "description": ""}],
                "hasNextPage": False,
            }

        self.comics.engine.filters, self.comics.engine.search = filters, search
        result = await self.search({"query": "星际"})
        self.assertEqual(len(result["comics"]), 1)
        self.assertEqual(result["sources"][0]["language_filter"], "unavailable")
        self.assertEqual(result["comics"][0]["language_evidence"]["basis"], "translation_tag")

    async def mixed_comic(self):
        source = await self.multi_source()
        manga = {**self.site.manga("55", 1, "Star Courier"), "description": ""}
        comic = self.comics.catalog.register(source, manga)
        self.rows = [
            self.chapter(1, "Chinese (Simplified)", "甲组"),
            self.chapter(2, "English", "甲组"),
            self.chapter(3, "Chinese (Simplified)", "乙组"),
            self.chapter(4, "", "甲组"),
        ]

        async def detail(*args):
            return {"manga": manga, "chapters": copy.deepcopy(self.rows)}

        self.comics.engine.detail = detail
        return comic, await self.call("detail", {"comic_id": comic["comic_id"]})

    @staticmethod
    def chapter(number, language, group):
        return {
            "id": number,
            "name": f"Chapter {number}" + (f" [{language}]" if language else ""),
            "scanlator": group,
            "sourceOrder": number,
            "url": "/chapter/" + str(number),
            "pageCount": 3,
            "uploadDate": number * 1000,
            "chapterNumber": number,
            "mangaId": 1,
        }

    async def test_new_subscription_locks_language_and_group_and_tracks_only_that_version(self):
        comic, detail = await self.mixed_comic()
        version = next(v for v in detail["versions"] if v["scanlator"] == "甲组")
        saved = await self.call(
            "subscriptions/save",
            {"comic_id": comic["comic_id"], "mode": "new", "version_id": version["id"]},
        )
        self.assertEqual(saved["job_ids"], [])
        self.rows += [
            self.chapter(5, "English", "甲组"),
            self.chapter(6, "Chinese (Simplified)", "乙组"),
            self.chapter(7, "Chinese (Simplified)", "甲组"),
            self.chapter(8, "", "甲组"),
        ]
        scan = await self.call("subscriptions/scan", {"comic_id": comic["comic_id"]})
        self.assertEqual(len(scan["job_ids"]), 1)
        job = self.repo.job(scan["job_ids"][0])
        self.assertEqual(job["comic_chapter"]["engine_chapter_id"], 7)
        self.assertEqual(job["comic_metadata"]["language"], "zh-Hans")
        self.assertIn("zh-Hans-甲组", job["comic_relative_path"])
        repeated = await self.call("subscriptions/scan", {"comic_id": comic["comic_id"]})
        self.assertEqual(repeated["job_ids"], [])
        self.assertEqual(
            repeated["subscription"]["version_policy"], saved["subscription"]["version_policy"]
        )

    async def test_legacy_subscription_keeps_its_original_selection_without_migration(self):
        comic, detail = await self.mixed_comic()
        old = {
            "comic_id": comic["comic_id"],
            "state": "active",
            "scanlator": "甲组",
            "interval_seconds": 21600,
            "seen": [c["version_key"] for c in detail["chapters"]],
            "next_scan_at": self.now,
            "code": "ready",
        }
        self.repo.put("comic_subscription", comic["comic_id"], old)
        self.rows.extend(
            [self.chapter(5, "English", "甲组"), self.chapter(6, "Chinese (Simplified)", "甲组")]
        )
        result = await self.call("subscriptions/scan", {"comic_id": comic["comic_id"]})
        self.assertEqual(len(result["job_ids"]), 2)
        self.assertNotIn("version_policy", result["subscription"])
        self.assertTrue(set(old["seen"]).issubset(result["subscription"]["seen"]))
        self.assertEqual(result["subscription"]["interval_seconds"], 21600)
        self.assertTrue(
            all(
                self.repo.job(key)["comic_metadata"]["language"] == "all"
                for key in result["job_ids"]
            )
        )

    async def test_selected_version_disappearing_does_not_switch_to_other_language(self):
        comic, detail = await self.mixed_comic()
        version = next(v for v in detail["versions"] if v["scanlator"] == "甲组")
        saved = await self.call(
            "subscriptions/save",
            {"comic_id": comic["comic_id"], "mode": "new", "version_id": version["id"]},
        )
        self.rows = [self.chapter(5, "English", "甲组")]
        with self.assertRaises(Fault):
            await self.call("subscriptions/scan", {"comic_id": comic["comic_id"]})
        current = self.repo.get("comic_subscription", comic["comic_id"])
        self.assertEqual(current["code"], "comic_chinese_version_missing")
        self.assertEqual(current["seen"], saved["subscription"]["seen"])
        self.assertEqual(current["version_policy"], saved["subscription"]["version_policy"])
        self.assertEqual(self.repo.job_count(), 0)

    async def test_version_filter_precedes_pagination_and_unknown_choice_cannot_enqueue(self):
        comic, _ = await self.mixed_comic()
        self.rows = [
            self.chapter(i, "English" if i <= 210 else "Chinese (Simplified)", "甲组")
            for i in range(1, 421)
        ]
        detail = await self.call(
            "detail",
            {"comic_id": comic["comic_id"], "refresh": True, "chapter_version": "preferred"},
        )
        self.assertEqual(detail["total"], 210)
        self.assertEqual(len(detail["chapters"]), 200)
        self.assertTrue(
            all(c["language_evidence"]["language"] == "zh-Hans" for c in detail["chapters"])
        )
        more = await self.call(
            "detail", {"comic_id": comic["comic_id"], "offset": 200, "chapter_version": "preferred"}
        )
        self.assertEqual(len(more["chapters"]), 10)
        with self.assertRaises(Fault):
            await self.call(
                "subscriptions/save", {"comic_id": comic["comic_id"], "version_id": "not-a-version"}
            )
        self.assertEqual(self.repo.job_count(), 0)

    async def test_old_cached_directory_projects_language_without_rewriting_original_rows(self):
        found = await self.search({"query": "星际"})
        comic_id = found["comics"][0]["comic_id"]
        await self.call("detail", {"comic_id": comic_id})
        directory = self.repo.get("comic_directory", comic_id)
        for chapter in directory["chapters"]:
            chapter.pop("language_evidence", None)
        stored = self.repo.put("comic_directory", comic_id, directory)
        result = await self.call("detail", {"comic_id": comic_id, "chapter_version": "preferred"})
        self.assertEqual(len(result["chapters"]), 2)
        self.assertEqual(result["chapters"][0]["language_evidence"]["language"], "zh")
        self.assertEqual(self.repo.get("comic_directory", comic_id), stored)

    async def test_empty_group_is_an_exact_selection_not_all_future_groups(self):
        comic, _ = await self.mixed_comic()
        self.rows = [self.chapter(1, "Chinese (Simplified)", "")]
        detail = await self.call("detail", {"comic_id": comic["comic_id"], "refresh": True})
        version = next(v for v in detail["versions"] if not v["scanlator"])
        await self.call(
            "subscriptions/save",
            {"comic_id": comic["comic_id"], "mode": "new", "version_id": version["id"]},
        )
        self.rows += [
            self.chapter(2, "Chinese (Simplified)", "新组"),
            self.chapter(3, "Chinese (Simplified)", ""),
        ]
        result = await self.call("subscriptions/scan", {"comic_id": comic["comic_id"]})
        self.assertEqual(len(result["job_ids"]), 1)
        self.assertEqual(
            self.repo.job(result["job_ids"][0])["comic_chapter"]["engine_chapter_id"], 3
        )


class LanguageEvidenceTests(unittest.TestCase):
    def test_title_script_is_not_content_language_and_explicit_foreign_marker_wins(self):
        self.assertEqual(evidence({"title": "学校生活"}, "all")["language"], "unknown")
        self.assertEqual(
            evidence({"title": "English Title", "description": "Language: Chinese"}, "all")[
                "language"
            ],
            "zh",
        )
        self.assertEqual(evidence({"title": "作品 [English]"}, "zh")["language"], "en")
        self.assertEqual(evidence({"title": "作品 [汉化]"}, "all")["basis"], "translation_tag")

    def test_language_group_filters_do_not_change_original_language_or_other_preferences(self):
        values = [
            {
                "__typename": "SelectFilter",
                "name": "Original language",
                "values": ["Japanese", "Chinese"],
            },
            {
                "__typename": "GroupFilter",
                "name": "Languages",
                "filters": [
                    {"__typename": "CheckBoxFilter", "name": "English"},
                    {"__typename": "CheckBoxFilter", "name": "Chinese"},
                ],
            },
        ]
        self.assertEqual(
            chinese_filter_plans(values),
            [
                [
                    {"position": 1, "groupChange": {"position": 0, "checkBoxState": False}},
                    {"position": 1, "groupChange": {"position": 1, "checkBoxState": True}},
                ]
            ],
        )

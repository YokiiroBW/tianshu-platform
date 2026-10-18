"""Actual loopback HTTP pagination, using the shipped isolated rule worker."""

import json
import time
import unittest

from aiohttp import web

from book_fixtures import BookSite
from services.platform.contracts import Fault
from services.platform.media.books.engine import RuleEngine


class PagedSite(BookSite):
    def __init__(self):
        super().__init__()
        self.multiple = False
        self.empty_middle = False
        self.login_middle = False
        self.request_times = []

    async def handle(self, request):
        if not request.path.startswith("/paging/"):
            return await super().handle(request)
        self.hits[request.path] += 1
        self.request_times.append(time.monotonic())
        self.received_headers[request.path] = dict(request.headers)
        if request.headers.get("X-Book-Source") != "fixture":
            raise web.HTTPUnauthorized()
        if request.path.startswith("/paging/toc/"):
            if not request.path.endswith("index.json"):
                raise web.HTTPNotFound()
            number = int(request.path.split("/")[3])
            following = [f"../{number + 1}/index.json"] if number < 3 else ["../1/index.json"]
            if self.multiple and number == 1:
                following = ["../2/index.json", "../3/index.json"]
            return web.json_response(
                {
                    "chapters": [{"name": f"目录{number}", "url": f"chapter-{number}.html"}],
                    "next": [
                        *following,
                        f"chapter-{number}.html",
                        "index.json#top",
                        "javascript:void(0)",
                        "#disabled",
                    ],
                }
            )
        if request.path.endswith("/info"):
            if request.headers.get("X-Mode") != "explicit":
                raise web.HTTPBadRequest()
            return web.Response(text="脚本补充正文")
        if request.path.startswith("/paging/chapter/1/"):
            number = int(request.path.rsplit("/", 1)[-1][4])
            following = [f"part{number + 1}.json"] if number < 3 else ["../2/part1.json"]
            if self.multiple and number == 1:
                following = ["part2.json", "part3.json", "../2/part1.json"]
            return web.json_response(
                {
                    "content": "请先登录后阅读本章内容。"
                    if self.login_middle and number == 2
                    else ""
                    if self.empty_middle and number == 2
                    else f"正文分页{number}",
                    "next": [*following, "part1.json#repeat", "javascript:void(0)"],
                }
            )
        raise web.HTTPNotFound()


class PaginationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.site = await PagedSite().start()
        self.addAsyncCleanup(self.site.runner.cleanup)
        self.engine = RuleEngine(allowed_private_origins=[self.site.url])
        self.addAsyncCleanup(self.engine.close)
        self.rules = self.site.source()
        self.rules["header"] = json.dumps({"X-Book-Source": "fixture", "X-Mode": "source"})
        self.rules["ruleToc"] = {
            "chapterList": "$.chapters[*]",
            "chapterName": "$.name",
            "chapterUrl": '@js:baseUrl.replace("index.json", JSON.parse(result).url)',
            "nextTocUrl": "$.next[*]",
        }
        self.rules["ruleContent"] = {
            "content": "@js:JSON.parse(result).content",
            "nextContentUrl": "$.next[*]",
        }

    async def directory(self):
        return await self.engine.call(
            self.rules, "chapters", url=self.site.url + "/paging/toc/1/index.json"
        )

    async def chapter(self):
        return await self.engine.call(
            self.rules,
            "content",
            url=self.site.url + "/paging/chapter/1/part1.json",
            next_url=self.site.url + "/paging/chapter/2/part1.json",
        )

    async def test_sequential_directory_keeps_relative_urls_and_headers(self):
        chapters = await self.directory()
        self.assertEqual([c["name"] for c in chapters], ["目录1", "目录2", "目录3"])
        self.assertEqual(
            [c["url"] for c in chapters],
            [self.site.url + f"/paging/toc/{n}/chapter-{n}.html" for n in (1, 2, 3)],
        )
        self.assertEqual(list(self.site.hits.values()), [1, 1, 1])

    async def test_interval_applies_to_every_directory_and_script_request(self):
        self.rules["concurrentRate"] = "150"
        await self.directory()
        self.rules["ruleContent"] = {
            "content": '@js:java.ajax(\'info,{"headers":{"X-Mode":"explicit"}}\')',
            "nextContentUrl": "@js:null",
        }
        await self.chapter()
        times = self.site.request_times
        self.assertEqual(len(times), 5)
        self.assertTrue(all(b - a >= 0.13 for a, b in zip(times, times[1:])), times)

    async def test_rate_window_survives_worker_boundary_and_failed_request(self):
        self.rules["concurrentRate"] = "2/1500"
        self.rules["ruleContent"] = {"content": "$.content"}
        await self.chapter()
        with self.assertRaises(Fault):
            await self.engine.call(self.rules, "content", url=self.site.url + "/paging/missing")
        await self.chapter()
        times = self.site.request_times
        self.assertEqual(len(times), 3)
        self.assertGreaterEqual(times[2] - times[0], 1.48)

    async def test_page_array_walks_in_order_without_cycles_or_repeated_requests(self):
        self.site.multiple = True
        chapters = await self.directory()
        self.assertEqual(len(chapters), 3)
        self.assertEqual(list(self.site.hits.values()), [1, 1, 1])
        result = await self.chapter()
        self.assertEqual(result["content"].split(), ["正文分页1", "正文分页2", "正文分页3"])
        self.assertEqual(self.site.hits["/paging/chapter/2/part1.json"], 0)
        for number in (1, 2, 3):
            self.assertEqual(self.site.hits[f"/paging/chapter/1/part{number}.json"], 1)

    async def test_sequential_content_uses_current_page_and_stops_before_next_chapter(self):
        result = await self.chapter()
        self.assertEqual(result["content"].split(), ["正文分页1", "正文分页2", "正文分页3"])
        self.assertEqual(self.site.hits["/paging/chapter/2/part1.json"], 0)

    async def test_empty_middle_page_fails_instead_of_publishing_partial_chapter(self):
        self.site.empty_middle = True
        with self.assertRaises(Fault) as caught:
            await self.chapter()
        self.assertEqual(caught.exception.code, "book_content_empty")
        self.assertEqual(self.site.hits["/paging/chapter/1/part3.json"], 0)

    async def test_script_ajax_inherits_headers_and_explicit_options_win(self):
        self.rules["ruleContent"] = {
            "content": '@js:java.ajax(\'info,{"headers":{"X-Mode":"explicit"}}\')',
            "nextContentUrl": "@js:null",
        }
        result = await self.chapter()
        self.assertEqual(result["content"].strip(), "脚本补充正文")
        self.assertEqual(
            list(self.site.hits), ["/paging/chapter/1/part1.json", "/paging/chapter/1/info"]
        )

    async def test_login_notice_on_second_page_rejects_the_whole_chapter(self):
        self.site.login_middle = True
        with self.assertRaises(Fault) as caught:
            await self.chapter()
        self.assertEqual(caught.exception.code, "book_source_auth_required")
        self.assertEqual(self.site.hits["/paging/chapter/1/part3.json"], 0)

    async def test_inherited_credentials_are_not_forwarded_to_another_origin(self):
        other = await PagedSite().start()
        self.addAsyncCleanup(other.runner.cleanup)
        self.engine.allowed_private_origins = (self.site.url, other.url)
        self.rules["header"] = json.dumps(
            {
                "X-Book-Source": "fixture",
                "Cookie": "fixture-only=1",
                "Authorization": "Bearer fixture-only",
            }
        )
        target = other.url + '/paging/chapter/1/info,{"headers":{"X-Mode":"explicit"}}'
        self.rules["ruleContent"] = {
            "content": "@js:java.ajax(" + json.dumps(target) + ")",
            "nextContentUrl": "@js:null",
        }
        result = await self.chapter()
        self.assertIn("脚本补充正文", result["content"])
        self.assertIn("Cookie", self.site.received_headers["/paging/chapter/1/part1.json"])
        received = other.received_headers["/paging/chapter/1/info"]
        self.assertNotIn("Cookie", received)
        self.assertNotIn("Authorization", received)


if __name__ == "__main__":
    unittest.main()

"""Synthetic, loopback-only book site shared by engine and actual HTTP browser tests."""

import asyncio
from collections import Counter

from aiohttp import web

from services.platform.media.books.engine import RuleEngine
from services.platform.media.books.network import Network


class BookSite:
    def __init__(self):
        self.count = 2
        self.failed = set()
        self.hits = Counter()
        self.received_headers = {}
        self.delay = 0
        self.content_delay = 0
        self.bad_feed = False
        self.url = ""

    def source(self):
        return {
            "bookSourceName": "合成测试书站",
            "bookSourceUrl": self.url,
            "bookSourceGroup": "测试文学",
            "bookSourceType": 0,
            "searchUrl": self.url + "/search?key={{key}}&page={{page}}",
            "ruleSearch": {
                "bookList": "$.books[*]",
                "name": "$.name",
                "author": "$.author",
                "bookUrl": "$.url",
                "intro": "$.intro",
            },
            "ruleBookInfo": {
                "name": "$.name",
                "author": "$.author",
                "intro": "$.intro",
                "tocUrl": "$.toc",
            },
            "ruleToc": {
                "chapterList": "$.chapters[*]",
                "chapterName": "$.name",
                "chapterUrl": "$.url",
            },
            "ruleContent": {"content": "$.content"},
        }

    async def handle(self, request):
        self.hits[request.path] += 1
        self.received_headers[request.path] = dict(request.headers)
        if request.path == "/search":
            if self.delay:
                await asyncio.sleep(self.delay)
            return web.json_response(
                {
                    "books": [
                        {
                            "name": "山海之间",
                            "author": "合成作者",
                            "url": "/book/1",
                            "intro": "一位旅人沿着山脉寻找旧日的灯火。这是完全合成的测试文本。",
                        }
                    ]
                }
            )
        if request.path == "/book/1":
            return web.json_response(
                {
                    "name": "山海之间",
                    "author": "合成作者",
                    "intro": "这是一部合成的山林旅行小说，用来验证书籍订阅。",
                    "toc": "/toc",
                }
            )
        if request.path == "/toc":
            return web.json_response(
                {
                    "chapters": [
                        {"name": f"第 {index} 章 山间来信", "url": f"/chapter/{index}"}
                        for index in range(1, self.count + 1)
                    ]
                }
            )
        if request.path.startswith("/chapter/"):
            if self.content_delay:
                await asyncio.sleep(self.content_delay)
            index = int(request.path.rsplit("/", 1)[-1])
            if index in self.failed:
                raise web.HTTPServiceUnavailable()
            return web.json_response(
                {
                    "content": f"清晨的雾气从山谷升起。第 {index} 章的旅人推开木窗，看见远处的灯火。\n\n他展开一封旧信，继续向海边走去。\n这是合成正文，供隔离测试使用。"
                }
            )
        if request.path == "/rules":
            return (
                web.Response(text="<html>temporarily unavailable</html>")
                if self.bad_feed
                else web.json_response([self.source()])
            )
        if request.path == "/html":
            return web.Response(
                body='<html><body><div class="entry"><a href="/book/1">山海之间</a><em>合成作者</em></div></body></html>'.encode(
                    "gb18030"
                ),
                content_type="text/html",
                charset="gb18030",
            )
        if request.path == "/opds":
            if request.headers.get("Authorization") != "Basic c3ludGhldGljOnNlY3JldA==":
                raise web.HTTPUnauthorized()
            return web.Response(
                text='<feed xmlns="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/terms/"><entry><id>synthetic-book</id><title>山海之间</title><author><name>合成作者</name></author><summary>来自已连接书库的简介。</summary><dc:language>zh-CN</dc:language><dc:publisher>合成出版社</dc:publisher><dc:identifier>urn:isbn:9781234567897</dc:identifier></entry></feed>',
                content_type="application/atom+xml",
            )
        raise web.HTTPNotFound()

    async def start(self):
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self.handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    def connect(self, books):
        books.engine = RuleEngine(allowed_private_origins=[self.url])
        books.network = Network(allowed_private_origins=[self.url])


class LiveConsole:
    def session_valid(self, session):
        return session.get("live", True)

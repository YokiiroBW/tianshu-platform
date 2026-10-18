"""Synthetic, loopback-only book site shared by engine and actual HTTP browser tests."""

import asyncio
from collections import Counter
from html import escape
import re

from aiohttp import web

from services.platform.media.books.engine import RuleEngine
from services.platform.media.books.network import Network


class BookSite:
    def __init__(self):
        self.count = 2
        self.failed = set()
        self.rate_limited = set()
        self.notices = {}
        self.login_chapters = set()
        self.hits = Counter()
        self.received_headers = {}
        self.delay = 0
        self.content_delay = 0
        self.bad_feed = False
        self.url = ""
        self.warehouse_version = 1
        self.warehouse_failure = False
        self.warehouse_page_failure = False
        self.switch_counts = {"a": 5, "b": 5}
        self.switch_primary_failed = False

    def switching_source(self, side):
        prefix = self.url + "/switch/" + side
        return {
            **self.source(),
            "bookSourceName": "自动优选·主源" if side == "a" else "自动优选·快源",
            "bookSourceUrl": prefix,
            "bookSourceGroup": "换源测试",
            "searchUrl": prefix + "/search?key={{key}}",
        }

    def switching(self, request):
        parts = request.path.split("/")
        side = parts[2]
        prefix = "/switch/" + side
        tail = "/".join(parts[3:])
        if tail == "search":
            return web.json_response(
                {"books": [{"name": "换源验证", "author": "合成作者", "url": prefix + "/book/1"}]}
            )
        if tail == "book/1":
            if side == "a" and self.switch_primary_failed:
                raise web.HTTPServiceUnavailable()
            return web.json_response(
                {"name": "换源验证", "author": "合成作者", "toc": prefix + "/toc"}
            )
        if tail == "toc":
            return web.json_response(
                {
                    "chapters": [
                        {
                            "name": f"第{number}章 切源{number}号信",
                            "url": prefix + f"/chapter/{number}",
                        }
                        for number in range(1, self.switch_counts[side] + 1)
                    ]
                }
            )
        if tail.startswith("chapter/"):
            number = int(parts[-1])
            if side == "a" and self.switch_primary_failed:
                raise web.HTTPServiceUnavailable()
            return web.json_response(
                {
                    "content": "\n\n".join(
                        f"第{number}章的旅人写下第{line}封信，沿山路寻找灯塔。这是自动优选书源的合成正文。"
                        for line in range(12)
                    )
                }
            )
        raise web.HTTPNotFound()

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
        if request.path == "/__fixture/switching":
            if request.method == "POST":
                body = await request.json()
                self.switch_counts["b"] = body.get("new_count", self.switch_counts["b"])
                self.switch_primary_failed = body.get("primary_failed", self.switch_primary_failed)
            return web.json_response(
                {
                    "sources": [self.switching_source(side) for side in ("a", "b")],
                    "hits": {
                        path: count
                        for path, count in self.hits.items()
                        if path.startswith("/switch/")
                    },
                }
            )
        if request.path.startswith("/switch/"):
            return self.switching(request)
        if request.path == "/login":
            return web.Response(text='<form>请登录<input type="password"></form>')
        if request.path == "/__fixture/notices" and request.method == "POST":
            body = await request.json()
            self.notices = {int(key): value for key, value in body.items()}
            return web.json_response({"ok": True})
        if request.path == "/古籍/中文.html":
            return web.json_response({"path": request.path, "query": request.query.get("书名")})
        if request.path == "/__fixture/warehouse" and request.method == "POST":
            body = await request.json()
            self.warehouse_version = body.get("version", self.warehouse_version)
            return web.json_response({"version": self.warehouse_version})
        if request.path.startswith("/yuedu/"):
            return self.warehouse(request)
        if request.path == "/large-rules.json":
            import json

            return web.Response(text=" " * (9 * 1024 * 1024) + json.dumps([self.source()]))
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
            if index in self.login_chapters:
                raise web.HTTPFound("/login")
            if index in self.rate_limited:
                raise web.HTTPTooManyRequests()
            if index in self.failed:
                raise web.HTTPServiceUnavailable()
            return web.json_response(
                {
                    "content": self.notices.get(
                        index,
                        f"清晨的雾气从山谷升起。第 {index} 章的旅人推开木窗，看见远处的灯火。\n\n他展开一封旧信，继续向海边走去。\n这是合成正文，供隔离测试使用。",
                    )
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
        books.warehouse.base_url = self.url

    def warehouse(self, request):
        """The same HTML selectors and stable endpoints as the public warehouse."""
        part = request.path.split("/")[2]
        if part not in {"shuyuan", "shuyuans"}:
            raise web.HTTPNotFound()
        names = {"101": "山海书源", "102": "林间书源", "103": "远山书源", "201": "合成精选合集"}
        path = f"/yuedu/{part}"
        match = re.search(r"/id/(\d+)\.(json|html)$", request.path)
        if match:
            identity = match[1]
            if identity not in names:
                raise web.HTTPNotFound()
            if match[2] == "json":
                if self.warehouse_failure:
                    raise web.HTTPServiceUnavailable()
                ids = ["101", "102"] if part == "shuyuans" else [identity]
                rules = [
                    {
                        **self.source(),
                        "bookSourceUrl": self.url + "/source/" + item,
                        "bookSourceName": names[item] + f" · v{self.warehouse_version}",
                    }
                    for item in ids
                ]
                if part == "shuyuans":
                    rules.append({**self.source(), "bookSourceType": 1})
                return web.json_response(rules)
            if self.warehouse_page_failure:
                return web.Response(text="<html>maintenance</html>")
            return web.Response(
                content_type="text/html",
                text=f'''
                <h1>{names[identity]}<p>https://fixture.invalid</p></h1>
                <blockquote><p>合成作者 · 1天前</p></blockquote>
                <div class="contents"><p>用于验证源仓库浏览的合成说明。</p><script>window.warehouseUnsafe = true</script></div>
                <input id="jsonurl" value="{self.url}{path}/json/id/{identity}.json">
                <pre>private rule content must not be projected</pre>''',
            )
        if self.warehouse_page_failure:
            return web.Response(text="<html>maintenance</html>")
        ids = ["201"] if part == "shuyuans" else ["101", "102", "103"]
        query = request.query.get("keys", "")
        ids = [identity for identity in ids if query in names[identity]]
        page = int(request.query.get("page", "1"))
        selected = ids[(page - 1) * 2 : page * 2]
        rows = "".join(
            f'''<div class="ylist"><h2><a href="{path}/content/id/{identity}.html">{names[identity]}</a><p>1天前</p></h2><span>用户: 合成作者</span><span>3.X</span><span>下载:42</span></div>'''
            for identity in selected
        )
        more = (
            f'<a href="{path}/index.html?page={page + 1}">下一页</a>' if len(ids) > page * 2 else ""
        )
        return web.Response(
            content_type="text/html",
            text=f'<nav id="navsub">阅读</nav>{rows}<div class="layui-laypage">{more}</div><script>{escape(query)}</script>',
        )


class LiveConsole:
    def session_valid(self, session):
        return session.get("live", True)

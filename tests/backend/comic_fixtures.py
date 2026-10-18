"""Synthetic HTTP source/archive/reader peers; never contacts real comic sites."""

import hashlib
import io
import uuid
import zipfile
from collections import Counter
from pathlib import Path

from aiohttp import web
from PIL import Image


def page_bytes(number, format="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (80 + number, 100), (number * 30 % 255, 90, 160)).save(output, format)
    return output.getvalue()


class ComicSite:
    def __init__(self, root):
        self.root = Path(root)
        self.count, self.hits, self.fail_pages = 2, Counter(), set()
        self.records, self.intent, self.books = {}, {}, {}
        self.lose_observe = False
        self.reader_pending = False
        self.no_search = False
        self.url = ""

    def manga(self, source="9007199254740993", number=1, title="星际邮差"):
        return {
            "id": number,
            "title": title,
            "author": "合成作者",
            "artist": "合成画师",
            "description": "仅用于漫画订阅联验的合成作品。",
            "genre": ["冒险"],
            "status": "ONGOING",
            "thumbnailUrl": "/image/cover",
            "sourceId": source,
            "url": "/comic/" + str(number),
            "inLibrary": False,
        }

    async def graphql(self, request):
        self.hits["graphql"] += 1
        body = await request.json()
        query, variables = body["query"], body.get("variables", {})
        if "aboutServer" in query:
            data = {
                "aboutServer": {"name": "Synthetic Suwayomi", "version": "fixture"},
                "settings": {
                    "globalUpdateInterval": 12,
                    "autoDownloadNewChapters": False,
                    "downloadAsCbz": False,
                },
            }
        elif "sources(first" in query:
            data = {
                "sources": {
                    "totalCount": 2,
                    "nodes": [
                        {
                            "id": "9007199254740993",
                            "name": "合成漫画源",
                            "lang": "zh",
                            "contentWarning": "SAFE",
                        },
                        {
                            "id": "9007199254740994",
                            "name": "合成私有分组",
                            "lang": "zh",
                            "contentWarning": "MIXED",
                        },
                    ],
                }
            }
        elif "fetchSourceManga" in query:
            if self.no_search and variables["input"]["type"] == "SEARCH":
                return web.json_response(
                    {
                        "errors": [
                            {
                                "message": "Exception while fetching data (/fetchSourceManga) : No search"
                            }
                        ]
                    }
                )
            source = variables["input"]["source"]
            data = {
                "fetchSourceManga": {
                    "hasNextPage": False,
                    "mangas": [self.manga(source), self.manga(source, 2, "完全无关的热榜")],
                }
            }
        elif "fetchMangaAndChapters" in query:
            chapters = [
                {
                    "id": i,
                    "mangaId": 1,
                    "name": f"第{i}章 星间来信",
                    "chapterNumber": i,
                    "pageCount": 3,
                    "sourceOrder": i,
                    "scanlator": "测试版本",
                    "url": f"/chapter/{i}",
                    "uploadDate": i * 1000,
                }
                for i in range(1, self.count + 1)
            ]
            data = {"fetchMangaAndChapters": {"manga": self.manga(), "chapters": chapters}}
        elif "fetchChapterPages" in query:
            identity = variables["id"]
            data = {
                "fetchChapterPages": {
                    "chapter": {"id": identity, "mangaId": 1, "pageCount": 3},
                    "pages": [f"/image/{identity}/{page}" for page in range(1, 4)],
                }
            }
        else:
            raise web.HTTPBadRequest()
        return web.json_response({"data": data})

    async def image(self, request):
        self.hits[request.path] += 1
        if request.path in self.fail_pages:
            raise web.HTTPServiceUnavailable()
        value = request.path.rsplit("/", 1)[-1]
        return web.Response(
            body=page_bytes(int(value) if value.isdigit() else 1), content_type="image/png"
        )

    async def archive(self, request):
        self.hits[request.method + " " + request.path] += 1
        if request.headers.get("Authorization") != "Bearer isolated-archive":
            raise web.HTTPUnauthorized()
        if request.path.endswith("/targets"):
            return web.json_response(
                {
                    "protocol": "service-imports-v1",
                    "targets": [
                        {
                            "target_id": "comic-main",
                            "library_id": "asset-main",
                            "root_relative_path": "Cartoon/漫画/天枢",
                            "observation_ready": True,
                            "operation": "observe",
                        }
                    ],
                }
            )
        parts = request.path.split("/")
        key = parts[4]
        if request.method == "GET":
            if key not in self.records:
                raise web.HTTPNotFound()
            return web.json_response(self.records[key])
        if request.method != "POST" or parts[-1] != "observe":
            raise web.HTTPNotFound()
        body = await request.json()
        if key in self.intent and self.intent[key] != body:
            raise web.HTTPConflict()
        self.intent[key] = body
        file = self.root / body["relative_path"]
        content = file.read_bytes()
        if (
            str(len(content)) != body["length"]
            or hashlib.sha256(content).hexdigest() != body["sha256"]
        ):
            raise web.HTTPConflict()
        receipt = {
            "receipt_id": key,
            "library_id": "asset-main",
            "entry_id": str(uuid.uuid5(uuid.NAMESPACE_URL, body["relative_path"])),
            "observation_version": "1",
            "content_version": body["sha256"],
            "length": body["length"],
            "sha256": body["sha256"],
            "relative_path": body["relative_path"],
            "observed_at": "2026-10-18T00:00:00Z",
        }
        self.records[key] = {
            "protocol": "service-imports-v1",
            "key": key,
            "state": "committed",
            "intent": body,
            "receipt": receipt,
        }
        if self.lose_observe:
            self.lose_observe = False
            raise web.HTTPServiceUnavailable()
        return web.json_response(self.records[key])

    async def komga(self, request):
        self.hits[request.method + " " + request.path] += 1
        if request.headers.get("X-API-Key") != "isolated-komga":
            raise web.HTTPUnauthorized()
        if request.path == "/api/v1/libraries":
            return web.json_response(
                [{"id": "komga-main", "name": "漫画", "root": "file:/Cartoon/漫画/"}]
            )
        if request.path.endswith("/scan"):
            if not self.reader_pending:
                for file in self.root.rglob("*.cbz"):
                    relative = file.relative_to(self.root).as_posix()
                    identity = hashlib.sha256(relative.encode()).hexdigest()[:12]
                    folder = relative.rsplit("/", 1)[0]
                    series = hashlib.sha256(folder.encode()).hexdigest()[:12]
                    with zipfile.ZipFile(file) as package:
                        pages = sum(name.endswith(".png") for name in package.namelist())
                    self.books[identity] = {
                        "id": identity,
                        "seriesId": series,
                        "libraryId": "komga-main",
                        "url": "file:/Cartoon/漫画/天枢/" + relative,
                        "sizeBytes": file.stat().st_size,
                        "media": {"status": "READY", "pagesCount": pages},
                    }
            return web.json_response({})
        if request.path == "/api/v1/series":
            rows = list(
                {
                    b["seriesId"]: {
                        "id": b["seriesId"],
                        "libraryId": b["libraryId"],
                        "url": b["url"].rsplit("/", 1)[0],
                    }
                    for b in self.books.values()
                }.values()
            )
        elif request.path.endswith("/books"):
            series = request.path.split("/")[-2]
            rows = [b for b in self.books.values() if b["seriesId"] == series]
        else:
            raise web.HTTPNotFound()
        return web.json_response({"content": rows, "last": True})

    async def start(self):
        app = web.Application()
        app.router.add_post("/api/graphql", self.graphql)
        app.router.add_get("/image/{tail:.*}", self.image)
        app.router.add_get("/api/v1/manga/{identity}/thumbnail", self.image)
        app.router.add_route("*", "/assetlink/v1/service-imports/{tail:.*}", self.archive)
        app.router.add_route("*", "/api/v1/{tail:.*}", self.komga)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        return self

    def settings(self, inputs):
        return {
            "import_directory": str(inputs),
            "connections": {
                "suwayomi": {"url": self.url},
                "assetlibrary": {"url": self.url, "token": "isolated-archive"},
                "komga": {"url": self.url, "token": "isolated-komga"},
            },
            "targets": {
                "general": {
                    "directory": str(self.root),
                    "asset_target_id": "comic-main",
                    "asset_library_id": "asset-main",
                    "komga_library_id": "komga-main",
                    "komga_root": "/Cartoon/漫画/天枢",
                }
            },
        }

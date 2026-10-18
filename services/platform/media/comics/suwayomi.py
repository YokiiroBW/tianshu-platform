"""Suwayomi GraphQL control and same-origin image streams; no direct site scraping."""

import asyncio
import base64
import os
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import aiohttp

from ...contracts import Fault, require
from .packages import MAX_PAGE_BYTES

MANGA_FIELDS = (
    "id title author artist description genre status thumbnailUrl sourceId url realUrl inLibrary"
)
CHAPTER_FIELDS = "id name chapterNumber pageCount mangaId isDownloaded sourceOrder scanlator url realUrl uploadDate"


def service_url(value):
    require(isinstance(value, str) and 0 < len(value) <= 2048, "comic_connection_invalid", 400)
    try:
        parsed = urlsplit(value)
        require(
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment,
            "comic_connection_invalid",
            400,
        )
        _ = parsed.port
    except ValueError:
        raise Fault("comic_connection_invalid", 400) from None
    return value.rstrip("/")


class Suwayomi:
    def __init__(self, connection):
        self.connection = connection
        self.session = None
        self.parallel = asyncio.Semaphore(4)
        self.source_locks = {}

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def client(self):
        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60, sock_connect=10, sock_read=35),
                cookie_jar=aiohttp.DummyCookieJar(),
                trust_env=False,
            )
        return self.session

    async def settings(self):
        config = await self.connection()
        require(config.get("url"), "comic_engine_not_configured", 409)
        base = service_url(config["url"])
        headers = {}
        if config.get("username"):
            token = base64.b64encode(
                (config["username"] + ":" + config.get("password", "")).encode()
            ).decode()
            headers["Authorization"] = "Basic " + token
        elif config.get("token"):
            headers["Authorization"] = "Bearer " + config["token"]
        return base, headers

    @staticmethod
    def status(status):
        require(status not in {401, 403}, "comic_engine_auth_required", 409)
        require(status != 429, "comic_source_rate_limited", 503)
        require(200 <= status < 300, "comic_engine_unavailable", 503)

    async def query(self, query, variables=None):
        base, headers = await self.settings()
        try:
            async with (await self.client()).post(
                base + "/api/graphql",
                json={"query": query, "variables": variables or {}},
                headers=headers,
                allow_redirects=False,
            ) as response:
                self.status(response.status)
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    data.extend(chunk)
                    require(len(data) <= 8 * 1024**2, "comic_response_too_large", 503)
                import json

                try:
                    result = json.loads(data)
                except (ValueError, UnicodeDecodeError):
                    raise Fault("comic_engine_invalid_response", 503) from None
                if isinstance(result, dict) and result.get("errors"):
                    messages = [
                        str(e.get("message", ""))[:1000].casefold()
                        for e in result["errors"]
                        if isinstance(e, dict)
                    ]
                    if any(": no search" in message for message in messages):
                        raise Fault("comic_source_search_unsupported", 409)
                    if any(": connection refused" in message for message in messages):
                        raise Fault("comic_source_connection_refused", 503)
                require(
                    isinstance(result, dict)
                    and not result.get("errors")
                    and isinstance(result.get("data"), dict),
                    "comic_source_unavailable",
                    503,
                )
                return result["data"]
        except (aiohttp.ClientError, TimeoutError):
            raise Fault("comic_engine_unavailable", 503) from None

    async def probe(self):
        value = await self.query(
            "query { aboutServer { name version } settings { globalUpdateInterval autoDownloadNewChapters downloadAsCbz } }"
        )
        return {
            "state": "ready",
            **value["aboutServer"],
            "engine_schedule": value["settings"],
            "mode": "page_streams_without_library_membership",
        }

    async def sources(self):
        value = await self.query(
            "query { sources(first: 500) { nodes { id name displayName lang contentWarning supportsLatest } totalCount } }"
        )
        result = value["sources"]
        require(isinstance(result.get("nodes"), list), "comic_engine_invalid_response", 503)
        return [
            {**s, "id": str(s["id"])} for s in result["nodes"] if str(s["id"]) != "0"
        ], result.get("totalCount", len(result["nodes"]))

    async def search(self, source_id, query, page=1):
        # Source execution is serial, while independent sources can return progressively.
        lock = self.source_locks.setdefault(source_id, asyncio.Lock())
        async with self.parallel, lock:
            mutation = (
                "mutation($input: FetchSourceMangaInput!) { fetchSourceManga(input: $input) { hasNextPage mangas { "
                + MANGA_FIELDS
                + " } } }"
            )
            inputs = {"source": source_id, "type": "SEARCH", "query": query, "page": page}
            mode = "search"
            try:
                value = await self.query(mutation, {"input": inputs})
            except Fault as error:
                if error.code != "comic_source_search_unsupported":
                    raise
                # Some official catalogues intentionally have no search operation.
                # Fetch only this catalogue page; Sources still checks query relevance.
                value = await self.query(
                    mutation, {"input": {"source": source_id, "type": "POPULAR", "page": page}}
                )
                mode = "catalogue"
            result = value.get("fetchSourceManga")
            require(
                isinstance(result, dict) and isinstance(result.get("mangas"), list),
                "comic_engine_invalid_response",
                503,
            )
            return {**result, "search_mode": mode}

    async def detail(self, manga_id, source_id):
        lock = self.source_locks.setdefault(source_id, asyncio.Lock())
        async with self.parallel, lock:
            value = await self.query(
                "mutation($input: FetchMangaAndChaptersInput!) { fetchMangaAndChapters(input: $input) { manga { "
                + MANGA_FIELDS
                + " } chapters { "
                + CHAPTER_FIELDS
                + " } } }",
                {"input": {"id": int(manga_id), "fetchManga": True, "fetchChapters": True}},
            )
            result = value.get("fetchMangaAndChapters")
            require(
                isinstance(result, dict)
                and isinstance(result.get("manga"), dict)
                and isinstance(result.get("chapters"), list),
                "comic_engine_invalid_response",
                503,
            )
            require(str(result["manga"]["sourceId"]) == source_id, "comic_source_changed", 409)
            return result

    async def pages(self, chapter_id, manga_id):
        value = await self.query(
            "mutation($id: Int!) { fetchChapterPages(input: {chapterId: $id}) { chapter { id mangaId pageCount } pages } }",
            {"id": int(chapter_id)},
        )
        result = value.get("fetchChapterPages")
        require(
            isinstance(result, dict)
            and isinstance(result.get("pages"), list)
            and result.get("chapter", {}).get("mangaId") == int(manga_id),
            "comic_engine_invalid_response",
            503,
        )
        pages = result["pages"]
        require(
            0 < len(pages) <= 10000 and all(isinstance(p, str) and len(p) <= 8192 for p in pages),
            "comic_directory_empty",
            409,
        )
        require(
            not result["chapter"].get("pageCount") or result["chapter"]["pageCount"] == len(pages),
            "comic_page_count_mismatch",
            409,
        )
        return pages

    async def download_page(self, page_url, target):
        base, headers = await self.settings()
        url = urljoin(base + "/", page_url)
        origin, peer = urlsplit(base), urlsplit(url)
        require(
            (origin.scheme, origin.hostname, origin.port) == (peer.scheme, peer.hostname, peer.port)
            and not peer.username
            and not peer.fragment,
            "comic_image_origin_changed",
            409,
        )
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".partial")
        try:
            async with (await self.client()).get(
                url, headers=headers, allow_redirects=False
            ) as response:
                self.status(response.status)
                length = 0
                with temporary.open("xb") as output:
                    async for chunk in response.content.iter_chunked(128 * 1024):
                        length += len(chunk)
                        require(length <= MAX_PAGE_BYTES, "comic_page_too_large", 409)
                        output.write(chunk)
                    require(length > 0, "comic_page_invalid", 409)
                    output.flush()
                    os.fsync(output.fileno())
                temporary.replace(target)
        except (aiohttp.ClientError, TimeoutError):
            raise Fault("comic_source_unavailable", 503) from None
        finally:
            temporary.unlink(missing_ok=True)

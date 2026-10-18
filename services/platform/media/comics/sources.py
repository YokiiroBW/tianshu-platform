"""Progressive aggregated searches and source health; sessions own transient results."""

import asyncio
import re
import unicodedata
import uuid

from ...contracts import Fault, digest, require
from .catalog import public_comic


def matches(query, item):
    def normalize(text):
        return unicodedata.normalize("NFKC", text).casefold()

    terms = normalize(query).split()
    text = normalize(" ".join(str(item.get(key) or "") for key in ("title", "author", "artist")))
    compact = re.sub(r"\s+", "", text)
    return all(term in text or re.sub(r"\s+", "", term) in compact for term in terms)


class Sources:
    def __init__(self, comics):
        self.comics, self.media, self.repo = comics, comics.media, comics.repo
        self.searches = {}

    async def close(self):
        tasks = [s["task"] for s in self.searches.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.searches.clear()

    async def engine_id(self):
        row = await self.comics.settings.remote_connection("suwayomi")
        require(row.get("url"), "comic_engine_not_configured", 409)
        return digest(row["url"])[:24]

    async def refresh(self):
        rows, total = await self.comics.engine.sources()
        sources = await self.media.local(
            self.comics.catalog.save_sources, rows, await self.engine_id()
        )
        return {"sources": sources, "total": total, "truncated": total > len(rows) + 1}

    async def list(self):
        engine_id = await self.engine_id()
        rows = await self.media.local(self.repo.list, "comic_source")
        return [row for row in rows if row["engine_id"] == engine_id]

    async def search(self, body, session):
        query = body.get("query", "")
        require(isinstance(query, str) and 0 < len(query.strip()) <= 200, "invalid_input", 400)
        ids = body.get("source_ids", [])
        require(
            isinstance(ids, list) and len(ids) <= 100 and all(isinstance(v, str) for v in ids),
            "invalid_input",
            400,
        )
        language = body.get("language", "")
        require(isinstance(language, str) and len(language) <= 20, "invalid_input", 400)
        kind = body.get("library_kind", "general")
        require(kind in {"general", "private", "all"}, "invalid_input", 400)
        page = body.get("page", 1)
        require(type(page) is int and 1 <= page <= 100, "invalid_input", 400)
        rows = await self.list()
        if not rows:
            rows = (await self.refresh())["sources"]
        selected = [
            r
            for r in rows
            if r["enabled"]
            and (not ids or r["source_id"] in ids)
            and (not language or r["language"] == language)
            and (kind == "all" or (r["content_warning"] == "SAFE") == (kind == "general"))
        ][:100]
        now = self.repo.clock()
        for key, search in list(self.searches.items()):
            if (
                search["created_at"] < now - 600
                or len(self.searches) >= 32
                and search["task"].done()
            ):
                search["task"].cancel()
                del self.searches[key]
        require(len(self.searches) < 32, "comic_search_capacity", 429)
        search_id = uuid.uuid4().hex
        result = {
            "search_id": search_id,
            "created_at": now,
            "session": session,
            "state": "running",
            "comics": [],
            "sources": [],
            "total": len(selected),
            "query": query.strip(),
            "page": page,
        }
        self.searches[search_id] = result

        async def collect(source):
            try:
                found = await self.comics.engine.search(source["source_id"], query.strip(), page)
                count, filtered = 0, 0
                for item in found["mangas"][:200]:
                    if not isinstance(item, dict):
                        continue
                    if not matches(query.strip(), item):
                        filtered += 1
                        continue
                    comic = await self.media.local(self.comics.catalog.register, source, item)
                    result["comics"].append(public_comic(comic))
                    count += 1
                status = {
                    "source_id": source["source_id"],
                    "name": source["name"],
                    "code": "ready",
                    "count": count,
                    "filtered": filtered,
                    "search_mode": found.get("search_mode", "search"),
                    "has_next_page": bool(found.get("hasNextPage")),
                }
            except Fault as error:
                status = {
                    "source_id": source["source_id"],
                    "name": source["name"],
                    "code": error.code,
                    "count": 0,
                }
            except (KeyError, ValueError, TypeError):
                status = {
                    "source_id": source["source_id"],
                    "name": source["name"],
                    "code": "comic_engine_invalid_response",
                    "count": 0,
                }
            result["sources"].append(status)

            def record():
                with self.repo.transaction() as db:
                    current = self.repo._document(db, "comic_source", source["source_key"])
                    if current:
                        self.repo.put(
                            "comic_source",
                            source["source_key"],
                            {**current, "code": status["code"], "checked_at": self.repo.clock()},
                            db=db,
                        )

            await self.media.local(record)

        async def run():
            try:
                await asyncio.gather(*(collect(source) for source in selected))
                result["state"] = "completed"
            except asyncio.CancelledError:
                result["state"] = "cancelled"
                raise
            except Exception:
                result["state"] = "failed"
                result["code"] = "comic_search_failed"

        result["task"] = asyncio.create_task(run())
        return self.results(search_id, session)

    def results(self, identity, session):
        search = self.searches.get(identity)
        require(search and search["session"] is session, "not_found", 404)
        return {
            **{
                k: v
                for k, v in search.items()
                if k not in {"session", "task", "created_at", "comics", "sources"}
            },
            "comics": list({c["comic_id"]: c for c in search["comics"]}.values()),
            "sources": list(search["sources"]),
        }

    def cancel(self, identity, session):
        result = self.results(identity, session)
        self.searches[identity]["task"].cancel()
        return {**result, "state": "cancelled"}

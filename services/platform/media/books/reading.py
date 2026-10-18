"""Reading progress and explicit, short-lived co-reading grants owned by the book library."""

import math
import uuid

from ...contracts import Fault, canonical, require

PATH = "/internal/v1/books/reading"


class Reading:
    def __init__(self, books):
        self.books, self.repo = books, books.repo
        self.live_sessions = {}

    def text(self, body):
        book = self.books.catalog.book(body.get("book_id"))
        chapter = self.books.catalog.chapter(book["book_id"], body.get("chapter_id"))
        require(chapter, "book_content_unavailable", 409)
        if body.get("audio_job"):
            job = self.repo.get("book_audio_job", body["audio_job"])
            require(
                job
                and job["state"] == "ready"
                and job["book_id"] == book["book_id"]
                and job["chapter_id"] == chapter["chapter_id"],
                "not_found",
                404,
            )
            chapter = {**chapter, "content": job["text"], "content_hash": job["content_hash"]}
        require(
            chapter["content_hash"] == body.get("content_hash"), "book_reading_text_changed", 409
        )
        return book, chapter

    def open(self, owner, body, db):
        current = self.repo._document(db, "book_reader", owner)
        # Compare-and-swap prevents a delayed opening tab from replacing a newer reader.
        require(
            (current or {}).get("revision", 0) == body.get("expected_revision"),
            "version_conflict",
            409,
        )
        row = {
            "owner": owner,
            "reader_id": uuid.uuid4().hex,
            "book_id": body["book_id"],
            "chapter_id": body["chapter_id"],
            "content_hash": body["content_hash"],
            "audio_job": body.get("audio_job"),
            "offset": 0,
            "seconds": 0,
            "sequence": 0,
            "sharing": False,
            "closed": False,
            "updated_at": self.repo.clock(),
        }
        prior = self.repo._document(db, "book_reading_progress", owner + ":" + body["book_id"])
        if (
            prior
            and prior["chapter_id"] == row["chapter_id"]
            and prior["content_hash"] == row["content_hash"]
        ):
            row.update(offset=prior["offset"], seconds=prior["seconds"])
        return self.repo.put("book_reader", owner, row, db=db)

    def update(self, owner, body, chapter, scope, db):
        row = self.repo._document(db, "book_reader", owner)
        require(
            row and row["reader_id"] == body.get("reader_id") and not row["closed"],
            "book_reading_session_changed",
            409,
        )
        require(
            type(body.get("sequence")) is int and body["sequence"] > row["sequence"],
            "book_reading_stale_progress",
            409,
        )
        offset, seconds = body.get("offset", row["offset"]), body.get("seconds", row["seconds"])
        require(
            type(offset) is int and 0 <= offset <= len(chapter["content"]), "invalid_input", 400
        )
        require(
            type(seconds) in {int, float} and math.isfinite(seconds) and 0 <= seconds <= 86400,
            "invalid_input",
            400,
        )
        require(type(body.get("sharing", False)) is bool, "invalid_input", 400)
        sharing = body.get("sharing", False)
        require(
            not sharing or scope and scope["person_id"] and scope["audience"] == "self_private",
            "book_reading_private_required",
            409,
        )
        changed_grant = sharing != row["sharing"] or scope != row.get("scope")
        result = self.repo.put(
            "book_reader",
            owner,
            {
                **row,
                "offset": offset,
                "seconds": seconds,
                "scope": scope,
                "binding": {k: body.get(k) for k in ("conversation", "actor")},
                "sharing": sharing,
                "sequence": body["sequence"],
                "grant_id": uuid.uuid4().hex if changed_grant else row.get("grant_id"),
                "updated_at": self.repo.clock(),
            },
            db=db,
        )
        self.repo.put(
            "book_reading_progress",
            owner + ":" + row["book_id"],
            {
                k: result[k]
                for k in (
                    "book_id",
                    "chapter_id",
                    "content_hash",
                    "offset",
                    "seconds",
                    "updated_at",
                )
            },
            db=db,
        )
        return result

    def close(self, owner, body, db):
        row = self.repo._document(db, "book_reader", owner)
        if row and row["reader_id"] == body.get("reader_id") and not row["closed"]:
            self.repo.put(
                "book_reader",
                owner,
                {**row, "sharing": False, "closed": True, "updated_at": self.repo.clock()},
                db=db,
            )
        return {"closed": True}

    async def web(self, web, operation, body, session):
        console, local = web.web.console, self.books.media.local
        owner = console.config["principal"]
        if operation == "view":
            _, choices = await local(console.authority)
            choices = [
                {
                    **choice,
                    "actor_labels": {
                        actor: (console.platform.role_runtime.get(actor) or {}).get("name", actor)
                        for actor in choice["actors"]
                    },
                }
                for choice in choices
            ]
            return {
                "reader": await local(self.repo.get, "book_reader", owner),
                "choices": choices,
                "progress": await local(
                    self.repo.get, "book_reading_progress", owner + ":" + body.get("book_id", "")
                ),
            }
        if operation == "close":
            result = await web.web.receipt(
                body, "books/reading/close", session, lambda db: self.close(owner, body, db)
            )
            self.live_sessions.pop(body.get("reader_id"), None)
            return result
        if operation == "open":
            await local(self.text, body)
            result = await web.web.receipt(
                body,
                "books/reading/open",
                session,
                lambda db: {"reader": self.open(owner, body, db)},
            )
            self.live_sessions[result["reader"]["reader_id"]] = (session, console)
            # The durable current reader selects the only active grant. Retain a small
            # live-session window so a delayed replay cannot erase a newer tab's mapping.
            while len(self.live_sessions) > 16:
                self.live_sessions.pop(next(iter(self.live_sessions)))
            return result
        if operation == "progress":
            row = await local(self.repo.get, "book_reader", owner)
            require(
                row and row["reader_id"] == body.get("reader_id"),
                "book_reading_session_changed",
                409,
            )
            _, chapter = await local(self.text, row)
            scope = None
            if body.get("sharing"):
                _, _, _, scope = await local(console.dialogue.selection, body)
            return await web.web.receipt(
                body,
                "books/reading/progress",
                session,
                lambda db: {"reader": self.update(owner, body, chapter, scope, db)},
            )
        raise Fault("not_found", 404)


class ReadingAccess:
    """The existing origin authority determines both the requester and the library owner."""

    def __init__(self, platform):
        self.p = platform
        self.reading = platform.media.books.reading

    def validate(self, body):
        self.p.contracts.check("reading-state#request", body)
        require(
            isinstance(body, dict)
            and set(body) == {"schema_version", "query", "scope", "grant_id"}
            and body["schema_version"] == 1,
            "invalid_input",
            400,
        )
        self.p.contracts.check("common#query", body["query"])
        self.p.contracts.check("common#scope", body["scope"])
        require(
            body["grant_id"] is None
            or isinstance(body["grant_id"], str)
            and len(body["grant_id"]) <= 64,
            "invalid_input",
            400,
        )

    def read(self, header, body):
        result = self._read(header, body)
        self.p.contracts.check("reading-state#response", result)
        return result

    def _read(self, header, body):
        self.validate(body)
        context = self.p.origins.resolve(
            header,
            {
                "schema_version": 1,
                "request_id": body["query"]["request_id"],
                "assertion_ref": body["query"]["origin"]["assertion_ref"],
            },
        )["context"]
        require(
            context["audience_service"] == "companion"
            and context["allowed_scope"] == body["scope"],
            "forbidden",
            403,
        )
        scope = body["scope"]
        response = {
            "schema_version": 1,
            "request_id": body["query"]["request_id"],
            "scope": scope,
            "catalog": [],
            "catalog_total": 0,
            "active": None,
            "grant_current": False,
            "observed_at": self.reading.repo.clock(),
        }
        if scope["audience"] != "self_private" or not scope["person_id"]:
            return response
        config = self.p.settings.get("web", {})
        owner = config.get("principal")
        principal = self.p.auth.principals.get(owner)
        if not principal:
            return response
        with self.p.store.connect() as db:
            identity = db.execute(
                "SELECT person_id FROM identities WHERE account=?",
                (canonical(principal["account"]),),
            ).fetchone()
        if not identity or identity[0] != scope["person_id"]:
            return response
        role = self.p.role_runtime.get(scope["actor_id"])
        if role and (
            not self.p.role_runtime.active(scope["actor_id"])
            or "dialogue" not in role["capabilities"]
        ):
            return response
        books = self.reading.repo.list("book")
        subscribed = {r["book_id"] for r in self.reading.repo.list("book_subscription")}
        books = [
            b
            for b in books
            if b.get("artifact") or b["book_id"] in subscribed or b["source_type"] != "remote"
        ]
        response["catalog_total"] = len(books)
        response["catalog"] = [
            {
                "book_id": b["book_id"],
                "title": b["metadata"]["title"][:160],
                "author": b["metadata"].get("author", "")[:100],
                "chapters": b["chapter_count"],
            }
            for b in books[:80]
        ]
        row = self.reading.repo.get("book_reader", owner)
        if (
            not row
            or row["closed"]
            or not row["sharing"]
            or self.reading.repo.clock() - row["updated_at"] > 75
        ):
            return response
        live = self.reading.live_sessions.get(row["reader_id"])
        if not live or not live[1].session_valid(live[0]):
            return response
        try:
            _, _, _, current_scope = live[1].dialogue.selection(row["binding"])
            if current_scope != row["scope"] or any(
                scope[k] != current_scope[k] for k in ("actor_id", "person_id", "audience")
            ):
                return response
            book, chapter = self.reading.text(row)
        except Fault:
            return response
        offset = min(row["offset"], len(chapter["content"]))
        start = max(0, offset - 800)
        # Only text through the current reading position; never inject unread following chapters.
        excerpt = chapter["content"][start:offset]
        response["active"] = {
            "book_id": book["book_id"],
            "title": book["metadata"]["title"],
            "author": book["metadata"].get("author", ""),
            "chapter_id": chapter["chapter_id"],
            "chapter_title": chapter["title"],
            "content_hash": chapter["content_hash"],
            "offset": offset,
            "seconds": row["seconds"],
            "excerpt": excerpt,
            "excerpt_start": start,
            "grant_id": row["grant_id"],
            "updated_at": row["updated_at"],
        }
        response["grant_current"] = body["grant_id"] == row["grant_id"]
        return response

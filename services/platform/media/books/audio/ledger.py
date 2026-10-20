"""Audio-specific attempts and quota reservations in the existing media repository."""

from datetime import datetime, timezone
import json
import uuid

from ....contracts import Fault, digest, require
from .config import defaults, profile_id
from .segments import segments


class Ledger:
    def __init__(self, audio):
        self.audio, self.books, self.repo = audio, audio.books, audio.repo

    def prepare(self, book_id, selected, chapter_ids=None):
        self.books.catalog.book(book_id)
        root = str(self.audio.files.root())
        chapters = self.books.catalog.directory(book_id)
        if chapter_ids is not None:
            require(
                isinstance(chapter_ids, list)
                and 0 < len(chapter_ids) <= 20000
                and all(isinstance(c, str) for c in chapter_ids),
                "invalid_input",
                400,
            )
            require(set(chapter_ids) <= {c["chapter_id"] for c in chapters}, "not_found", 404)
            chapters = [c for c in chapters if c["chapter_id"] in chapter_ids]
        rows, missing = [], 0
        for chapter in chapters:
            cached = self.books.catalog.chapter(book_id, chapter["chapter_id"])
            if not cached:
                missing += 1
                continue
            key = digest({"text": cached["content"], "profile": selected})
            rows.append(
                {
                    "id": digest([book_id, chapter["chapter_id"], profile_id(selected)])[:32],
                    "book_id": book_id,
                    "chapter_id": chapter["chapter_id"],
                    "title": chapter["title"],
                    "order": chapter["order"],
                    "profile_id": profile_id(selected),
                    "profile": selected,
                    "root": root,
                    "key": key,
                    "text": cached["content"],
                    "content_hash": cached["content_hash"],
                }
            )
        return rows, missing

    def estimate(self, rows, missing):
        ready = queued = changed = characters = 0
        for row in rows:
            prior = self.repo.get("book_audio_job", row["id"])
            if prior:
                changed += int(prior["content_hash"] != row["content_hash"])
                ready += int(prior["state"] == "ready")
                queued += int(prior["state"] != "ready")
            else:
                for part in segments(row["text"]):
                    key = digest({"part": part["text"], "profile": row["profile"]})
                    if not self.audio.files.read(row["root"], key):
                        characters += len(part["text"])
        return {
            "chapters": len(rows),
            "ready": ready,
            "existing": queued,
            "missing_text": missing,
            "changed_text": changed,
            "new_characters": characters,
            "new_chapters": len(rows) - ready - queued,
        }

    def enqueue(self, rows, db):
        count = 0
        for row in rows:
            if not self.repo._document(db, "book_audio_job", row["id"]):
                self.repo.put(
                    "book_audio_job",
                    row["id"],
                    {
                        **row,
                        "state": "queued",
                        "code": "queued",
                        "lease": 0,
                        "created_at": self.repo.clock(),
                        "completed_parts": 0,
                    },
                    db=db,
                )
                count += 1
        return {"queued": count, "reused": len(rows) - count}

    def claim(self):
        now = self.repo.clock()
        with self.repo.transaction() as db:
            rows = db.execute(
                "SELECT body FROM documents WHERE kind='book_audio_job' "
                "AND (json_extract(body,'$.state') IN ('queued','waiting') OR "
                "(json_extract(body,'$.state')='working' AND json_extract(body,'$.lease')<?)) "
                "ORDER BY json_extract(body,'$.created_at'),json_extract(body,'$.order')",
                (now,),
            ).fetchall()
            for value in rows:
                row = self.repo.payloads.read("book_audio_job", json.loads(value[0]))
                if row.get("next_at", 0) > now:
                    continue
                plan = self.repo._document(db, "book_audio_plan", row["book_id"])
                if plan and plan.get("paused") and plan["profile_id"] == row["profile_id"]:
                    continue
                row.update(
                    state="working", code="generating", owner=uuid.uuid4().hex, lease=now + 240
                )
                return self.repo.put("book_audio_job", row["id"], row, db=db)
        return None

    def update(self, job, **changes):
        with self.repo.transaction() as db:
            current = self.repo._document(db, "book_audio_job", job["id"])
            require(current and current.get("owner") == job["owner"], "version_conflict", 409)
            return self.repo.put("book_audio_job", job["id"], {**current, **changes}, db=db)

    def begin(self, job, part, key):
        now = self.repo.clock()
        provider = job["profile"]["provider"]
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        with self.repo.transaction() as db:
            current = self.repo._document(db, "book_audio_attempt", key)
            if current and current["state"] == "inflight":
                if current["lease"] > now:
                    raise Fault("book_audio_inflight", 409)
                current = self.repo.put(
                    "book_audio_attempt", key, {**current, "state": "unknown"}, db=db
                )
            if current and current["state"] not in {"rejected", "retry"}:
                # Ready with missing/corrupt files also needs explicit regeneration.
                raise Fault(
                    "book_audio_unknown"
                    if current["state"] != "ready"
                    else "book_audio_file_missing",
                    409,
                )
            config = self.repo._document(db, "book_audio_config", provider) or defaults(provider)
            budget_id = provider + ":" + day
            budget = self.repo._document(db, "book_audio_budget", budget_id) or {"characters": 0}
            require(
                config["daily_characters"] == 0
                or budget["characters"] + len(part["text"]) <= config["daily_characters"],
                "book_audio_budget",
                409,
            )
            self.repo.put(
                "book_audio_budget",
                budget_id,
                {
                    "characters": budget["characters"] + len(part["text"]),
                    "day": day,
                    "provider": provider,
                },
                db=db,
            )
            return self.repo.put(
                "book_audio_attempt",
                key,
                {
                    "key": key,
                    "state": "inflight",
                    "token": uuid.uuid4().hex,
                    "lease": now + 240,
                    "characters": len(part["text"]),
                    "budget_id": budget_id,
                    "started_at": now,
                },
                db=db,
            )

    def attempt_done(self, attempt, state, code=None):
        with self.repo.transaction() as db:
            current = self.repo._document(db, "book_audio_attempt", attempt["key"])
            require(current and current.get("token") == attempt["token"], "version_conflict", 409)
            if state == "rejected" and current["state"] == "inflight":
                budget = self.repo._document(db, "book_audio_budget", current["budget_id"])
                self.repo.put(
                    "book_audio_budget",
                    current["budget_id"],
                    {**budget, "characters": max(0, budget["characters"] - current["characters"])},
                    db=db,
                )
            self.repo.put(
                "book_audio_attempt",
                attempt["key"],
                {**current, "state": state, "code": code, "finished_at": self.repo.clock()},
                db=db,
            )

    def retry(self, body, db):
        job = self.repo._document(db, "book_audio_job", body.get("job_id"))
        require(job and job["state"] in {"failed", "unknown", "waiting"}, "version_conflict", 409)
        require(job["revision"] == body.get("expected_revision"), "version_conflict", 409)
        for part in segments(job["text"]):
            key = digest({"part": part["text"], "profile": job["profile"]})
            attempt = self.repo._document(db, "book_audio_attempt", key)
            if attempt and attempt["state"] in {"unknown", "ready", "inflight"}:
                require(
                    attempt["state"] != "inflight" or attempt["lease"] <= self.repo.clock(),
                    "book_audio_inflight",
                    409,
                )
                require(body.get("allow_charge") is True, "book_audio_retry_confirmation", 409)
                self.repo.put("book_audio_attempt", key, {**attempt, "state": "retry"}, db=db)
        self.repo.put(
            "book_audio_job",
            job["id"],
            {**job, "state": "queued", "code": "queued", "next_at": 0},
            db=db,
        )
        return {"queued": 1}

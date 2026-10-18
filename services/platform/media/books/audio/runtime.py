"""One supervised book-audio worker, independent of video and music download capacity."""

import asyncio
from datetime import datetime, timezone

from ....contracts import Fault, digest, require
from .config import AudioConfig, EDGE_VOICES, FISH_MODELS, profile, profile_id
from .files import AudioFiles
from .ledger import Ledger
from .providers import Providers, ProviderRejected
from .segments import segments


def public_job(row):
    return {
        key: row.get(key)
        for key in (
            "id",
            "chapter_id",
            "title",
            "order",
            "profile_id",
            "state",
            "code",
            "completed_parts",
            "content_hash",
            "artifact",
            "revision",
            "created_at",
        )
    }


class BookAudio:
    def __init__(self, books):
        self.books, self.repo, self.local = books, books.repo, books.media.local
        self.config = AudioConfig(books)
        self.files = AudioFiles(books)
        self.providers = Providers()
        self.ledger = Ledger(self)
        self.wake = asyncio.Event()
        self.lock = asyncio.Lock()

    def settings(self):
        day = datetime.fromtimestamp(self.repo.clock(), timezone.utc).strftime("%Y-%m-%d")
        return {
            "providers": [
                self.config.public(self.config.get(p)) for p in ("edge", "fish", "local")
            ],
            "voices": [{"id": k, "name": v} for k, v in EDGE_VOICES],
            "fish_models": FISH_MODELS,
            "usage": {
                p: (self.repo.get("book_audio_budget", p + ":" + day) or {}).get("characters", 0)
                for p in ("edge", "fish", "local")
            },
            "budget_day": day,
        }

    async def view(self, book_id):
        await self.local(self.books.catalog.book, book_id)
        rows = await self.local(self.repo.list, "book_audio_job")
        return {
            "jobs": sorted(
                [public_job(r) for r in rows if r["book_id"] == book_id],
                key=lambda r: (r["order"], r["created_at"]),
            ),
            "plan": await self.local(self.repo.get, "book_audio_plan", book_id),
            **await self.local(self.settings),
        }

    async def prepare(self, body):
        selected = profile(await self.local(self.config.get, body.get("provider", "edge")))
        rows, missing = await self.local(
            self.ledger.prepare, body.get("book_id"), selected, body.get("chapter_ids")
        )
        estimate = await self.local(self.ledger.estimate, rows, missing)
        return selected, rows, {**estimate, "profile": selected, "profile_id": profile_id(selected)}

    async def run_once(self):
        async with self.lock:
            job = await self.local(self.ledger.claim)
            if not job:
                return False
            try:
                cached = await self.local(self.files.read, job["root"], job["key"])
                if cached:
                    artifact = await self.local(self.files.link, job, cached)
                else:
                    pieces = []
                    for index, part in enumerate(segments(job["text"])):
                        plan = await self.local(self.repo.get, "book_audio_plan", job["book_id"])
                        if plan and plan.get("paused") and plan["profile_id"] == job["profile_id"]:
                            await self.local(self.ledger.update, job, state="queued", code="paused")
                            return True
                        key = digest({"part": part["text"], "profile": job["profile"]})
                        cached = await self.local(self.files.read, job["root"], key)
                        if cached is None:
                            credential = await self.local(self.config.credentials, job["profile"])
                            attempt = await self.local(self.ledger.begin, job, part, key)
                            try:
                                raw, boundaries = await self.providers.synthesize(
                                    job["profile"], part["text"], credential
                                )
                                cached = await self.local(
                                    self.files.publish,
                                    job["root"],
                                    key,
                                    part["text"],
                                    raw,
                                    boundaries,
                                )
                            except ProviderRejected as error:
                                await self.local(
                                    self.ledger.attempt_done, attempt, "rejected", error.code
                                )
                                raise
                            except BaseException:
                                await asyncio.shield(
                                    self.local(
                                        self.ledger.attempt_done,
                                        attempt,
                                        "unknown",
                                        "book_audio_unknown",
                                    )
                                )
                                raise
                            await self.local(self.ledger.attempt_done, attempt, "ready")
                        pieces.append((cached, part))
                        await self.local(
                            self.ledger.update,
                            job,
                            completed_parts=index + 1,
                            lease=self.repo.clock() + 240,
                        )
                    artifact = await self.local(self.files.assemble, job, pieces)
                await self.local(
                    self.ledger.update,
                    job,
                    state="ready",
                    code="ready",
                    artifact=artifact,
                    lease=0,
                    completed_at=self.repo.clock(),
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                code = error.code if isinstance(error, Fault) else "book_audio_unknown"
                waiting = code in {
                    "book_audio_budget",
                    "book_audio_inflight",
                    "book_audio_rate_limited",
                }
                unknown = code in {
                    "book_audio_unknown",
                    "book_audio_remote_unknown",
                    "book_audio_response_invalid",
                }
                await self.local(
                    self.ledger.update,
                    job,
                    state="waiting" if waiting else "unknown" if unknown else "failed",
                    code=code,
                    next_at=self.repo.clock() + (3600 if code == "book_audio_budget" else 120),
                    lease=0,
                )
            return True

    async def reconcile(self):
        for plan in await self.local(self.repo.list, "book_audio_plan"):
            if not plan["auto_new"] or plan.get("paused"):
                continue
            # A checkpoint includes all chapters already present when auto-follow was enabled,
            # even those not selected for initial generation.
            directory = await self.local(self.books.catalog.directory, plan["book_id"])
            initial = set(plan["initial_chapters"])
            added = [c["chapter_id"] for c in directory if c["chapter_id"] not in initial]
            if not added:
                continue
            try:
                rows, _ = await self.local(
                    self.ledger.prepare, plan["book_id"], plan["profile"], added
                )
            except Fault as error:
                # A disconnected book volume must not stop unrelated media loops.
                await self.local(
                    self.repo.put,
                    "book_audio_plan",
                    plan["book_id"],
                    {**plan, "code": error.code},
                    plan["revision"],
                )
                continue

            def enqueue():
                with self.repo.transaction() as db:
                    current = self.repo._document(db, "book_audio_plan", plan["book_id"])
                    if current and current["revision"] == plan["revision"]:
                        self.ledger.enqueue(rows, db)

            await self.local(enqueue)

    async def loop(self):
        next_scan = 0
        while True:
            self.wake.clear()
            if self.repo.clock() >= next_scan:
                await self.reconcile()
                next_scan = self.repo.clock() + 30
            if await self.run_once():
                continue
            try:
                await asyncio.wait_for(self.wake.wait(), 5)
            except TimeoutError:
                pass

    async def manifest(self, job_id):
        job = await self.local(self.repo.get, "book_audio_job", job_id)
        require(job and job["state"] == "ready", "book_audio_not_ready", 409)
        manifest = await self.local(self.files.read, job["root"], job["key"])
        if manifest is None:
            await self.local(self.missing, job)
        require(manifest, "book_audio_file_missing", 409)
        return {
            **manifest,
            "job_id": job_id,
            "url": "/api/web/media/books/files/" + job["book_id"] + "/audio-" + job_id,
        }

    async def file(self, book_id, job_id):
        job = await self.local(self.repo.get, "book_audio_job", job_id)
        require(job and job["book_id"] == book_id and job["state"] == "ready", "not_found", 404)
        manifest = await self.local(self.files.read, job["root"], job["key"])
        if manifest is None:
            await self.local(self.missing, job)
        require(manifest, "book_audio_file_missing", 409)
        return (
            self.files.cache(job["root"], job["key"]) / "audio.mp3",
            job["title"] + ".mp3",
            "audio/mpeg",
        )

    def missing(self, job):
        with self.repo.transaction() as db:
            current = self.repo._document(db, "book_audio_job", job["id"])
            if current and current["state"] == "ready" and current["key"] == job["key"]:
                self.repo.put(
                    "book_audio_job",
                    job["id"],
                    {**current, "state": "failed", "code": "book_audio_file_missing"},
                    db=db,
                )

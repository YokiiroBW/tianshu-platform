"""Index existing originals and decode inbox files through the shared media queue."""

import asyncio
import os
from pathlib import Path

import mutagen

from ...contracts import Fault, digest, require
from ..destination import safe_path
from ..organizer import library_lock
from ..video_storage import copy_file
from . import tools
from .audio import cover, probe, record
from .downloads import settled
from .quality import verify_quality

PLAIN = {".flac", ".mp3", ".m4a"}
ENCRYPTED = {
    ".ncm",
    ".qmc0",
    ".qmc3",
    ".qmcflac",
    ".qmcogg",
    ".mflac",
    ".mflac0",
    ".mgg",
    ".mgg0",
    ".kgm",
    ".kgma",
    ".vpr",
    ".kgg",
}


def stamp(path):
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def discover(directory):
    root, items = Path(directory), []
    require(root.is_dir(), "music_directory_unavailable", 409)
    safe_path(root, ".incoming").mkdir(exist_ok=True)
    for folder, directories, files in os.walk(root, followlinks=False):
        directories[:] = [
            name
            for name in directories
            if name not in {"歌单", "@eaDir", "#recycle", "attachments"}
            and (not name.startswith(".") or name == ".incoming")
            and not (Path(folder) / name).is_symlink()
            and not (Path(folder) / name).is_junction()
        ]
        for name in files:
            path = Path(folder) / name
            relative = path.relative_to(root).as_posix()
            if (
                path.suffix.lower() not in PLAIN | ENCRYPTED
                or path.is_symlink()
                or path.is_junction()
            ):
                continue
            if path.suffix.lower() in ENCRYPTED and not relative.startswith(".incoming/"):
                continue
            items.append({"path": relative, **stamp(path)})
    return items


def metadata(path, content_id, fallback_title=None):
    audio = mutagen.File(path, easy=True)
    tags = audio.tags if audio is not None and audio.tags else {}

    def values(*keys):
        for key in keys:
            if tags.get(key):
                return [str(v) for v in tags[key]]
        return []

    return {
        "provider": "local",
        "source_id": content_id,
        "title": next(iter(values("title")), fallback_title or path.stem),
        "artists": values("artist"),
        "album": next(iter(values("album")), ""),
        "album_artists": values("albumartist"),
        "track_number": next(iter(values("tracknumber")), ""),
        "disc": next(iter(values("discnumber")), ""),
        "date": next(iter(values("date")), ""),
        "availability": "known",
    }


class Imports:
    def __init__(self, music):
        self.music, self.media = music, music.media
        self.repo, self.catalog = music.media.repo, music.library.catalog
        self.lock = asyncio.Lock()

    def view(self):
        return self.repo.get("music_import_scan", "library") or {
            "state": "not_scanned",
            "next_scan_at": 0,
        }

    def enqueue(self, *, item=None):
        settings = self.music.library.settings()
        require(settings["directory"], "music_directory_unavailable", 409)
        operation = "import" if item else "scan"
        key = digest({"operation": operation, "directory": settings["directory"], "input": item})
        with self.repo.transaction() as db:
            job = self.repo.enqueue(
                {
                    "media_kind": "audio",
                    "audio_operation": operation,
                    "audio_key": key,
                    "provider": "local",
                    "source_id": key,
                    "title": item["path"] if item else "扫描本地音乐曲库",
                    "account_id": None,
                    "creator": "",
                    "target_id": "music-library",
                    "destination_directory": settings["directory"],
                    "audio_layout": settings["layout"],
                    "quality": {"level": "best", "allow_fallback": True},
                    "input": item,
                },
                db=db,
            )
            if operation == "scan" and job["state"] in {
                "completed",
                "failed",
                "cancelled",
                "unknown",
            }:
                job = self.repo.update_job(
                    job["job_id"],
                    {
                        "state": "queued",
                        "stage": "queued",
                        "code": "queued",
                        "cancel_requested": False,
                    },
                    db=db,
                )
            if operation == "scan":
                prior = self.repo._document(db, "music_import_scan", "library") or {}
                self.repo.put(
                    "music_import_scan",
                    "library",
                    {
                        **prior,
                        "state": "queued",
                        "job_id": job["job_id"],
                        "next_scan_at": self.repo.clock() + 3600,
                    },
                    db=db,
                )
        return {"job_id": job["job_id"], "state": job["state"]}

    async def index(self, root, item):
        local, storage = self.media.local, self.music.downloads.storage
        path = safe_path(root, item["path"])
        try:
            with path.open("rb") as stream:
                stream.read(1)
        except PermissionError:
            raise Fault("music_file_unreadable", 409) from None
        before = await local(stamp, path)
        observed = await probe(path, None, self.media.config["engine"])
        track = await local(metadata, path, observed["content_id"])
        track["duration_ms"] = round(observed["duration_seconds"] * 1000)
        file = {"path": item["path"], "kind": "audio", **await local(record, path)}
        require(before == await local(stamp, path), "music_source_changed", 409)
        quality = verify_quality({"level": "best", "allow_fallback": True}, "observed", observed)

        def save():
            with library_lock(str(root)):
                duplicate = self.catalog.content_file(observed["content_id"], str(root))
                valid_duplicate = duplicate and storage.matches(duplicate)
                with self.repo.transaction() as db:
                    binding = self.catalog.register("local", track, db)
                    if valid_duplicate:
                        self.catalog.merge(binding["recording_id"], duplicate["recording_id"], db)
                        return duplicate["recording_id"]
                    previous = self.catalog.file(binding["recording_id"], db=db)
                # Leave existing originals intact; a valid canonical file already serves all playlists.
                if previous and storage.matches(previous):
                    if not previous.get("content_id"):
                        with self.repo.transaction() as db:
                            self.catalog.store_file(
                                previous["recording_id"],
                                {**previous, "content_id": observed["content_id"]},
                                db,
                            )
                    return previous["recording_id"]
                with self.repo.transaction() as db:
                    result = self.catalog.store_file(
                        binding["recording_id"],
                        {
                            "directory": str(root),
                            "files": [file],
                            "quality": quality,
                            "content_id": observed["content_id"],
                            "managed": False,
                            "requested_quality": {"level": "best", "allow_fallback": True},
                            "warnings": [],
                            "download_provider": "local",
                            "download_source_id": track["source_id"],
                        },
                        db,
                    )
                    return result["recording_id"]

        return await local(save)

    async def scan(self, job, update, *, finish=True):
        local = self.media.local
        root = Path(job["destination_directory"])
        items = await local(discover, str(root))
        counts = {"total": len(items), "indexed": 0, "unchanged": 0, "queued": 0, "failed": 0}
        for index, item in enumerate(items):
            key = digest({"directory": str(root), "path": item["path"]})
            prior = await local(self.repo.get, "music_file_observation", key)
            if (
                prior
                and prior.get("state") not in {"missing", "failed"}
                and all(prior.get(k) == item[k] for k in ("size", "mtime_ns"))
            ):
                counts["unchanged"] += 1
                counts["failed"] += prior.get("state") == "failed"
                continue
            result = {**item, "directory": str(root), "checked_at": self.repo.clock()}
            try:
                if Path(item["path"]).suffix.lower() in ENCRYPTED:
                    queued = await local(self.enqueue, item=item)
                    result.update(**queued)
                    counts["queued"] += 1
                else:
                    result.update(state="indexed", recording_id=await self.index(root, item))
                    counts["indexed"] += 1
            except (Fault, OSError, mutagen.MutagenError) as error:
                result.update(
                    state="failed",
                    code=error.code if isinstance(error, Fault) else "music_audio_invalid",
                )
                counts["failed"] += 1
            await local(self.repo.put, "music_file_observation", key, result)
            if index % 10 == 0:
                await update(progress=(index + 1) / max(1, len(items)))
                await local(
                    self.repo.put,
                    "music_import_scan",
                    "library",
                    {**self.view(), **counts, "state": "scanning"},
                )
        await local(
            self.repo.put,
            "music_import_scan",
            "library",
            {
                **counts,
                "directory": str(root),
                "state": "completed",
                "checked_at": self.repo.clock(),
                "next_scan_at": self.repo.clock() + 3600,
            },
        )
        await local(self.mark_missing, root, {item["path"] for item in items})
        await local(self.music.downloads.storage.export_related, job)
        if finish:
            await update(state="completed", stage="completed", code="music_indexed", progress=1)

    def mark_missing(self, root, seen):
        for row in self.repo.list("music_file_observation"):
            if (
                row["directory"] == str(root)
                and row["path"] not in seen
                and row["state"] != "missing"
            ):
                self.repo.put(
                    "music_file_observation",
                    digest({"directory": str(root), "path": row["path"]}),
                    {**row, "state": "missing"},
                )
        for artifact in self.repo.list("music_library_file"):
            if artifact["directory"] != str(root):
                continue
            current = self.catalog.file(artifact["recording_id"])
            if not current or current["recording_id"] != artifact["recording_id"]:
                continue
            audio = next((f for f in artifact["files"] if f["kind"] == "audio"), None)
            missing = not audio or not safe_path(root, audio["path"]).is_file()
            if missing != artifact.get("missing", False):
                with self.repo.transaction() as db:
                    self.catalog.store_file(
                        artifact["recording_id"], {**artifact, "missing": missing}, db
                    )

    async def ensure_indexed(self, job, update):
        async with self.lock:
            status = await self.media.local(self.view)
            if (
                not status.get("checked_at")
                or status.get("directory") != job["destination_directory"]
            ):
                await self.scan(job, update, finish=False)

    async def convert(self, job, owner, update):
        local, storage, downloads = (
            self.media.local,
            self.music.downloads.storage,
            self.music.downloads,
        )
        item = job["input"]
        require(item["path"].startswith(".incoming/"), "unsafe_library_path", 409)
        source = safe_path(Path(job["destination_directory"]), item["path"])
        require(
            await local(stamp, source) == {k: item[k] for k in ("size", "mtime_ns")},
            "music_source_changed",
            409,
        )
        require(
            item["size"] <= min(1024**3, self.media.config["engine"]["max_bytes"]),
            "music_file_too_large",
            409,
        )
        raw = await local(storage.stage, job, "decoded.part")
        picture = await local(storage.stage, job, "decoded.cover")
        staged = await local(storage.stage, job, "original" + source.suffix.lower())
        for path in (raw, picture, staged):
            await local(path.unlink, missing_ok=True)
        await settled(local, copy_file, source, staged)
        require(
            await local(record, source) == await local(record, staged), "music_source_changed", 409
        )
        data = await tools.invoke(
            "musicdecode",
            {"input": str(staged), "output": str(raw), "cover": str(picture)},
            timeout=600,
        )
        observed = await probe(raw, None, self.media.config["engine"])
        track = await local(metadata, raw, observed["content_id"], source.stem)
        meta = data.get("metadata") or {}
        raw_artists = meta.get("artist") or []
        names = data.get("artists") or [
            v[0] for v in raw_artists if isinstance(v, list) and v and isinstance(v[0], str)
        ]
        track.update(
            title=data.get("title") or track["title"],
            album=data.get("album") or track["album"],
            artists=names or track["artists"],
            duration_ms=round(observed["duration_seconds"] * 1000),
        )

        def bind():
            with self.repo.transaction() as db:
                return self.catalog.register("local", track, db)

        binding = await local(bind)
        job = await update(recording_id=binding["recording_id"], track=track)
        reused = await settled(local, storage.reuse, job, owner)
        if not reused:
            artwork = await local(lambda: cover(picture.read_bytes())) if picture.exists() else None
            quality = verify_quality(job["quality"], "observed", observed)
            package = await downloads.prepare_package(job, track, raw, observed, quality, artwork)
            job = await update(
                audio_package=package, state="publishing", stage="publishing", code="publishing"
            )
            job = await settled(local, storage.publish, job, owner)
        else:
            job = reused
        await settled(local, storage.cleanup, job)
        await local(storage.export_related, job)
        return job

    async def run(self, job, owner):
        local, repo, storage = self.media.local, self.repo, self.music.downloads.storage

        async def update(**changes):
            nonlocal job
            job = await local(
                repo.update_job, job["job_id"], changes, owner=owner, generation=job["generation"]
            )
            return job

        running = asyncio.current_task()

        async def heartbeat():
            while True:
                await asyncio.sleep(5)
                try:
                    await local(storage.guard, job, owner)
                except Fault:
                    running.cancel()
                    return

        pulse = asyncio.create_task(heartbeat())
        try:
            await local(storage.guard, job, owner)
            if job["audio_operation"] == "scan":
                async with self.lock:
                    await self.scan(job, update)
            else:
                await self.convert(job, owner, update)
        except asyncio.CancelledError:
            current = await local(repo.job, job["job_id"])
            if current and current["state"] != "completed":
                await local(
                    repo.update_job,
                    job["job_id"],
                    {
                        "state": "cancelled" if current["cancel_requested"] else "retry_wait",
                        "code": "worker_interrupted",
                        "retry_delay": 30,
                    },
                    generation=job["generation"],
                )
            raise
        except (Fault, OSError, mutagen.MutagenError) as error:
            code = error.code if isinstance(error, Fault) else "media_io_failure"
            await update(state="failed", stage="failed", code=code)
            if job["audio_operation"] == "scan":
                await local(
                    repo.put,
                    "music_import_scan",
                    "library",
                    {**self.view(), "state": "failed", "code": code},
                )
        finally:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)

    async def pump(self):
        while True:
            try:
                settings, status = (
                    await self.media.local(self.music.library.settings),
                    await self.media.local(self.view),
                )
                if settings["directory"] and self.repo.clock() >= status.get("next_scan_at", 0):
                    await self.media.local(self.enqueue)
                    self.media.wake.set()
            except (Fault, OSError):
                pass
            await asyncio.sleep(30)

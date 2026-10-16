"""Small, recoverable lyric sidecar updates. Audio bytes and identity never change."""

import hashlib
import json
from pathlib import Path

import mutagen

from ...contracts import digest, require
from ..destination import safe_path
from ..organizer import library_lock
from ..video_storage import sync_directory, sync_file
from .lyrics_text import LIMIT, clean, sidecars

KIND = "music_lyrics"
WEEK = 7 * 86400


def audio_file(artifact):
    return next(f for f in artifact["files"] if f["kind"] == "audio")


def archive_key(artifact):
    return digest({"directory": artifact["directory"], "audio": audio_file(artifact)})


def schedule(repo, identity, artifact, db):
    current = repo._document(db, KIND, identity) or {}
    key = archive_key(artifact)
    if current.get("archive_key") != key:
        repo.put(
            KIND,
            identity,
            {
                "recording_id": identity,
                "archive_key": key,
                "state": "queued",
                "next_check_at": 0,
                "attempts": 0,
            },
            db=db,
        )


def read_local(artifact):
    root, audio = Path(artifact["directory"]), audio_file(artifact)
    path = safe_path(root, audio["path"])
    require(
        path.is_file() and path.stat().st_size == audio["size_bytes"], "music_source_changed", 409
    )
    base = Path(audio["path"]).with_suffix("").as_posix()

    def read(suffix):
        candidate = safe_path(root, base + suffix)
        if candidate.is_file() and candidate.stat().st_size <= LIMIT * 4:
            return clean(candidate.read_text(encoding="utf-8-sig", errors="replace"))
        return ""

    result = {
        "original": read(".original.lrc") or read(".lrc"),
        "translated": read(".zh.lrc"),
        "romanized": read(".rom.lrc"),
    }
    if not result["original"]:
        try:
            tags = mutagen.File(path)
            if tags is not None:
                for key in ("lyrics", "LYRICS", "\u00a9lyr"):
                    values = tags.get(key)
                    if values:
                        result["original"] = clean(values[0])
                        break
                if (
                    not result["original"]
                    and tags.tags is not None
                    and hasattr(tags.tags, "getall")
                ):
                    values = tags.tags.getall("USLT")
                    if values:
                        result["original"] = clean(values[0].text)
        except (mutagen.MutagenError, ValueError):
            pass
    return result


def file_digest(path):
    if not path.is_file():
        return None
    require(path.stat().st_size <= LIMIT * 4, "music_lyrics_changed", 409)
    return hashlib.sha256(path.read_bytes()).hexdigest()


class LyricStorage:
    def __init__(self, music):
        self.music, self.repo = music, music.media.repo

    def current(self, artifact, owner, generation, db):
        self.repo.require_scan(db, "music_lyrics", owner, generation)
        catalog = self.music.library.catalog
        current = catalog.file(artifact["recording_id"], db=db)
        settings = self.repo._document(db, "music_settings", "library") or {}
        require(
            current
            and current["recording_id"] == artifact["recording_id"]
            and archive_key(current) == archive_key(artifact)
            and settings.get("directory") == artifact["directory"],
            "scope_changed",
            409,
        )
        return current

    def publish(self, artifact, value, result, owner, generation):
        identity, root = artifact["recording_id"], Path(artifact["directory"])
        base = Path(audio_file(artifact)["path"]).with_suffix("").as_posix()
        with library_lock(str(root)) as (_, control):
            with self.repo.transaction() as db:
                current = self.current(artifact, owner, generation, db)
                state = self.repo._document(db, KIND, identity) or {}
                pending = state.get("pending")
                if not pending:
                    writes = []
                    for suffix, text in sidecars(value).items():
                        relative = base + suffix
                        content = (text + "\n").encode("utf8")
                        writes.append(
                            {
                                "path": relative,
                                "content": content.decode("utf8"),
                                "sha256": hashlib.sha256(content).hexdigest(),
                                "size_bytes": len(content),
                                "kind": "lyrics",
                                "before": file_digest(safe_path(root, relative)),
                            }
                        )
                    pending = {"writes": writes, "result": result}
                    self.repo.put(
                        KIND,
                        identity,
                        {
                            **state,
                            "recording_id": identity,
                            "archive_key": archive_key(artifact),
                            "state": "publishing",
                            "pending": pending,
                        },
                        db=db,
                    )
            # The persisted intent makes interrupted replacements idempotent.
            changed = False
            for index, item in enumerate(pending["writes"]):
                target = safe_path(root, item["path"])
                actual = file_digest(target)
                require(actual in {item["before"], item["sha256"]}, "music_lyrics_changed", 409)
                if actual == item["sha256"]:
                    continue
                temporary = safe_path(control, f"lyrics-{identity}-{index}.tmp")
                temporary.write_bytes(item["content"].encode("utf8"))
                sync_file(temporary)
                temporary.replace(target)
                sync_directory(target.parent)
                changed = True
            with self.repo.transaction() as db:
                current = self.current(artifact, owner, generation, db)
                items = [
                    {k: item[k] for k in ("path", "sha256", "size_bytes", "kind")}
                    for item in pending["writes"]
                ]
                paths = {item["path"] for item in items}
                result = pending["result"]
                updated = {
                    **current,
                    "files": [f for f in current["files"] if f["path"] not in paths] + items,
                    "lyrics": result,
                }
                if result["coverage"]["original"]:
                    updated["warnings"] = [
                        w for w in updated.get("warnings", []) if w != "music_lyrics_unavailable"
                    ]
                # This is metadata maintenance, never a new/retired audio artifact.
                self.repo.put("music_library_file", identity, updated, db=db)
                for raw in db.execute(
                    "SELECT kind,id,body FROM documents WHERE kind IN ('music_artifact','music_publication')"
                ).fetchall():
                    other = json.loads(raw["body"])
                    if other.get("files") and archive_key(other) == archive_key(artifact):
                        self.repo.put(
                            raw["kind"],
                            raw["id"],
                            {
                                **other,
                                "files": updated["files"],
                                "warnings": updated.get("warnings", []),
                                "lyrics": result,
                            },
                            db=db,
                        )
                # Update historical receipts without requeuing jobs or changing leases.
                db.execute(
                    "UPDATE jobs SET body=json_set(body,'$.audio_receipt',json(?)) WHERE json_extract(body,'$.recording_id')=? AND json_extract(body,'$.audio_receipt.directory')=?",
                    (json.dumps(updated, ensure_ascii=False), identity, artifact["directory"]),
                )
                self.repo.put(
                    KIND,
                    identity,
                    {
                        "recording_id": identity,
                        "archive_key": archive_key(artifact),
                        **result,
                        "changed": changed,
                    },
                    db=db,
                )
            return {**result, "changed": changed}

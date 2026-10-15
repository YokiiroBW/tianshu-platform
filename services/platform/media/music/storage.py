"""Audio publication intents and verified files, in the shared media ledger and root lock."""

import json
import os
import unicodedata
from pathlib import Path

from ...contracts import Fault, digest, require
from ..destination import safe_path
from ..identity import looks_like_windows_device_name
from ..organizer import library_lock
from ..video_storage import copy_file, sync_directory, sync_file
from .audio import record


def component(value, fallback="未知", maximum=100):
    value = unicodedata.normalize("NFC", value or fallback)
    value = (
        "".join(
            "_" if ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF or c in '/\\:<>"|?*' else c
            for c in value
        ).strip(" .")
        or fallback
    )
    if looks_like_windows_device_name(value):
        value = "_" + value
    return value.encode("utf-8")[:maximum].decode("utf-8", errors="ignore").rstrip(" .") or fallback


def stem(track, layout):
    artist = component(", ".join(track.get("artists") or []), "未知艺术家", 80)
    album_artist = component(
        ", ".join(track.get("album_artists") or track.get("artists") or []), "未知艺术家", 80
    )
    return "/".join(
        (
            album_artist if layout == "album_artist_album" else artist,
            component(track.get("album"), "未知专辑", 100),
            artist + " - " + component(track.get("title"), track["source_id"], 80),
        )
    )


class AudioStorage:
    def __init__(self, music):
        self.music, self.media = music, music.media
        self.repo = music.media.repo

    def stage(self, job, name):
        root = Path(self.media.config["staging_directory"])
        relative = f".music/{job['job_id']}/{job['generation']}/{name}"
        path = safe_path(root, relative)
        with self.repo.transaction() as db:
            prior = self.repo._document(db, "music_stage", job["job_id"]) or {"files": []}
            self.repo.put(
                "music_stage",
                job["job_id"],
                {
                    "files": list(dict.fromkeys([*prior["files"], relative])),
                    "state": "retained",
                },
                db=db,
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def check_scope(self, job):
        settings = self.music.library.settings()
        require(
            settings["directory"] == job["destination_directory"]
            and settings["layout"] == job["audio_layout"],
            "scope_changed",
            409,
        )
        require(Path(settings["directory"]).is_dir(), "music_directory_unavailable", 409)

    def guard(self, job, owner):
        self.check_scope(job)
        self.repo.heartbeat(job["job_id"], owner, job["generation"])
        account, _ = self.music.library.account(job["account_id"])
        require(account["revision"] == job["account_revision"], "account_changed", 409)

    def matches(self, artifact):
        root = Path(artifact["directory"])
        try:
            return bool(artifact["files"]) and all(
                record(safe_path(root, item["path"]))
                == {k: item[k] for k in ("sha256", "size_bytes")}
                for item in artifact["files"]
            )
        except (OSError, Fault):
            return False

    def reusable(self, job):
        self.check_scope(job)
        artifact = self.repo.get("music_artifact", job["audio_key"])
        if (
            artifact
            and artifact["directory"] == job["destination_directory"]
            and self.matches(artifact)
        ):
            return artifact
        return None

    def finish_job(self, job, owner, artifact, *, reused=False):
        # Files have crossed the publication point. A late cancel cannot undo that fact.
        with self.repo.transaction() as db:
            row = db.execute(
                "SELECT owner,generation FROM jobs WHERE id=?", (job["job_id"],)
            ).fetchone()
            require(
                row and row["owner"] == owner and row["generation"] == job["generation"],
                "lease_conflict",
                409,
            )
            self.repo.put("music_artifact", job["audio_key"], artifact, db=db)
            return self.repo.update_job(
                job["job_id"],
                {
                    "state": "completed",
                    "stage": "completed",
                    "code": "music_archived",
                    "audio_receipt": artifact,
                    "audio_reused": reused,
                    "progress": 1,
                    "actual_quality": artifact["quality"]["level"],
                    "audio_quality": artifact["quality"],
                    "cancel_requested": False,
                },
                generation=job["generation"],
                db=db,
            )

    def reuse(self, job, owner):
        self.guard(job, owner)
        with library_lock(job["destination_directory"]):
            artifact = self.reusable(job)
            if artifact:
                self.guard(job, owner)
                return self.finish_job(job, owner, artifact, reused=True)
        return None

    def publish(self, job, owner):
        self.guard(job, owner)
        package = job["audio_package"]
        with library_lock(job["destination_directory"]) as (root, control):
            intent = self.repo.get("music_publication", job["job_id"])
            if intent is None or intent["state"] == "retired":
                expected_revision = intent["revision"] if intent else 0
                base = stem(package["track"], job["audio_layout"])
                # Existing files (including unregistered originals) always keep their names and bytes.
                for suffix in (
                    "",
                    f" [网易云{job['source_id']}-{job['audio_key'][:8]}]",
                    f" [{job['job_id']}-{job['generation']}]",
                ):
                    proposed = base + suffix
                    parent = safe_path(root, proposed + package["files"][0]["suffix"]).parent
                    names = (
                        {p.name.casefold() for p in parent.iterdir()} if parent.is_dir() else set()
                    )
                    if all(
                        (Path(proposed).name + f["suffix"]).casefold() not in names
                        for f in package["files"]
                    ):
                        break
                else:
                    raise Fault("music_target_conflict", 409)
                intent = self.repo.put(
                    "music_publication",
                    job["job_id"],
                    {
                        "directory": str(root),
                        "job_id": job["job_id"],
                        "source_id": job["source_id"],
                        "quality": package["quality"],
                        "state": "prepared",
                        "warnings": package["warnings"],
                        "files": [{**f, "path": proposed + f["suffix"]} for f in package["files"]],
                    },
                    expected_revision,
                )
            require(intent["directory"] == str(root), "scope_changed", 409)
            for index, item in enumerate(intent["files"]):
                self.guard(job, owner)
                expected = {k: item[k] for k in ("sha256", "size_bytes")}
                target = safe_path(root, item["path"])
                temporary = safe_path(control, f"music-{job['job_id']}-{index}.tmp")
                if target.exists():
                    require(record(target) == expected, "music_target_conflict", 409)
                    if temporary.exists() and record(temporary) == expected:
                        temporary.unlink()
                    continue
                source = safe_path(Path(self.media.config["staging_directory"]), item["staging"])
                require(record(source) == expected, "music_staging_changed", 409)
                target.parent.mkdir(parents=True, exist_ok=True)
                # This exact private temporary was reserved by the durable intent above.
                if temporary.exists() and record(temporary) != expected:
                    temporary.unlink()
                if not temporary.exists():
                    copy_file(source, temporary)
                require(record(temporary) == expected, "music_staging_changed", 409)
                self.guard(job, owner)
                try:
                    os.link(temporary, target)  # Atomic no-replace, including on SMB.
                except FileExistsError:
                    require(record(target) == expected, "music_target_conflict", 409)
                except OSError:
                    raise Fault("music_atomic_publish_unavailable", 409) from None
                sync_directory(target.parent)
                temporary.unlink()
            require(self.matches(intent), "music_publication_unverified", 409)
            artifact = {k: intent[k] for k in ("directory", "source_id", "quality", "warnings")}
            artifact.update(
                published_at=self.repo.clock(),
                files=[
                    {k: f[k] for k in ("path", "sha256", "size_bytes", "kind")}
                    for f in intent["files"]
                ],
            )
            self.check_scope(job)
            self.repo.put("music_publication", job["job_id"], {**intent, "state": "published"})
            return self.finish_job(job, owner, artifact)

    def retire(self, job):
        with self.repo.transaction() as db:
            intent = self.repo._document(db, "music_publication", job["job_id"])
            if intent and intent["state"] != "retired":
                self.repo.put(
                    "music_publication_history",
                    f"{job['job_id']}:{intent['revision']}",
                    intent,
                    db=db,
                )
                self.repo.put(
                    "music_publication", job["job_id"], {**intent, "state": "retired"}, db=db
                )

    def cleanup(self, job):
        stage = self.repo.get("music_stage", job["job_id"])
        if not stage or stage["state"] == "cleaned":
            return
        artifact = job.get("audio_receipt")
        require(artifact and self.matches(artifact), "music_publication_unverified", 409)
        root = Path(self.media.config["staging_directory"])
        for relative in stage["files"]:
            require(relative.startswith(f".music/{job['job_id']}/"), "unsafe_library_path", 409)
            path = safe_path(root, relative)
            path.unlink(missing_ok=True)
            try:
                path.parent.rmdir()
            except OSError:
                pass
        self.repo.put("music_stage", job["job_id"], {**stage, "state": "cleaned"})

    def export(self, identity):
        settings = self.music.library.settings()
        require(
            settings["directory"] and Path(settings["directory"]).is_dir(),
            "music_directory_unavailable",
            409,
        )
        with library_lock(settings["directory"]) as (root, control):
            playlist, tracks = self.music.library.export_snapshot(identity)
            exported, lines, verified = [], ["#EXTM3U"], {}
            for track in tracks:
                job = track.pop("job", None)
                artifact = job.get("audio_receipt") if job else None
                audio = (
                    next((f for f in artifact["files"] if f["kind"] == "audio"), None)
                    if artifact
                    else None
                )
                if job and job["job_id"] not in verified:
                    verified[job["job_id"]] = (
                        artifact and artifact["directory"] == str(root) and self.matches(artifact)
                    )
                valid = verified.get(job["job_id"]) if job else False
                relative = "../../" + audio["path"] if valid and audio else None
                exported.append({**track, "file": relative})
                if relative:
                    label = " - ".join([", ".join(track["artists"]), track["title"]])
                    lines += ["#EXTINF:-1," + label.replace("\n", " ").replace("\r", " "), relative]
            folder = "歌单/" + digest(identity)[:20]
            result = {
                "playlist_id": identity,
                "name": playlist["name"],
                "revision": playlist["revision"],
                "tracks": exported,
            }
            payloads = {
                "playlist.m3u8": "\n".join(lines) + "\n",
                "playlist.json": json.dumps(result, ensure_ascii=False, indent=2),
            }
            # A managed export directory is claimed once. Unknown existing directories are retained.
            owner = self.repo.get("music_export", digest({"root": str(root), "playlist": identity}))
            directory = safe_path(root, folder)
            require(owner or not directory.exists(), "music_target_conflict", 409)
            self.repo.put(
                "music_export",
                digest({"root": str(root), "playlist": identity}),
                {"directory": str(root), "relative": folder, "playlist_id": identity},
            )
            directory.mkdir(parents=True, exist_ok=True)
            for name, content in payloads.items():
                temporary = safe_path(control, "music-export-" + digest(identity)[:20] + ".tmp")
                temporary.write_text(content, encoding="utf-8", newline="\n")
                sync_file(temporary)
                temporary.replace(safe_path(directory, name))
            sync_directory(directory)
            return {
                "directory": str(directory),
                "total": len(tracks),
                "exported": sum(bool(t["file"]) for t in exported),
                "files": list(payloads),
            }

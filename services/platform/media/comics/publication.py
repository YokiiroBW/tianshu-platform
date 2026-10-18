"""Publish immutable CBZ files under the registered, dedicated comic output root."""

import hashlib
import os
import uuid
from pathlib import Path

from ...contracts import require
from ..destination import safe_path
from ..organizer import library_lock
from ..video_storage import copy_file, sync_directory


def file_hash(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def verify(path, artifact):
    require(
        path.is_file()
        and path.stat().st_size == artifact["size_bytes"]
        and file_hash(path) == artifact["sha256"],
        "comic_file_conflict",
        409,
    )


def publish(repo, job, source, owner):
    # Reuse the existing cross-process volume lock and relative-path protections.
    with library_lock(job["comic_target"]["directory"]) as (root, control):
        destination = safe_path(root, job["comic_relative_path"])
        if destination.exists():
            verify(destination, job["comic_artifact"])
            return {"reused": True, "published_at": repo.clock()}
        temporary = safe_path(control, ".comic-" + uuid.uuid4().hex)
        try:
            copy_file(source, temporary)
            verify(temporary, job["comic_artifact"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            safe_path(root, job["comic_relative_path"])
            # Fence the final filesystem change with the same job generation as the worker.
            with repo.transaction() as db:
                row = db.execute(
                    "SELECT owner,generation,body FROM jobs WHERE id=?", (job["job_id"],)
                ).fetchone()
                import json

                require(
                    row and row["owner"] == owner and row["generation"] == job["generation"],
                    "lease_conflict",
                    409,
                )
                require(not json.loads(row["body"])["cancel_requested"], "job_cancelled", 409)
                try:
                    # Atomic create-if-absent; unlike replace(), this never overwrites an outsider.
                    os.link(temporary, destination)
                except FileExistsError:
                    require(False, "comic_file_conflict", 409)
                sync_directory(destination.parent)
            return {"reused": False, "published_at": repo.clock()}
        finally:
            temporary.unlink(missing_ok=True)

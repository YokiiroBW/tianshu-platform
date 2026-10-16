"""Playlist requests share one recording job per library root, across accounts and policies."""

import json

from ...contracts import digest, require
from ..repository import VISIBLE
from .quality import policy, minimum_level, quality_rank
from .catalog import Catalog, lookup_key, album_identity


def acquisition_request(request):
    """Only identity/quality changes require another all-channel comparison."""
    track = request["track"]
    return {
        "provider": request["provider"],
        "source_id": request["source_id"],
        "quality": request["quality"],
        "identity": lookup_key(track),
        "album": album_identity(track),
        "duration_ms": track.get("duration_ms"),
        "isrc": track.get("isrc"),
        "disc": track.get("disc"),
        "track_number": track.get("track_number"),
        "provider_data": track.get("provider_data"),
    }


def enqueue_playlist(library, identity, db, *, manual=False):
    repo = library.repo
    playlist = repo._document(db, "music_playlist", identity)
    require(playlist is not None, "not_found", 404)
    account = repo._document(db, "music_account", playlist["account_id"])
    require(account, "music_account_unavailable", 409)
    settings = repo._document(db, "music_settings", "library")
    require(settings and settings["directory"], "music_directory_unavailable", 409)
    quality = policy(playlist.get("quality"))
    catalog = Catalog(repo)
    library_id = catalog.root_id(db)
    previous = (repo._document(db, "music_playlist_jobs", identity) or {}).get("jobs", {})
    mapping = {source: previous[source] for source in playlist["entries"] if source in previous}
    queued, unavailable = 0, 0
    for source in dict.fromkeys(playlist["entries"]):
        track = repo._document(db, "music_track", playlist["account_id"] + ":track:" + source)
        if not track or track["availability"] != "known":
            unavailable += 1
            continue
        binding = catalog.register(account["provider"], track, db)
        recording_id = binding["recording_id"]
        key = digest(
            {"library": library_id, "recording": recording_id, "root": settings["directory"]}
        )
        request_id = digest({"playlist": identity, "source": source})
        request = {
            "request_id": request_id,
            "recording_id": recording_id,
            "playlist_id": identity,
            "provider": account["provider"],
            "account_id": account["account_id"],
            "account_revision": account["revision"],
            "source_id": source,
            "quality": quality,
            "track": {k: v for k, v in track.items() if k != "revision"},
        }
        previous_request = repo._document(db, "music_request", request_id)
        changed = (
            previous_request is None
            or {k: v for k, v in previous_request.items() if k != "revision"} != request
        )
        if changed:
            repo.put("music_request", request_id, request, db=db)
        needs_check = previous_request is None or acquisition_request(
            previous_request
        ) != acquisition_request(request)
        existing = db.execute(
            "SELECT body FROM jobs WHERE "
            + VISIBLE
            + " AND json_extract(body,'$.media_kind')='audio' "
            "AND json_extract(body,'$.recording_id')=? "
            "AND json_extract(body,'$.destination_directory')=? ORDER BY created,id LIMIT 1",
            (recording_id, settings["directory"]),
        ).fetchone()
        job = (
            json.loads(existing[0])
            if existing
            else repo.enqueue(
                {
                    "media_kind": "audio",
                    "provider": account["provider"],
                    "recording_id": recording_id,
                    "source_key": binding["source_key"],
                    "request_ids": [request_id],
                    "source_id": source,
                    "audio_key": key,
                    "title": track["title"],
                    "creator": ", ".join(track["artists"]),
                    "account_id": account["account_id"],
                    "account_revision": account["revision"],
                    "subscription_id": identity,
                    "track": track,
                    "quality": quality,
                    "target_id": "music-library",
                    "destination_directory": settings["directory"],
                    "audio_layout": settings["layout"],
                },
                db=db,
            )
        )
        if previous_request is None and existing and job["state"] == "completed":
            artifact = catalog.file(recording_id, db=db)
            if artifact and quality_rank(artifact["quality"])[0] >= minimum_level(quality):
                needs_check = False
        request_ids = list(dict.fromkeys([*job.get("request_ids", []), request_id]))
        updates = {}
        if request_ids != job.get("request_ids"):
            updates["request_ids"] = request_ids
        if needs_check:
            updates.update(
                {
                    "request_version": job.get("request_version", 0) + 1,
                    "quality_review": False,
                }
            )
        if (manual or needs_check) and job["state"] in {
            "failed",
            "auth_required",
            "cancelled",
            "unknown",
            "completed",
        }:
            updates.update(
                {
                    "state": "queued",
                    "stage": "queued",
                    "code": "queued",
                    "cancel_requested": False,
                    "attempts": 0,
                    "quality": quality,
                    "account_id": account["account_id"],
                    "account_revision": account["revision"],
                    "provider": account["provider"],
                    "source_id": source,
                    "source_key": binding["source_key"],
                    "track": track,
                    "audio_layout": settings["layout"],
                    "title": track["title"],
                    "creator": ", ".join(track["artists"]),
                }
            )
        if updates:
            job = repo.update_job(job["job_id"], updates, db=db)
        queued += job["state"] == "queued"
        mapping[source] = job["job_id"]
    repo.put("music_playlist_jobs", identity, {"jobs": mapping}, db=db)
    return {"queued": queued, "unavailable": unavailable, "total": len(mapping)}

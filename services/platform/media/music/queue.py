"""Playlist requests share one recording job per library root, across accounts and policies."""

from ...contracts import digest, require
from .quality import policy
from .catalog import Catalog


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
        job = repo.enqueue(
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
        request_ids = list(dict.fromkeys([*job.get("request_ids", []), request_id]))
        updates = {}
        if request_ids != job.get("request_ids") or changed:
            updates = {
                "request_ids": request_ids,
                "request_version": job.get("request_version", 0) + 1,
                "quality_review": False,
            }
        if (manual or changed) and job["state"] in {
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

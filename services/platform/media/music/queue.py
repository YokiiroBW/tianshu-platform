"""Playlist membership feeds the existing job queue, with one audio identity per policy/root."""

from ...contracts import digest, require
from .quality import policy


def enqueue_playlist(library, identity, db, *, manual=False):
    repo = library.repo
    playlist = repo._document(db, "music_playlist", identity)
    require(playlist is not None, "not_found", 404)
    account = repo._document(db, "music_account", playlist["account_id"])
    require(account and account["state"] == "ready", "music_auth_required", 409)
    settings = repo._document(db, "music_settings", "library")
    require(settings and settings["directory"], "music_directory_unavailable", 409)
    quality = policy(playlist.get("quality"))
    previous = (repo._document(db, "music_playlist_jobs", identity) or {}).get("jobs", {})
    mapping = {source: previous[source] for source in playlist["entries"] if source in previous}
    queued, unavailable = 0, 0
    for source in dict.fromkeys(playlist["entries"]):
        track = repo._document(db, "music_track", playlist["account_id"] + ":track:" + source)
        if not track or track["availability"] != "known":
            unavailable += 1
            continue
        key = digest(
            {
                "account": account["account_id"],
                "track": source,
                "quality": quality,
                "root": settings["directory"],
                "layout": settings["layout"],
            }
        )
        job = repo.enqueue(
            {
                "media_kind": "audio",
                "provider": "netease",
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
        if manual and job["state"] in {
            "failed",
            "auth_required",
            "cancelled",
            "unknown",
            "completed",
        }:
            refreshed = (
                {"track": track, "title": track["title"], "creator": ", ".join(track["artists"])}
                if not job.get("audio_package")
                else {}
            )
            job = repo.update_job(
                job["job_id"],
                {
                    "state": "queued",
                    "stage": "queued",
                    "code": "queued",
                    "cancel_requested": False,
                    "attempts": 0,
                    **refreshed,
                },
                db=db,
            )
        queued += job["state"] == "queued"
        mapping[source] = job["job_id"]
    repo.put("music_playlist_jobs", identity, {"jobs": mapping}, db=db)
    return {"queued": queued, "unavailable": unavailable, "total": len(mapping)}

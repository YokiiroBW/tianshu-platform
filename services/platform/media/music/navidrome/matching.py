"""Match an archived file, never just a title, to Navidrome's indexed song."""

from pathlib import Path, PurePosixPath

from ...destination import safe_path
from ..catalog import normalized, same_recording
from ..audio import tag_identity


def mapped_members(tracks, songs, connection):
    by_path, by_size = {}, {}
    for song in songs:
        if not isinstance(song.get("size"), int) or not isinstance(song.get("suffix"), str):
            continue
        path = str(song.get("real_path", song.get("path", ""))).replace("\\", "/")
        by_path.setdefault(path, []).append(song)
        by_size.setdefault((song.get("size"), song.get("suffix", "").lower()), []).append(song)
    members, cached = [], {}
    for track in tracks:
        artifact = track.get("artifact")
        audio = next((f for f in (artifact or {}).get("files", []) if f["kind"] == "audio"), None)
        member = {k: track[k] for k in ("position", "recording_id", "title")}
        member.update(song_id=None, code="not_downloaded", file=None)
        if audio:
            member["file"] = audio["path"]
            key = (artifact["recording_id"], audio["path"], audio["sha256"])
            if key not in cached:
                cached[key] = match_file(track, artifact, audio, by_path, by_size, connection)
            member.update(cached[key])
        members.append(member)
    return members


def match_file(track, artifact, audio, by_path, by_size, connection):
    path = safe_path(Path(artifact["directory"]), audio["path"])
    try:
        present = path.is_file() and path.stat().st_size == audio["size_bytes"]
    except OSError:
        present = False
    if not present:
        return {"code": "local_file_missing"}
    remote = str(PurePosixPath(connection["music_directory"]) / audio["path"])
    exact = [r for r in by_path.get(remote, []) if r.get("size") == audio["size_bytes"]]
    if len(exact) == 1:
        return {"song_id": exact[0]["id"], "code": "matched", "method": "path_size"}
    if exact:
        return {"code": "ambiguous", "candidate_ids": [r["id"] for r in exact]}
    # Navidrome normally reports a synthetic Artist/Album path. Require unique,
    # corroborated metadata AND the exact archived byte size/format in that mode.
    metadata = {
        **track,
        **(tag_identity(track) if artifact.get("managed") else {}),
        "duration_ms": artifact.get("quality", {}).get("duration_seconds", 0) * 1000,
    }
    candidates = []
    for song in by_size.get((audio["size_bytes"], path.suffix[1:].lower()), []):
        if not isinstance(song.get("duration"), (int, float)):
            continue
        if not all(isinstance(song.get(k), str) for k in ("title", "album")):
            continue
        reported = str(song.get("real_path", song.get("path", ""))).replace("\\", "/")
        if reported.startswith("/"):
            continue  # A real path pointing elsewhere is not our archived file.
        raw_artists = song.get("artists") or []
        names = (
            [a["name"] for a in raw_artists if isinstance(a, dict) and a.get("name")]
            if isinstance(raw_artists, list)
            else []
        )
        if not names:
            names = [song.get("artist", "")]
        other = {
            "title": song.get("title"),
            "artists": names,
            "album": song.get("album"),
            "duration_ms": song.get("duration", 0) * 1000,
        }
        # Integer Subsonic duration truncates the measured duration by < 1 second.
        if abs(metadata["duration_ms"] - other["duration_ms"]) < 1000:
            other["duration_ms"] = metadata["duration_ms"]
        if same_recording(metadata, other) and normalized(metadata.get("album")):
            candidates.append(song)
    if len(candidates) == 1:
        return {"song_id": candidates[0]["id"], "code": "matched", "method": "metadata_size"}
    return {
        "code": "ambiguous" if candidates else "waiting_index",
        "candidate_ids": [r["id"] for r in candidates],
    }

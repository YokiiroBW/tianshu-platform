"""Music accounts and ordered playlist snapshots in the existing media Repository."""

import json
import os
from pathlib import Path

from ...contracts import require
from ..config import text
from .catalog import Catalog, ENRICHED_FIELDS, source_key
from .providers import PROVIDERS, identity
from .quality import policy, satisfies
from .queue import enqueue_playlist
from .navidrome.state import mark_dirty


def public_account(account):
    return {
        key: account[key]
        for key in ("account_id", "provider", "user_id", "label", "state", "revision", "checked_at")
    }


def summary(playlist):
    return {key: value for key, value in playlist.items() if key != "entries"}


class MusicLibrary:
    def __init__(self, repository, accounts):
        self.repo, self.accounts = repository, accounts
        self.catalog = Catalog(repository)

    def account(self, identity):
        require(isinstance(identity, str), "invalid_input", 400)
        account = self.repo.get("music_account", identity)
        require(
            account is not None and account["state"] != "revoked", "music_account_unavailable", 409
        )
        require(account["state"] == "ready", "music_auth_required", 409)
        return account, {
            **json.loads(self.accounts.unseal(identity, account["secret"])),
            "_music_account_id": identity,
            "_music_account_revision": account["revision"],
        }

    def check_account(self, account, db):
        current = self.repo._document(db, "music_account", account["account_id"])
        require(
            current and current["revision"] == account["revision"] and current["state"] == "ready",
            "account_changed",
            409,
        )

    def save_account(self, profile, cookie, provider="netease"):
        cookie = {key: value for key, value in cookie.items() if not key.startswith("_music_")}
        require(provider in PROVIDERS, "invalid_input", 400)
        account_id = provider + ":" + identity(profile["user_id"])
        require(
            isinstance(cookie.get("MUSIC_U" if provider == "netease" else "cookie"), str)
            and cookie.get("MUSIC_U" if provider == "netease" else "cookie"),
            "music_auth_required",
            409,
        )
        with self.repo.transaction() as db:
            account = self.repo.put(
                "music_account",
                account_id,
                {
                    "account_id": account_id,
                    "provider": provider,
                    "user_id": profile["user_id"],
                    "label": profile["label"] or PROVIDERS[provider],
                    "state": "ready",
                    "checked_at": self.repo.clock(),
                    "secret": self.accounts.seal(account_id, json.dumps(cookie)),
                },
                db=db,
            )
            control = self.repo._document(db, "music_request_control", account_id)
            if control and control.get("auth_required"):
                self.repo.put(
                    "music_request_control", account_id, {**control, "auth_required": False}, db=db
                )
            return public_account(account)

    def account_state(self, account, state):
        current = self.repo.get("music_account", account["account_id"])
        if current and current["revision"] != account["revision"] and current["state"] == state:
            return current
        return self.repo.put(
            "music_account",
            account["account_id"],
            {
                **account,
                "state": state,
                "checked_at": self.repo.clock(),
                "secret": None if state == "revoked" else account["secret"],
            },
            account["revision"],
        )

    def revoke(self, body, db):
        account = self.repo._document(db, "music_account", body["account_id"])
        require(account is not None, "not_found", 404)
        account = self.repo.put(
            "music_account",
            account["account_id"],
            {
                **account,
                "state": "revoked",
                "secret": None,
                "checked_at": self.repo.clock(),
            },
            body["expected_revision"],
            db=db,
        )
        return {"account": public_account(account)}

    def settings(self):
        return self.repo.get("music_settings", "library") or {
            "revision": 0,
            "directory": "",
            "layout": "artist_album",
            "filename": "artist_title",
        }

    def save_settings(self, body, db):
        directory = text(body.get("directory"), 4096)
        require(Path(directory).is_absolute(), "music_directory_absolute", 400)
        require(body.get("layout") in {"artist_album", "album_artist_album"}, "invalid_input", 400)
        return {
            "settings": self.repo.put(
                "music_settings",
                "library",
                {
                    "directory": directory,
                    "layout": body["layout"],
                    "filename": "artist_title",
                },
                body["expected_revision"],
                db=db,
            )
        }

    def inspect_directory(self, directory):
        root = Path(text(directory, 4096))
        require(root.is_absolute(), "music_directory_absolute", 400)
        require(
            not any(p.is_symlink() or p.is_junction() for p in (root, *root.parents)),
            "unsafe_library_path",
            409,
        )
        try:
            require(root.is_dir(), "music_directory_unavailable", 409)
            rows = []
            with os.scandir(root) as artists:
                for artist in artists:
                    if len(rows) >= 12:
                        break
                    if (
                        artist.name.startswith(".")
                        or artist.name == "attachments"
                        or not artist.is_dir(follow_symlinks=False)
                        or Path(artist.path).is_junction()
                    ):
                        continue
                    with os.scandir(artist.path) as albums:
                        count = 0
                        for album in albums:
                            if count >= 2:
                                break
                            if (
                                not album.is_dir(follow_symlinks=False)
                                or Path(album.path).is_junction()
                            ):
                                continue
                            count += 1
                            with os.scandir(album.path) as files:
                                samples = []
                                for file in files:
                                    if len(samples) >= 3:
                                        break
                                    if file.is_file(follow_symlinks=False) and Path(
                                        file.name
                                    ).suffix.lower() in {
                                        ".flac",
                                        ".mp3",
                                        ".m4a",
                                        ".ogg",
                                        ".opus",
                                        ".lrc",
                                    }:
                                        samples.append(file.name)
                            rows.append(
                                {"artist": artist.name, "album": album.name, "files": samples}
                            )
            return {
                "state": "readable",
                "checked_at": self.repo.clock(),
                "samples": rows,
                "sample_only": True,
            }
        except OSError:
            return {
                "state": "unavailable",
                "checked_at": self.repo.clock(),
                "samples": [],
                "sample_only": True,
            }

    def view(self):
        return {
            "configured": True,
            "provider": "netease",
            "audio_download_available": True,
            "accounts": [public_account(row) for row in self.repo.list("music_account")],
            "playlists": [summary(row) for row in self.repo.list("music_playlist")],
            "settings": self.settings(),
        }

    def subscribe(self, account, snapshot, interval, expected, db):
        self.check_account(account, db)
        identity = account["account_id"] + ":playlist:" + snapshot["playlist"]["source_id"]
        current = self.repo._document(db, "music_playlist", identity)
        require((current["revision"] if current else 0) == expected, "version_conflict", 409)
        return self.install_snapshot(
            account,
            identity,
            snapshot,
            {
                "interval_seconds": interval,
                "state": "active",
                "revision": expected,
            },
            db,
        )

    def install_snapshot(self, account, identity, snapshot, current, db):
        self.check_account(account, db)
        for track in snapshot["tracks"]:
            track = {**track, "provider": account["provider"]}
            if track.get("availability") == "known":
                track.update(self.catalog.register(account["provider"], track, db))
            track_id = account["account_id"] + ":track:" + track["source_id"]
            previous = self.repo._document(db, "music_track", track_id)
            if previous and track["availability"] == "unavailable":
                track = {**previous, "availability": "unavailable"}
            self.repo.put("music_track", track_id, track, db=db)
        now = self.repo.clock()
        result = self.repo.put(
            "music_playlist",
            identity,
            {
                **snapshot["playlist"],
                "playlist_id": identity,
                "provider": account["provider"],
                "account_id": account["account_id"],
                "entries": snapshot["entries"],
                "member_count": len(snapshot["entries"]),
                "state": current["state"],
                "interval_seconds": current["interval_seconds"],
                "synced_at": now,
                "metadata_synced_at": now,
                "next_scan_at": now + current["interval_seconds"],
                "code": "ready",
                "complete": True,
                "auto_download": current.get("auto_download", False),
                "quality": policy(current.get("quality")),
            },
            current["revision"],
            db=db,
        )
        if result["auto_download"]:
            enqueue_playlist(self, identity, db)
        mark_dirty(self.repo, db, identity)
        return {"playlist": summary(result)}

    def complete_scan(self, account, subscription, snapshot, owner, generation):
        with self.repo.transaction() as db:
            self.repo.require_scan(db, "music:" + subscription["playlist_id"], owner, generation)
            if snapshot.get("unchanged"):
                self.check_account(account, db)
                result = self.repo.put(
                    "music_playlist",
                    subscription["playlist_id"],
                    {
                        **subscription,
                        **snapshot["playlist"],
                        "code": "ready",
                        "synced_at": self.repo.clock(),
                        "next_scan_at": self.repo.clock() + subscription["interval_seconds"],
                    },
                    subscription["revision"],
                    db=db,
                )
                return {"playlist": summary(result)}
            return self.install_snapshot(
                account, subscription["playlist_id"], snapshot, subscription, db
            )

    def control(self, body, db):
        row = self.repo._document(db, "music_playlist", body["playlist_id"])
        require(row is not None, "not_found", 404)
        require(body["action"] in {"pause", "resume"}, "invalid_input", 400)
        row = self.repo.put(
            "music_playlist",
            row["playlist_id"],
            {
                **row,
                "state": "paused" if body["action"] == "pause" else "active",
                "next_scan_at": self.repo.clock(),
            },
            body["expected_revision"],
            db=db,
        )
        return {"playlist": summary(row)}

    def source_track(self, playlist, source, db):
        track = self.repo._document(db, "music_track", playlist["account_id"] + ":track:" + source)
        binding = self.catalog.source(source_key(playlist["provider"], source), db=db)
        if binding:
            track = {**track, "recording_id": binding["recording_id"]}
            metadata = binding["metadata"]
            if metadata.get("metadata_enriched"):
                # Keep the remote snapshot and its member IDs intact. Display and
                # export the same hash-confirmed metadata used to tag the archive.
                track.update({k: metadata[k] for k in ENRICHED_FIELDS if metadata.get(k)})
        return track

    def detail(self, identity, offset, limit=100):
        require(type(offset) is int and offset >= 0, "invalid_input", 400)
        with self.repo.transaction() as db:
            row = self.repo._document(db, "music_playlist", identity)
            require(row is not None, "not_found", 404)
            tracks = []
            mapping = (self.repo._document(db, "music_playlist_jobs", identity) or {}).get(
                "jobs", {}
            )
            for index, source in enumerate(row["entries"][offset : offset + limit], offset):
                track = self.source_track(row, source, db)
                job_row = db.execute(
                    "SELECT body FROM jobs WHERE id=?", (mapping.get(source, ""),)
                ).fetchone()
                job = json.loads(job_row[0]) if job_row else None
                artifact = self.artifact(track, row["provider"], db)
                audio = next(
                    (f for f in (artifact or {}).get("files", []) if f["kind"] == "audio"), None
                )
                state = job["state"] if job else "not_downloaded"
                if audio and (
                    state in {"completed", "not_downloaded"}
                    or state in {"failed", "auth_required", "cancelled"}
                    and satisfies(policy(row.get("quality")), artifact)
                ):
                    state = "completed"
                tracks.append(
                    {
                        **track,
                        "position": index + 1,
                        "audio_state": state if audio or state != "completed" else "not_downloaded",
                        "audio_code": "music_archived"
                        if state == "completed" and audio
                        else job["code"]
                        if job
                        else None,
                        "audio_quality": artifact.get("quality") if artifact else None,
                        "audio_unlock": (artifact or {}).get("unlock")
                        if state == "completed"
                        else (job or {}).get("audio_unlock"),
                        "download_provider": artifact.get("download_provider")
                        if artifact
                        else None,
                        "download_source_id": artifact.get("download_source_id")
                        if artifact
                        else None,
                        "file": audio["path"] if audio else None,
                        "job_id": job["job_id"] if job else None,
                    }
                )
            return {
                "playlist": summary(row),
                "tracks": tracks,
                "offset": offset,
                "more": offset + len(tracks) < len(row["entries"]),
            }

    def artifact(self, track, provider, db):
        binding = self.catalog.source(source_key(provider, track["source_id"]), db=db)
        artifact = self.catalog.file(binding["recording_id"], db=db) if binding else None
        settings = self.repo._document(db, "music_settings", "library") or {}
        return (
            artifact
            if artifact
            and not artifact.get("missing")
            and artifact["directory"] == settings.get("directory")
            else None
        )

    def downloads(self, body, db):
        row = self.repo._document(db, "music_playlist", body["playlist_id"])
        require(row is not None, "not_found", 404)
        require(type(body["auto_download"]) is bool, "invalid_input", 400)
        row = self.repo.put(
            "music_playlist",
            row["playlist_id"],
            {
                **row,
                "auto_download": body["auto_download"],
                "quality": policy(body["quality"]),
            },
            body["expected_revision"],
            db=db,
        )
        result = enqueue_playlist(self, row["playlist_id"], db) if row["auto_download"] else {}
        return {"playlist": summary(row), **result}

    def download(self, body, db):
        row = self.repo._document(db, "music_playlist", body["playlist_id"])
        require(row and row["revision"] == body["expected_revision"], "version_conflict", 409)
        return enqueue_playlist(self, row["playlist_id"], db, manual=True)

    def export_snapshot(self, identity):
        with self.repo.transaction() as db:
            playlist = self.repo._document(db, "music_playlist", identity)
            require(playlist is not None, "not_found", 404)
            mapping = (self.repo._document(db, "music_playlist_jobs", identity) or {}).get(
                "jobs", {}
            )
            tracks = []
            for index, source in enumerate(playlist["entries"]):
                track = self.source_track(playlist, source, db)
                row = db.execute(
                    "SELECT body FROM jobs WHERE id=?", (mapping.get(source, ""),)
                ).fetchone()
                tracks.append(
                    {
                        "source_id": source,
                        "position": index + 1,
                        "title": track["title"],
                        "artists": track["artists"],
                        "album": track["album"],
                        "provider": playlist["provider"],
                        "recording_id": track.get("recording_id"),
                        "artifact": self.artifact(track, playlist["provider"], db),
                        "job": json.loads(row[0]) if row else None,
                    }
                )
            return playlist, tracks

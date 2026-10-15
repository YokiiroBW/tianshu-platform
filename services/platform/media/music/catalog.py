"""Recording identities and the single music-library file index in Media's ledger."""

import json
import re
import unicodedata
import uuid

from ...contracts import digest, require


def normalized(value):
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE)


def artists(track):
    return sorted({normalized(a) for a in track.get("artists", []) if normalized(a)})


def lookup_key(track):
    title, names = normalized(track.get("title")), artists(track)
    return digest({"title": title, "artists": names}) if title and names else None


def same_recording(left, right):
    """Only a unique, corroborated metadata match is eligible before hearing the audio."""
    if not lookup_key(left) or lookup_key(left) != lookup_key(right):
        return False
    durations = [value.get("duration_ms") for value in (left, right)]
    if not all(isinstance(d, (int, float)) and d > 0 for d in durations):
        return False
    if abs(durations[0] - durations[1]) > min(2000, min(durations) * 0.008):
        return False
    version_words = r"live|remaster(?:ed)?|remix|acoustic|instrumental|karaoke|现场|翻唱|重制|伴奏|纯音乐|演唱会"
    versions = [
        sorted(
            re.findall(version_words, (t.get("title", "") + " " + t.get("album", "")).casefold())
        )
        for t in (left, right)
    ]
    if versions[0] != versions[1]:
        return False
    a, b = normalized(left.get("isrc")), normalized(right.get("isrc"))
    if a and b:
        return a == b
    for key in ("disc", "track_number"):
        a, b = str(left.get(key) or "").lstrip("0"), str(right.get(key) or "").lstrip("0")
        if a and b and a != b:
            return False
    album = normalized(left.get("album"))
    return bool(album and album == normalized(right.get("album")))


def source_key(provider, identity, private_scope=""):
    return digest({"provider": provider, "id": identity, "private_scope": private_scope})


class Catalog:
    def __init__(self, repo):
        self.repo = repo

    def root_id(self, db):
        current = self.repo._document(db, "music_catalog", "library")
        if current is None:
            current = self.repo.put(
                "music_catalog", "library", {"library_id": str(uuid.uuid4())}, db=db
            )
        return current["library_id"]

    def resolve(self, identity, db):
        seen = set()
        while identity:
            require(identity not in seen and len(seen) < 32, "music_identity_conflict", 409)
            seen.add(identity)
            row = self.repo._document(db, "music_recording", identity)
            require(row is not None, "music_recording_missing", 409)
            if not row.get("merged_into"):
                return identity
            identity = row["merged_into"]
        return None

    def register(self, provider, track, db, *, private_scope=""):
        self.root_id(db)
        key = source_key(provider, track["source_id"], private_scope)
        existing = self.repo._document(db, "music_source", key)
        candidates, method = [], "source"
        if existing:
            identity = self.resolve(existing["recording_id"], db)
            method = existing.get("match_method", "source")
        else:
            match = lookup_key(track)
            indexed = self.repo._document(db, "music_match_index", match) if match else None
            candidates = sorted(
                {self.resolve(i, db) for i in (indexed or {}).get("recordings", [])}
            )
            exact = [
                i
                for i in candidates
                if same_recording(track, self.repo._document(db, "music_recording", i)["metadata"])
            ]
            if len(exact) == 1:
                identity, method = exact[0], "metadata_strict"
            else:
                identity, method = str(uuid.uuid4()), "new"
                self.repo.put(
                    "music_recording",
                    identity,
                    {"recording_id": identity, "metadata": track, "created_at": self.repo.clock()},
                    db=db,
                )
                if match:
                    self.repo.put(
                        "music_match_index",
                        match,
                        {"recordings": sorted({*(indexed or {}).get("recordings", []), identity})},
                        db=db,
                    )
        self.repo.put(
            "music_source",
            key,
            {
                "source_key": key,
                "provider": provider,
                "source_id": track["source_id"],
                "private_scope": private_scope,
                "recording_id": identity,
                "match_method": method,
                "metadata": track,
                "candidates": [i for i in candidates if i != identity],
            },
            db=db,
        )
        index = self.repo._document(db, "music_source_index", identity) or {"keys": []}
        if key not in index["keys"]:
            self.repo.put("music_source_index", identity, {"keys": [*index["keys"], key]}, db=db)
        return {"recording_id": identity, "source_key": key, "match_method": method}

    def recording(self, identity):
        with self.repo.transaction() as db:
            return self.repo._document(db, "music_recording", self.resolve(identity, db))

    def sources(self, identity, provider):
        with self.repo.transaction() as db:
            target = self.resolve(identity, db)
            index = self.repo._document(db, "music_source_index", target) or {"keys": []}
            rows = [self.source(key, db=db) for key in index["keys"]]
            return [
                row["metadata"]
                for row in rows
                if row and row["provider"] == provider and row["recording_id"] == target
            ]

    def file(self, identity, *, db=None):
        if db is None:
            with self.repo.transaction() as transaction:
                return self.file(identity, db=transaction)
        return self.repo._document(db, "music_library_file", self.resolve(identity, db))

    def content_file(self, content_id, directory):
        with self.repo.transaction() as db:
            index = self.repo._document(db, "music_content_index", content_id)
            if index:
                artifact = self.file(index["recording_id"], db=db)
                if (
                    artifact
                    and artifact["directory"] == directory
                    and artifact.get("content_id") == content_id
                ):
                    return artifact
        return None

    def store_file(self, identity, artifact, db):
        identity = self.resolve(identity, db)
        result = self.repo.put(
            "music_library_file", identity, {**artifact, "recording_id": identity}, db=db
        )
        if artifact.get("content_id"):
            self.repo.put(
                "music_content_index", artifact["content_id"], {"recording_id": identity}, db=db
            )
        return result

    def merge(self, source, target, db):
        source, target = self.resolve(source, db), self.resolve(target, db)
        if source != target:
            left = self.repo._document(db, "music_source_index", source) or {"keys": []}
            right = self.repo._document(db, "music_source_index", target) or {"keys": []}
            self.repo.put(
                "music_source_index",
                target,
                {"keys": sorted(set(left["keys"] + right["keys"]))},
                db=db,
            )
            row = self.repo._document(db, "music_recording", source)
            self.repo.put("music_recording", source, {**row, "merged_into": target}, db=db)
        return target

    def source(self, key, *, db=None):
        if db is None:
            with self.repo.transaction() as transaction:
                return self.source(key, db=transaction)
        row = self.repo._document(db, "music_source", key)
        if row:
            return {**row, "recording_id": self.resolve(row["recording_id"], db)}
        return None

    def link(self, body, db):
        source = self.repo._document(db, "music_source", body["source_key"])
        require(source is not None, "not_found", 404)
        target = self.resolve(body["recording_id"], db)
        result = self.repo.put(
            "music_source",
            source["source_key"],
            {**source, "recording_id": target, "match_method": "user", "candidates": []},
            body["expected_revision"],
            db=db,
        )
        index = self.repo._document(db, "music_source_index", target) or {"keys": []}
        self.repo.put(
            "music_source_index",
            target,
            {"keys": sorted({*index["keys"], source["source_key"]})},
            db=db,
        )
        return {"source": result}

    def view(self, offset=0, limit=100):
        require(type(offset) is int and offset >= 0, "invalid_input", 400)
        with self.repo.transaction() as db:
            rows = db.execute(
                "SELECT body FROM documents WHERE kind='music_recording' "
                "AND json_extract(body,'$.merged_into') IS NULL ORDER BY id LIMIT ? OFFSET ?",
                (limit + 1, offset),
            ).fetchall()
            items = []
            for raw in rows[:limit]:
                row = json.loads(raw[0])
                artifact = self.file(row["recording_id"], db=db)
                items.append(
                    {**row, "file": artifact if artifact and not artifact.get("missing") else None}
                )
            counts = dict(
                db.execute(
                    "SELECT kind,count(*) FROM documents WHERE kind IN "
                    "('music_recording','music_library_file','music_source','music_file_observation') GROUP BY kind"
                )
            )
            counts["music_recording"] = db.execute(
                "SELECT count(*) FROM documents WHERE kind='music_recording' AND json_extract(body,'$.merged_into') IS NULL"
            ).fetchone()[0]
            counts["music_library_file"] = db.execute(
                "SELECT count(*) FROM documents f JOIN documents r ON r.id=f.id AND r.kind='music_recording' WHERE f.kind='music_library_file' AND json_extract(r.body,'$.merged_into') IS NULL AND coalesce(json_extract(f.body,'$.missing'),0)=0"
            ).fetchone()[0]
            return {"items": items, "offset": offset, "more": len(rows) > limit, "counts": counts}

    def bootstrap(self):
        """Register old source/artifact documents without rewriting old songs or jobs."""
        with self.repo.transaction() as db:
            if (self.repo._document(db, "music_catalog", "legacy_registered") or {}).get(
                "version", 0
            ) >= 2:
                return
            rows = db.execute("SELECT id,body FROM documents WHERE kind='music_track'").fetchall()
            for row in rows:
                track = json.loads(row["body"])
                if track.get("availability") != "known":
                    continue
                provider = row["id"].split(":", 1)[0]
                linked = self.register(provider, track, db)
                self.repo.put("music_track", row["id"], {**track, **linked}, db=db)
            jobs = db.execute("SELECT body FROM jobs").fetchall()
            for raw in jobs:
                job = json.loads(raw[0])
                artifact = job.get("audio_receipt")
                if job.get("media_kind") != "audio" or job.get("audio_operation"):
                    continue
                source = self.source(
                    source_key(job.get("provider", "netease"), job["source_id"]), db=db
                )
                if source:
                    self.repo.update_job(
                        job["job_id"],
                        {
                            "recording_id": source["recording_id"],
                            "source_key": source["source_key"],
                        },
                        db=db,
                    )
                    if artifact and not self.file(source["recording_id"], db=db):
                        self.store_file(
                            source["recording_id"],
                            {
                                **artifact,
                                "managed": True,
                                "requested_quality": job.get("quality"),
                                "download_provider": job.get("provider", "netease"),
                                "download_source_id": job["source_id"],
                            },
                            db,
                        )
            self.repo.put("music_catalog", "legacy_registered", {"version": 2}, db=db)

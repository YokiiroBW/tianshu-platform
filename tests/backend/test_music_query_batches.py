"""Synthetic large-playlist reads, aliases and scheduling, including optional disposable PG."""

import json
import os
import unittest
import uuid
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import test_music_foundation as foundation
from music_fixtures import SyntheticNetease, snapshot
from services.platform import backends
from services.platform.contracts import Fault, digest
from services.platform.media.music.catalog import Catalog
from services.platform.media.music.maintenance import CHECK, SETTINGS
from services.platform.media.music.imports import safe_path, stamp
from services.platform.media.music.resolver import Resolver
from services.platform.media.music.runtime import Music
from services.platform.media.accounts import Accounts
from services.platform.media.repository import Repository


class SelectedReads:
    def __init__(self, connection, statements):
        self.connection, self.statements = connection, statements

    @property
    def row_factory(self):
        return self.connection.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self.connection.row_factory = value

    def execute(self, statement, parameters=()):
        if statement.lstrip().upper().startswith("SELECT"):
            self.statements.append(statement)
        return self.connection.execute(statement, parameters)

    def __getattr__(self, name):
        return getattr(self.connection, name)


class MusicQueryBatchTests(unittest.IsolatedAsyncioTestCase):
    subscribe = foundation.MusicFoundationTests.subscribe
    postgresql = False

    async def asyncSetUp(self):
        await foundation.MusicFoundationTests.asyncSetUp(self)
        if self.postgresql:
            runtime = backends.start(
                {
                    "database_path": str(self.repo.database),
                    "storage_backends": {
                        "postgresql_url": os.environ["TS231_TEST_POSTGRES"],
                        "namespace": "ts380_" + uuid.uuid4().hex,
                    },
                }
            )
            self.addCleanup(runtime.close)
            self.repo = self.media.repo = Repository(self.repo.database, runtime=runtime)
            self.accounts = self.media.accounts = Accounts(self.repo)
            self.music = Music(self.media)
            self.addAsyncCleanup(self.music.close)
            self.music.connector = SyntheticNetease()
            self.library = self.music.library
            runtime.finalize()
        self.directory = str(self.root)
        self.repo.put(
            "music_settings", "library", {"directory": self.directory, "layout": "artist_album"}
        )

    @contextmanager
    def reads(self):
        statements = []
        original = self.repo.backend.connect

        def connect(**kwargs):
            return SelectedReads(original(**kwargs), statements)

        with patch.object(self.repo.backend, "connect", connect):
            yield statements

    def playlist(self, count):
        value = snapshot([str(1000 + i) for i in range(count)])
        for track in value["tracks"]:
            track["duration_ms"] = 3000
        return value, self.subscribe(value)

    def archive_jobs(self, playlist):
        with self.repo.transaction() as db:
            self.library.downloads(
                {
                    "playlist_id": playlist["playlist_id"],
                    "auto_download": True,
                    "quality": {"level": "lossless", "allow_fallback": False},
                    "expected_revision": playlist["revision"],
                },
                db,
            )
            for raw in db.execute("SELECT body FROM jobs").fetchall():
                job = json.loads(raw[0])
                self.repo.update_job(job["job_id"], {"state": "completed"}, db=db)
                self.repo.put(
                    "music_library_file",
                    job["recording_id"],
                    {
                        "recording_id": job["recording_id"],
                        "directory": self.directory,
                        "quality": {"level": "lossless"},
                        "files": [{"kind": "audio", "path": job["source_id"] + ".flac"}],
                    },
                    db=db,
                )

    async def test_large_playlist_detail_export_and_catalog_use_bounded_reads(self):
        value, playlist = self.playlist(300)
        self.archive_jobs(playlist)
        with self.repo.transaction() as db:
            row = self.repo._document(db, "music_playlist", playlist["playlist_id"])
            self.repo.put(
                "music_playlist",
                playlist["playlist_id"],
                {**row, "entries": [*row["entries"], row["entries"][0]]},
                db=db,
            )
        with self.reads() as queries:
            detail = self.library.detail(playlist["playlist_id"], 0, 400)
        self.assertLessEqual(len(queries), 13)
        self.assertEqual(len(detail["tracks"]), 301)
        self.assertEqual(detail["tracks"][-1]["position"], 301)
        self.assertEqual(detail["tracks"][0]["file"], detail["tracks"][-1]["file"])
        self.assertEqual({t["audio_state"] for t in detail["tracks"]}, {"completed"})
        with self.reads() as queries:
            _, exported = self.library.export_snapshot(playlist["playlist_id"])
        self.assertLessEqual(len(queries), 13)
        self.assertEqual([t["source_id"] for t in exported[:-1]], value["entries"])
        self.assertTrue(all(t["artifact"] and t["job"] for t in exported))
        with self.reads() as queries:
            catalog = self.library.catalog.view(0, 400)
        self.assertLessEqual(len(queries), 8)
        self.assertEqual(len(catalog["items"]), 300)

    async def test_alias_missing_and_wrong_directory_semantics_are_shared_by_batch_reads(self):
        _, playlist = self.playlist(3)
        self.archive_jobs(playlist)
        tracks = self.library.detail(playlist["playlist_id"], 0)["tracks"]
        first, second, third = [t["recording_id"] for t in tracks]
        with self.repo.transaction() as db:
            self.library.catalog.merge(first, second, db)
            artifact = self.repo._document(db, "music_library_file", third)
            self.repo.put(
                "music_library_file", third, {**artifact, "directory": "elsewhere"}, db=db
            )
            job_id = tracks[0]["job_id"]
            self.repo.put("deleted_job", job_id, {"deleted_at": self.repo.clock()}, db=db)
        detail = self.library.detail(playlist["playlist_id"], 0)["tracks"]
        self.assertEqual(detail[0]["recording_id"], second)
        self.assertEqual(detail[0]["file"], detail[1]["file"])
        self.assertEqual(detail[0]["job_id"], job_id)
        self.assertIsNone(detail[2]["file"])
        self.assertEqual(detail[2]["audio_state"], "not_downloaded")
        with self.repo.transaction() as db:
            row = self.repo._document(db, "music_recording", second)
            self.repo.put("music_recording", second, {**row, "merged_into": first}, db=db)
        for read in (
            lambda: self.library.detail(playlist["playlist_id"], 0),
            lambda: self.library.catalog.file(first),
        ):
            with self.assertRaises(Fault) as caught:
                read()
            self.assertEqual(caught.exception.code, "music_identity_conflict")

    async def test_maintenance_scans_in_batches_then_queues_one_due_recording(self):
        _, playlist = self.playlist(48)
        self.archive_jobs(playlist)
        with self.repo.transaction() as db:
            self.repo.put(
                SETTINGS,
                "settings",
                {"enabled": True, "enabled_at": self.repo.clock(), "interval_seconds": 604800},
                db=db,
            )
            jobs = [json.loads(row[0]) for row in db.execute("SELECT body FROM jobs")]
            for job in jobs:
                key, _ = self.music.maintenance.key(job, db)
                self.repo.put(
                    CHECK,
                    key,
                    {"next_check_at": self.repo.clock() + 600, "job_id": job["job_id"]},
                    db=db,
                )
        with self.reads() as queries:
            result = self.music.maintenance.plan()
        self.assertEqual(result, {"state": "scheduled", "recordings": 48})
        self.assertLessEqual(len(queries), 10)
        chosen = jobs[0]
        with self.repo.transaction() as db:
            key, _ = self.music.maintenance.key(chosen, db)
            self.repo.put(CHECK, key, {"next_check_at": 0}, db=db)
        self.assertEqual(self.music.maintenance.plan()["job_id"], chosen["job_id"])
        queued, _ = self.repo.jobs(states={"queued"})
        self.assertEqual([job["job_id"] for job in queued], [chosen["job_id"]])
        self.assertEqual(queued[0]["generation"], chosen["generation"])

    async def test_resolver_policy_uses_one_request_snapshot_and_retains_strict_membership(self):
        _, playlist = self.playlist(1)
        self.archive_jobs(playlist)
        job = self.repo.jobs()[0][0]
        keys = []
        with self.repo.transaction() as db:
            for i in range(40):
                identity, source = "policy-" + str(i), "1000"
                quality = {"level": "lossless" if i == 7 else "standard", "allow_fallback": False}
                self.repo.put(
                    "music_playlist", identity, {"entries": [source], "quality": quality}, db=db
                )
                key = digest(identity)
                keys.append(key)
                self.repo.put(
                    "music_request",
                    key,
                    {"playlist_id": identity, "source_id": source, "quality": quality},
                    db=db,
                )
            self.repo.update_job(job["job_id"], {"request_ids": keys, "request_version": 41}, db=db)
        with self.reads() as queries:
            quality, version = Resolver(self.music).requests(job)
        self.assertEqual(len(queries), 3)
        self.assertEqual((quality["minimum_level"], version), (3, 41))
        self.repo.put("music_playlist", "policy-7", {"entries": [], "quality": quality})
        self.assertEqual(Resolver(self.music).requests(job)[0]["minimum_level"], 0)

    async def test_snapshot_registers_each_track_once_and_queue_stays_idempotent(self):
        value, playlist = self.playlist(8)
        with self.repo.transaction() as db:
            current = self.repo._document(db, "music_playlist", playlist["playlist_id"])
            current = self.repo.put(
                "music_playlist", playlist["playlist_id"], {**current, "auto_download": True}, db=db
            )
            registered = []
            original = Catalog.register

            def register(catalog, provider, track, connection, **kwargs):
                registered.append(track["source_id"])
                return original(catalog, provider, track, connection, **kwargs)

            with patch.object(Catalog, "register", register):
                self.library.install_snapshot(
                    self.account, playlist["playlist_id"], value, current, db
                )
        self.assertEqual(registered, value["entries"])
        before = self.repo.jobs()[0]
        with self.repo.transaction() as db:
            current = self.repo._document(db, "music_playlist", playlist["playlist_id"])
            self.library.install_snapshot(self.account, playlist["playlist_id"], value, current, db)
        after = self.repo.jobs()[0]
        self.assertEqual(
            [(j["job_id"], j["request_version"]) for j in before],
            [(j["job_id"], j["request_version"]) for j in after],
        )

    async def test_unchanged_index_scan_batches_observations_and_aliases(self):
        _, playlist = self.playlist(300)
        self.archive_jobs(playlist)
        with self.repo.transaction() as db:
            for i in range(300):
                relative = str(1000 + i) + ".flac"
                path = self.root / relative
                path.write_bytes(b"synthetic-index-fixture")
                self.repo.put(
                    "music_file_observation",
                    digest({"directory": self.directory, "path": relative}),
                    {
                        "directory": self.directory,
                        "path": relative,
                        "state": "indexed",
                        **stamp(path),
                    },
                    db=db,
                )
        update = AsyncMock()
        with (
            self.reads() as queries,
            patch.object(self.music.imports, "index", AsyncMock()) as index,
        ):
            await self.music.imports.scan(
                {"destination_directory": self.directory, "job_id": "synthetic-scan"}, update
            )
        index.assert_not_awaited()
        self.assertLessEqual(len(queries), 10)
        self.assertEqual(self.music.imports.view()["unchanged"], 300)
        self.assertEqual(self.music.imports.view()["failed"], 0)

    async def test_missing_scan_cannot_replace_a_new_publication_after_its_read_phase(self):
        _, playlist = self.playlist(1)
        self.archive_jobs(playlist)
        artifact = self.repo.list("music_library_file")[0]
        replacement = {**artifact, "files": [{"kind": "audio", "path": "new.flac"}]}
        (self.root / "new.flac").write_bytes(b"new-synthetic-publication")
        published = []

        def publish_before_stat(root, relative):
            # The actual filesystem phase must be outside its media transaction. Publishing
            # here would deadlock if that writer lock were still held by the scanner.
            if not published:
                published.append(
                    self.repo.put("music_library_file", artifact["recording_id"], replacement)
                )
            return safe_path(root, relative)

        with patch("services.platform.media.music.imports.safe_path", publish_before_stat):
            self.music.imports.mark_missing(self.root, set())
        self.assertEqual(self.library.catalog.file(artifact["recording_id"]), published[0])


@unittest.skipUnless(
    os.environ.get("TS231_TEST_POSTGRES"), "requires disposable TS231_TEST_POSTGRES"
)
class PostgreSQLMusicQueryBatchTests(MusicQueryBatchTests):
    postgresql = True

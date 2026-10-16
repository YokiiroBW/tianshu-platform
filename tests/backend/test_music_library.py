import asyncio
import base64
import copy
import json
import os
import struct
import unittest

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from mutagen.flac import FLAC

import test_music_downloads as fixtures
from services.platform.contracts import Fault
from services.platform.media.music.catalog import lookup_key, same_recording, search_title
from services.platform.media.music.quality import aggregate, requested_levels
from services.platform.media.music.imports import metadata
from unittest.mock import patch


def encrypted_ncm(audio, title, artists, album, image):
    """Synthetic original: generated sine audio, never a commercial recording."""

    def aes(value, key):
        pad = padding.PKCS7(128).padder()
        encrypt = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        return encrypt.update(pad.update(value) + pad.finalize()) + encrypt.finalize()

    key = b"synthetic-stream-key"
    core = bytes(v ^ 0x64 for v in aes(b"neteasecloudmusic" + key, b"hzHRAmso5kInbaxW"))
    metadata = json.dumps(
        {"format": "flac", "musicName": title, "artist": [[a, 1] for a in artists], "album": album}
    ).encode()
    meta = bytes(
        v ^ 0x63
        for v in b"163 key(Don't modify):"
        + base64.b64encode(aes(b"music:" + metadata, b"#14ljk_!\\]&0U<'("))
    )
    box, j = list(range(256)), 0
    for i in range(256):
        j = (box[i] + j + key[i % len(key)]) & 255
        box[i], box[j] = box[j], box[i]
    mask = [
        box[(box[(i + 1) & 255] + box[((i + 1) + box[(i + 1) & 255]) & 255]) & 255]
        for i in range(256)
    ]
    payload = bytes(value ^ mask[index & 255] for index, value in enumerate(audio))
    return (
        b"CTENFDAM\x00\x00"
        + struct.pack("<I", len(core))
        + core
        + struct.pack("<I", len(meta))
        + meta
        + bytes(5)
        + struct.pack("<II", len(image), len(image))
        + image
        + payload
    )


class MusicLibraryTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(fixtures.MusicDownloadTests.setUpClass.__func__)
    asyncSetUp = fixtures.MusicDownloadTests.asyncSetUp
    fixture_url = fixtures.MusicDownloadTests.fixture_url
    value = fixtures.MusicDownloadTests.value
    subscribe = fixtures.MusicDownloadTests.subscribe
    queue = fixtures.MusicDownloadTests.queue
    run_job = fixtures.MusicDownloadTests.run_job
    file = fixtures.MusicDownloadTests.file

    def test_versions_and_ambiguous_missing_metadata_are_not_merged(self):
        original = self.api.value["tracks"][0]
        for changes in (
            {"title": original["title"] + " (Live)"},
            {"album": "另一版本"},
            {"duration_ms": 14000},
            {"artists": ["翻唱歌手"]},
            {"duration_ms": None},
        ):
            self.assertFalse(same_recording(original, {**original, **changes}))
        matched = {**original, "isrc": "CN-A01-26-00001"}
        self.assertTrue(same_recording(matched, {**matched, "album": "精选集", "track_number": 9}))

    def test_joined_credits_and_album_advisory_match_without_merging_other_editions(self):
        original = {
            **self.api.value["tracks"][0],
            "artists": ["Artist A/Artist B"],
            "album": "Album (Explicit)",
        }
        candidate = {**original, "artists": ["Artist B", "Artist A"], "album": "Album"}
        self.assertEqual(lookup_key(original), lookup_key(candidate))
        self.assertTrue(same_recording(original, candidate))
        for changes in (
            {"album": "Album (Clean)"},
            {"album": "Album (Live)"},
            {"album": "Another Album"},
            {"duration_ms": None},
            {"artists": ["Artist C"]},
        ):
            self.assertFalse(same_recording(original, {**candidate, **changes}))
        with self.repo.transaction() as db:
            first = self.library.catalog.register("kugou", original, db)
            second = self.library.catalog.register("qq", {**candidate, "source_id": "qq-other"}, db)
        self.assertEqual(first["recording_id"], second["recording_id"])

    def test_same_hash_metadata_enrichment_keeps_recording_and_enables_cross_source_binding(self):
        original = {**self.api.value["tracks"][0], "artists": [], "album": "", "duration_ms": None}
        enriched = {**self.api.value["tracks"][0], "metadata_enriched": True}
        with self.repo.transaction() as db:
            first = self.library.catalog.register("kugou", original, db)
            refreshed = self.library.catalog.register("kugou", enriched, db)
            second = self.library.catalog.register(
                "qq", {**enriched, "source_id": "qq-enriched", "metadata_enriched": False}, db
            )
        self.assertEqual(
            self.library.catalog.recording(first["recording_id"])["recording_id"],
            refreshed["recording_id"],
        )
        self.assertEqual(refreshed["recording_id"], second["recording_id"])

    def test_existing_unarchived_legacy_identity_reindexes_and_reuses_unique_recording(self):
        original = {
            **self.api.value["tracks"][0],
            "title": "Duet",
            "artists": ["A/B"],
            "album": "Record (Explicit)",
        }
        candidate = {
            **original,
            "source_id": "other-duet",
            "artists": ["A", "B"],
            "album": "Record",
        }
        with self.repo.transaction() as db:
            first = self.library.catalog.register("kugou", original, db)
            db.execute(
                "DELETE FROM documents WHERE kind='music_match_index' AND id=?",
                (lookup_key(original),),
            )
            second = self.library.catalog.register("netease", candidate, db)
            self.assertNotEqual(first["recording_id"], second["recording_id"])
            refreshed = self.library.catalog.register("kugou", original, db)
        self.assertEqual(refreshed["recording_id"], second["recording_id"])
        self.assertEqual(
            self.library.catalog.recording(first["recording_id"])["recording_id"],
            second["recording_id"],
        )

    def test_enrichment_preserves_existing_archive_and_explicit_identity(self):
        for protection in ("archive", "user"):
            original = {
                **self.api.value["tracks"][0],
                "source_id": protection,
                "album": "",
                "duration_ms": None,
            }
            enriched = {
                **self.api.value["tracks"][0],
                "source_id": protection,
                "metadata_enriched": True,
            }
            with self.repo.transaction() as db:
                first = self.library.catalog.register("kugou", original, db)
                if protection == "archive":
                    self.repo.put(
                        "music_library_file",
                        first["recording_id"],
                        {"quality": {"level": "lossless"}},
                        db=db,
                    )
                else:
                    source = self.repo._document(db, "music_source", first["source_key"])
                    self.repo.put(
                        "music_source",
                        first["source_key"],
                        {**source, "match_method": "user"},
                        db=db,
                    )
                refreshed = self.library.catalog.register("kugou", enriched, db)
            self.assertEqual(first["recording_id"], refreshed["recording_id"])

    def test_strict_playlist_quality_is_not_weakened_by_another_playlist(self):
        combined = aggregate(
            [
                {"level": "best", "allow_fallback": True},
                {"level": "lossless", "allow_fallback": False},
            ]
        )
        self.assertEqual(requested_levels(combined), ["hires", "lossless"])

    def test_filename_title_reindexes_existing_source_without_merging_other_versions(self):
        original = {**self.api.value["tracks"][0], "title": "A recording.mp3"}
        candidate = {**original, "source_id": "qq-matching", "title": "A recording"}
        with self.repo.transaction() as db:
            binding = self.library.catalog.register("kugou", original, db)
            # Simulate the old lookup index while preserving the existing recording.
            db.execute(
                "DELETE FROM documents WHERE kind='music_match_index' AND id=?",
                (lookup_key(original),),
            )
            again = self.library.catalog.register("kugou", original, db)
            matched = self.library.catalog.register("qq", candidate, db)
        self.assertEqual(binding["recording_id"], again["recording_id"])
        self.assertEqual(binding["recording_id"], matched["recording_id"])
        self.assertEqual(search_title(original), "A recording")
        self.assertTrue(same_recording(original, candidate))
        self.assertFalse(same_recording(original, {**candidate, "title": "A recording (Live)"}))
        self.assertFalse(same_recording(original, {**candidate, "album": "Different edition"}))

    async def test_archive_refreshes_shared_exports_without_rewriting_unrelated_playlists(self):
        shared = self.value(["101"])
        shared["playlist"]["source_id"] = "901"
        shared = self.subscribe(shared)
        unrelated = self.value(["201"])
        unrelated["playlist"]["source_id"] = "902"
        unrelated = self.subscribe(unrelated)
        storage = self.music.downloads.storage
        for playlist in (self.playlist, shared, unrelated):
            storage.export(playlist["playlist_id"])
        self.queue()
        with patch.object(storage, "export", wraps=storage.export) as exported:
            result = await self.run_job()
        self.assertEqual(result["state"], "completed", result.get("code"))
        self.assertEqual(
            {call.args[0] for call in exported.call_args_list},
            {self.playlist["playlist_id"], shared["playlist_id"]},
        )

    async def test_existing_tagged_file_is_indexed_and_reused_without_modification(self):
        path = self.library_root / "Music Tag Web 原件.flac"
        path.write_bytes(self.audio["flac"])
        tags = FLAC(path)
        track = self.api.value["tracks"][0]
        for key, value in {
            "title": track["title"],
            "artist": track["artists"],
            "album": track["album"],
        }.items():
            tags[key] = value
        tags.save()
        before = path.read_bytes()
        self.queue()
        result = await self.run_job()
        self.assertEqual(result["state"], "completed", result.get("code"))
        self.assertTrue(result["audio_reused"])
        self.assertEqual(self.audio_transfers, 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.file(result), path)
        path.rename(self.library_root / "改名原件.flac")
        self.music.imports.enqueue()
        scan = await self.run_job()
        self.assertEqual(scan["state"], "completed", scan.get("code"))
        detail = self.library.detail(self.playlist["playlist_id"], 0)
        self.assertEqual(detail["tracks"][0]["file"], "改名原件.flac")

    async def test_enrichment_link_reuses_existing_audio_before_full_transfer(self):
        self.queue()
        first = await self.run_job()
        before = self.audio_transfers
        original = copy.deepcopy(self.api.value["tracks"][0])
        client = fixtures.SyntheticNetease()

        async def enrich(value, cookie):
            return {
                **original,
                "source_id": value["source_id"],
                "provider": "kugou",
                "metadata_enriched": True,
            }

        client.enrich, client.download, client.lyrics = enrich, self.api.download, self.api.lyrics
        self.music.connectors["kugou"] = client
        public = self.library.save_account(
            {"user_id": "456", "label": "Kugou"}, {"cookie": "fixture"}, "kugou"
        )
        account = self.repo.get("music_account", public["account_id"])
        value = self.value(["101"])
        value["tracks"][0].update(source_id="kugou101", artists=[], album="", duration_ms=None)
        value["entries"] = ["kugou101"]
        with self.repo.transaction() as db:
            playlist = self.library.subscribe(account, value, 3600, 0, db)["playlist"]
        self.queue(playlist)
        recovered = await self.run_job()
        self.assertEqual(recovered["state"], "completed", recovered.get("code"))
        self.assertTrue(recovered["audio_reused"])
        self.assertEqual(self.audio_transfers, before)
        self.assertEqual(self.file(first), self.file(recovered))
        self.assertEqual(len(list(self.library_root.rglob("*.flac"))), 1)
        detail = self.library.detail(playlist["playlist_id"], 0)["tracks"][0]
        exported = self.library.export_snapshot(playlist["playlist_id"])[1][0]
        self.assertEqual(detail["artists"], original["artists"])
        self.assertEqual(exported["artists"], original["artists"])
        self.assertEqual(exported["recording_id"], first["audio_receipt"]["recording_id"])
        # The next automatic cloud snapshot still contains the sparse original.
        # It must not make an archived song disappear from the Navidrome export.
        with self.repo.transaction() as db:
            self.library.catalog.register("kugou", value["tracks"][0], db)
        again = self.library.export_snapshot(playlist["playlist_id"])[1][0]
        self.assertEqual(again["artists"], original["artists"])
        self.assertEqual(again["album"], original["album"])
        self.assertEqual(again["recording_id"], exported["recording_id"])

    async def test_real_decoder_ncm_import_reuses_recording_and_retains_original(self):
        if not os.environ.get("TIANSHU_MUSICDECODE"):
            self.skipTest("Real pinned decoder executable required")
        inbox = self.library_root / ".incoming"
        inbox.mkdir()
        track = self.api.value["tracks"][0]
        raw = encrypted_ncm(
            self.audio["flac"], track["title"], track["artists"], track["album"], self.image
        )
        path = inbox / "synthetic.ncm"
        path.write_bytes(raw)
        self.music.imports.enqueue()
        scan = await self.run_job()
        self.assertEqual(scan["state"], "completed", scan.get("code"))
        imported = await self.run_job()
        self.assertEqual(imported["state"], "completed", imported.get("code"))
        self.assertEqual(FLAC(self.file(imported))["title"], [track["title"]])
        self.assertEqual(FLAC(self.file(imported))["artist"], track["artists"])
        self.assertEqual(path.read_bytes(), raw)
        self.queue()
        downloaded = await self.run_job()
        self.assertTrue(downloaded["audio_reused"])
        self.assertEqual(self.audio_transfers, 0)
        self.assertEqual(self.file(imported), self.file(downloaded))
        self.assertEqual(len(list(self.library_root.rglob("*.flac"))), 1)

    async def test_cross_platform_search_rejects_wrong_recording(self):
        original = copy.deepcopy(self.api.value["tracks"][0])

        async def denied(*args):
            raise Fault("music_download_forbidden", 409)

        self.api.download = denied
        client = fixtures.SyntheticNetease()

        async def search(query, cookie):
            return [{**original, "title": original["title"] + " (Live)", "source_id": "wrong"}]

        async def should_not_download(*args):
            self.fail("Wrong recording must not be downloaded")

        client.search, client.download = search, should_not_download
        self.music.connectors["qq"] = client
        self.library.save_account({"user_id": "456", "label": "QQ"}, {"cookie": "fixture"}, "qq")
        self.queue()
        job = await self.run_job()
        self.assertEqual(job["code"], "music_download_forbidden")
        self.assertFalse(list(self.library_root.rglob("*.flac")))

    async def test_quality_upgrade_replaces_only_managed_audio_and_updates_each_playlist(self):
        self.queue()
        first = await self.run_job()
        old_path = self.file(first)
        value = self.value(["101"])
        value["playlist"]["source_id"] = "902"
        second_playlist = self.subscribe(value)
        with self.repo.transaction() as db:
            second_playlist = self.library.downloads(
                {
                    "playlist_id": second_playlist["playlist_id"],
                    "expected_revision": second_playlist["revision"],
                    "auto_download": False,
                    "quality": {"level": "hires", "allow_fallback": False},
                },
                db,
            )["playlist"]
        self.file_bytes, self.level = self.audio["hires"], "hires"
        self.queue(second_playlist)
        upgraded = await self.run_job()
        self.assertEqual(upgraded["state"], "completed", upgraded.get("code"))
        self.assertEqual(upgraded["actual_quality"], "hires")
        self.assertFalse(old_path.exists())
        self.assertEqual(len(list(self.library_root.rglob("*.flac"))), 1)
        for playlist in (self.playlist, second_playlist):
            detail = self.library.detail(playlist["playlist_id"], 0)
            self.assertEqual(
                detail["tracks"][0]["file"],
                self.file(upgraded).relative_to(self.library_root).as_posix(),
            )
            self.assertEqual(detail["tracks"][0]["audio_quality"]["level"], "hires")

    async def test_corrupt_encrypted_input_fails_and_is_retained(self):
        if not os.environ.get("TIANSHU_MUSICDECODE"):
            self.skipTest("Real pinned decoder executable required")
        inbox = self.library_root / ".incoming"
        inbox.mkdir()
        path = inbox / "corrupt.ncm"
        original = b"CTENFDAM\x00\x00\xff\xff\xff\xff"
        path.write_bytes(original)
        self.music.imports.enqueue()
        await self.run_job()
        failed = await self.run_job()
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["code"], "music_decode_failed")
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(list(self.library_root.rglob("*.flac")))

    async def test_higher_quality_request_during_download_is_not_lost(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.api.download

        async def gated(*args):
            entered.set()
            await release.wait()
            return await original(*args)

        self.api.download = gated
        self.queue()
        task = asyncio.create_task(self.run_job())
        await entered.wait()
        with self.repo.transaction() as db:
            self.library.downloads(
                {
                    "playlist_id": self.playlist["playlist_id"],
                    "expected_revision": self.playlist["revision"],
                    "auto_download": True,
                    "quality": {"level": "hires", "allow_fallback": False},
                },
                db,
            )
        release.set()
        first = await task
        self.assertEqual(first["state"], "queued")
        self.api.download = original
        self.file_bytes, self.level = self.audio["hires"], "hires"
        second = await self.run_job()
        self.assertEqual(second["state"], "completed", second.get("code"))
        self.assertEqual(second["actual_quality"], "hires")
        self.assertEqual(len(list(self.library_root.rglob("*.flac"))), 1)

    async def test_deployed_legacy_artifact_is_adopted_without_rewriting_or_downloading(self):
        self.queue()
        first = await self.run_job()
        original = self.file(first).read_bytes()
        # Explicit isolated pre-catalog fixture; never run this against production.
        with self.repo.transaction() as db:
            db.execute(
                "DELETE FROM documents WHERE kind IN ('music_catalog','music_recording','music_source','music_match_index','music_source_index','music_library_file','music_content_index','music_import_scan','music_file_observation')"
            )
            artifact = {
                k: v
                for k, v in first["audio_receipt"].items()
                if k
                not in {
                    "content_id",
                    "recording_id",
                    "download_provider",
                    "download_source_id",
                    "requested_quality",
                }
            }
            self.repo.update_job(
                first["job_id"], {"recording_id": None, "audio_receipt": artifact}, db=db
            )
        self.library.catalog.bootstrap()
        adopted = self.repo.job(first["job_id"])
        self.assertTrue(adopted["recording_id"])
        self.queue()
        reused = await self.run_job()
        self.assertEqual(reused["state"], "completed", reused.get("code"))
        self.assertTrue(reused["audio_reused"])
        self.assertEqual(self.audio_transfers, 1)
        self.assertEqual(self.file(reused).read_bytes(), original)
        self.assertTrue(self.library.catalog.file(reused["recording_id"])["content_id"])

    async def test_failed_strict_upgrade_keeps_other_playlist_archived(self):
        self.queue()
        first = await self.run_job()
        value = self.value(["101"])
        value["playlist"]["source_id"] = "902"
        playlist = self.subscribe(value)
        with self.repo.transaction() as db:
            self.library.downloads(
                {
                    "playlist_id": playlist["playlist_id"],
                    "expected_revision": playlist["revision"],
                    "auto_download": True,
                    "quality": {"level": "hires", "allow_fallback": False},
                },
                db,
            )
        failed = await self.run_job()
        self.assertEqual(failed["state"], "failed")
        low = self.library.detail(self.playlist["playlist_id"], 0)["tracks"][0]
        high = self.library.detail(playlist["playlist_id"], 0)["tracks"][0]
        self.assertEqual(low["audio_state"], "completed")
        self.assertEqual(high["audio_state"], "failed")
        self.assertEqual(low["file"], high["file"])
        self.assertTrue(self.file(first).is_file())

    async def test_unchanged_file_is_retried_after_access_failure(self):
        path = self.library_root / "original.flac"
        path.write_bytes(self.audio["flac"])
        self.music.imports.enqueue()

        async def unavailable(*args):
            raise Fault("music_file_unreadable", 409)

        with patch.object(self.music.imports, "index", unavailable):
            await self.run_job()
        self.assertEqual(self.music.imports.view()["failed"], 1)
        self.music.imports.enqueue()
        await self.run_job()
        self.assertEqual(self.music.imports.view()["failed"], 0)
        self.assertEqual(self.music.imports.view()["indexed"], 1)

    def test_untagged_decoded_audio_uses_original_filename(self):
        path = self.root / "decoded.part"
        path.write_bytes(self.audio["flac"])
        self.assertEqual(metadata(path, "fixture", "原始歌名")["title"], "原始歌名")

    @unittest.skipIf(os.name == "nt", "POSIX read-permission recovery runs in Linux candidate")
    async def test_real_file_permission_failure_is_retryable_without_audio_change(self):
        path = self.library_root / "permission.flac"
        path.write_bytes(self.audio["flac"])
        before = path.stat().st_mtime_ns
        path.chmod(0)
        self.addCleanup(path.chmod, 0o600)
        self.music.imports.enqueue()
        await self.run_job()
        observation = self.repo.list("music_file_observation")[0]
        self.assertEqual(observation["code"], "music_file_unreadable")
        path.chmod(0o600)
        self.assertEqual(path.stat().st_mtime_ns, before)
        self.music.imports.enqueue()
        await self.run_job()
        self.assertEqual(self.music.imports.view()["failed"], 0)
        self.assertEqual(self.music.imports.view()["indexed"], 1)

    async def test_parallel_songs_share_publication_without_busy_failures(self):
        value = self.value([str(200 + i) for i in range(8)])
        value["playlist"]["source_id"] = "parallel"
        playlist = self.subscribe(value)
        self.queue(playlist)
        jobs = [self.repo.claim("parallel-" + str(i)) for i in range(8)]
        await asyncio.gather(
            *(self.media.worker.run(job, "parallel-" + str(i)) for i, job in enumerate(jobs))
        )
        self.assertTrue(all(self.repo.job(job["job_id"])["state"] == "completed" for job in jobs))
        self.assertEqual(len(list(self.library_root.rglob("*.flac"))), 1)

    async def test_external_library_busy_is_waiting_not_a_permanent_download_failure(self):
        self.queue()
        job = self.repo.claim("fixture")
        job = self.repo.update_job(
            job["job_id"], {"attempts": 4}, owner="fixture", generation=job["generation"]
        )

        def busy(*args):
            raise Fault("busy", 409)

        with patch.object(self.music.downloads.storage, "publish", busy):
            await self.media.worker.run(job, "fixture")
        result = self.repo.job(job["job_id"])
        self.assertEqual(result["state"], "retry_wait")
        self.assertEqual(result["code"], "busy")
        self.assertEqual(result["attempts"], 4)

    async def test_completed_generation_cleanup_preserves_new_worker_staging(self):
        self.queue()
        finished = await self.run_job()
        next_job = {**finished, "generation": finished["generation"] + 1}
        staging = self.music.downloads.storage.stage(next_job, "source.part")
        staging.write_bytes(b"next-generation-owned-transfer")
        self.music.downloads.storage.cleanup(finished)
        self.assertEqual(staging.read_bytes(), b"next-generation-owned-transfer")
        self.assertEqual(self.repo.get("music_stage", finished["job_id"])["state"], "retained")

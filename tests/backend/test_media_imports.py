"""In-place inventory uses isolated files/evidence and the real SQLite/pin owners."""

import asyncio
import copy
import hashlib
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services.platform.contracts import Fault
from services.platform.media.config import configuration
from services.platform.media.download_cache import DownloadCache
from services.platform.media.engine import Engine, run_process
from services.platform.media.import_records import manifest_entries, observation
from services.platform.media.imports import Imports, item_id
from services.platform.media.repository import Repository
from services.platform.media.retention import Retention

BV = "BV1xx411c7mD"
CID = "123"
INFO = {
    "format": {"duration": "2"},
    "streams": [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "hevc",
            "width": 3840,
            "height": 2160,
            "r_frame_rate": "60000/1001",
            "color_transfer": "smpte2084",
            "color_primaries": "bt2020",
            "side_data_list": [{"side_data_type": "DOVI configuration record", "dv_profile": 8}],
        },
        {"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 2},
        {"index": 2, "codec_type": "audio", "codec_name": "eac3", "channels": 6},
    ],
}


async def local(function, *args, **kwargs):
    return await asyncio.to_thread(function, *args, **kwargs)


class MediaImportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="media-import-synthetic-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.imported = self.root / "old-library"
        self.imported.mkdir()
        self.media = SimpleNamespace(
            config={
                "staging_directory": str(self.root / "staging"),
                "targets": {},
                "import_roots": [
                    {
                        "root_id": "legacy",
                        "label": "Synthetic old library",
                        "directory": str(self.imported),
                    }
                ],
            },
            repo=Repository(self.root / "media.sqlite"),
            local=local,
            engine=Engine({"ffprobe": os.environ.get("MEDIA_TEST_FFPROBE", "ffprobe")}),
        )
        self.media.engine.probe = AsyncMock(return_value=copy.deepcopy(INFO))
        self.media.engine.download = AsyncMock(side_effect=AssertionError("must reuse"))
        self.media.retention = Retention(self.media)
        self.media.imports = Imports(self.media)

    def video(self, relative="BV1xx411c7mD/P1.mp4", content=b"synthetic old media bytes"):
        path = self.imported.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def entry(self, path, *, quality=None, binding="exact_path"):
        relative = path.relative_to(self.imported).as_posix()
        identity = {
            "kind": "bili_sync_page",
            "binding": binding,
            "relative_path": relative,
            "bvid": BV,
            "cid": CID,
            "part_index": 1,
            "source_digest": "a" * 64,
        }
        if binding == "unique_bv_part":
            identity["expected_duration_seconds"] = 2
        stat = path.stat()
        return {
            "relative_path": relative,
            "bvid": BV,
            "cid": CID,
            "part_index": 1,
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "identity_evidence": identity,
            "quality_evidence": None
            if quality is None
            else {
                "kind": "actual_download",
                "quality": quality,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size_bytes": stat.st_size,
                "source_digest": "b" * 64,
            },
        }

    async def reconcile(self, *entries):
        return await self.media.imports.reconcile(
            "legacy", evidence_manifest={"schema_version": 1, "entries": list(entries)}
        )

    def record(self, path):
        return self.media.repo.get(
            "media_import", item_id("legacy", path.relative_to(self.imported).as_posix())
        )

    async def test_unknown_full_inventory_has_actual_streams_without_hash_or_publication(self):
        path = self.video()
        before = observation(path)
        files_before = sorted(
            p.relative_to(self.imported).as_posix() for p in self.imported.rglob("*")
        )
        with patch(
            "services.platform.media.imports.file_record",
            side_effect=AssertionError("unknown need not hash"),
        ):
            result = await self.reconcile(self.entry(path, binding="unique_bv_part"))
        self.assertEqual(
            (1, 1, 0, 1, "completed"),
            tuple(
                result[k] for k in ("registered", "cid_confirmed", "verified", "unknown", "state")
            ),
        )
        record = self.record(path)
        self.assertEqual("unknown", record["state"])
        self.assertEqual(CID, record["cid"])
        self.assertIsNone(record["actual_quality"])
        self.assertIsNone(record["sha256"])
        self.assertEqual(["actual_quality"], record["missing_evidence"])
        self.assertEqual(INFO["streams"], record["media"]["rawffprobe_streams"])
        self.assertEqual("smpte2084", record["media"]["video_streams"][0]["color_transfer"])
        self.assertEqual(
            ["aac", "eac3"], [s["codec_name"] for s in record["media"]["audio_tracks"]]
        )
        self.assertEqual(before, observation(path))
        self.assertEqual(
            files_before,
            sorted(p.relative_to(self.imported).as_posix() for p in self.imported.rglob("*")),
        )
        self.assertIsNone(self.media.retention.source(BV, CID, "120"))
        self.assertEqual([], self.media.repo.list("media_source"))
        self.assertEqual([], self.media.repo.list("file_transfer"))
        self.assertEqual([], self.media.repo.list("target_head"))
        self.assertEqual(0, self.media.repo.job_count())

    async def test_only_regular_videos_skips_synology_metadata_recycle_and_links(self):
        path = self.video()
        self.video("@eaDir/thumbnail.mp4")
        self.video("#recycle/deleted.mp4")
        (self.imported / "fake.mp4").mkdir()
        (self.imported / "fake.mp4" / "index.jpg").write_bytes(b"not video")
        (self.imported / "notes.txt").write_text("BV1xx411c7mD")
        try:
            (self.imported / "alias.mp4").symlink_to(path)
            (self.imported / "alias-dir").symlink_to(path.parent, target_is_directory=True)
        except OSError:
            pass
        result = await self.reconcile(self.entry(path))
        self.assertEqual(1, result["discovered"])
        self.assertEqual(1, len(self.media.repo.list("media_import")))

    async def test_batch_reconcile_preserves_cid_and_is_idempotent(self):
        first = self.video("a.mp4")
        second = self.video("b.mp4")
        await self.reconcile(self.entry(first))
        previous = self.record(first)
        self.media.engine.probe.reset_mock()
        await self.reconcile(self.entry(second))
        self.assertEqual(previous, self.record(first))
        self.assertEqual(1, self.media.engine.probe.await_count)
        self.media.engine.probe.reset_mock()
        await self.reconcile(self.entry(second))
        self.media.engine.probe.assert_not_awaited()

    async def test_complete_evidence_upgrades_unknown_and_reuses_original_via_cache_pin(self):
        path = self.video()
        await self.reconcile(self.entry(path))
        result = await self.reconcile(self.entry(path, quality=80))
        self.assertEqual(1, result["verified"])
        self.assertEqual("80", self.record(path)["actual_quality"])
        job = self.media.repo.enqueue(
            {
                "bvid": BV,
                "cid": CID,
                "selected_cids": [CID],
                "target_id": "new",
                "quality": {"mode": "exact", "quality_id": "80", "allow_fallback": False},
                "layout": "single",
                "account_id": None,
            }
        )
        job = self.media.repo.claim("operator-test")
        result, reused = await DownloadCache(self.media).obtain(
            job, {"cid": CID}, None, "operator-test", "80"
        )
        self.assertTrue(reused)
        self.assertEqual("import", result["source"]["kind"])
        self.assertEqual(str(path), result["source"]["file"])
        self.assertEqual(self.record(path)["sha256"], result["source"]["sha256"])
        self.assertEqual(path.read_bytes(), Path(result["file"]).read_bytes())
        self.media.engine.download.assert_not_awaited()
        self.assertIsNone(self.media.repo.download_source(BV, CID, "80"))
        # Linking a pin can update source ctime; it must not invalidate unchanged bytes.
        self.assertIsNotNone(self.media.retention.source(BV, CID, "80"))
        self.assertIsNone(self.media.retention.source(BV, CID, "120"))
        await self.reconcile(self.entry(path))
        self.assertEqual("verified", self.record(path)["state"])

    async def test_scope_withdrawal_repoint_and_same_size_corruption_invalidate_sources(self):
        path = self.video()
        await self.reconcile(self.entry(path, quality=80))
        self.media.config["import_roots"][0]["directory"] = str(self.root / "different")
        self.assertIsNone(self.media.retention.source(BV, CID, "80"))
        self.media.config["import_roots"][0]["directory"] = str(self.imported)
        saved_roots = self.media.config.pop("import_roots")
        self.assertIsNone(self.media.retention.source(BV, CID, "80"))
        self.media.config["import_roots"] = saved_roots
        stat = path.stat()
        path.write_bytes(b"x" * stat.st_size)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertIsNone(self.media.retention.source(BV, CID, "80"))
        path.unlink()
        self.assertIsNone(self.media.retention.source(BV, CID, "80"))

    async def test_duration_and_hash_mismatch_do_not_promote_or_destroy_prior_verified(self):
        path = self.video()
        entry = self.entry(path, quality=80, binding="unique_bv_part")
        entry["identity_evidence"]["expected_duration_seconds"] = 120
        result = await self.reconcile(entry)
        self.assertEqual(1, result["invalid"])
        self.assertEqual("media_duration_mismatch", self.record(path)["code"])
        entry = self.entry(path, quality=80)
        entry["quality_evidence"]["sha256"] = "0" * 64
        await self.reconcile(entry)
        self.assertEqual("import_evidence_conflict", self.record(path)["code"])
        await self.reconcile(self.entry(path, quality=80))
        before = self.record(path)
        result = await self.reconcile(entry)
        self.assertEqual(1, result["conflicts"])
        self.assertEqual(before, self.record(path))
        self.assertIsNotNone(self.media.retention.source(BV, CID, "80"))

    async def test_changed_observation_and_invalid_audio_keep_inventory_unknown_reason(self):
        path = self.video()
        entry = self.entry(path)
        entry["mtime_ns"] += 1
        await self.reconcile(entry)
        self.assertEqual("import_observation_changed", self.record(path)["code"])
        self.media.engine.probe.return_value = {
            "format": {"duration": "2"},
            "streams": [INFO["streams"][0]],
        }
        await self.reconcile(self.entry(path))
        self.assertEqual("media_audio_missing", self.record(path)["code"])
        self.assertEqual("hevc", self.record(path)["media"]["video_streams"][0]["codec_name"])
        self.assertEqual([], self.media.repo.list("media_source"))

    async def test_cancel_keeps_completed_rows_and_resume_skips_them(self):
        first, second = self.video("a.mp4"), self.video("b.mp4")
        entered, release = asyncio.Event(), asyncio.Event()

        async def probe(path):
            if path == second:
                entered.set()
                await release.wait()
            return copy.deepcopy(INFO)

        self.media.engine.probe.side_effect = probe
        task = asyncio.create_task(self.reconcile(self.entry(first), self.entry(second)))
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNotNone(self.record(first))
        self.assertIsNone(self.record(second))
        self.assertEqual("interrupted", self.media.repo.get("media_import_run", "legacy")["state"])
        self.media.engine.probe.side_effect = None
        self.media.engine.probe.reset_mock()
        result = await self.reconcile(self.entry(first), self.entry(second))
        self.assertEqual(2, result["registered"])
        self.assertEqual(1, self.media.engine.probe.await_count)

    async def test_cancelled_hash_keeps_root_lock_until_file_thread_finishes(self):
        path = self.video()
        entered, release = threading.Event(), threading.Event()
        from services.platform.media.engine import file_record

        def blocking_hash(*args):
            entered.set()
            release.wait(5)
            return file_record(*args)

        with patch("services.platform.media.imports.file_record", blocking_hash):
            task = asyncio.create_task(self.reconcile(self.entry(path, quality=80)))
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            with self.assertRaises(Fault) as error:
                await self.reconcile(self.entry(path))
            self.assertEqual("busy", error.exception.code)
            release.set()
            for _ in range(100):
                try:
                    result = await self.reconcile(self.entry(path))
                    break
                except Fault as error:
                    self.assertEqual("busy", error.code)
                    await asyncio.sleep(0.01)
            else:
                self.fail("cancelled local work did not release its root lock")
        self.assertEqual(1, result["unknown"])

    async def test_stale_cas_cannot_overwrite_new_verified_source(self):
        path = self.video()
        await self.reconcile(self.entry(path))
        old = self.record(path)
        await self.reconcile(self.entry(path, quality=80))
        with self.assertRaises(Fault) as error:
            self.media.repo.import_commit(
                item_id("legacy", path.relative_to(self.imported).as_posix()),
                {k: v for k, v in old.items() if k != "revision"},
                old["revision"],
            )
        self.assertEqual("version_conflict", error.exception.code)
        self.assertEqual("verified", self.record(path)["state"])

    async def test_video_changed_during_probe_is_invalid_and_can_be_reconciled_again(self):
        path = self.video()
        entry = self.entry(path, quality=80)

        async def replace_during_probe(_):
            path.write_bytes(b"new content has a different observation")
            return copy.deepcopy(INFO)

        self.media.engine.probe.side_effect = replace_during_probe
        result = await self.reconcile(entry)
        self.assertEqual(1, result["invalid"])
        self.assertEqual([], self.media.repo.list("media_source"))
        self.media.engine.probe.side_effect = None
        result = await self.reconcile(self.entry(path, quality=80))
        self.assertEqual(1, result["verified"])

    async def test_nfo_bv_is_inventory_hint_and_cannot_prove_cid_or_quality(self):
        path = self.video()
        path.with_suffix(".nfo").write_text("<movie><id>" + BV + "</id></movie>")
        result = await self.reconcile()
        self.assertEqual(1, result["unknown"])
        record = self.record(path)
        self.assertEqual(BV, record["bvid"])
        self.assertIsNone(record["cid"])
        self.assertFalse(record["identity_confirmed"])
        self.assertEqual(["cid", "identity_evidence", "actual_quality"], record["missing_evidence"])

    async def test_source_revision_changed_during_hash_is_rejected(self):
        path = self.video()
        await self.reconcile(self.entry(path, quality=80))
        from services.platform.media.engine import file_record

        def change_revision(*args):
            record = self.record(path)
            self.media.repo.put(
                "media_import",
                item_id("legacy", path.relative_to(self.imported).as_posix()),
                record,
            )
            return file_record(*args)

        with patch("services.platform.media.imports.file_record", change_revision):
            self.assertIsNone(self.media.retention.source(BV, CID, "80"))

    def test_closed_manifest_rejects_inference_conflicts_and_unsafe_paths(self):
        entry = self.entry(self.video(), quality=80)
        mutations = [
            ("relative_path", "../escape.mp4"),
            ("relative_path", "C:escape.mp4"),
            ("relative_path", "a\\b.mp4"),
            ("quality_evidence", {**entry["quality_evidence"], "quality": "best"}),
            ("identity_evidence", {**entry["identity_evidence"], "cid": "999"}),
        ]
        for key, value in mutations:
            with self.subTest(key=key, value=value), self.assertRaises(Fault):
                manifest_entries({"schema_version": 1, "entries": [{**entry, key: value}]})
        with self.assertRaises(Fault):
            manifest_entries({"schema_version": 1, "entries": [entry, entry]})

    def test_import_configuration_is_closed_and_never_creates_missing_roots(self):
        missing = self.root / "not-created"
        settings = {
            "database_path": str(self.root / "platform.sqlite"),
            "contract_directory": str(self.root / "contracts/text-dialogue/v1"),
            "media": {
                "staging_directory": str(self.root / "staging"),
                "import_roots": [{"root_id": "legacy", "label": "Old", "directory": str(missing)}],
            },
        }
        result = configuration(settings)
        self.assertEqual(str(missing), result["import_roots"][0]["directory"])
        self.assertFalse(missing.exists())
        for directory in (self.root / "staging", self.root, self.root / "staging/nested"):
            settings["media"]["import_roots"][0]["directory"] = str(directory)
            with self.subTest(directory=directory), self.assertRaises(Fault):
                configuration(settings)

    async def test_real_ffprobe_reads_streams_once_and_verified_registration_does_not_modify_media(
        self,
    ):
        ffmpeg = os.environ.get("MEDIA_TEST_FFMPEG", "ffmpeg")
        ffprobe = os.environ.get("MEDIA_TEST_FFPROBE", "ffprobe")
        if not shutil.which(ffmpeg) or not shutil.which(ffprobe):
            self.skipTest("FFmpeg/ffprobe unavailable")
        path = self.imported / "real-synthetic.mp4"
        code, _, _ = await run_process(
            [
                ffmpeg,
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=160x90:d=2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=2",
                "-c:v",
                "mpeg4",
                "-c:a",
                "aac",
                "-shortest",
                "-y",
                str(path),
            ]
        )
        self.assertEqual(0, code)
        self.media.engine = Engine({"ffprobe": ffprobe})
        before = observation(path)
        with patch("services.platform.media.engine.run_process", wraps=run_process) as process:
            result = await self.reconcile(self.entry(path, quality=80))
        self.assertEqual(1, result["verified"])
        self.assertEqual(1, process.await_count)
        self.assertEqual(before, observation(path))
        self.assertEqual("mpeg4", self.record(path)["media"]["video_streams"][0]["codec_name"])
        self.assertEqual("aac", self.record(path)["media"]["audio_tracks"][0]["codec_name"])

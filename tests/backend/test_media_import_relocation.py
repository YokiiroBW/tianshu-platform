"""Exact path maintenance exercises real temporary files, hardlinks and SQLite."""

import asyncio
import copy
import hashlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services.platform.contracts import Fault, digest
from services.platform.media.download_cache import DownloadCache
from services.platform.media.engine import Engine
from services.platform.media.import_records import manifest_entries, observation
from services.platform.media.import_relocation import ImportRelocation
from services.platform.media.imports import Imports, item_id
from services.platform.media.organizer import library_lock
from services.platform.media.repository import Repository
from services.platform.media.retention import Retention

BV = "BV1xx411c7mD"
INFO = {
    "format": {"duration": "2"},
    "streams": [
        {"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160},
        {"codec_type": "audio", "codec_name": "aac", "channels": 2},
    ],
}


async def local(function, *args, **kwargs):
    return await asyncio.to_thread(function, *args, **kwargs)


class ImportRelocationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="import-relocation-synthetic-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.old, self.new = self.root / "old", self.root / "new"
        self.old.mkdir()
        self.new.mkdir()
        self.media = SimpleNamespace(
            config={
                "staging_directory": str(self.root / "staging"),
                "targets": {},
                "import_roots": [
                    {"root_id": "legacy", "label": "Synthetic", "directory": str(self.old)}
                ],
            },
            repo=Repository(self.root / "media.sqlite"),
            local=local,
            engine=Engine({}),
        )
        self.media.engine.probe = AsyncMock(return_value=copy.deepcopy(INFO))
        self.media.engine.download = AsyncMock(side_effect=AssertionError("must not download"))
        self.media.imports = Imports(self.media)
        self.media.retention = Retention(self.media)
        self.relocation = ImportRelocation(self.media)

    async def register(self, states=("unknown",)):
        entries = []
        for index, state in enumerate(states, 1):
            path = self.old / f"{index}.mp4"
            path.write_bytes(f"synthetic original bytes {index}".encode())
            identity = {
                "kind": "bili_sync_page",
                "binding": "exact_path",
                "relative_path": path.name,
                "bvid": BV,
                "cid": str(index),
                "part_index": index,
                "source_digest": "a" * 64,
                "expected_duration_seconds": 30 if state == "invalid" else 2,
            }
            info = observation(path)
            entries.append(
                {
                    "relative_path": path.name,
                    "bvid": BV,
                    "cid": str(index),
                    "part_index": index,
                    "size_bytes": info["size_bytes"],
                    "mtime_ns": info["mtime_ns"],
                    "identity_evidence": identity,
                    "quality_evidence": None
                    if state != "verified"
                    else {
                        "kind": "actual_download",
                        "quality": 80,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "size_bytes": info["size_bytes"],
                        "source_digest": "b" * 64,
                    },
                }
            )
        await self.media.imports.reconcile(
            "legacy", evidence_manifest={"schema_version": 1, "entries": entries}
        )
        records = self.media.repo.list("media_import")
        self.assertEqual(sorted(states), sorted(record["state"] for record in records))
        return records

    def plan(self, *, link=True, migration_id="synthetic-layout", same_relative=False):
        entries = []
        for record in self.media.repo.list("media_import"):
            old_relative = record["relative_path"]
            new_relative = old_relative if same_relative else "UP主/合成作者 [UID1]/" + old_relative
            destination = self.new.joinpath(*new_relative.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                if link:
                    os.link(self.old / old_relative, destination)
                else:
                    shutil.copy2(self.old / old_relative, destination)
            entries.append(
                {
                    "old_relative_path": old_relative,
                    "new_relative_path": new_relative,
                    "expected_record": record,
                }
            )
        return {
            "schema_version": 1,
            "migration_id": migration_id,
            "root_id": "legacy",
            "old_directory": str(self.old),
            "new_directory": str(self.new),
            "entries": entries,
        }

    def repoint(self):
        self.media.config["import_roots"][0]["directory"] = str(self.new)

    def assert_fault(self, code, function, *args):
        with self.assertRaises(Fault) as error:
            function(*args)
        self.assertEqual(code, error.exception.code)

    async def test_three_states_rebind_identity_evidence_and_current_sources_without_hashing_links(
        self,
    ):
        await self.register(("unknown", "invalid", "verified"))
        plan = self.plan()
        self.repoint()  # Supported even when configuration was already changed before prepare.
        with patch(
            "services.platform.media.import_relocation.file_record",
            side_effect=AssertionError("hardlink must not hash"),
        ):
            prepared = self.relocation.prepare(plan)
            self.assertEqual("validated", self.relocation.validate(plan["migration_id"])["state"])
            receipt = self.relocation.commit(plan["migration_id"])
        self.assertEqual(3, len(receipt["entries"]))
        self.assertFalse(receipt["replayed"])
        for entry in receipt["entries"]:
            expected, actual = entry["expected_record"], entry["record"]
            for key in (
                "bvid",
                "cid",
                "part_index",
                "state",
                "code",
                "actual_quality",
                "sha256",
                "identity_confirmed",
                "quality_evidence",
                "missing_evidence",
                "media",
            ):
                self.assertEqual(expected[key], actual[key], key)
            self.assertEqual(expected["revision"] + 1, actual["revision"])
            self.assertIsNone(self.media.repo.get("media_import", entry["old_record_id"]))
            self.assertEqual(item_id("legacy", entry["new_relative_path"]), entry["new_record_id"])
            self.assertEqual(
                entry["new_relative_path"], actual["identity_evidence"]["relative_path"]
            )
            evidence = {
                key: actual[key]
                for key in (
                    "relative_path",
                    "bvid",
                    "cid",
                    "part_index",
                    "identity_evidence",
                    "quality_evidence",
                )
            }
            evidence.update(
                size_bytes=actual["observation"]["size_bytes"],
                mtime_ns=actual["observation"]["mtime_ns"],
            )
            manifest_entries({"schema_version": 1, "entries": [evidence]})
            self.assertEqual(digest(evidence), actual["evidence_digest"])
            self.assertEqual(
                expected["evidence_digest"], actual["relocation"]["old_evidence_digest"]
            )
            self.assertTrue((self.old / entry["old_relative_path"]).exists())
            self.assertEqual(
                (self.old / entry["old_relative_path"]).read_bytes(),
                (self.new / entry["new_relative_path"]).read_bytes(),
            )
        verified = next(
            entry for entry in receipt["entries"] if entry["record"]["state"] == "verified"
        )
        source = self.media.repo.get("media_source", "import:" + verified["new_record_id"])
        self.assertEqual(verified["record"]["revision"], source["import_record_revision"])
        self.assertEqual(verified["record"]["evidence_digest"], source["evidence_digest"])
        self.assertEqual(1, len(self.media.repo.list("media_source")))
        self.assertEqual(
            prepared, self.media.repo.get("import_relocation_plan", plan["migration_id"])
        )

    async def test_global_download_cache_reuses_verified_relocated_source_and_keeps_unknown_unavailable(
        self,
    ):
        await self.register(("verified", "unknown"))
        plan = self.plan()
        self.relocation.prepare(plan)
        self.repoint()
        receipt = self.relocation.commit(plan["migration_id"])
        self.media.repo.enqueue(
            {
                "bvid": BV,
                "selected_cids": ["1"],
                "target_id": "other-source",
                "quality": {"mode": "exact", "quality_id": "80", "allow_fallback": False},
                "layout": "single",
            }
        )
        job = self.media.repo.claim("synthetic-operator")
        result, reused = await DownloadCache(self.media).obtain(
            job, {"cid": "1"}, None, "synthetic-operator", "80"
        )
        verified = next(
            entry for entry in receipt["entries"] if entry["record"]["state"] == "verified"
        )
        self.assertTrue(reused)
        self.assertEqual(verified["new_record_id"], result["source"]["import_record_id"])
        self.assertEqual(str(self.new / verified["new_relative_path"]), result["source"]["file"])
        self.media.engine.download.assert_not_awaited()
        self.assertIsNone(self.media.retention.source(BV, "2", "80"))
        self.assertIsNone(self.media.repo.download_source(BV, "1", "80"))

    async def test_copy_requires_real_double_hash_and_preserves_unknown_quality(self):
        await self.register(("unknown", "invalid", "verified"))
        plan = self.plan(link=False)
        from services.platform.media.engine import file_record

        with patch(
            "services.platform.media.import_relocation.file_record", wraps=file_record
        ) as hashed:
            prepared = self.relocation.prepare(plan)
            self.assertEqual(6, hashed.call_count)
            self.assertTrue(
                all(entry["proof"]["method"] == "sha256" for entry in prepared["entries"])
            )
            self.repoint()
            receipt = self.relocation.commit(plan["migration_id"])
            self.assertEqual(12, hashed.call_count)
        unknown = next(
            entry["record"] for entry in receipt["entries"] if entry["record"]["state"] == "unknown"
        )
        self.assertIsNone(unknown["sha256"])
        self.assertIsNone(unknown["actual_quality"])

    async def test_changed_copied_content_with_restored_size_and_mtime_is_rejected(self):
        await self.register()
        plan = self.plan(link=False)
        destination = self.new / plan["entries"][0]["new_relative_path"]
        info = destination.stat()
        destination.write_bytes(b"x" * info.st_size)
        os.utime(destination, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assert_fault("import_relocation_content_changed", self.relocation.prepare, plan)
        self.assertEqual([], self.media.repo.list("import_relocation_plan"))
        self.assertEqual(str(self.old), self.media.repo.list("media_import")[0]["directory"])

    async def test_file_changed_after_prepare_is_rejected_without_database_mutation(self):
        await self.register()
        plan = self.plan()
        self.relocation.prepare(plan)
        destination = self.new / plan["entries"][0]["new_relative_path"]
        info = destination.stat()
        destination.write_bytes(b"x" * info.st_size)
        os.utime(destination, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
        self.repoint()
        self.assert_fault(
            "import_observation_changed", self.relocation.commit, plan["migration_id"]
        )
        self.assertEqual([], self.media.repo.list("import_relocation"))

    async def test_expected_revision_and_omitted_or_added_records_are_conflicts(self):
        await self.register(("unknown", "invalid"))
        plan = self.plan()
        omitted = {**plan, "entries": plan["entries"][:1]}
        self.assert_fault("version_conflict", self.relocation.prepare, omitted)
        stale = copy.deepcopy(plan)
        stale["entries"][0]["expected_record"]["revision"] += 1
        self.assert_fault("version_conflict", self.relocation.prepare, stale)
        self.relocation.prepare(plan)
        record = plan["entries"][0]["expected_record"]
        self.media.repo.put("media_import", item_id("legacy", record["relative_path"]), record)
        self.repoint()
        self.assert_fault("version_conflict", self.relocation.commit, plan["migration_id"])

    async def test_destination_document_and_mapping_collisions_are_rejected(self):
        await self.register(("unknown", "invalid"))
        plan = self.plan()
        duplicate = copy.deepcopy(plan)
        duplicate["entries"][1]["new_relative_path"] = duplicate["entries"][0]["new_relative_path"]
        self.assert_fault("import_relocation_path_conflict", self.relocation.prepare, duplicate)
        destination_id = item_id("legacy", plan["entries"][0]["new_relative_path"])
        self.media.repo.put(
            "media_import",
            destination_id,
            {
                "directory": str(self.new),
                "root_id": "legacy",
                "relative_path": plan["entries"][0]["new_relative_path"],
            },
        )
        self.assert_fault("import_relocation_path_conflict", self.relocation.prepare, plan)

    async def test_collision_created_after_prepare_is_rejected_in_transaction(self):
        await self.register()
        plan = self.plan()
        prepared = self.relocation.prepare(plan)
        self.repoint()
        entry = prepared["entries"][0]
        self.media.repo.put(
            "media_source", "import:" + entry["new_record_id"], {"kind": "unrelated"}
        )
        self.assert_fault(
            "import_relocation_path_conflict",
            self.media.repo.import_relocate,
            prepared,
        )
        self.assertEqual(
            entry["expected_record"], self.media.repo.get("media_import", entry["old_record_id"])
        )

    async def test_transaction_failure_rolls_back_every_record_and_source(self):
        await self.register(("verified", "unknown"))
        plan = self.plan()
        self.relocation.prepare(plan)
        before_imports, before_sources = (
            self.media.repo.list("media_import"),
            self.media.repo.list("media_source"),
        )
        original_put = self.media.repo.put

        def fail_receipt(kind, *args, **kwargs):
            if kind == "import_relocation":
                raise OSError("synthetic storage failure")
            return original_put(kind, *args, **kwargs)

        self.repoint()
        with (
            patch.object(self.media.repo, "put", side_effect=fail_receipt),
            self.assertRaises(OSError),
        ):
            self.relocation.commit(plan["migration_id"])
        self.assertEqual(before_imports, self.media.repo.list("media_import"))
        self.assertEqual(before_sources, self.media.repo.list("media_source"))
        self.assertEqual([], self.media.repo.list("import_relocation"))
        self.assertEqual("committed", self.relocation.commit(plan["migration_id"])["state"])

    async def test_repeat_and_plan_id_conflict_and_replay_after_operator_removes_old_links(self):
        await self.register(("verified", "invalid", "unknown"))
        plan = self.plan()
        prepared = self.relocation.prepare(plan)
        self.assertEqual(prepared, self.relocation.prepare(plan))
        self.repoint()
        receipt = self.relocation.commit(plan["migration_id"])
        before = self.media.repo.list("media_import"), self.media.repo.list("media_source")
        for entry in plan["entries"]:
            (
                self.old / entry["old_relative_path"]
            ).unlink()  # Simulates later, separately authorized operator cleanup.
        self.assertTrue(self.relocation.commit(plan["migration_id"])["replayed"])
        self.assertEqual(prepared, self.relocation.prepare(plan))
        self.assertEqual(
            before, (self.media.repo.list("media_import"), self.media.repo.list("media_source"))
        )
        modified = copy.deepcopy(plan)
        modified["entries"][0]["new_relative_path"] = "different.mp4"
        self.assert_fault("idempotency_conflict", self.relocation.prepare, modified)
        self.assertEqual(
            receipt["revision"],
            self.media.repo.get("import_relocation", plan["migration_id"])["revision"],
        )

    async def test_root_only_rebinding_keeps_correct_record_id_and_continuous_revision(self):
        await self.register(("verified",))
        plan = self.plan(same_relative=True)
        self.relocation.prepare(plan)
        self.repoint()
        receipt = self.relocation.commit(plan["migration_id"])
        entry = receipt["entries"][0]
        self.assertEqual(entry["old_record_id"], entry["new_record_id"])
        self.assertEqual(2, entry["record"]["revision"])
        self.assertIsNotNone(self.media.retention.source(BV, "1", "80"))
        self.assertTrue(self.relocation.commit(plan["migration_id"])["replayed"])

    async def test_historical_nfo_records_remain_original_and_receipt_maps_only_proven_video_paths(
        self,
    ):
        await self.register()
        metadata = self.media.repo.put(
            "import_metadata_plan",
            "old-nfo",
            {
                "plan_id": "old-nfo",
                "root_id": "legacy",
                "root_directory": str(self.old),
                "state": "completed",
            },
        )
        nfo = self.media.repo.put(
            "import_metadata_entry",
            "original-nfo",
            {
                "plan_id": "old-nfo",
                "entry_id": "original-nfo",
                "nfo_relative_path": "movie.nfo",
                "video_relative_paths": ["1.mp4"],
                "state": "already_applied",
            },
        )
        plan = self.plan()
        self.relocation.prepare(plan)
        self.repoint()
        result = self.relocation.commit(plan["migration_id"])
        history = result["historical_metadata"][0]
        self.assertEqual(digest(metadata), history["digest"])
        self.assertEqual("movie.nfo", history["entries"][0]["old_nfo_relative_path"])
        self.assertEqual(
            plan["entries"][0]["new_relative_path"],
            history["entries"][0]["video_paths"][0]["new_relative_path"],
        )
        self.assertEqual(metadata, self.media.repo.get("import_metadata_plan", "old-nfo"))
        self.assertEqual(nfo, self.media.repo.get("import_metadata_entry", "original-nfo"))

    async def test_unknown_without_manifest_or_identity_does_not_acquire_proof_of_quality(self):
        path = self.old / "orphan.mp4"
        path.write_bytes(b"synthetic unidentified file")
        await self.media.imports.reconcile(
            "legacy", evidence_manifest={"schema_version": 1, "entries": []}
        )
        plan = self.plan()
        self.relocation.prepare(plan)
        self.repoint()
        record = self.relocation.commit(plan["migration_id"])["entries"][0]["record"]
        for field in (
            "bvid",
            "cid",
            "part_index",
            "identity_evidence",
            "quality_evidence",
            "evidence_digest",
            "actual_quality",
            "sha256",
        ):
            self.assertIsNone(record[field])
        self.assertEqual("unknown", record["state"])
        self.assertEqual([], self.media.repo.list("media_source"))

    async def test_unsafe_mapping_missing_file_scope_and_original_changes_are_rejected(self):
        await self.register()
        plan = self.plan()
        unsafe = copy.deepcopy(plan)
        unsafe["entries"][0]["new_relative_path"] = "../escape.mp4"
        self.assert_fault("import_invalid_manifest", self.relocation.prepare, unsafe)
        destination = self.new / plan["entries"][0]["new_relative_path"]
        destination.unlink()
        with self.assertRaises(FileNotFoundError):
            self.relocation.prepare(plan)
        os.link(self.old / "1.mp4", destination)
        self.relocation.prepare(plan)
        self.assert_fault("import_scope_changed", self.relocation.commit, plan["migration_id"])
        self.media.config["import_roots"][0]["directory"] = str(self.root / "other")
        self.assert_fault("import_scope_changed", self.relocation.validate, plan["migration_id"])
        self.repoint()
        (self.old / "1.mp4").write_bytes(b"different bytes")
        self.assert_fault(
            "import_observation_changed", self.relocation.commit, plan["migration_id"]
        )

    async def test_relocation_uses_the_existing_import_lock(self):
        await self.register()
        plan = self.plan()
        lock_path = Path(self.media.config["staging_directory"]) / ".imports" / digest("legacy")
        with library_lock(lock_path):
            self.assert_fault("busy", self.relocation.prepare, plan)

    async def test_stale_invalid_source_is_removed_without_restoring_verification(self):
        await self.register(("verified",))
        identity = item_id("legacy", "1.mp4")
        old = self.media.repo.get("media_import", identity)
        self.media.repo.put(
            "media_import",
            identity,
            {
                **old,
                "state": "invalid",
                "code": "media_duration_mismatch",
                "actual_quality": None,
                "sha256": None,
            },
        )
        plan = self.plan()
        self.relocation.prepare(plan)
        self.repoint()
        receipt = self.relocation.commit(plan["migration_id"])
        self.assertEqual("invalid", receipt["entries"][0]["record"]["state"])
        self.assertEqual(1, len(receipt["entries"][0]["removed_stale_sources"]))
        self.assertEqual([], self.media.repo.list("media_source"))

    async def test_stale_verified_source_conflict_rolls_back_and_never_promotes_it(self):
        await self.register(("verified",))
        plan = self.plan()
        self.relocation.prepare(plan)
        source_id = "import:" + item_id("legacy", "1.mp4")
        source = self.media.repo.get("media_source", source_id)
        self.media.repo.put("media_source", source_id, {**source, "import_record_revision": 999})
        self.repoint()
        self.assert_fault("import_source_changed", self.relocation.commit, plan["migration_id"])
        self.assertEqual(
            plan["entries"][0]["expected_record"],
            self.media.repo.get("media_import", item_id("legacy", "1.mp4")),
        )
        self.assertEqual([], self.media.repo.list("import_relocation"))

    async def test_repository_rechecks_expected_snapshots_even_after_file_validation(self):
        await self.register()
        plan = self.plan()
        prepared = self.relocation.prepare(plan)
        self.relocation.validate(plan["migration_id"])
        record = plan["entries"][0]["expected_record"]
        self.media.repo.put(
            "media_import", item_id("legacy", "1.mp4"), {**record, "code": "changed"}
        )
        self.assert_fault("version_conflict", self.media.repo.import_relocate, prepared)
        self.assertEqual([], self.media.repo.list("import_relocation"))

    async def test_copy_replay_rehashes_target_after_original_was_removed(self):
        await self.register()
        plan = self.plan(link=False)
        self.relocation.prepare(plan)
        self.repoint()
        self.relocation.commit(plan["migration_id"])
        (self.old / "1.mp4").unlink()
        self.assertTrue(self.relocation.commit(plan["migration_id"])["replayed"])
        destination = self.new / plan["entries"][0]["new_relative_path"]
        info = destination.stat()
        destination.write_bytes(b"x" * info.st_size)
        os.utime(destination, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assert_fault(
            "import_relocation_content_changed", self.relocation.commit, plan["migration_id"]
        )

    async def test_replay_rejects_changed_current_source_reference(self):
        await self.register(("verified",))
        plan = self.plan()
        self.relocation.prepare(plan)
        self.repoint()
        entry = self.relocation.commit(plan["migration_id"])["entries"][0]
        source_id = "import:" + entry["new_record_id"]
        source = self.media.repo.get("media_source", source_id)
        self.media.repo.put("media_source", source_id, {**source, "import_record_revision": 99})
        self.assert_fault("import_source_changed", self.relocation.commit, plan["migration_id"])

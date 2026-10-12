import base64
import hashlib
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fixtures import CONTRACT
from services.platform.contracts import Fault
from services.platform.media.config import configuration
from services.platform.media.engine import file_record
from services.platform.media.organizer import FileOrganizer, library_lock
from services.platform.media.repository import Repository
from services.platform.media.runtime import PackageContract


class FileOrganizerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.staging = self.root / "staging"
        self.staging.mkdir()
        self.repo = Repository(self.root / "media.sqlite")
        self.contract = PackageContract(CONTRACT.parents[1] / "media-package/v1")
        self.library = FileOrganizer(self.repo, self.contract, self.staging)
        self.target = {
            "target_id": "local-test",
            "library_id": str(uuid.uuid4()),
            "directory": str(self.root / "library"),
            "kind": "media_server",
        }

    def package(self, title="first", video=b"synthetic-video"):
        identity = str(uuid.uuid4())
        staging_ref = "pkg_" + identity.replace("-", "")
        directory = self.staging / staging_ref
        directory.mkdir()
        files = []
        for name, kind, cid, data in [
            ("video.mp4", "video", "123", video),
            ("movie.nfo", "nfo", None, ("<movie><title>" + title + "</title></movie>").encode()),
            ("source.json", "source", None, b'{"fixture":true}'),
        ]:
            path = directory / name
            path.write_bytes(data)
            files.append(file_record(path, name, kind, cid))
        manifest = {
            "schema_version": 1,
            "package_id": identity,
            "staging_ref": staging_ref,
            "library_id": self.target["library_id"],
            "provider": "bilibili",
            "bvid": "BV1xx411c7mD",
            "layout": "single",
            "media_extension": "mp4",
            "selected_parts": [{"cid": "123", "episode_number": None}],
            "files": files,
        }
        raw = json.dumps(manifest).encode()
        return {
            "manifest_base64": base64.b64encode(raw).decode(),
            "manifest_digest": hashlib.sha256(raw).hexdigest(),
        }

    def publish(self, package, key="first"):
        operation = self.library.prepare(self.target, package, key)
        return self.library.publish(self.target, operation)

    def test_default_configuration_and_explicit_remote_compatibility(self):
        settings = {
            "database_path": str(self.root / "platform.sqlite"),
            "contract_directory": str(CONTRACT),
            "media": {},
        }
        config = configuration(settings)
        self.assertEqual(config["targets"], {})
        settings["media"]["targets"] = [
            {
                "target_id": "old",
                "label": "Existing",
                "publisher": {"base_url": "https://fixture.invalid"},
                "library_id": self.target["library_id"],
            }
        ]
        self.assertEqual(configuration(settings)["targets"]["old"]["kind"], "assetlibrary")
        settings["media"]["targets"] = [
            {"target_id": "bad", "label": "Overlap", "directory": str(self.root)}
        ]
        with self.assertRaises(Fault):
            configuration(settings)

    def test_real_files_durable_receipt_and_idempotency_without_asset_service(self):
        package = self.package()
        operation = self.publish(package)
        self.assertEqual(operation["status"], "indexed")
        destination = Path(self.target["directory"]) / operation["receipt"]["target_relative_path"]
        self.assertEqual((destination / "video.mp4").read_bytes(), b"synthetic-video")
        restarted = FileOrganizer(Repository(self.repo.database), self.contract, self.staging)
        self.assertEqual(restarted.prepare(self.target, package, "first"), operation)
        self.assertEqual(restarted.publish(self.target, operation), operation)
        self.assertEqual(len(self.repo.list("file_transfer")), 1)
        with self.assertRaises(Fault):
            restarted.prepare(self.target, self.package("changed"), "first")
        source = self.staging / operation["manifest"]["staging_ref"] / "video.mp4"
        self.assertTrue(source.samefile(destination / "video.mp4"))
        sidecar = self.staging / operation["manifest"]["staging_ref"] / "movie.nfo"
        sidecar.write_bytes(b"independent-metadata")
        self.assertNotEqual((destination / "movie.nfo").read_bytes(), b"independent-metadata")

    def test_metadata_revision_and_owned_file_protection(self):
        first = self.publish(self.package())
        package = {
            **self.package("updated"),
            "base_operation_id": first["operation_id"],
            "base_version": first["version"],
        }
        second = self.publish(package, "second")
        destination = Path(self.target["directory"]) / second["relative"]
        self.assertIn("updated", (destination / "movie.nfo").read_text())
        self.assertEqual((destination / "video.mp4").read_bytes(), b"synthetic-video")
        third = {
            **self.package("third"),
            "base_operation_id": second["operation_id"],
            "base_version": second["version"],
        }
        (destination / "user-note.txt").write_text("must survive")
        with self.assertRaises(Fault):
            self.library.prepare(self.target, third, "third")
        self.assertEqual((destination / "user-note.txt").read_text(), "must survive")

    def test_subdirectories_copy_fallback_and_recovery_keep_relative_receipt(self):
        package = {**self.package(), "destination_subdirectory": "中文/收藏"}
        with patch(
            "services.platform.media.organizer.os.link", side_effect=OSError("cross device")
        ):
            operation = self.publish(package)
        self.assertEqual(operation["relative"], "中文/收藏/bilibili-BV1xx411c7mD-cid-123")
        destination = Path(self.target["directory"]) / operation["relative"]
        source = self.staging / operation["manifest"]["staging_ref"] / "video.mp4"
        self.assertFalse(source.samefile(destination / "video.mp4"))
        self.assertEqual(source.read_bytes(), (destination / "video.mp4").read_bytes())
        self.assertEqual(self.library.read(self.target, operation["operation_id"]), operation)

    def test_crash_after_install_before_receipt_recovers_on_read(self):
        operation = self.library.prepare(self.target, self.package(), "crash")
        save = self.library.save

        def crash(value, **changes):
            if changes.get("status") == "indexed":
                raise OSError("synthetic process loss")
            return save(value, **changes)

        with patch.object(self.library, "save", side_effect=crash):
            with self.assertRaises(OSError):
                self.library.publish(self.target, operation)
        restarted = FileOrganizer(Repository(self.repo.database), self.contract, self.staging)
        recovered = restarted.read(self.target, operation["operation_id"])
        self.assertEqual(recovered["status"], "indexed")
        self.assertEqual(recovered["receipt"]["operation_id"], operation["operation_id"])

    def test_crash_in_directory_swap_resumes_with_previous_edition_preserved(self):
        first = self.publish(self.package())
        package = {
            **self.package("updated"),
            "base_operation_id": first["operation_id"],
            "base_version": first["version"],
        }
        operation = self.library.prepare(self.target, package, "swap")
        rename = Path.rename

        def crash(path, target):
            if path.name.startswith("incoming-"):
                raise OSError("synthetic crash in swap gap")
            return rename(path, target)

        with patch.object(Path, "rename", crash):
            with self.assertRaises(OSError):
                self.library.publish(self.target, operation)
        restarted = FileOrganizer(Repository(self.repo.database), self.contract, self.staging)
        pending = restarted.read(self.target, operation["operation_id"])
        self.assertEqual(pending["status"], "publishing")
        done = restarted.publish(self.target, pending)
        self.assertEqual(done["status"], "indexed")
        self.assertIn(
            "updated", (Path(self.target["directory"]) / done["relative"] / "movie.nfo").read_text()
        )

    def test_cancel_and_concurrent_lock_do_not_claim_publication(self):
        operation = self.library.prepare(self.target, self.package(), "cancel")
        with library_lock(self.target["directory"]):
            with self.assertRaises(Fault) as caught:
                self.library.publish(self.target, operation)
        self.assertEqual(caught.exception.code, "busy")
        self.assertEqual(self.library.cancel(self.target, operation)["status"], "cancelled")
        self.assertFalse((Path(self.target["directory"]) / operation["relative"]).exists())

    def test_changed_source_unowned_destination_and_traversal_never_publish(self):
        package = self.package()
        manifest = json.loads(base64.b64decode(package["manifest_base64"]))
        source = self.staging / manifest["staging_ref"] / "video.mp4"
        source.write_bytes(b"changed")
        with self.assertRaises(Fault):
            self.library.prepare(self.target, package, "bad")
        destination = Path(self.target["directory"]) / "bilibili-BV1xx411c7mD-cid-123"
        destination.mkdir()
        with self.assertRaises(Fault):
            self.library.prepare(self.target, self.package(), "unowned")
        self.assertTrue(destination.is_dir())
        manifest["files"][0]["path"] = "../outside.mp4"
        raw = json.dumps(manifest).encode()
        package = {
            "manifest_base64": base64.b64encode(raw).decode(),
            "manifest_digest": hashlib.sha256(raw).hexdigest(),
        }
        with self.assertRaises((Fault, ValueError)):
            self.library.prepare(self.target, package, "traversal")
        self.assertFalse((self.root / "outside.mp4").exists())

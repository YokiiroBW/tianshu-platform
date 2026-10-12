"""Dashboard counts cover visible jobs; capacity is read from actual media roots."""

import asyncio
import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.platform.media.repository import Repository
from services.platform.media.runtime import Media
from services.platform.media.storage import media_storage


EMPTY_COUNTS = {
    "total": 0,
    "published": 0,
    "processing": 0,
    "queued": 0,
    "attention": 0,
    "cancelled": 0,
}


def seed_jobs(repo, amounts, *, valid_body=True):
    """Bulk synthetic ledger rows, including unreadable bodies to detect accidental JSON scans."""
    with repo.transaction() as db:
        for state, amount in amounts.items():
            for _ in range(amount):
                identity = str(uuid.uuid4())
                body = (
                    json.dumps({"job_id": identity, "state": state})
                    if valid_body
                    else "invalid-json"
                )
                db.execute(
                    "INSERT INTO jobs(id,dedupe,state,created,updated,body) VALUES(?,?,?,?,?,?)",
                    (identity, identity, state, 100, 100, body),
                )


class RepositoryOverviewTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Repository(Path(directory.name) / "media.sqlite")

    def test_empty_ledger_reports_zero_counts(self):
        self.assertEqual(self.repo.job_overview(), EMPTY_COUNTS)

    def test_all_states_and_more_than_one_page_are_counted_without_reading_bodies(self):
        seed_jobs(
            self.repo,
            {
                "published": 11,
                "completed": 13,
                "downloading": 1,
                "validating": 2,
                "metadata_ready": 3,
                "publishing": 4,
                "asset_indexed": 5,
                "library_verifying": 6,
                "queued": 137,
                "retry_wait": 7,
                "failed": 17,
                "unknown": 19,
                "auth_required": 23,
                "waiting_metadata": 29,
                "cancelled": 31,
            },
            valid_body=False,
        )
        self.assertEqual(
            self.repo.job_overview(),
            {
                "total": 308,
                "published": 24,
                "processing": 21,
                "queued": 144,
                "attention": 88,
                "cancelled": 31,
            },
        )


class MediaStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def target(self, directory, label="Media"):
        return {"kind": "media_server", "directory": str(directory), "label": label}

    def test_existing_media_roots_on_one_device_share_capacity_without_enumerating_files(self):
        first, second = self.root / "one", self.root / "two"
        first.mkdir()
        second.mkdir()
        usage = SimpleNamespace(total=1000, used=600, free=400)
        with patch("services.platform.media.storage.shutil.disk_usage", return_value=usage):
            result = media_storage({"one": self.target(first), "two": self.target(second)})
        self.assertEqual(result["state"], "available")
        self.assertEqual(len(result["volumes"]), 1)
        volume = result["volumes"][0]
        self.assertEqual(volume["target_ids"], ["one", "two"])
        self.assertEqual(volume["directories"], [str(first), str(second)])
        self.assertEqual(
            (volume["total_bytes"], volume["used_bytes"], volume["free_bytes"]), (1000, 600, 400)
        )

    def test_missing_root_never_reports_its_existing_parent_volume(self):
        missing = self.root / "not-mounted" / "media"
        with patch("services.platform.media.storage.shutil.disk_usage") as read:
            result = media_storage({"missing": self.target(missing)})
        read.assert_not_called()
        self.assertEqual(result["state"], "unavailable")
        self.assertIsNone(result["volumes"][0]["free_bytes"])
        self.assertFalse(missing.exists())

    def test_no_direct_media_targets_is_not_configured(self):
        self.assertEqual(
            media_storage({"remote": {"kind": "assetlibrary"}}),
            {"state": "not_configured", "volumes": []},
        )

    def test_multiple_devices_are_separate_volumes(self):
        with (
            patch("services.platform.media.storage.Path.is_dir", return_value=True),
            patch(
                "services.platform.media.storage.Path.stat",
                side_effect=[SimpleNamespace(st_dev=1), SimpleNamespace(st_dev=2)],
            ),
            patch(
                "services.platform.media.storage.shutil.disk_usage",
                return_value=SimpleNamespace(total=1000, used=600, free=400),
            ),
        ):
            result = media_storage(
                {"one": self.target(self.root / "one"), "two": self.target(self.root / "two")}
            )
        self.assertEqual(len(result["volumes"]), 2)
        self.assertNotEqual(result["volumes"][0]["storage_id"], result["volumes"][1]["storage_id"])

    def test_unreadable_volume_returns_null_bytes_without_guessing_capacity(self):
        with patch(
            "services.platform.media.storage.shutil.disk_usage",
            side_effect=PermissionError("synthetic private path"),
        ):
            result = media_storage({"one": self.target(self.root)})
        self.assertEqual(result["state"], "unavailable")
        self.assertIsNone(result["volumes"][0]["total_bytes"])
        self.assertNotIn("synthetic", json.dumps(result))

    def test_stat_permission_failure_does_not_fall_back_to_an_unrelated_parent(self):
        with (
            patch(
                "services.platform.media.storage.Path.stat",
                side_effect=PermissionError("synthetic"),
            ),
            patch("services.platform.media.storage.shutil.disk_usage") as read,
        ):
            result = media_storage({"one": self.target(self.root)})
        self.assertEqual(result["state"], "unavailable")
        read.assert_not_called()


class MediaOverviewViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.media = object.__new__(Media)
        self.media.config = {
            "targets": {
                "one": {
                    "kind": "media_server",
                    "target_id": "one",
                    "label": "Media",
                    "directory": self.directory.name,
                    "servers": [],
                }
            },
            "staging_directory": str(Path(self.directory.name) / "future-staging"),
        }
        self.media.repo = Repository(Path(self.directory.name) / "media.sqlite")
        self.media.engine = SimpleNamespace(
            status={"state": "ready", "version": "2026.08.19", "code": "ready"}
        )

        async def local(operation, *arguments):
            return await asyncio.to_thread(operation, *arguments)

        self.media.local = local

    async def test_view_preserves_first_page_and_reports_full_counts_off_the_event_loop(self):
        seed_jobs(self.media.repo, {"published": 120, "queued": 17})
        event_loop_thread = threading.get_ident()

        def disk_usage(candidate):
            self.assertNotEqual(threading.get_ident(), event_loop_thread)
            return SimpleNamespace(total=1000, used=600, free=400)

        with patch("services.platform.media.storage.shutil.disk_usage", side_effect=disk_usage):
            view = await self.media.view()
        self.assertEqual(len(view["jobs"]), 100)
        self.assertEqual(
            view["overview"]["jobs"], {**EMPTY_COUNTS, "total": 137, "published": 120, "queued": 17}
        )
        self.assertEqual(view["overview"]["storage"]["state"], "available")
        self.assertTrue(view["configured"])
        self.assertEqual(view["accounts"], [])
        self.assertEqual(view["subscriptions"], [])
        self.assertFalse(Path(self.media.config["staging_directory"]).exists())

    async def test_storage_failure_keeps_ledger_and_configured_view_available(self):
        with patch(
            "services.platform.media.storage.shutil.disk_usage", side_effect=OSError("synthetic")
        ):
            view = await self.media.view()
        self.assertTrue(view["configured"])
        self.assertEqual(view["overview"]["jobs"], EMPTY_COUNTS)
        self.assertIsNone(view["overview"]["storage"]["volumes"][0]["total_bytes"])

    async def test_unconfigured_view_has_no_overview_and_does_not_read_disk(self):
        self.media.config = None
        with patch("services.platform.media.storage.shutil.disk_usage") as read:
            view = await self.media.view()
        self.assertIsNone(view["overview"])
        self.assertFalse(view["configured"])
        self.assertEqual(view["jobs"], [])
        read.assert_not_called()

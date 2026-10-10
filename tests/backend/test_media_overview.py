"""Dashboard counts cover the full ledger; disk readings expose only the staging volume."""

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
from services.platform.media.runtime import Media, staging_storage


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


class StagingStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_existing_staging_volume_is_read_without_enumerating_files(self):
        staging = self.root / "staging"
        staging.mkdir()
        usage = SimpleNamespace(total=1000, used=600, free=400)
        with patch("services.platform.media.runtime.shutil.disk_usage", return_value=usage) as read:
            self.assertEqual(
                staging_storage(staging),
                {"state": "available", "total_bytes": 1000, "used_bytes": 600, "free_bytes": 400},
            )
        read.assert_called_once_with(staging)

    def test_uncreated_staging_uses_nearest_existing_parent_without_creating_directories(self):
        staging = self.root / "not-created" / "staging"
        usage = SimpleNamespace(total=1000, used=200, free=800)
        with patch("services.platform.media.runtime.shutil.disk_usage", return_value=usage) as read:
            result = staging_storage(staging)
        read.assert_called_once_with(self.root)
        self.assertEqual(result["free_bytes"], 800)
        self.assertFalse((self.root / "not-created").exists())
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_unreadable_volume_returns_null_bytes_without_guessing_capacity(self):
        with patch(
            "services.platform.media.runtime.shutil.disk_usage",
            side_effect=PermissionError("synthetic private path"),
        ):
            result = staging_storage(self.root)
        self.assertEqual(
            result,
            {"state": "unavailable", "total_bytes": None, "used_bytes": None, "free_bytes": None},
        )
        self.assertNotIn("synthetic", json.dumps(result))

    def test_stat_permission_failure_does_not_fall_back_to_an_unrelated_parent(self):
        with (
            patch(
                "services.platform.media.runtime.Path.stat",
                side_effect=PermissionError("synthetic"),
            ),
            patch("services.platform.media.runtime.shutil.disk_usage") as read,
        ):
            result = staging_storage(self.root)
        self.assertEqual(result["state"], "unavailable")
        read.assert_not_called()


class MediaOverviewViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.media = object.__new__(Media)
        self.media.config = {
            "targets": {},
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

        with patch("services.platform.media.runtime.shutil.disk_usage", side_effect=disk_usage):
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
            "services.platform.media.runtime.shutil.disk_usage", side_effect=OSError("synthetic")
        ):
            view = await self.media.view()
        self.assertTrue(view["configured"])
        self.assertEqual(view["overview"]["jobs"], EMPTY_COUNTS)
        self.assertIsNone(view["overview"]["storage"]["total_bytes"])

    async def test_unconfigured_view_has_no_overview_and_does_not_read_disk(self):
        self.media.config = None
        with patch("services.platform.media.runtime.shutil.disk_usage") as read:
            view = await self.media.view()
        self.assertIsNone(view["overview"])
        self.assertFalse(view["configured"])
        self.assertEqual(view["jobs"], [])
        read.assert_not_called()

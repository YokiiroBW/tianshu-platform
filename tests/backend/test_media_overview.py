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
from services.platform.media.overview import RuntimeSample, read_counters
from services.platform.media.storage import media_storage
from services.platform.media.storage_breakdown import StorageBreakdown


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
        self.media.runtime_sample = RuntimeSample()
        self.media.engine = SimpleNamespace(
            status={"state": "ready", "version": "2026.08.19", "code": "ready"}
        )

        async def local(operation, *arguments):
            return await asyncio.to_thread(operation, *arguments)

        self.media.local = local
        self.media.storage_breakdown = StorageBreakdown(self.media)
        self.addAsyncCleanup(self.media.storage_breakdown.close)

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

    async def test_music_capacity_is_read_without_reclassifying_video_storage(self):
        self.media.repo.put("music_settings", "library", {"directory": self.directory.name})
        view = await self.media.view()
        self.assertEqual(view["overview"]["music"]["total"], 0)
        self.assertEqual(view["overview"]["database"]["state"], "ready")
        self.assertIsNone(view["overview"]["runtime"]["receive_bytes_per_second"])
        self.assertEqual(
            view["overview"]["music_storage"]["volumes"][0]["storage_id"],
            view["overview"]["storage"]["volumes"][0]["storage_id"],
        )

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

    async def test_view_returns_capacity_without_waiting_for_directory_scan(self):
        entered, release = threading.Event(), threading.Event()
        from services.platform.media.storage_scan import scan_files

        def delayed_scan(*args):
            entered.set()
            release.wait(3)
            return scan_files(*args)

        with patch(
            "services.platform.media.storage_breakdown.scan_files", side_effect=delayed_scan
        ):
            result = await asyncio.wait_for(self.media.view(), 1)
            self.assertEqual(
                result["overview"]["storage"]["volumes"][0]["breakdown"]["state"], "scanning"
            )
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            again = await asyncio.wait_for(self.media.view(), 1)
            self.assertEqual(
                again["overview"]["storage"]["volumes"][0]["breakdown"]["state"], "scanning"
            )
            release.set()
            await self.media.storage_breakdown.task


class DashboardDetailsTests(unittest.TestCase):
    def test_per_medium_recent_order_hidden_jobs_and_private_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Repository(Path(folder) / "media.sqlite")
            with repo.transaction() as db:
                for i in range(130):
                    row = {
                        "job_id": str(i),
                        "media_kind": "video" if i < 7 else "audio",
                        "state": "completed",
                        "title": str(i),
                        "updated_at": i,
                        "secret": "never project this",
                    }
                    db.execute(
                        "INSERT INTO jobs(id,dedupe,state,created,updated,body) VALUES(?,?,?,?,?,?)",
                        (str(i), str(i), "completed", 130 - i, i, json.dumps(row)),
                    )
            repo.put("deleted_job", "129", {})
            repo.put(
                "music_playlist",
                "one",
                {
                    "state": "active",
                    "code": "ready",
                    "auto_download": True,
                    "member_count": 200,
                    "entries": ["private"],
                },
            )
            repo.put(
                "music_playlist",
                "two",
                {"state": "paused", "code": "music_auth_required", "member_count": 4},
            )
            for identity, extra in (("ok", {}), ("merged", {"merged_into": "ok"}), ("missing", {})):
                repo.put("music_recording", identity, extra)
                repo.put("music_library_file", identity, {"missing": identity == "missing"})
            result = repo.dashboard_details()
            self.assertEqual(
                [r["job_id"] for r in result["recent"]["video"]], ["6", "5", "4", "3", "2"]
            )
            self.assertEqual(
                [r["job_id"] for r in result["recent"]["audio"]],
                ["128", "127", "126", "125", "124"],
            )
            self.assertEqual(
                result["music"],
                {
                    "total": 2,
                    "active": 1,
                    "paused": 1,
                    "attention": 1,
                    "auto_download": 1,
                    "members": 204,
                    "files": 1,
                },
            )
            self.assertNotIn("private", json.dumps(result))
            self.assertNotIn("secret", json.dumps(result))
            self.assertEqual(repo.job_count(), 129)
            self.assertEqual(result["database"]["schema_version"], 2)


class RuntimeSampleTests(unittest.TestCase):
    def test_unknown_first_sample_cached_rates_reset_and_missing_readings(self):
        now = [10.0]
        cpu = [1.0]
        values = [(100, 1000, 2000)]
        sample = RuntimeSample(
            monotonic=lambda: now[0],
            process_time=lambda: cpu[0],
            counters=lambda: values[0],
            clock=lambda: 1000,
        )
        first = sample.view()
        self.assertIsNone(first["cpu_percent"])
        self.assertIsNone(first["receive_bytes_per_second"])
        now[0] = 10.5
        self.assertEqual(first, sample.view())
        now[0], cpu[0], values[0] = 12, 1.5, (120, 1200, 2400)
        measured = sample.view()
        self.assertEqual(measured["cpu_percent"], 25)
        self.assertEqual(measured["receive_bytes_per_second"], 100)
        self.assertEqual(measured["send_bytes_per_second"], 200)
        now[0], values[0] = 14, (None, 1, 2)
        self.assertIsNone(sample.view()["receive_bytes_per_second"])
        now[0], values[0] = 16, (None, None, None)
        self.assertIsNone(sample.view()["memory_bytes"])
        self.assertIsNone(sample.view()["send_bytes_per_second"])

    def test_linux_counters_exclude_loopback_and_unsupported_is_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            proc = Path(folder)
            self.assertEqual(read_counters(proc), (None, None, None))
            (proc / "self").mkdir()
            (proc / "net").mkdir()
            (proc / "self/status").write_text("VmRSS: 12 kB\n")
            (proc / "net/dev").write_text(
                "header\nlo: 10000 0 0 0 0 0 0 0 20000\neth0: 100 0 0 0 0 0 0 0 200\neth1: 20 0 0 0 0 0 0 0 30\n"
            )
            self.assertEqual(read_counters(proc), (12288, 120, 230))

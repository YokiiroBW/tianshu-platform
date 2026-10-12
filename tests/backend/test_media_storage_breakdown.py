"""Real file metadata accounting and asynchronous scope/cache/cancellation behavior."""

import asyncio
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services.platform.contracts import Fault
from services.platform.media.config import configuration
from services.platform.media.storage import media_storage
from services.platform.media.storage_breakdown import (
    CACHE_SECONDS,
    StorageBreakdown,
    project_breakdown,
)
from services.platform.media.storage_scan import ScanCancelled, map_location, scan_files


SERVER = {
    "server_id": "emby",
    "kind": "emby",
    "media_root": "/video/subscriptions",
    "base_url": "https://synthetic.invalid",
}


async def local(function, *args, **kwargs):
    return await asyncio.to_thread(function, *args, **kwargs)


class StorageFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.subscription = self.root / "subscriptions"
        self.subscription.mkdir()
        self.targets = {
            "one": {
                "target_id": "one",
                "kind": "media_server",
                "label": "Media",
                "directory": str(self.subscription),
                "servers": [],
            }
        }

    def volume(self):
        storage = media_storage(self.targets)
        volume = storage["volumes"][0]
        volume.update(total_bytes=1024**2, free_bytes=900 * 1024, used_bytes=124 * 1024)
        return volume

    def file(self, relative, content=b"synthetic file"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def bytes(self, *files):
        return sum(
            getattr(path.stat(), "st_blocks", 0) * 512
            if hasattr(path.stat(), "st_blocks")
            else path.stat().st_size
            for path in files
        )

    def scan(self, emby=(), code=None, cancel=None):
        volume = self.volume()
        result = scan_files(
            [volume],
            [(volume["storage_id"], str(self.subscription))],
            [str(path) for path in emby],
            code,
            cancel or threading.Event(),
        )
        return result[volume["storage_id"]]


class FilesystemAccountingTests(StorageFixture):
    def test_real_allocations_and_hardlinks_across_subscription_and_emby_are_counted_once(self):
        source = self.file("subscriptions/source.mp4", b"video" * 1000)
        sidecar = self.file("subscriptions/source.nfo")
        other = self.file("emby/other.mp4", b"outside" * 1000)
        link = self.root / "emby" / "shared.mp4"
        os.link(source, link)
        measurement = self.scan([self.root / "emby", self.subscription])
        self.assertEqual(measurement["subscription_bytes"], self.bytes(source, sidecar))
        self.assertEqual(measurement["emby_bytes"], self.bytes(other))
        self.assertEqual(
            measurement["basis"], "allocated" if hasattr(source.stat(), "st_blocks") else "logical"
        )
        projection = project_breakdown(self.volume(), measurement, 123, False)
        self.assertEqual(projection["state"], "ready")
        self.assertEqual(
            sum(
                projection[key]
                for key in ("subscription_bytes", "emby_bytes", "other_bytes", "unused_bytes")
            ),
            self.volume()["total_bytes"],
        )

    def test_overlapping_emby_roots_and_duplicate_files_are_deduplicated_globally(self):
        one = self.file("emby/sub/one.mp4")
        two = self.file("emby/two.mp4")
        os.link(one, self.root / "emby" / "another.mp4")
        measurement = self.scan([self.root / "emby", self.root / "emby/sub", self.root / "emby"])
        self.assertEqual(measurement["emby_bytes"], self.bytes(one, two))

    def test_unreadable_emby_root_keeps_known_subscription_and_unknown_other(self):
        source = self.file("subscriptions/source.mp4")
        missing = self.root / "unavailable-music"
        measurement = self.scan([missing])
        self.assertEqual(measurement["subscription_bytes"], self.bytes(source))
        self.assertIsNone(measurement["emby_bytes"])
        projection = project_breakdown(self.volume(), measurement, 123, False)
        self.assertEqual(projection["state"], "partial")
        self.assertEqual(projection["code"], "storage_path_unavailable")
        self.assertIsNone(projection["other_bytes"])

    def test_permission_denied_is_unknown_without_losing_other_category(self):
        source = self.file("subscriptions/source.mp4")
        music = self.root / "music"
        music.mkdir()
        scandir = os.scandir

        def observed(path):
            if Path(path) == music:
                raise PermissionError("synthetic music ACL")
            return scandir(path)

        with patch("services.platform.media.storage_scan.os.scandir", side_effect=observed):
            measurement = self.scan([music])
        self.assertEqual(measurement["subscription_bytes"], self.bytes(source))
        self.assertIsNone(measurement["emby_bytes"])
        self.assertEqual(measurement["code"], "storage_path_unavailable")

    def test_unreadable_subscription_is_unknown_but_empty_emby_is_zero(self):
        with patch("services.platform.media.storage_scan.os.scandir", side_effect=PermissionError):
            measurement = self.scan()
        self.assertIsNone(measurement["subscription_bytes"])
        self.assertEqual(measurement["emby_bytes"], 0)
        self.assertEqual(measurement["code"], "storage_subscription_unavailable")

    def test_symlink_and_special_file_are_excluded_without_following(self):
        source = self.file("subscriptions/source.mp4")
        outside = self.file("outside/private.mp4", b"outside" * 10000)
        try:
            (self.subscription / "linked").symlink_to(outside.parent, target_is_directory=True)
        except OSError:
            self.skipTest("Host cannot create a directory symlink")
        measurement = self.scan()
        self.assertEqual(measurement["subscription_bytes"], self.bytes(source))

    def test_cross_device_subdirectory_is_not_entered_or_assigned_to_parent_volume(self):
        source = self.file("subscriptions/source.mp4")
        mounted = self.file("subscriptions/other-device/private.mp4", b"outside" * 1000).parent
        actual = os.scandir
        lstat = Path.lstat

        def observed_lstat(path):
            value = lstat(path)
            if path == mounted:
                return SimpleNamespace(
                    st_dev=value.st_dev + 1, st_ino=value.st_ino, st_mode=value.st_mode
                )
            return value

        class ForeignEntry:
            def __init__(self, entry):
                self.entry, self.path = entry, entry.path

            def stat(self, **kwargs):
                value = self.entry.stat(**kwargs)
                if Path(self.path) == mounted:
                    return SimpleNamespace(
                        st_dev=value.st_dev + 1, st_ino=value.st_ino, st_mode=value.st_mode
                    )
                return value

        class Entries:
            def __init__(self, path):
                self.entries = actual(path)

            def __enter__(self):
                return (ForeignEntry(entry) for entry in self.entries)

            def __exit__(self, *args):
                self.entries.close()

        with (
            patch("services.platform.media.storage_scan.os.scandir", side_effect=Entries),
            patch("services.platform.media.storage_scan.Path.lstat", observed_lstat),
        ):
            measurement = self.scan()
        self.assertEqual(measurement["subscription_bytes"], self.bytes(source))

    def test_logical_fallback_reports_estimate_instead_of_claiming_allocated_bytes(self):
        source = self.file("subscriptions/source.mp4")
        lstat = Path.lstat

        def no_blocks(path):
            value = lstat(path)
            return SimpleNamespace(
                st_dev=value.st_dev,
                st_ino=value.st_ino,
                st_mode=value.st_mode,
                st_size=value.st_size,
            )

        with patch("services.platform.media.storage_scan.Path.lstat", no_blocks):
            measurement = self.scan()
        self.assertEqual(measurement["basis"], "logical")
        self.assertEqual(measurement["subscription_bytes"], source.stat().st_size)
        self.assertEqual(
            project_breakdown(self.volume(), measurement, 123, False)["code"],
            "storage_logical_estimate",
        )

    def test_allocations_exceeding_available_used_capacity_are_partial_without_clamping(self):
        measurement = {
            "subscription_bytes": 500,
            "emby_bytes": 600,
            "basis": "allocated",
            "code": None,
        }
        volume = {"state": "available", "total_bytes": 1000, "free_bytes": 100}
        projected = project_breakdown(volume, measurement, 123, False)
        self.assertEqual(projected["state"], "partial")
        self.assertEqual(projected["subscription_bytes"], 500)
        self.assertEqual(projected["emby_bytes"], 600)
        self.assertIsNone(projected["other_bytes"])
        self.assertEqual(projected["code"], "storage_measurement_inconsistent")

    def test_cancel_is_checked_before_any_directory_read(self):
        cancelled = threading.Event()
        cancelled.set()
        with patch("services.platform.media.storage_scan.os.scandir") as read:
            with self.assertRaises(ScanCancelled):
                self.scan(cancel=cancelled)
        read.assert_not_called()

    def test_same_volume_multiple_targets_share_subscription_inode_ownership(self):
        source = self.file("subscriptions/source.mp4")
        another = self.file("another/source.nfo")
        os.link(source, another.parent / "linked.mp4")
        self.targets["two"] = {
            **self.targets["one"],
            "target_id": "two",
            "directory": str(another.parent),
        }
        volumes = media_storage(self.targets)["volumes"]
        self.assertEqual(len(volumes), 1)
        roots = [
            (volumes[0]["storage_id"], target["directory"]) for target in self.targets.values()
        ]
        measurement = scan_files(volumes, roots, [], None, threading.Event())[
            volumes[0]["storage_id"]
        ]
        self.assertEqual(measurement["subscription_bytes"], self.bytes(source, another))

    def test_real_distinct_volumes_assign_music_by_device_instead_of_server_identity(self):
        if not Path("/dev/shm").is_dir():
            self.skipTest("Linux /dev/shm is required for real multi-device accounting")
        another = tempfile.TemporaryDirectory(dir="/dev/shm")
        self.addCleanup(another.cleanup)
        other_root = Path(another.name)
        if other_root.stat().st_dev == self.subscription.stat().st_dev:
            self.skipTest("Two distinct filesystem devices are required")
        second = other_root / "subscriptions"
        second.mkdir()
        music = other_root / "music"
        music.mkdir()
        video = self.file("subscriptions/video.mp4", b"v" * 8000)
        song = music / "song.flac"
        song.write_bytes(b"m" * 8000)
        self.targets["two"] = {**self.targets["one"], "target_id": "two", "directory": str(second)}
        volumes = media_storage(self.targets)["volumes"]
        self.assertEqual(len(volumes), 2)
        roots = [
            (volume["storage_id"], self.targets[identity]["directory"])
            for volume in volumes
            for identity in volume["target_ids"]
        ]
        measured = scan_files(volumes, roots, [str(music)], None, threading.Event())
        first, other = (
            next(volume for volume in volumes if identity in volume["target_ids"])
            for identity in ("one", "two")
        )
        self.assertEqual(measured[first["storage_id"]]["subscription_bytes"], self.bytes(video))
        self.assertEqual(measured[first["storage_id"]]["emby_bytes"], 0)
        self.assertEqual(measured[other["storage_id"]]["subscription_bytes"], 0)
        self.assertEqual(measured[other["storage_id"]]["emby_bytes"], self.bytes(song))


class MappingTests(StorageFixture):
    def test_longest_complete_prefix_and_explicit_override(self):
        mappings = [
            {"server_id": "emby", "server_prefix": "/video", "local_prefix": str(self.root)},
            {
                "server_id": "emby",
                "server_prefix": "/video/subscriptions",
                "local_prefix": str(self.subscription),
            },
        ]
        self.assertEqual(
            map_location("emby", "/video/subscriptions/chinese/片段", mappings),
            self.subscription / "chinese/片段",
        )
        self.assertIsNone(map_location("emby", "/videotest/file.mp4", mappings))
        self.assertIsNone(map_location("other-server", "/video/file.mp4", mappings))
        self.assertIsNone(map_location("emby", "/video/../outside", mappings))
        self.assertIsNone(map_location("emby", "https://outside.invalid/video", mappings))
        mappings.append({**mappings[1], "local_prefix": str(self.root / "override")})
        self.assertEqual(
            map_location("emby", "/video/subscriptions", mappings), self.root / "override"
        )

    def test_config_validates_mappings_without_creating_or_reading_local_roots(self):
        settings = {
            "database_path": str(self.root / "platform.sqlite"),
            "contract_directory": str(self.root / "contracts/text-dialogue/v1"),
            "media": {
                "storage_mappings": [
                    {
                        "server_id": "emby",
                        "server_prefix": "/video/存储库",
                        "local_prefix": str(self.root / "not-mounted"),
                    }
                ]
            },
        }
        parsed = configuration(settings)
        self.assertEqual(parsed["storage_mappings"][0]["server_prefix"], "/video/存储库")
        self.assertFalse((self.root / "not-mounted").exists())
        for mapping in [
            {
                "server_id": "emby",
                "server_prefix": "/video/../private",
                "local_prefix": str(self.root),
            },
            {"server_id": "emby", "server_prefix": "/video", "local_prefix": "relative"},
        ]:
            settings["media"]["storage_mappings"] = [mapping]
            with self.assertRaises(Fault):
                configuration(settings)
        settings["media"]["storage_mappings"] = [parsed["storage_mappings"][0]] * 2
        with self.assertRaises(Fault):
            configuration(settings)


class BackgroundStorageTests(StorageFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.time = [100.0]
        self.media = SimpleNamespace(
            config={"targets": self.targets, "storage_mappings": []},
            local=local,
            servers=SimpleNamespace(libraries=AsyncMock(return_value=[])),
        )
        self.owner = StorageBreakdown(
            self.media, monotonic=lambda: self.time[0], clock=lambda: 1234.0
        )
        self.addAsyncCleanup(self.owner.close)

    def storage(self):
        return {"state": "available", "volumes": [self.volume()]}

    async def settled(self):
        task = self.owner.task
        if task:
            await asyncio.wait_for(asyncio.shield(task), 3)

    async def test_first_view_returns_scanning_then_cache_reuses_snapshot_and_ttl_refreshes(self):
        self.file("subscriptions/source.mp4")
        first = self.owner.view(self.storage())["volumes"][0]["breakdown"]
        self.assertEqual(first["state"], "scanning")
        self.assertIsNone(first["subscription_bytes"])
        self.assertEqual(first["emby_bytes"], 0)
        self.assertEqual(first["unused_bytes"], 900 * 1024)
        await self.settled()
        second = self.owner.view(self.storage())["volumes"][0]["breakdown"]
        self.assertEqual(second["state"], "ready")
        self.assertEqual(second["measured_at"], 1234)
        self.assertFalse(second["refreshing"])
        snapshot_task = self.owner.task
        self.owner.view(self.storage())
        self.assertIs(self.owner.task, snapshot_task)
        self.time[0] += CACHE_SECONDS
        third = self.owner.view(self.storage())["volumes"][0]["breakdown"]
        self.assertEqual(third["state"], "ready")
        self.assertTrue(third["refreshing"])
        await self.settled()

    async def test_view_is_fast_and_concurrent_calls_share_one_scan(self):
        entered, release = threading.Event(), threading.Event()
        scanner = scan_files

        def slow_scan(*args):
            entered.set()
            release.wait(3)
            return scanner(*args)

        with patch(
            "services.platform.media.storage_breakdown.scan_files", side_effect=slow_scan
        ) as scan:
            self.owner.view(self.storage())
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task = self.owner.task
            for _ in range(20):
                self.assertEqual(
                    self.owner.view(self.storage())["volumes"][0]["breakdown"]["state"], "scanning"
                )
                self.assertIs(self.owner.task, task)
            self.assertEqual(scan.call_count, 1)
            release.set()
            await self.settled()

    async def test_emby_locations_map_all_libraries_and_ignore_empty_local_libraries(self):
        self.targets["one"]["servers"] = [dict(SERVER)]
        self.media.config["storage_mappings"] = [
            {"server_id": "emby", "server_prefix": "/video", "local_prefix": str(self.root)}
        ]
        source = self.file("subscriptions/source.mp4")
        other = self.file("movies/other.mp4")
        self.media.servers.libraries.return_value = [
            {"paths": ["/video/subscriptions", "/video/movies"]},
            {"paths": []},
        ]
        self.owner.view(self.storage())
        await self.settled()
        result = self.owner.view(self.storage())["volumes"][0]["breakdown"]
        self.assertEqual(result["subscription_bytes"], self.bytes(source))
        self.assertEqual(result["emby_bytes"], self.bytes(other))
        self.assertEqual(result["state"], "ready")
        self.media.servers.libraries.assert_awaited_once()

    async def test_failed_remote_unmapped_paths_and_missing_mapping_root_are_unknown(self):
        self.targets["one"]["servers"] = [dict(SERVER)]
        self.file("subscriptions/source.mp4")
        cases = [
            (Fault("media_server_unavailable", 503), None, "storage_emby_unavailable"),
            (None, [{"paths": ["/unmapped/library"]}], "storage_path_unmapped"),
            (None, [{"paths": ["/video/not-mounted"]}], "storage_path_unavailable"),
        ]
        self.media.config["storage_mappings"] = [
            {"server_id": "emby", "server_prefix": "/video", "local_prefix": str(self.root)}
        ]
        for exception, libraries, code in cases:
            self.owner.invalidate()
            self.media.servers.libraries.side_effect = exception
            self.media.servers.libraries.return_value = libraries
            self.owner.view(self.storage())
            await self.settled()
            result = self.owner.view(self.storage())["volumes"][0]["breakdown"]
            self.assertEqual(result["state"], "partial")
            self.assertIsNotNone(result["subscription_bytes"])
            self.assertIsNone(result["emby_bytes"])
            self.assertIsNone(result["other_bytes"])
            self.assertEqual(result["code"], code)

    async def test_scope_change_drops_old_numbers_and_prevents_stale_scan_publication(self):
        entered, release = threading.Event(), threading.Event()
        scanner = scan_files
        calls = []

        def paused_scan(*args):
            calls.append(args[-1])
            if len(calls) == 1:
                entered.set()
                release.wait(3)
            return scanner(*args)

        with patch("services.platform.media.storage_breakdown.scan_files", side_effect=paused_scan):
            self.owner.view(self.storage())
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            self.media.config["storage_mappings"] = [
                {"server_id": "unused", "server_prefix": "/video", "local_prefix": str(self.root)}
            ]
            self.assertEqual(
                self.owner.view(self.storage())["volumes"][0]["breakdown"]["state"], "scanning"
            )
            await asyncio.sleep(0.05)
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0].is_set())
            release.set()
            await self.settled()
            self.assertEqual(len(calls), 2)
            self.assertEqual(
                self.owner.view(self.storage())["volumes"][0]["breakdown"]["state"], "ready"
            )

    async def test_close_cooperatively_cancels_thread_and_does_not_publish_late_results(self):
        entered = threading.Event()

        def cancellable_scan(*args):
            entered.set()
            args[-1].wait(3)
            raise ScanCancelled

        with patch(
            "services.platform.media.storage_breakdown.scan_files", side_effect=cancellable_scan
        ):
            self.owner.view(self.storage())
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            await asyncio.wait_for(self.owner.close(), 1)
        self.assertIsNone(self.owner.snapshot)
        self.assertFalse(self.owner.file_work)
        self.assertFalse(self.owner.scan_gate.locked())
        self.assertFalse(self.owner.view(self.storage())["volumes"][0]["breakdown"]["refreshing"])

    async def test_maintenance_refresh_waits_for_result_and_force_reuses_inflight_work(self):
        results = await asyncio.gather(
            self.owner.refresh(force=True), self.owner.refresh(force=True)
        )
        self.assertTrue(
            all(value["volumes"][0]["breakdown"]["state"] == "ready" for value in results)
        )

    async def test_jellyfin_only_is_no_emby_and_capacity_unavailable_is_not_zero(self):
        self.targets["one"]["servers"] = [{**SERVER, "kind": "jellyfin"}]
        self.owner.view(self.storage())
        await self.settled()
        result = self.owner.view(self.storage())["volumes"][0]["breakdown"]
        self.assertEqual(result["emby_bytes"], 0)
        self.media.servers.libraries.assert_not_called()
        volume = self.volume()
        volume.update(state="unavailable", total_bytes=None, free_bytes=None, used_bytes=None)
        result = self.owner.view({"state": "unavailable", "volumes": [volume]})["volumes"][0][
            "breakdown"
        ]
        self.assertEqual(result["state"], "unavailable")
        self.assertIsNone(result["unused_bytes"])

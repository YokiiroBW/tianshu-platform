"""Storage sharing on actual files, including Linux source and target on different devices."""

import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_media_organizer import OrganizerFixture
from services.platform.contracts import Fault
from services.platform.media.destination import subdirectory
from services.platform.media.organizer import FileOrganizer, MARKER
from services.platform.media.video_storage import VideoStorage, copy_file


class HardlinkStorageTests(OrganizerFixture, unittest.TestCase):
    def publish_in(self, folder, key, *, video=b"shared-video", title="title"):
        return self.publish({**self.package(title, video), "destination_subdirectory": folder}, key)

    def video(self, operation):
        return Path(self.target["directory"]) / operation["relative"] / "video.mp4"

    def pool(self):
        return Path(self.target["directory"]) / ".tianshu-transfer" / "videos"

    def legacy(self, folder, key):
        # Reproduce pre-TS126 cross-volume copies with actual receipted publications.
        def install(storage, source, destination, entry, **kwargs):
            copy_file(source, destination)
            return "copy"

        with patch.object(VideoStorage, "install", install):
            return self.publish_in(folder, key, title=folder)

    def assert_shared(self, first, second):
        self.assertTrue(self.video(first).samefile(self.video(second)))
        self.assertGreaterEqual(self.video(first).stat().st_nlink, 3)
        self.assertEqual(first["receipt"]["video_storage"]["mode"], "hardlink")
        self.assertEqual(second["receipt"]["video_storage"]["mode"], "hardlink")
        blobs = list(self.pool().iterdir())
        self.assertEqual(len(blobs), 1)
        self.assertEqual(blobs[0].suffix, "")
        self.assertTrue(blobs[0].samefile(self.video(first)))
        a, b = self.video(first).parent / "movie.nfo", self.video(second).parent / "movie.nfo"
        self.assertFalse(a.samefile(b))
        self.assertNotEqual(a.read_bytes(), b.read_bytes())

    def test_cross_volume_simulation_copies_video_once_and_two_chinese_directories_share(self):
        original_link = os.link

        def cross_volume(source, destination):
            if Path(source).is_relative_to(self.staging):
                raise OSError(errno.EXDEV, "synthetic separate device")
            return original_link(source, destination)

        with (
            patch("services.platform.media.video_storage.os.link", cross_volume),
            patch("services.platform.media.video_storage.copy_file", wraps=copy_file) as copies,
        ):
            first = self.publish_in("UP主/音乐", "up", title="UP主标题")
            second = self.publish_in("收藏夹/音乐", "fav", title="收藏夹标题")
        self.assertEqual(len(copies.call_args_list), 1)
        self.assert_shared(first, second)
        self.video(first).unlink()
        self.assertEqual(self.video(second).read_bytes(), b"shared-video")
        self.assertEqual(next(self.pool().iterdir()).read_bytes(), b"shared-video")

    @unittest.skipIf(
        os.name == "nt", "Linux /tmp and /dev/shm are required for real cross-device validation"
    )
    def test_real_linux_cross_device_copies_once_then_links_both_directories(self):
        if not Path("/dev/shm").is_dir():
            self.skipTest("/dev/shm is unavailable")
        source_temp = tempfile.TemporaryDirectory(dir="/tmp", prefix="tianshu-source-")
        target_temp = tempfile.TemporaryDirectory(dir="/dev/shm", prefix="tianshu-target-")
        self.addCleanup(source_temp.cleanup)
        self.addCleanup(target_temp.cleanup)
        self.staging = Path(source_temp.name)
        self.target["directory"] = str(Path(target_temp.name) / "library")
        if self.staging.stat().st_dev == Path(target_temp.name).stat().st_dev:
            self.skipTest("/tmp and /dev/shm have the same st_dev")
        self.library = FileOrganizer(self.repo, self.contract, self.staging)
        with patch("services.platform.media.video_storage.copy_file", wraps=copy_file) as copies:
            first = self.publish_in("UP主/跨卷", "real-up", title="UP主")
            second = self.publish_in("收藏夹/跨卷", "real-fav", title="收藏夹")
        self.assertEqual(len(copies.call_args_list), 1)
        self.assertNotEqual(self.staging.stat().st_dev, self.video(first).stat().st_dev)
        self.assert_shared(first, second)

    def test_different_content_quality_with_same_length_does_not_share(self):
        high = self.publish_in("高清", "high", video=b"quality-80")
        low = self.publish_in("低清", "low", video=b"quality-64")
        self.assertEqual(self.video(high).stat().st_size, self.video(low).stat().st_size)
        self.assertFalse(self.video(high).samefile(self.video(low)))
        self.assertEqual(len(list(self.pool().iterdir())), 2)

    def test_unsupported_hardlink_copies_and_reports_actual_mode(self):
        with patch(
            "services.platform.media.video_storage.os.link",
            side_effect=OSError(errno.ENOTSUP, "no links"),
        ):
            first = self.publish_in("UP主", "up")
            second = self.publish_in("收藏夹", "fav")
            before = self.video(second).stat().st_ino
            result = self.library.deduplicate(self.target, second["operation_id"])
            again = self.library.deduplicate(self.target, second["operation_id"])
        self.assertEqual(result, again)
        self.assertEqual(before, self.video(second).stat().st_ino)
        self.assertEqual(result["video_storage"]["mode"], "copy")
        self.assertEqual(first["receipt"]["video_storage"]["mode"], "copy")
        self.assertFalse(self.video(first).samefile(self.video(second)))
        self.assertEqual(self.video(first).read_bytes(), self.video(second).read_bytes())

    def test_existing_conversion_is_idempotent_preserves_metadata_and_receipt_identity(self):
        first, second = self.legacy("UP主/旧成品", "up"), self.legacy("收藏夹/旧成品", "fav")
        self.assertFalse(self.video(first).samefile(self.video(second)))
        preserved = {
            path: (path.read_bytes(), path.stat().st_ino)
            for operation in (first, second)
            for path in [
                self.video(operation).parent / "movie.nfo",
                self.video(operation).parent / MARKER,
            ]
        }
        a = self.library.deduplicate(self.target, first["operation_id"])
        b = self.library.deduplicate(self.target, second["operation_id"])
        self.assertTrue(self.video(first).samefile(self.video(second)))
        self.assertEqual(a["video_storage"]["mode"], "hardlink")
        self.assertEqual(b["video_storage"]["mode"], "hardlink")
        before = self.library.read(self.target, second["operation_id"])
        self.assertEqual(self.library.deduplicate(self.target, second["operation_id"]), b)
        self.assertEqual(self.library.read(self.target, second["operation_id"]), before)
        for path, original in preserved.items():
            self.assertEqual((path.read_bytes(), path.stat().st_ino), original)
        for key in ("operation_id", "package_id", "target_relative_path", "files"):
            self.assertEqual(b["receipt"][key], second["receipt"][key])
        self.assertEqual(before["version"], second["version"])
        self.assertEqual(self.repo.job_count(), 0)

    def test_existing_conversion_failure_before_or_after_replace_recovers_without_false_success(
        self,
    ):
        first = self.legacy("UP主", "up")
        self.library.deduplicate(self.target, first["operation_id"])
        original_replace = os.replace
        for after in (False, True):
            with self.subTest(after_replace=after):
                second = self.legacy("收藏夹" + str(after), "fav" + str(after))
                target_video = self.video(second)

                def crash(source, destination):
                    if Path(destination) == target_video:
                        if after:
                            original_replace(source, destination)
                        raise OSError(errno.EIO, "synthetic interruption")
                    return original_replace(source, destination)

                with (
                    patch("services.platform.media.video_storage.os.replace", crash),
                    self.assertRaises(OSError),
                ):
                    self.library.deduplicate(self.target, second["operation_id"])
                self.assertEqual(target_video.read_bytes(), b"shared-video")
                self.assertEqual(
                    self.library.read(self.target, second["operation_id"])["receipt"][
                        "video_storage"
                    ]["mode"],
                    "copy",
                )
                restarted = FileOrganizer(self.repo, self.contract, self.staging)
                result = restarted.deduplicate(self.target, second["operation_id"])
                self.assertEqual(result["video_storage"]["mode"], "hardlink")
                self.assertTrue(self.video(first).samefile(target_video))

    def test_incoming_retry_keeps_shared_video_and_does_not_copy_again(self):
        package = {**self.package(), "destination_subdirectory": "中文/重试"}
        operation = self.library.prepare(self.target, package, "retry")
        original_rename = Path.rename

        def crash(path, destination):
            if path.name.startswith("incoming-"):
                raise OSError(errno.EIO, "synthetic crash before install")
            return original_rename(path, destination)

        with patch.object(Path, "rename", crash), self.assertRaises(OSError):
            self.library.publish(self.target, operation)
        incoming = (
            Path(self.target["directory"])
            / ".tianshu-transfer"
            / ("incoming-" + operation["operation_id"])
            / "video.mp4"
        )
        inode = incoming.stat().st_ino
        with patch("services.platform.media.video_storage.copy_file", wraps=copy_file) as copies:
            result = self.library.publish(
                self.target, self.library.read(self.target, operation["operation_id"])
            )
        self.assertEqual(copies.call_count, 0)
        self.assertEqual(self.video(result).stat().st_ino, inode)
        self.assertEqual(result["receipt"]["video_storage"]["mode"], "hardlink")

    def test_corrupt_pool_is_not_reused_or_modified_in_place(self):
        first = self.publish_in("UP主", "up")
        shared = next(self.pool().iterdir())
        old_inode = shared.stat().st_ino
        shared.write_bytes(b"broken-video")
        second = self.publish_in("收藏夹", "fav")
        self.assertNotEqual(shared.stat().st_ino, old_inode)
        self.assertEqual(self.video(first).stat().st_ino, old_inode)
        self.assertEqual(self.video(first).read_bytes(), b"broken-video")
        self.assertEqual(self.video(second).read_bytes(), b"shared-video")
        with self.assertRaises(Fault):
            self.library.deduplicate(self.target, first["operation_id"])

    def test_conversion_refuses_unowned_or_changed_publication_and_reserved_control_directory(self):
        operation = self.legacy("旧成品", "old")
        note = self.video(operation).parent / "用户文件.txt"
        note.write_text("keep", encoding="utf-8")
        with self.assertRaises(Fault):
            self.library.deduplicate(self.target, operation["operation_id"])
        self.assertEqual(note.read_text(encoding="utf-8"), "keep")
        self.assertEqual(list(self.pool().iterdir()), [])
        for value in (".tianshu-transfer/用户目录", ".TIANSHU-TRANSFER"):
            with self.assertRaises(Fault) as error:
                subdirectory(value)
            self.assertEqual(error.exception.code, "invalid_destination_subdirectory")

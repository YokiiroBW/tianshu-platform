"""NAS Btrfs subvolumes share capacity, but keep their distinct inode namespaces."""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.platform.media.storage import media_storage
from services.platform.media.storage_filesystem import capacity_key, parse_mountinfo, read_mountinfo
from services.platform.media.storage_scan import scan_files

# Mount IDs are synthetic; device/root/mountpoint/source/subvolid come from the
# actual NAS container's 2026-10-13 read-only mountinfo evidence.
NAS_MOUNTINFO = """112 105 0:39 /@syno/Video/存储库/短视频/bilibili /media/Bilibili rw,nodev,relatime - btrfs /dev/mapper/cachedev_1 rw,subvolid=260,subvol=/@syno/Video
113 105 0:39 /@syno/Video/存储库 /media/observe/emby-video ro,relatime - btrfs /dev/mapper/cachedev_1 rw,subvolid=260,subvol=/@syno/Video
114 105 0:39 /@syno/Music/音乐存档 /media/observe/emby-music ro,relatime - btrfs /dev/mapper/cachedev_1 rw,subvolid=264,subvol=/@syno/Music
"""
VIDEO_DEVICE, MUSIC_DEVICE = 6291518, 7340078


class FilesystemIdentityTests(unittest.TestCase):
    def test_actual_nas_devices_differ_from_mountinfo_but_share_btrfs_capacity(self):
        mounts = parse_mountinfo(NAS_MOUNTINFO)
        with patch("services.platform.media.storage_filesystem.LINUX", True):
            video = capacity_key("/media/Bilibili", VIDEO_DEVICE, mounts)
            music = capacity_key("/media/observe/emby-music/音乐", MUSIC_DEVICE, mounts)
        self.assertEqual(video, ("btrfs", "0:39", "/dev/mapper/cachedev_1"))
        self.assertEqual(video, music)
        self.assertNotEqual(VIDEO_DEVICE, MUSIC_DEVICE)

    def test_longest_boundary_match_decodes_escapes_and_honors_overmounted_other_filesystem(self):
        document = (
            NAS_MOUNTINFO
            + r"""115 105 0:40 /space\040root /media/with\040space ro shared:1 - btrfs /dev/mapper/cache\134name rw
116 114 8:1 / /media/observe/emby-music/external ro - ext4 /dev/sda1 ro
117 105 0:41 / /media/with\134name ro - btrfs /dev/mapper/another rw
"""
        )
        mounts = parse_mountinfo(document)
        with patch("services.platform.media.storage_filesystem.LINUX", True):
            self.assertEqual(
                capacity_key("/media/with space/library", 99, mounts),
                ("btrfs", "0:40", "/dev/mapper/cache\\name"),
            )
            self.assertEqual(
                capacity_key("/media/observe/emby-music/external/library", 101, mounts),
                ("device", 101),
            )
            self.assertEqual(
                capacity_key("/media/observe/emby-music-other", 102, mounts), ("device", 102)
            )
            self.assertEqual(
                capacity_key("/media/with\\name/library", 103, mounts),
                ("btrfs", "0:41", "/dev/mapper/another"),
            )

    def test_non_linux_missing_mountinfo_and_untrusted_source_fall_back_to_device(self):
        mounts = parse_mountinfo(NAS_MOUNTINFO)
        with (
            patch("services.platform.media.storage_filesystem.LINUX", False),
            patch("services.platform.media.storage_filesystem.Path.read_text") as read,
        ):
            self.assertEqual(read_mountinfo(), [])
            self.assertEqual(
                capacity_key("/media/Bilibili", VIDEO_DEVICE, mounts), ("device", VIDEO_DEVICE)
            )
            read.assert_not_called()
        with patch("services.platform.media.storage_filesystem.LINUX", True):
            self.assertEqual(
                capacity_key("/media/Bilibili", VIDEO_DEVICE, []), ("device", VIDEO_DEVICE)
            )
            unknown = [{**mounts[0], "source": "none"}]
            self.assertEqual(
                capacity_key("/media/Bilibili", VIDEO_DEVICE, unknown), ("device", VIDEO_DEVICE)
            )
            with patch(
                "services.platform.media.storage_filesystem.Path.read_text",
                side_effect=PermissionError,
            ):
                self.assertEqual(read_mountinfo(), [])


class BtrfsAccountingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.video, self.music = self.root / "video", self.root / "music"
        for root in (self.video, self.music):
            root.mkdir()
            (root / "ordinary-file").write_bytes(b"synthetic metadata accounting")
        self.targets = {
            name: {"target_id": name, "kind": "media_server", "label": name, "directory": str(root)}
            for name, root in (("video", self.video), ("music", self.music))
        }

    def observed(self, path, value):
        path = Path(path)
        device = (
            VIDEO_DEVICE
            if path.is_relative_to(self.video)
            else MUSIC_DEVICE
            if path.is_relative_to(self.music)
            else value.st_dev
        )
        return SimpleNamespace(
            st_mode=value.st_mode,
            st_dev=device,
            st_ino=1 if device in (VIDEO_DEVICE, MUSIC_DEVICE) else value.st_ino,
            st_size=value.st_size,
            st_blocks=2,
        )

    def context(self, *, music_source="/dev/mapper/cachedev_1"):
        mounts = [
            {
                "device": "0:39",
                "mountpoint": self.video.as_posix(),
                "filesystem": "btrfs",
                "source": "/dev/mapper/cachedev_1",
            },
            {
                "device": "0:39",
                "mountpoint": self.music.as_posix(),
                "filesystem": "btrfs",
                "source": music_source,
            },
        ]
        actual_stat, actual_lstat, actual_scandir = Path.stat, Path.lstat, os.scandir
        observed = self.observed

        class Entry:
            def __init__(self, entry):
                self.entry, self.path = entry, entry.path

            def stat(self, **kwargs):
                return observed(self.path, self.entry.stat(**kwargs))

        class Entries:
            def __init__(self, path):
                self.entries = actual_scandir(path)

            def __enter__(self):
                return (Entry(entry) for entry in self.entries)

            def __exit__(self, *args):
                self.entries.close()

        from contextlib import ExitStack

        context = ExitStack()
        context.enter_context(patch("services.platform.media.storage_filesystem.LINUX", True))
        context.enter_context(
            patch("services.platform.media.storage.read_mountinfo", return_value=mounts)
        )
        context.enter_context(
            patch("services.platform.media.storage_scan.read_mountinfo", return_value=mounts)
        )
        context.enter_context(
            patch(
                "services.platform.media.storage.shutil.disk_usage",
                return_value=SimpleNamespace(total=1000000, used=500000, free=500000),
            )
        )
        context.enter_context(
            patch.object(
                Path, "stat", lambda path, **kwargs: observed(path, actual_stat(path, **kwargs))
            )
        )
        context.enter_context(
            patch.object(Path, "lstat", lambda path: observed(path, actual_lstat(path)))
        )
        context.enter_context(
            patch("services.platform.media.storage_scan.os.scandir", side_effect=Entries)
        )
        return context

    def test_same_btrfs_source_groups_capacity_but_different_devices_inode_one_are_both_counted(
        self,
    ):
        with self.context():
            storage = media_storage(self.targets)
            self.assertEqual(len(storage["volumes"]), 1)
            volume = storage["volumes"][0]
            measured = scan_files(
                [volume],
                [(volume["storage_id"], str(self.video))],
                [str(self.music)],
                None,
                threading.Event(),
            )[volume["storage_id"]]
        self.assertEqual(measured["subscription_bytes"], 1024)
        self.assertEqual(measured["emby_bytes"], 1024)

    def test_music_observation_root_counts_in_video_capacity_without_a_music_target(self):
        with self.context():
            volume = media_storage({"video": self.targets["video"]})["volumes"][0]
            measured = scan_files(
                [volume],
                [(volume["storage_id"], str(self.video))],
                [str(self.music)],
                None,
                threading.Event(),
            )[volume["storage_id"]]
        self.assertEqual(measured["subscription_bytes"], 1024)
        self.assertEqual(measured["emby_bytes"], 1024)

    def test_different_source_is_separate_even_when_reported_total_and_free_are_equal(self):
        with self.context(music_source="/dev/mapper/cachedev_2"):
            storage = media_storage(self.targets)
            self.assertEqual(len(storage["volumes"]), 2)
            volumes = storage["volumes"]
            self.assertEqual(volumes[0]["total_bytes"], volumes[1]["total_bytes"])
            self.assertEqual(volumes[0]["free_bytes"], volumes[1]["free_bytes"])
            self.assertNotEqual(volumes[0]["storage_id"], volumes[1]["storage_id"])
            video = next(volume for volume in volumes if "video" in volume["target_ids"])
            measured = scan_files(
                [video],
                [(video["storage_id"], str(self.video))],
                [str(self.music)],
                None,
                threading.Event(),
            )[video["storage_id"]]
            self.assertEqual(measured["subscription_bytes"], 1024)
            self.assertEqual(measured["emby_bytes"], 0)

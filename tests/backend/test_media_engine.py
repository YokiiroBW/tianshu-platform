"""Real pinned yt-dlp metadata serialization with isolated subprocess fixtures."""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yt_dlp import YoutubeDL, parse_options

from services.platform.contracts import Fault
from services.platform.media.engine import Engine


# Safe selected-format fields from the actual 2026.08.19 BV/P extraction.
# No URLs, headers, cookies, title or account data are included.
MERGED_INFO = {
    "id": "BV1ibdVBKEMe_p1",
    "format_id": "30080+30280",
    "vcodec": "avc1.640032",
    "acodec": "mp4a.40.2",
    "width": 1080,
    "height": 1920,
    "ext": "mp4",
    "requested_formats": [
        {
            "format_id": "30080",
            "quality": 80,
            "vcodec": "avc1.640032",
            "acodec": "none",
            "width": 1080,
            "height": 1920,
            "ext": "mp4",
        },
        {"format_id": "30280", "vcodec": "none", "acodec": "mp4a.40.2", "ext": "m4a"},
    ],
}


class MediaEngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="media-engine-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.engine = Engine(
            {
                "python": sys.executable,
                "ffmpeg": "ffmpeg",
                "ffprobe": "ffprobe",
                "max_bytes": 1024,
                "timeout_seconds": 30,
            }
        )
        self.job = {
            "bvid": "BV1ibdVBKEMe",
            "quality": {"mode": "exact", "quality_id": "80", "allow_fallback": False},
        }
        self.part = {"index": 1, "cid": "37584830724", "duration_seconds": 2}
        self.info = copy.deepcopy(MERGED_INFO)
        self.commands = []
        self.media_size = 32
        self.probe = {
            "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
            "format": {"duration": "2"},
        }

    async def _process(self, command, **kwargs):
        self.commands.append(command)
        if command[1:3] == ["-m", "yt_dlp"]:
            # Parse the real CLI flags and use the real writer. Without the new
            # flag, this reproduces the cleaned disk JSON that lost quality.
            options = parse_options(command[3:]).ydl_opts
            with YoutubeDL({**options, "quiet": True}) as downloader:
                downloader._write_info_json(
                    "video", copy.deepcopy(self.info), str(self.directory / "download.info.json")
                )
            (self.directory / "download.mp4").write_bytes(b"x" * self.media_size)
            return 0, b"", b""
        self.assertEqual("ffprobe", command[0])
        return 0, json.dumps(self.probe).encode(), b""

    async def _download(self):
        with patch("services.platform.media.engine.run_process", self._process):
            return await self.engine.download(
                self.job, self.part, "synthetic=value", self.directory
            )

    def _assert_temporary_metadata_removed(self):
        self.assertFalse((self.directory / ".session.txt").exists())
        self.assertFalse(list(self.directory.glob("*.info.json")))

    async def test_actual_merged_info_json_keeps_selected_quality_and_verifies_media(self):
        self.assertNotIn("quality", self.info)
        cleaned = YoutubeDL.sanitize_info(copy.deepcopy(self.info), remove_private_keys=True)
        self.assertNotIn("requested_formats", cleaned)
        result = await self._download()
        self.assertEqual("80", result["actual_quality"])
        self.assertEqual("mp4", result["extension"])
        self.assertIn("--no-clean-info-json", self.commands[0])
        self.assertEqual("ffprobe", self.commands[1][0])
        self._assert_temporary_metadata_removed()

    async def test_exact_mismatch_is_not_replaced_by_requested_quality(self):
        self.info["requested_formats"][0]["quality"] = 64
        with self.assertRaises(Fault) as error:
            await self._download()
        self.assertEqual("quality_mismatch", error.exception.code)
        self.assertTrue((self.directory / "download.mp4").exists())
        self._assert_temporary_metadata_removed()

    async def test_allowed_fallback_returns_actual_selected_quality(self):
        self.job["quality"]["allow_fallback"] = True
        self.info["requested_formats"][0]["quality"] = 64
        result = await self._download()
        self.assertEqual("64", result["actual_quality"])

    async def test_progressive_video_quality_remains_supported(self):
        self.info.pop("requested_formats")
        self.info["quality"] = 80.0
        result = await self._download()
        self.assertEqual("80", result["actual_quality"])

    async def test_missing_or_audio_only_quality_cannot_verify_video(self):
        for video in (
            {"vcodec": "avc1.640032"},
            {"quality": 80},
            {"vcodec": "none", "quality": 80},
            {"vcodec": "avc1.640032", "quality": True},
            {"vcodec": "avc1.640032", "quality": 80.5},
        ):
            with self.subTest(video=video):
                self.info["requested_formats"] = [video]
                with self.assertRaises(Fault) as error:
                    await self._download()
                self.assertEqual("quality_unverified", error.exception.code)
                self._assert_temporary_metadata_removed()

    async def test_downloaded_bv_and_part_must_match_the_requested_identity(self):
        for identity in ("BV1ibdVBKEMe_p2", "BV1Q541167Qg_p1", "BV1ibdVBKEMe", None):
            with self.subTest(identity=identity):
                self.info["id"] = identity
                with self.assertRaises(Fault) as error:
                    await self._download()
                self.assertEqual("source_identity_changed", error.exception.code)
                self._assert_temporary_metadata_removed()

    async def test_file_size_and_ffprobe_validation_still_apply(self):
        self.media_size = 1025
        with self.assertRaises(Fault) as error:
            await self._download()
        self.assertEqual("download_invalid_output", error.exception.code)
        self.media_size = 32
        self.probe["streams"] = [{"codec_type": "video"}]
        with self.assertRaises(Fault) as error:
            await self._download()
        self.assertEqual("media_audio_missing", error.exception.code)
        self._assert_temporary_metadata_removed()


if __name__ == "__main__":
    unittest.main()

"""Real pinned yt-dlp metadata serialization with isolated subprocess fixtures."""

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from yt_dlp import YoutubeDL, parse_options
from yt_dlp.postprocessor.ffmpeg import FFmpegMergerPP, FFmpegPostProcessor

from services.platform.contracts import Fault
from services.platform.media.config import ENGINE_VERSION
from services.platform.media.engine import Engine, executable_path, run_process


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
        tools = self.directory / "tools"
        tools.mkdir()
        suffix = ".exe" if os.name == "nt" else ""
        for name in ("ffmpeg", "ffprobe"):
            tool = tools / (name + suffix)
            tool.write_bytes(b"synthetic executable fixture")
            tool.chmod(0o755)
        self.engine = Engine(
            {
                "python": sys.executable,
                "ffmpeg": str(tools / ("ffmpeg" + suffix)),
                "ffprobe": str(tools / ("ffprobe" + suffix)),
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
        self.output_name = "download.mp4"
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
            (self.directory / self.output_name).write_bytes(b"x" * self.media_size)
            return 0, b"", b""
        self.assertEqual(self.engine.config["ffprobe"], command[0])
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
        self.assertEqual(self.engine.config["ffprobe"], self.commands[1][0])
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

    async def test_missing_tool_is_unavailable_for_check_and_download(self):
        self.engine.config["ffmpeg"] = str(self.directory / "missing-ffmpeg")
        with patch(
            "services.platform.media.engine.run_process",
            AsyncMock(return_value=(0, ENGINE_VERSION.encode(), b"")),
        ):
            self.assertEqual("engine_unavailable", (await self.engine.check())["code"])
        with self.assertRaises(Fault) as error:
            await self._download()
        self.assertEqual("engine_unavailable", error.exception.code)
        self.assertFalse((self.directory / ".session.txt").exists())

    async def test_unmerged_video_fragment_is_not_a_finished_download(self):
        self.output_name = "download.f30112.mp4"
        (self.directory / "download.f30280.m4a").write_bytes(b"synthetic audio fragment")
        with self.assertRaises(Fault) as error:
            await self._download()
        self.assertEqual("download_invalid_output", error.exception.code)
        self.assertEqual(1, len(self.commands))
        self.assertTrue((self.directory / self.output_name).exists())
        self.assertTrue((self.directory / "download.f30280.m4a").exists())
        self._assert_temporary_metadata_removed()

    async def test_finished_mkv_remains_supported_with_unmerged_residue_present(self):
        self.output_name = "download.mkv"
        (self.directory / "download.f30112.mp4").write_bytes(b"old synthetic video fragment")
        result = await self._download()
        self.assertEqual("mkv", result["extension"])
        self.assertEqual(str(self.directory / "download.mkv"), result["file"])
        self.assertTrue((self.directory / "download.f30112.mp4").exists())


class MediaEngineToolIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="media-engine-tools-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        command = os.environ.get("MEDIA_TEST_FFMPEG") or shutil.which("ffmpeg")
        if not command or not Path(command).is_file():
            self.skipTest("real ffmpeg not configured")
        self.ffmpeg = str(Path(command).resolve())
        self.ffprobe = executable_path(
            os.environ.get("MEDIA_TEST_FFPROBE")
            or str(Path(command).with_name("ffprobe" + Path(command).suffix))
        )

    def test_basename_and_explicit_paths_are_usable_by_pinned_yt_dlp(self):
        with patch.dict(
            os.environ,
            {"PATH": str(Path(self.ffmpeg).parent) + os.pathsep + os.environ.get("PATH", "")},
        ):
            for command in ("ffmpeg", self.ffmpeg):
                with self.subTest(command=command):
                    resolved = executable_path(command)
                    self.assertEqual(self.ffmpeg, resolved)
                    with YoutubeDL({"ffmpeg_location": resolved, "quiet": True}) as downloader:
                        processor = FFmpegPostProcessor(downloader)
                        self.assertTrue(processor.available)
                        self.assertEqual(resolved, processor.executable)

    async def test_path_basename_drives_real_yt_dlp_merge_and_ffprobe_verification(self):
        video = self.directory / "download.f30080.mp4"
        audio = self.directory / "download.f30280.m4a"
        for arguments in (
            ["-f", "lavfi", "-i", "color=c=blue:s=160x90:d=1", "-an", "-c:v", "mpeg4", str(video)],
            [
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1",
                "-vn",
                "-c:a",
                "aac",
                str(audio),
            ],
        ):
            subprocess.run(
                [self.ffmpeg, "-v", "error", *arguments],
                check=True,
                capture_output=True,
                timeout=30,
            )
        engine = Engine(
            {
                "python": sys.executable,
                "ffmpeg": "ffmpeg",
                "ffprobe": "ffprobe",
                "max_bytes": 1024**2,
                "timeout_seconds": 30,
            }
        )
        info = copy.deepcopy(MERGED_INFO)
        for value in info["requested_formats"]:
            value["protocol"] = "https"
        merge_info = {
            **info,
            "filepath": str(self.directory / "download.mp4"),
            "__files_to_merge": [str(video), str(audio)],
        }

        async def merge_locally(command, **kwargs):
            if command[1:3] == ["-m", "yt_dlp"]:
                options = parse_options(command[3:]).ydl_opts
                self.assertEqual(self.ffmpeg, options["ffmpeg_location"])
                with YoutubeDL({**options, "quiet": True}) as downloader:
                    downloader._write_info_json(
                        "video", copy.deepcopy(info), str(self.directory / "download.info.json")
                    )
                    FFmpegMergerPP(downloader).run(merge_info)
                return 0, b"", b""
            return await run_process(command, **kwargs)

        with (
            patch.dict(
                os.environ,
                {"PATH": str(Path(self.ffmpeg).parent) + os.pathsep + os.environ.get("PATH", "")},
            ),
            patch("services.platform.media.engine.run_process", merge_locally),
        ):
            result = await engine.download(
                {
                    "bvid": "BV1ibdVBKEMe",
                    "quality": {"mode": "exact", "quality_id": "80", "allow_fallback": False},
                },
                {"index": 1, "duration_seconds": 1},
                "",
                self.directory,
            )
        self.assertEqual("80", result["actual_quality"])
        self.assertEqual(str(self.directory / "download.mp4"), result["file"])
        self.assertTrue(video.exists())
        self.assertTrue(audio.exists())


if __name__ == "__main__":
    unittest.main()

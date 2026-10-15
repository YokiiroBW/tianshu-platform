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
from yt_dlp.utils import get_compatible_ext

from services.platform.contracts import Fault
from services.platform.media.config import ENGINE_VERSION
from services.platform.media.engine import Engine, executable_path, file_record, run_process


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

    async def test_constrained_selection_and_actual_output_share_one_policy(self):
        self.job["quality"]["stream_options"] = {
            "codecs": ["avc"],
            "audio_max_quality": "30280",
            "allow_hdr": False,
        }
        self.probe["streams"] = [
            {"codec_type": "video", "codec_name": "h264", "color_transfer": "bt709"},
            {"codec_type": "audio", "codec_name": "aac"},
        ]
        formats = copy.deepcopy(self.info["requested_formats"])
        with patch.object(self.engine, "format_info", AsyncMock(return_value=formats)):
            result = await self._download()
        command = self.commands[0]
        self.assertEqual(command[command.index("--format") + 1], "30080+30280")
        self.assertEqual(result["stream_evidence"]["audio_quality"], "30280")
        self.probe["streams"][0]["codec_name"] = "hevc"
        with (
            patch.object(self.engine, "format_info", AsyncMock(return_value=formats)),
            self.assertRaises(Fault) as raised,
        ):
            await self._download()
        self.assertEqual(raised.exception.code, "quality_mismatch")

    async def test_probe_filters_the_same_allowed_pairs_as_download_selection(self):
        formats = copy.deepcopy(self.info["requested_formats"])
        formats.append(
            {**formats[0], "quality": 125, "format_id": "30125", "dynamic_range": "HDR10"}
        )
        with patch.object(self.engine, "format_info", AsyncMock(return_value=formats)):
            projected = await self.engine.formats(
                self.job["bvid"],
                self.part,
                "",
                self.directory / "probe",
                requested={**self.job["quality"], "stream_options": {"allow_hdr": False}},
            )
            self.assertEqual([value["quality_id"] for value in projected], ["80"])
            with self.assertRaises(Fault) as raised:
                await self.engine.formats(
                    self.job["bvid"],
                    self.part,
                    "",
                    self.directory / "probe",
                    requested={
                        **self.job["quality"],
                        "stream_options": {"audio_min_quality": "30251"},
                    },
                )
            self.assertEqual(raised.exception.code, "quality_unavailable")

    async def test_remux_targets_only_mov_text_subtitle_index_and_keeps_compatible_tracks(self):
        source = self.directory / "source.mp4"
        source.write_bytes(b"synthetic verified source")
        record = file_record(source, "cache", "video", "123")
        before = {
            "streams": [
                {"codec_type": "video", "codec_name": "h264"},
                {"codec_type": "audio", "codec_name": "aac"},
                *(
                    {"codec_type": "subtitle", "codec_name": codec}
                    for codec in ("subrip", "mov_text", "ass")
                ),
            ],
            "format": {"duration": "2"},
        }
        after = copy.deepcopy(before)
        after["streams"][3]["codec_name"] = "ass"
        commands = []

        async def process(command, **kwargs):
            commands.append(command)
            Path(command[-1]).write_bytes(b"synthetic remux result")
            return 0, b"", b""

        download = {
            "file": str(source),
            "actual_quality": "80",
            "extension": "mp4",
            "sha256": record["sha256"],
            "size_bytes": record["size_bytes"],
        }
        output = self.directory / "private.mkv"
        with (
            patch.object(self.engine, "verify", AsyncMock(side_effect=[before, after])),
            patch("services.platform.media.engine.run_process", process),
        ):
            result = await self.engine.remux(download, output, 2)
        command = commands[0]
        self.assertEqual(command[command.index("-c") + 1], "copy")
        self.assertEqual(command[command.index("-c:s:1") + 1], "ass")
        self.assertNotIn("-c:s:0", command)
        self.assertNotIn("-c:s:2", command)
        self.assertEqual(
            result["remux_source"]["subtitle_conversions"],
            [{"stream": 1, "from": "mov_text", "to": "ass"}],
        )
        after["streams"].pop()
        with (
            patch.object(self.engine, "verify", AsyncMock(side_effect=[before, after])),
            patch("services.platform.media.engine.run_process", process),
            self.assertRaises(Fault) as raised,
        ):
            await self.engine.remux(download, output, 2)
        self.assertEqual(raised.exception.code, "quality_mismatch")
        self.assertFalse(output.exists())
        self.assertEqual(source.read_bytes(), b"synthetic verified source")


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

    async def test_real_flac_uses_matroska_and_preserves_audio_without_transcoding(self):
        video = self.directory / "video.mp4"
        audio = self.directory / "audio.flac"
        for args in (
            [
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=160x90:d=1",
                "-an",
                "-c:v",
                "libx264",
                str(video),
            ],
            [
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1",
                "-vn",
                "-c:a",
                "flac",
                str(audio),
            ],
        ):
            subprocess.run(
                [self.ffmpeg, "-v", "error", *args], check=True, capture_output=True, timeout=30
            )
        info = copy.deepcopy(MERGED_INFO)
        info["requested_formats"][1].update(format_id="30251", acodec="flac", ext="flac")
        info["acodec"] = "flac"
        for value in info["requested_formats"]:
            value["protocol"] = "https"
        engine = Engine(
            {
                "python": sys.executable,
                "ffmpeg": self.ffmpeg,
                "ffprobe": self.ffprobe,
                "max_bytes": 1024**2,
                "timeout_seconds": 30,
            }
        )

        async def merge(command, **kwargs):
            if command[1:3] != ["-m", "yt_dlp"]:
                return await run_process(command, **kwargs)
            cli = parse_options(command[3:]).ydl_opts
            extension = get_compatible_ext(
                vcodecs=["avc1.640032"],
                acodecs=["flac"],
                vexts=["mp4"],
                aexts=["flac"],
                preferences=cli["merge_output_format"].split("/"),
            )
            self.assertEqual(extension, "mkv")
            with YoutubeDL({**cli, "quiet": True}) as downloader:
                downloader._write_info_json(
                    "video", copy.deepcopy(info), str(self.directory / "download.info.json")
                )
                FFmpegMergerPP(downloader).run(
                    {
                        **info,
                        "filepath": str(self.directory / "download.mkv"),
                        "__files_to_merge": [str(video), str(audio)],
                    }
                )
            return 0, b"", b""

        request = {
            "bvid": "BV1ibdVBKEMe",
            "quality": {
                "mode": "exact",
                "quality_id": "80",
                "allow_fallback": False,
                "stream_options": {"audio_min_quality": "30251"},
            },
        }
        with (
            patch.object(engine, "format_info", AsyncMock(return_value=info["requested_formats"])),
            patch("services.platform.media.engine.run_process", merge),
        ):
            result = await engine.download(
                request, {"index": 1, "duration_seconds": 1}, "", self.directory
            )
        self.assertEqual(result["extension"], "mkv")
        self.assertEqual(result["media"]["audio_tracks"][0]["codec_name"], "flac")
        self.assertEqual(result["stream_evidence"]["audio_quality"], "30251")
        self.assertEqual(result["media"]["video_streams"][0]["codec_name"], "h264")


if __name__ == "__main__":
    unittest.main()

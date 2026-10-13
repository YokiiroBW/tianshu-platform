"""Synthetic historical NFO preparation and path-owned server observations."""

import asyncio
import base64
import copy
import tempfile
import threading
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from xml.etree import ElementTree as ET

from PIL import Image

from media._fixtures import BV, CID_ONE, CID_TWO, MID_UP, OTHER_BV, snapshot
from services.platform.contracts import Fault
from services.platform.media.creator_cache import CreatorCache
from services.platform.media.creators import director_name
from services.platform.media.engine import Engine
from services.platform.media.import_metadata import ImportMetadata
from services.platform.media.import_metadata_files import save_candidate
from services.platform.media.import_nfo import render
from services.platform.media.import_records import observation
from services.platform.media.imports import Imports, item_id
from services.platform.media.probe_details import media_details
from services.platform.media.repository import Repository

INFO = {
    "format": {"duration": "386.878"},
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920,
            "height": 1080,
            "avg_frame_rate": "30000/1001",
            "sample_aspect_ratio": "1:1",
        },
        {"codec_type": "audio", "codec_name": "aac", "channels": 2, "tags": {"language": "zho"}},
    ],
}
MOVIE = b"""<?xml version="1.0" encoding="UTF-8"?>
<movie data="keep"><!--keep comment--><plot>old plot</plot><outline>old outline</outline>
<title>Original title</title><actor><name>Old author</name><role>arbitrary role</role><thumb>https://old/face</thumb></actor>
<uniqueid type="bilibili">BV1xx411c7mD</uniqueid><year>2023</year><premiered>2023-11-16</premiered>
<thumb>poster.jpg</thumb><custom key="keep">value</custom><fileinfo><custom>keep</custom></fileinfo></movie>"""
EPISODE = b"<episodedetails><plot/><outline/><title>Original episode</title><season>1</season><episode>8</episode></episodedetails>"
SHOW = MOVIE.replace(b"<movie ", b"<tvshow ").replace(b"</movie>", b"</tvshow>")


async def local(function, *args, **kwargs):
    return await asyncio.to_thread(function, *args, **kwargs)


class ImportMetadataTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="legacy-nfo-synthetic-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.library = self.root / "read-only-library"
        self.library.mkdir()
        self.media = SimpleNamespace(
            config={
                "staging_directory": str(self.root / "staging"),
                "targets": {
                    "target": {
                        "servers": [
                            {
                                "server_id": "emby",
                                "kind": "emby",
                                "library_id": "different-library",
                                "media_root": "/wrong/new/library",
                                "base_url": "https://synthetic.invalid",
                                "token": "synthetic",
                            }
                        ]
                    }
                },
                "import_roots": [
                    {"root_id": "old", "label": "Synthetic", "directory": str(self.library)}
                ],
            },
            local=local,
            repo=Repository(self.root / "media.sqlite"),
            engine=Engine({}),
            connector=SimpleNamespace(resolve=AsyncMock()),
            remote=SimpleNamespace(
                request=AsyncMock(), image=AsyncMock(), upload_image=AsyncMock()
            ),
        )
        self.media.engine.probe = AsyncMock(return_value=copy.deepcopy(INFO))
        self.media.engine.download = AsyncMock(side_effect=AssertionError("no downloads"))
        self.media.imports = Imports(self.media)
        self.media.creator_cache = CreatorCache(self.media, self.root / "avatars")
        self.media.import_metadata = ImportMetadata(self.media, self.root / "plans")
        picture = BytesIO()
        Image.new("RGB", (32, 32), "blue").save(picture, "PNG")
        self.picture = picture.getvalue()
        self.fetch = patch(
            "services.platform.media.creator_cache.fetch_image",
            AsyncMock(return_value=self.picture),
        )
        self.fetcher = self.fetch.start()
        self.addCleanup(self.fetch.stop)
        self.resolved = {
            "snapshot": snapshot(
                description="new description",
                parts=[
                    {
                        "cid": CID_ONE,
                        "index": 1,
                        "title": "New source title",
                        "duration_seconds": 5820,
                    },
                    {"cid": CID_TWO, "index": 2, "title": "Second", "duration_seconds": 386},
                ],
            ),
            "creator_images": [
                {"mid": MID_UP, "url": "https://i0.hdslb.com/bfs/face/synthetic.png"}
            ],
        }
        self.media.connector.resolve.return_value = self.resolved

    def video(self, relative="movie.mp4", cid=CID_ONE, bvid=BV, *, invalid=False, with_probe=True):
        path = self.library / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic video bytes " + relative.encode())
        record = {
            "root_id": "old",
            "directory": str(self.library),
            "relative_path": relative,
            "bvid": bvid,
            "cid": cid,
            "part_index": 1,
            "state": "invalid" if invalid else "unknown",
            "code": "media_duration_mismatch" if invalid else "import_quality_unknown",
            "actual_quality": None,
            "sha256": None,
            "identity_evidence": {"kind": "bili_sync_page", "expected_duration_seconds": 5820},
            "observation": observation(path),
            "media": media_details(INFO) if with_probe else None,
        }
        return self.media.repo.put("media_import", item_id("old", relative), record)

    def nfo(self, relative="movie.nfo", raw=MOVIE):
        path = self.library / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path

    async def prepare(self, **kwargs):
        return await self.media.import_metadata.prepare(
            "old", plan_id="synthetic", request_interval=0, **kwargs
        )

    def xml(self, entry):
        return ET.fromstring(base64.b64decode(entry["content_base64"]))

    async def test_preserves_existing_xml_unknown_quality_and_all_non_import_facts(self):
        record = self.video()
        original = self.nfo()
        self.media.repo.put("target_head", "unchanged", {"operation_id": "existing"})
        before = {
            kind: self.media.repo.list(kind)
            for kind in ("media_import", "media_source", "target_head", "file_transfer")
        }
        result = await self.prepare(resolved_by_bvid={BV: self.resolved})
        self.assertEqual("completed", result["state"])
        self.assertEqual(1, result["counts"]["ready"])
        entry = result["files"][0]
        xml = self.xml(entry)
        self.assertEqual("Original title", xml.findtext("title"))
        self.assertEqual("new description", xml.findtext("plot"))
        self.assertEqual("new description", xml.findtext("outline"))
        for xpath in (
            "actor/name",
            "actor/role",
            "actor/thumb",
            "uniqueid",
            "year",
            "premiered",
            "thumb",
            "custom",
            "fileinfo/custom",
        ):
            self.assertEqual(ET.fromstring(MOVIE).findtext(xpath), xml.findtext(xpath))
        self.assertIn(b"<!--keep comment-->", base64.b64decode(entry["content_base64"]))
        self.assertEqual("keep", xml.get("data"))
        self.assertEqual(director_name("测试UP主", MID_UP), xml.findtext("director"))
        self.assertEqual("unknown", xml.findtext("tianshu/source/quality_state"))
        self.assertIsNone(xml.find("tianshu/source/actual_quality"))
        self.assertIsNone(xml.find("tianshu/source/sha256"))
        self.assertEqual(CID_ONE, xml.find("tianshu/source").get("cid"))
        self.assertEqual("1920", xml.findtext("fileinfo/streamdetails/video/width"))
        self.assertEqual("2", xml.findtext("fileinfo/streamdetails/audio/channels"))
        self.assertEqual(MOVIE, original.read_bytes())
        self.assertEqual(record["observation"], observation(self.library / "movie.mp4"))
        for kind, rows in before.items():
            self.assertEqual(rows, self.media.repo.list(kind))
        self.assertEqual(0, self.media.repo.job_count())

        self.media.engine.download.assert_not_awaited()
        self.media.engine.probe.assert_not_awaited()
        self.media.connector.resolve.assert_not_awaited()

    async def test_multipart_parent_show_has_all_observations_and_no_part_streams(self):
        self.video("show/Season1/P1.mp4")
        self.video("show/Season1/P2.mp4", CID_TWO)
        self.nfo("show/Season1/P1.nfo", EPISODE)
        self.nfo("show/Season1/P2.nfo", EPISODE)
        self.nfo("show/tvshow.nfo", SHOW)
        result = await self.prepare()
        self.assertEqual((3, 2), (result["counts"]["files"], result["counts"]["videos"]))
        self.assertEqual(1, self.media.connector.resolve.await_count)
        self.media.connector.resolve.assert_awaited_once_with(BV, with_formats=False)
        self.assertEqual(1, self.fetcher.await_count)
        show = next(entry for entry in result["files"] if entry["type"] == "tvshow")
        self.assertEqual(2, len(show["video_observations"]))
        self.assertEqual(2, len(self.xml(show).findall("tianshu/source/parts/part")))
        self.assertIsNone(self.xml(show).find("fileinfo/streamdetails"))
        episode = self.xml(
            next(entry for entry in result["files"] if entry["type"] == "episodedetails")
        )
        self.assertIsNone(episode.find("uniqueid"))
        self.assertEqual(
            ("1", "8", "Original episode"),
            tuple(episode.findtext(tag) for tag in ("season", "episode", "title")),
        )

    async def test_duration_invalid_stays_invalid_and_provenance_is_explicit(self):
        record = self.video(invalid=True)
        self.nfo()
        fallback = {
            **self.resolved,
            "source_kind": "bili_sync_history",
            "source_digest": "a" * 64,
            "source_warning": "current_source_unavailable",
        }
        result = await self.prepare(
            resolved_by_bvid={BV: fallback}, source_errors={BV: {"code": "source_unavailable"}}
        )
        entry = result["files"][0]
        self.assertEqual("ready", entry["state"])
        self.assertIn("current_source_unavailable", entry["warnings"])
        xml = self.xml(entry)
        self.assertEqual("invalid", xml.findtext("tianshu/source/import_state"))
        self.assertEqual("5820", xml.findtext("tianshu/source/source_duration_seconds"))
        self.assertEqual("386.878", xml.findtext("tianshu/source/actual_duration_seconds"))
        self.assertEqual("bili_sync_history", xml.findtext("tianshu/source/metadata_source_kind"))
        self.assertEqual(record, self.media.repo.get("media_import", item_id("old", "movie.mp4")))

    async def test_proven_separated_episode_creates_only_adjacent_nfo_and_own_show(self):
        self.video("original/Season 1/P1.mp4")
        self.nfo("original/Season 1/P1.nfo", EPISODE)
        self.nfo("original/tvshow.nfo", SHOW)
        record = self.video("separated/Season 1/BV1xx411c7mD - S01E04.mp4", CID_TWO)
        self.media.repo.put(
            "media_import", item_id("old", record["relative_path"]), {**record, "part_index": 4}
        )
        orphan = self.nfo("original/Season 1/orphan-P4.nfo", EPISODE)
        result = await self.prepare()
        created = [entry for entry in result["files"] if entry["action"] == "create"]
        self.assertEqual(2, len(created))
        self.assertTrue(
            all(entry["original_sha256"] is None and entry["state"] == "ready" for entry in created)
        )
        episode = next(entry for entry in created if entry["type"] == "episodedetails")
        self.assertEqual(
            ("1", "4", "Second"),
            tuple(self.xml(episode).findtext(tag) for tag in ("season", "episode", "title")),
        )
        show = next(entry for entry in created if entry["type"] == "tvshow")
        self.assertEqual("Original title", self.xml(show).findtext("title"))
        self.assertEqual("separated/tvshow.nfo", show["nfo_relative_path"])
        self.assertIsNone(self.xml(show).find("fileinfo/streamdetails"))
        self.assertEqual(EPISODE, orphan.read_bytes())
        for entry in created:
            self.assertFalse((self.library / entry["nfo_relative_path"]).exists())
        replay = await self.prepare()
        self.assertEqual(result["files"], replay["files"])
        for entry in created:
            self.nfo(entry["nfo_relative_path"], base64.b64decode(entry["content_base64"]))
        applied = await self.prepare()
        self.assertTrue(
            all(
                entry["state"] == "already_applied"
                for entry in applied["files"]
                if entry["action"] == "create"
            )
        )

    async def test_new_nfo_conflicting_file_and_unproved_episode_number_are_not_overwritten(self):
        record = self.video("separated/Season 1/BV1xx411c7mD - S01E04.mp4", CID_TWO)
        self.media.repo.put(
            "media_import", item_id("old", record["relative_path"]), {**record, "part_index": 4}
        )
        plan = await self.prepare()
        episode = next(entry for entry in plan["files"] if entry["type"] == "episodedetails")
        different = EPISODE.replace(b"Original episode", b"operator-created title")
        path = self.nfo(episode["nfo_relative_path"], different)
        for _ in range(2):
            plan = await self.prepare()
            entry = next(entry for entry in plan["files"] if entry["type"] == "episodedetails")
            self.assertEqual(
                ("failed", "import_nfo_changed", None),
                (entry["state"], entry["code"], entry["original_sha256"]),
            )
            self.assertEqual(different, path.read_bytes())
        other = self.video("unproved/Season 1/BV1xx411c7mD - S01E04.mp4")
        plan = await self.prepare()
        self.assertIn(
            {"relative_path": other["relative_path"], "bvid": BV, "code": "import_nfo_missing"},
            plan["discovery_failures"],
        )

    async def test_repeated_preparation_is_byte_stable_and_applied_replay_keeps_before_hash(self):
        self.video()
        path = self.nfo()
        first = await self.prepare()
        cached_avatar = self.media.repo.get("creator_avatar", MID_UP)
        source_before = self.media.repo.list("import_metadata_source")
        second = await self.prepare()
        self.assertEqual(first["files"], second["files"])
        self.assertEqual(cached_avatar, self.media.repo.get("creator_avatar", MID_UP))
        self.assertEqual(source_before, self.media.repo.list("import_metadata_source"))
        self.assertEqual(1, self.media.connector.resolve.await_count)
        path.write_bytes(base64.b64decode(first["files"][0]["content_base64"]))
        replay = await self.prepare()
        self.assertEqual("already_applied", replay["files"][0]["state"])
        self.assertEqual(
            first["files"][0]["original_sha256"], replay["files"][0]["original_sha256"]
        )

    async def test_batches_resume_without_losing_prior_videos_or_sources(self):
        self.video("first.mp4")
        self.nfo("first.nfo")
        self.video("second.mp4", bvid=OTHER_BV)
        self.nfo("second.nfo", MOVIE.replace(BV.encode(), OTHER_BV.encode()))
        second = copy.deepcopy(self.resolved)
        second["snapshot"]["bvid"] = OTHER_BV
        result = await self.prepare(bvids=[BV], resolved_by_bvid={BV: self.resolved})
        self.assertEqual(1, result["counts"]["pending"])
        result = await self.prepare(bvids=[OTHER_BV], resolved_by_bvid={OTHER_BV: second})
        self.assertEqual(
            ("completed", 2, 2),
            (result["state"], result["counts"]["ready"], result["counts"]["videos"]),
        )
        restored = await self.media.import_metadata.plan("synthetic")
        self.assertEqual(result["files"], restored["files"])
        self.assertEqual(result["videos"], restored["videos"])
        self.media.connector.resolve.assert_not_awaited()

    async def test_missing_metadata_preserves_old_plot_and_actor_and_records_partial(self):
        self.video()
        self.nfo()
        result = await self.prepare(source_errors={BV: {"code": "source_unavailable"}})
        entry = result["files"][0]
        self.assertEqual(("partial", "source_unavailable"), (entry["state"], entry["code"]))
        self.assertEqual("old plot", self.xml(entry).findtext("plot"))
        self.assertEqual("Old author", self.xml(entry).findtext("actor/name"))
        self.assertIsNone(self.xml(entry).find("director"))
        self.assertEqual("unavailable", self.xml(entry).findtext("tianshu/source/metadata_state"))
        self.media.connector.resolve.assert_not_awaited()
        fixed = await self.prepare(resolved_by_bvid={BV: self.resolved})
        self.assertEqual("ready", fixed["files"][0]["state"])

    async def test_source_identity_conflicts_do_not_install_foreign_author(self):
        self.video()
        self.nfo()
        wrong = copy.deepcopy(self.resolved)
        wrong["snapshot"]["parts"][0]["cid"] = "987654321"
        result = await self.prepare(resolved_by_bvid={BV: wrong})
        self.assertEqual("import_metadata_identity_conflict", result["files"][0]["code"])
        self.assertIsNone(self.xml(result["files"][0]).find("director"))
        self.fetcher.assert_not_awaited()

    async def test_changed_nfo_or_video_fails_without_replacing_the_original(self):
        self.video()
        path = self.nfo()
        await self.prepare()
        path.write_bytes(MOVIE.replace(b"Original title", b"user changed title"))
        result = await self.prepare()
        self.assertEqual("import_nfo_changed", result["files"][0]["code"])
        result = await self.prepare()
        self.assertEqual("import_nfo_changed", result["files"][0]["code"])
        self.assertIn(b"user changed title", path.read_bytes())
        (self.library / "movie.mp4").write_bytes(b"changed video")
        result = await self.prepare()
        self.assertEqual("import_observation_changed", result["discovery_failures"][0]["code"])
        self.assertEqual([], (await self.media.import_metadata.plan("synthetic"))["files"])

    async def test_probe_only_when_needed_and_reuses_one_probe_for_episode_and_show(self):
        self.video("show/one.mp4", with_probe=False)
        self.nfo("show/one.nfo", EPISODE)
        self.nfo("show/tvshow.nfo", SHOW)
        await self.prepare()
        self.media.engine.probe.assert_awaited_once()
        self.media.engine.download.assert_not_awaited()

    async def test_output_cannot_overlap_read_only_import_scope(self):
        self.video()
        self.nfo()
        with self.assertRaises(Fault) as failure:
            await self.prepare(output_directory=str(self.library / "candidates"))
        self.assertEqual("import_output_overlaps_source", failure.exception.code)

    async def test_cancelled_candidate_thread_keeps_lock_and_restart_recovers(self):
        self.video()
        self.nfo()
        entered, finish = threading.Event(), threading.Event()

        def blocked(*args):
            entered.set()
            finish.wait(5)
            return save_candidate(*args)

        with patch("services.platform.media.import_metadata.save_candidate", blocked):
            task = asyncio.create_task(self.prepare())
            await asyncio.to_thread(entered.wait, 5)
            self.assertTrue(entered.is_set())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(
                "interrupted", self.media.repo.get("import_metadata_plan", "synthetic")["state"]
            )
            with self.assertRaises(Fault) as failure:
                await self.prepare()
            self.assertEqual("busy", failure.exception.code)
            finish.set()
        for _ in range(100):
            try:
                result = await self.prepare()
                break
            except Fault as error:
                self.assertEqual("busy", error.code)
                await asyncio.sleep(0.01)
        self.assertEqual("completed", result["state"])
        self.assertEqual(1, self.media.connector.resolve.await_count)

    async def test_same_plan_id_serializes_even_when_output_directory_differs(self):
        self.video()
        self.nfo()
        entered, finish = asyncio.Event(), asyncio.Event()

        async def resolve(*args, **kwargs):
            entered.set()
            await finish.wait()
            return self.resolved

        self.media.connector.resolve.side_effect = resolve
        first = asyncio.create_task(self.prepare(output_directory=str(self.root / "first-output")))
        await entered.wait()
        try:
            with self.assertRaises(Fault) as failure:
                await self.prepare(output_directory=str(self.root / "different-output"))
            self.assertIn(failure.exception.code, {"busy", "import_scope_changed"})
        finally:
            finish.set()
            await first

    async def test_verify_exact_legacy_paths_preserves_bare_provider_and_reuses_person_image(self):
        self.video()
        path = self.nfo()
        plan = await self.prepare()
        entry = plan["files"][0]
        path.write_bytes(base64.b64decode(entry["content_base64"]))
        row = {
            "Id": "legacy-video",
            "Type": "Movie",
            "Path": "/legacy/movie.mp4",
            "Name": "Original title",
            "Overview": "new description",
            "ProviderIds": {"bilibili": BV},
            "People": [
                {"Id": "director", "Type": "Director", "Name": director_name("测试UP主", MID_UP)}
            ],
            "MediaStreams": [
                {"Type": "Video", "Codec": "h264", "Width": 1920, "Height": 1080},
                {"Type": "Audio", "Codec": "aac", "Channels": 2},
            ],
        }
        self.media.remote.request.return_value = {
            "Items": [{**row, "Path": "/wrong/movie.mp4"}, row],
            "TotalRecordCount": 2,
        }
        self.media.remote.image.return_value = self.picture
        report = await self.media.import_metadata.verify(
            "synthetic", server_id="emby", library_id="legacy-library", server_root="/legacy"
        )
        self.assertEqual("verified", report["state"])
        self.assertEqual("legacy-video", report["files"][0]["item_id"])
        self.media.remote.upload_image.assert_not_awaited()
        query = self.media.remote.request.await_args.kwargs["params"]
        self.assertEqual("legacy-library", query["ParentId"])
        self.assertNotIn("AnyProviderIdEquals", query)
        self.assertEqual(0, self.media.repo.job_count())

        async def image_changed(*args):
            path.write_bytes(MOVIE)
            return self.picture

        self.media.remote.image.side_effect = image_changed
        report = await self.media.import_metadata.verify(
            "synthetic",
            server_id="emby",
            library_id="legacy-library",
            server_root="/legacy",
            refresh=False,
        )
        self.assertEqual("import_metadata_not_applied", report["files"][0]["code"])

    async def test_verify_not_applied_or_ambiguous_path_never_uploads_unrelated_people(self):
        self.video()
        path = self.nfo()
        plan = await self.prepare()
        self.media.remote.request.return_value = {"Items": [], "TotalRecordCount": 0}
        report = await self.media.import_metadata.verify(
            "synthetic",
            server_id="emby",
            library_id="legacy-library",
            server_root="/legacy",
            refresh=False,
        )
        self.assertEqual("import_metadata_not_applied", report["files"][0]["code"])
        path.write_bytes(base64.b64decode(plan["files"][0]["content_base64"]))
        row = {
            "Type": "Movie",
            "Path": "/legacy/movie.mp4",
            "People": [{"Id": "foreign", "Name": "测试UP主", "Type": "Actor"}],
        }
        self.media.remote.request.return_value = {"Items": [row, row], "TotalRecordCount": 2}
        report = await self.media.import_metadata.verify(
            "synthetic",
            server_id="emby",
            library_id="legacy-library",
            server_root="/legacy",
            refresh=False,
        )
        self.assertEqual("library_identity_conflict", report["files"][0]["code"])
        self.media.remote.upload_image.assert_not_awaited()


class LegacyNfoPureTests(unittest.TestCase):
    def test_confirmed_quality_is_per_file_and_director_delimiters_keep_raw_name(self):
        document = snapshot(creators=[{"mid": MID_UP, "name": "A,B|C;D/E", "role": "uploader"}])
        record = {
            "cid": CID_ONE,
            "state": "verified",
            "code": "import_verified",
            "actual_quality": "64",
            "sha256": "a" * 64,
            "observation": {"size_bytes": 50},
            "media": media_details(INFO),
        }
        xml = ET.fromstring(render(MOVIE, BV, [record], {"snapshot": document}))
        self.assertEqual("A，B｜C；D/E（B站 UID 946974）", xml.findtext("director"))
        self.assertEqual("A,B|C;D/E", xml.findtext("tianshu/creators/creator/name"))
        self.assertEqual("64", xml.findtext("tianshu/source/actual_quality"))
        self.assertEqual("a" * 64, xml.findtext("tianshu/source/sha256"))
        self.assertEqual("confirmed", xml.findtext("tianshu/source/quality_state"))

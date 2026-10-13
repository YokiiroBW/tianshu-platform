"""Actual per-P NFO fields, persistent MID avatars and Emby authenticated images."""

import asyncio
import base64
import json
import tempfile
import threading
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from xml.etree import ElementTree

from aiohttp import web
from PIL import Image

from fixtures import start_http
from media._fixtures import (
    CID_ONE,
    CID_TWO,
    MID_UP,
    multipart_request,
    multipart_snapshot,
    single_request,
    snapshot,
)
from services.platform.contracts import Fault
from services.platform.media.creator_cache import CreatorCache
from services.platform.media.creators import NFO_PROFILE, director_name
from services.platform.media.identity import media_key
from services.platform.media.metadata import normalize_bilibili
from services.platform.media.nfo_details import enrich_sidecars
from services.platform.media.probe_details import media_details
from services.platform.media.publication import Remote
from services.platform.media.repository import Repository
from services.platform.media.servers import MediaServers
from services.platform.media.sidecars import render_sidecars


def picture(color="blue"):
    output = BytesIO()
    Image.new("RGB", (64, 64), color).save(output, format="PNG")
    return output.getvalue()


async def local(function, *args, **kwargs):
    return await asyncio.to_thread(function, *args, **kwargs)


class NfoCreatorTests(unittest.TestCase):
    def download(self, quality, sar="1:1"):
        return {
            "actual_quality": quality,
            "sha256": "a" * 64,
            "size_bytes": 100,
            "source": {
                "kind": "library",
                "operation_id": "verified-operation",
                "file": "/private/library/file.mp4",
            },
            "media": media_details(
                {
                    "format": {"duration": "2.125"},
                    "streams": [
                        {
                            "codec_type": "video",
                            "codec_name": "hevc",
                            "width": 3840,
                            "height": 2160,
                            "sample_aspect_ratio": sar,
                            "avg_frame_rate": "60000/1001",
                            "color_transfer": "smpte2084",
                            "color_primaries": "bt2020",
                            "side_data_list": [
                                {"side_data_type": "DOVI configuration record", "dv_profile": 8}
                            ],
                        },
                        {
                            "codec_type": "audio",
                            "codec_name": "aac",
                            "channels": 2,
                            "sample_rate": "48000",
                            "tags": {"language": "zho"},
                        },
                        {
                            "codec_type": "audio",
                            "codec_name": "eac3",
                            "channels": 6,
                            "tags": {"language": "eng"},
                        },
                        {
                            "codec_type": "subtitle",
                            "codec_name": "subrip",
                            "tags": {"language": "eng"},
                        },
                    ],
                }
            ),
        }

    def test_single_director_identity_xml_escaping_actual_streams_and_source(self):
        metadata = normalize_bilibili(
            snapshot(creators=[{"mid": MID_UP, "name": "作者 & <同名>", "role": "uploader"}])
        )
        original = render_sidecars(metadata, single_request())
        bundle = enrich_sidecars(original, metadata, {CID_ONE: self.download("64")})
        root = ElementTree.fromstring(bundle.text("movie.nfo"))
        self.assertEqual("作者 & <同名>（B站 UID " + MID_UP + "）", root.findtext("director"))
        self.assertEqual([], root.findall("actor"))
        self.assertEqual(metadata.display_description, root.findtext("plot"))
        self.assertEqual("64", root.findtext("tianshu/source/actual_quality"))
        self.assertEqual(CID_ONE, root.find("tianshu/source").get("cid"))
        video = root.find("fileinfo/streamdetails/video")
        self.assertEqual("hevc", video.findtext("codec"))
        self.assertEqual("3840", video.findtext("width"))
        self.assertEqual("dolbyvision", video.findtext("hdrtype"))
        self.assertAlmostEqual(60000 / 1001, float(video.findtext("framerate")))
        self.assertEqual("2", video.findtext("durationinseconds"))
        self.assertEqual(
            ["aac", "eac3"],
            [node.findtext("codec") for node in root.findall("fileinfo/streamdetails/audio")],
        )
        self.assertEqual("6", root.findall("fileinfo/streamdetails/audio")[1].findtext("channels"))
        self.assertEqual("eng", root.findtext("fileinfo/streamdetails/subtitle/language"))
        source = json.loads(bundle.text("source.json"))
        self.assertEqual("64", source["parts"][0]["actual_media"]["actual_quality"])
        self.assertNotIn("/private", bundle.text("source.json"))
        self.assertNotIn("rawffprobe_streams", bundle.text("source.json"))
        self.assertEqual(NFO_PROFILE, source["nfo_profile"])
        self.assertEqual(original.expected_media, bundle.expected_media)
        self.assertEqual(original.referenced_images, bundle.referenced_images)
        self.assertEqual(
            bundle, enrich_sidecars(original, metadata, {CID_ONE: self.download("64")})
        )
        anamorphic = enrich_sidecars(original, metadata, {CID_ONE: self.download("64", sar="4:3")})
        self.assertAlmostEqual(
            3840 / 2160 * 4 / 3,
            float(
                ElementTree.fromstring(anamorphic.text("movie.nfo")).findtext(
                    "fileinfo/streamdetails/video/aspect"
                )
            ),
        )
        self.assertEqual(
            "4:3",
            json.loads(anamorphic.text("source.json"))["parts"][0]["actual_media"]["media"][
                "video_streams"
            ][0]["sample_aspect_ratio"],
        )

    def test_multipart_each_p_uses_own_actual_quality_and_show_has_no_streamdetails(self):
        metadata = normalize_bilibili(multipart_snapshot())
        bundle = enrich_sidecars(
            render_sidecars(metadata, multipart_request()),
            metadata,
            {CID_ONE: self.download("64"), CID_TWO: self.download("120")},
        )
        self.assertIsNone(ElementTree.fromstring(bundle.text("tvshow.nfo")).find("fileinfo"))
        self.assertEqual(
            "64",
            ElementTree.fromstring(bundle.text("Season 01/S01E01-cid-111111111.nfo")).findtext(
                "tianshu/source/actual_quality"
            ),
        )
        self.assertEqual(
            "120",
            ElementTree.fromstring(bundle.text("Season 01/S01E02-cid-222222222.nfo")).findtext(
                "tianshu/source/actual_quality"
            ),
        )
        parts = {part["cid"]: part for part in json.loads(bundle.text("source.json"))["parts"]}
        self.assertEqual("120", parts[CID_TWO]["actual_media"]["actual_quality"])
        self.assertIsNone(parts["333333333"]["actual_media"])
        self.assertEqual(
            ["联合投稿人"],
            [
                node.findtext("name")
                for node in ElementTree.fromstring(bundle.text("tvshow.nfo")).findall("actor")
            ],
        )

    def test_unknown_quality_is_not_invented_from_4k_dimensions(self):
        metadata = normalize_bilibili(snapshot())
        download = self.download(None)
        download["media"]["video_streams"][0]["side_data_list"] = []
        bundle = enrich_sidecars(
            render_sidecars(metadata, single_request()), metadata, {CID_ONE: download}
        )
        root = ElementTree.fromstring(bundle.text("movie.nfo"))
        self.assertIsNone(root.find("tianshu/source/actual_quality"))
        self.assertEqual("hdr10", root.findtext("fileinfo/streamdetails/video/hdrtype"))
        self.assertIsNone(
            json.loads(bundle.text("source.json"))["parts"][0]["actual_media"]["actual_quality"]
        )
        self.assertNotEqual(director_name("同名", "1"), director_name("同名", "2"))
        download["media"]["video_streams"][0].update(
            color_transfer="bt709", pix_fmt="yuv420p10le", codec_name="hevc"
        )
        sdr = enrich_sidecars(
            render_sidecars(metadata, single_request()), metadata, {CID_ONE: download}
        )
        self.assertIsNone(
            ElementTree.fromstring(sdr.text("movie.nfo")).find(
                "fileinfo/streamdetails/video/hdrtype"
            )
        )
        self.assertEqual("eac3", root.findall("fileinfo/streamdetails/audio")[1].findtext("codec"))


class CreatorCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="creator-cache-synthetic-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.now = 100
        self.media = SimpleNamespace(
            repo=Repository(self.root / "media.sqlite", clock=lambda: self.now),
            local=local,
            connector=SimpleNamespace(),
        )
        self.cache = CreatorCache(self.media, self.root / "persistent-avatars")
        self.url = "https://i0.hdslb.com/bfs/face/synthetic.png"

    async def test_same_mid_cross_video_restart_and_avatar_change_preserve_persistent_bytes(self):
        with patch(
            "services.platform.media.creator_cache.fetch_image", AsyncMock(return_value=picture())
        ) as fetch:
            first = await self.cache.obtain(MID_UP, self.url)
            again = await CreatorCache(self.media, self.cache.directory).obtain(MID_UP, self.url)
            self.assertEqual(first, again)
            self.assertEqual(1, fetch.await_count)
            self.assertEqual(picture(), Path(first["file"]).read_bytes())
            fetch.return_value = picture("red")
            changed = await self.cache.obtain(MID_UP, self.url.replace("synthetic", "changed"))
            self.assertNotEqual(first["sha256"], changed["sha256"])
            self.assertEqual(2, fetch.await_count)
            self.assertTrue(Path(first["file"]).is_file())
            self.now += 86401
            await self.cache.obtain(MID_UP, changed["url"])
            self.assertEqual(3, fetch.await_count)

    async def test_failure_and_corrupt_cache_are_explicit_and_retryable(self):
        with patch(
            "services.platform.media.creator_cache.fetch_image", AsyncMock(return_value=picture())
        ) as fetch:
            first = await self.cache.obtain(MID_UP, self.url)
            Path(first["file"]).write_bytes(b"corrupt")
            self.assertEqual("ready", (await self.cache.obtain(MID_UP, self.url))["state"])
            fetch.return_value = b"not an image"
            failed = await self.cache.obtain(MID_UP, self.url + "-changed")
            self.assertEqual(("retry", "creator_avatar_invalid"), (failed["state"], failed["code"]))
            self.assertEqual(first["sha256"], failed["previous"]["sha256"])
            fetch.return_value = picture("red")
            self.assertEqual(
                "ready", (await self.cache.obtain(MID_UP, self.url + "-changed"))["state"]
            )
        missing = await self.cache.obtain("123", None)
        self.assertEqual("creator_avatar_source_missing", missing["code"])

    async def test_cancel_during_save_keeps_mid_lock_until_thread_finishes(self):
        entered, release = threading.Event(), threading.Event()
        original = self.cache.save

        def slow_save(*args):
            entered.set()
            release.wait(5)
            return original(*args)

        with (
            patch(
                "services.platform.media.creator_cache.fetch_image",
                AsyncMock(return_value=picture()),
            ),
            patch.object(self.cache, "save", slow_save),
        ):
            task = asyncio.create_task(self.cache.obtain(MID_UP, self.url))
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            with self.assertRaises(Fault) as error:
                await self.cache.obtain(MID_UP, self.url)
            self.assertEqual("busy", error.exception.code)
            release.set()
            for _ in range(100):
                try:
                    result = await self.cache.obtain(MID_UP, self.url)
                    break
                except Fault as error:
                    self.assertEqual("busy", error.code)
                    await asyncio.sleep(0.01)
            else:
                self.fail("cache lock not released")
        self.assertEqual("ready", result["state"])


class EmbyCreatorImageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory(prefix="emby-person-synthetic-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.media = SimpleNamespace(
            repo=Repository(root / "media.sqlite"), local=local, connector=SimpleNamespace()
        )
        self.cache = CreatorCache(self.media, root / "avatars")
        with patch(
            "services.platform.media.creator_cache.fetch_image", AsyncMock(return_value=picture())
        ):
            self.avatar = await self.cache.obtain(
                MID_UP, "https://i0.hdslb.com/bfs/face/synthetic.png"
            )
        self.images, self.uploads = {}, []
        self.person = {
            "Name": director_name("测试UP主", MID_UP),
            "Type": "Director",
            "Id": "director-123",
        }
        self.path = "/media/target/video.mp4"
        self.quality_mismatch = False
        self.extra_people = []

        async def handler(request):
            self.assertEqual("synthetic-emby-token", request.headers.get("X-Emby-Token"))
            if request.path == "/Library/Refresh":
                return web.Response(status=204)
            if request.path == "/Items":
                return web.json_response(
                    {
                        "Items": [
                            {
                                "Id": "video-item",
                                "ProviderIds": {"bilibili": media_key("BV1xx411c7mD", CID_ONE)},
                                "Path": self.path,
                                "Name": "合成投稿",
                                "Overview": "合成简介",
                                "People": [self.person, *self.extra_people],
                            }
                        ]
                    }
                )
            self.assertEqual("/Items/director-123/Images/Primary", request.path)
            if request.method == "POST":
                self.assertEqual("image/png", request.content_type)
                encoded = await request.read()
                raw = base64.b64decode(encoded, validate=True)
                self.assertEqual(picture(), raw)
                self.uploads.append(encoded)
                self.images["director-123"] = picture("red") if self.quality_mismatch else raw
                return web.Response(status=204)
            if "director-123" not in self.images:
                return web.Response(status=404)
            return web.Response(body=self.images["director-123"], content_type="image/png")

        app = web.Application()
        app.router.add_route("*", "/{path:.*}", handler)
        runner, url = await start_http(app)
        self.addAsyncCleanup(runner.cleanup)
        self.remote = Remote()
        self.addAsyncCleanup(self.remote.close)
        self.server = {
            "server_id": "emby",
            "kind": "emby",
            "base_url": url,
            "token": "synthetic-emby-token",
            "library_id": "lib",
            "media_root": "/media",
        }
        self.job = {
            "bvid": "BV1xx411c7mD",
            "selected_cids": [CID_ONE],
            "snapshot": {
                "title": "合成投稿",
                "description": "合成简介",
                "creators": [{"mid": MID_UP, "name": "测试UP主", "role": "uploader"}],
                "parts": [{"cid": CID_ONE, "title": "P1"}],
            },
            "asset_receipt": {"target_relative_path": "target"},
            "package": {
                "nfo_profile": NFO_PROFILE,
                "manifest_base64": base64.b64encode(
                    json.dumps(
                        {
                            "layout": "single",
                            "files": [{"kind": "video", "cid": CID_ONE, "path": "video.mp4"}],
                        }
                    ).encode()
                ).decode(),
            },
            "creator_avatars": [{**self.avatar, "mid": MID_UP}],
        }

    async def verify(self):
        return (
            await MediaServers(self.remote, self.cache).verify({"servers": [self.server]}, self.job)
        )[0]

    async def test_authenticated_base64_upload_readback_and_same_image_reuse(self):
        result = await self.verify()
        self.assertEqual("verified", result["state"], result)
        self.assertEqual("director_image_verified", result["director_images"][0]["code"])
        self.assertEqual(1, len(self.uploads))
        self.assertEqual("verified", (await self.verify())["state"])
        self.assertEqual(1, len(self.uploads))

    async def test_wrong_type_same_name_other_directory_and_duplicate_people_never_upload(self):
        self.person["Type"] = "Actor"
        self.assertEqual("mismatch", (await self.verify())["state"])
        self.assertEqual([], self.uploads)

        self.person["Type"] = "Director"
        self.person["Name"] = "测试UP主"
        self.assertEqual("mismatch", (await self.verify())["state"])
        self.person["Name"] = director_name("测试UP主", MID_UP)
        self.path = "/different/target/video.mp4"
        self.assertEqual("pending", (await self.verify())["state"])
        self.assertEqual([], self.uploads)
        self.path = "/media/target/video.mp4"
        self.extra_people = [{**self.person, "Id": "ambiguous-person"}]
        result = await self.verify()
        self.assertEqual("pending", result["state"])
        self.assertEqual("director_identity_conflict", result["director_images"][0]["code"])
        self.assertEqual([], self.uploads)

    async def test_unavailable_avatar_or_wrong_readback_is_pending_not_verified(self):
        self.job["creator_avatars"][0]["state"] = "retry"
        self.assertEqual("pending", (await self.verify())["state"])
        self.assertEqual([], self.uploads)
        self.job["creator_avatars"][0]["state"] = "ready"
        self.quality_mismatch = True
        result = await self.verify()
        self.assertEqual("pending", result["state"])
        self.assertEqual("director_image_mismatch", result["director_images"][0]["code"])

    async def test_legacy_actor_package_is_not_failed_by_new_profile(self):
        self.job["package"].pop("nfo_profile")
        self.person.update(Name="测试UP主", Type="Actor")
        result = await self.verify()
        self.assertEqual("verified", result["state"])
        self.assertNotIn("director_images", result)
        self.assertEqual([], self.uploads)

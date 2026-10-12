"""Exercise the real HTTP observer when provider identities appear in multiple directories."""

import base64
import json
import unittest

from aiohttp import web

from fixtures import start_http
from services.platform.media.identity import item_key, media_key
from services.platform.media.publication import Remote
from services.platform.media.servers import MediaServers

BV = "BV1xx411c7mD"
CID = "123"


class MediaServerDirectoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.items = {}
        self.calls = []

        async def handle(request):
            self.calls.append((request.method, request.path))
            self.assertTrue(
                request.headers.get("X-Emby-Token") or request.headers.get("Authorization")
            )
            if request.path == "/Library/Refresh":
                return web.Response(status=204)
            self.assertEqual(request.path, "/Items")
            self.assertEqual(request.query["ParentId"], "fixture-library")
            identity = request.query["AnyProviderIdEquals"].removeprefix("bilibili.")
            return web.json_response({"Items": self.items.get(identity, [])})

        app = web.Application()
        app.router.add_route("*", "/{path:.*}", handle)
        runner, self.url = await start_http(app)
        self.addAsyncCleanup(runner.cleanup)
        self.remote = Remote()
        self.addAsyncCleanup(self.remote.close)
        self.observer = MediaServers(self.remote)

    def job(self, directory, layout):
        files = [
            {
                "kind": "video",
                "cid": CID,
                "path": "video.mp4" if layout == "single" else "Season 01/S01E01-cid-123.mp4",
            }
        ]
        return {
            "bvid": BV,
            "selected_cids": [CID],
            "snapshot": {
                "title": "同一投稿",
                "description": "合成完整简介",
                "creators": [{"name": "合成作者"}],
                "parts": [{"cid": CID, "title": "第一P"}],
            },
            "asset_receipt": {"target_relative_path": directory + "/bilibili-" + BV},
            "package": {
                "manifest_base64": base64.b64encode(
                    json.dumps({"layout": layout, "files": files}).encode()
                ).decode()
            },
        }

    def entries(self, job, prefix):
        root = "/library/视频/" + job["asset_receipt"]["target_relative_path"]
        manifest = json.loads(base64.b64decode(job["package"]["manifest_base64"]))
        single = manifest["layout"] == "single"
        identities = [media_key(BV, CID)] if single else [item_key(BV), media_key(BV, CID)]
        return {
            identity: {
                "Id": prefix + "-" + str(index),
                "Path": root + ("/" + manifest["files"][0]["path"] if single or index else ""),
                "ProviderIds": {"bilibili": identity},
                "Name": "同一投稿" if single or index == 0 else "第一P",
                "Overview": "合成完整简介",
                "People": [{"Name": "合成作者"}],
            }
            for index, identity in enumerate(identities)
        }

    async def verify(self, job, kind):
        results = await self.observer.verify(
            {
                "servers": [
                    {
                        "server_id": kind,
                        "kind": kind,
                        "base_url": self.url,
                        "token": "synthetic-loopback-only",
                        "library_id": "fixture-library",
                        "media_root": "/library/视频",
                    }
                ]
            },
            job,
        )
        return results[0]

    async def test_both_directories_verify_their_own_root_and_episode(self):
        for layout in ("single", "multipart"):
            first = self.job("收藏夹/第一目录", layout)
            second = self.job("订阅/第二目录", layout)
            first_entries, second_entries = (
                self.entries(first, "first"),
                self.entries(second, "second"),
            )
            self.items = {
                identity: [entry, second_entries[identity]]
                for identity, entry in first_entries.items()
            }
            for kind in ("emby", "jellyfin"):
                for job, prefix in [(first, "first"), (second, "second")]:
                    with self.subTest(layout=layout, kind=kind, directory=prefix):
                        result = await self.verify(job, kind)
                        self.assertEqual(result["state"], "verified", result)
                        self.assertEqual(result["item_id"], prefix + "-0")

    async def test_wrong_first_entry_correct_second_entry_is_selected(self):
        for layout in ("single", "multipart"):
            job = self.job("收藏夹/目标目录", layout)
            correct = self.entries(job, "correct")
            other = self.entries(self.job("收藏夹/其他目录", layout), "other")
            # A wrong entry may also have different metadata; it must not contaminate the target.
            self.items = {
                identity: [{**other[identity], "Name": "其他目录元数据"}, entry]
                for identity, entry in correct.items()
            }
            for kind in ("emby", "jellyfin"):
                with self.subTest(layout=layout, kind=kind):
                    result = await self.verify(job, kind)
                    self.assertEqual(result["state"], "verified", result)
                    self.assertEqual(result["item_id"], "correct-0")

    async def test_only_other_directory_means_target_pending(self):
        for layout in ("single", "multipart"):
            job = self.job("收藏夹/尚未收录", layout)
            self.items = {
                identity: [entry]
                for identity, entry in self.entries(
                    self.job("订阅/已收录", layout), "other"
                ).items()
            }
            for kind in ("emby", "jellyfin"):
                with self.subTest(layout=layout, kind=kind):
                    result = await self.verify(job, kind)
                    self.assertEqual(result["state"], "pending", result)
                    self.assertEqual(result["code"], "library_item_pending")
                    self.assertIsNone(result["item_id"])

    async def test_matching_path_still_requires_matching_metadata(self):
        job = self.job("目标目录", "single")
        self.items = {
            identity: [{**entry, "Overview": "尚未更新的简介"}]
            for identity, entry in self.entries(job, "correct").items()
        }
        for kind in ("emby", "jellyfin"):
            result = await self.verify(job, kind)
            self.assertEqual(result["state"], "mismatch", result)
            self.assertEqual(result["code"], "library_metadata_mismatch")

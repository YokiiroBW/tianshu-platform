"""Real Platform HTTP, SQLite, FFmpeg files and local publication; no AssetLibrary server."""

import base64
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web

from fixtures import ENV, start_http
from media_runtime_fixtures import BV, QUALITY, SyntheticEngine, Upstream, settings, subscription
from services.platform.media.bilibili import Bilibili
from services.platform.media.subscriptions import job_payload
from services.platform.server import create_app
from services.platform.service import Platform
from web_fixtures import PASSWORD


class StandaloneMediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.environment = patch.dict(os.environ, ENV)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.upstream = await Upstream().start()
        self.addAsyncCleanup(self.upstream.runner.cleanup)
        engine = {
            "python": sys.executable,
            "ffmpeg": os.environ.get("MEDIA_TEST_FFMPEG", "ffmpeg"),
            "ffprobe": os.environ.get("MEDIA_TEST_FFPROBE", "ffprobe"),
        }
        self.config = settings(self.temp.name, engine, "https://never-contact.invalid")
        self.config["media"]["targets"] = [
            {
                "target_id": "test-server",
                "label": "Test Jellyfin",
                "kind": "media_server",
                "directory": str(Path(self.temp.name) / "external-library"),
                "servers": [
                    {
                        "server_id": "test-server",
                        "kind": "jellyfin",
                        "base_url": "https://synthetic.invalid",
                        "token_env": "SYNTHETIC_TOKEN",
                        "library_id": "test",
                        "media_root": "/external",
                    }
                ],
            }
        ]
        self.config.pop("asset_connections", None)
        self.config.pop("web_assets", None)
        self.platform = Platform(self.config)
        self.addCleanup(self.platform.close)
        self.media = self.platform.media
        self.media.connector = Bilibili(api=self.upstream.url, passport=self.upstream.url)
        self.media.engine = SyntheticEngine(self.media.config["engine"])
        self.media.remote.request = AsyncMock(
            side_effect=AssertionError("AssetLibrary must not be contacted")
        )
        self.media.servers.verify = AsyncMock(
            return_value=[
                {
                    "server_id": "test-server",
                    "state": "verified",
                    "code": "verified",
                    "kind": "jellyfin",
                    "item_id": "test",
                }
            ]
        )
        self.media.start = AsyncMock()  # The test drives the real worker deterministically.
        self.runner, self.url = await start_http(create_app(self.platform))
        self.addAsyncCleanup(self.runner.cleanup)
        self.config["web"]["origin"] = self.url
        self.client = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.addAsyncCleanup(self.client.close)
        async with self.client.get(self.url + "/api/web/session") as response:
            session = await response.json()
        async with self.client.post(
            self.url + "/api/web/login",
            json={"username": "synthetic-admin", "password": PASSWORD},
            headers={"Origin": self.url, "X-CSRF-Token": session["csrf"]},
        ) as response:
            self.csrf = (await response.json())["csrf"]

    async def post(self, operation, body):
        async with self.client.post(
            self.url + "/api/web/media/" + operation,
            json=body,
            headers={"Origin": self.url, "X-CSRF-Token": self.csrf},
        ) as response:
            value = await response.json()
            self.assertEqual(response.status, 200, value)
            return value

    async def run_job(self, identity):
        job = self.media.repo.claim("standalone-test")
        self.assertEqual(job["job_id"], identity)
        await self.media.worker.run(job, "standalone-test")
        return self.media.repo.job(identity)

    async def enqueue(self):
        resolved = await self.media.connector.resolve(BV, with_formats=False)
        return self.media.repo.enqueue(
            job_payload(resolved, self.upstream.parts[BV], None, "test-server", dict(QUALITY))
        )

    def verify_output(self, job):
        self.assertEqual(job["state"], "completed", job.get("code"))
        receipt = job["asset_receipt"]
        self.assertEqual(receipt["storage"], "server_directory")
        destination = (
            Path(self.config["media"]["targets"][0]["directory"]) / receipt["target_relative_path"]
        )
        for entry in receipt["files"]:
            self.assertTrue((destination / entry["path"]).is_file(), entry)
        self.assertTrue(any(entry["kind"] == "nfo" for entry in receipt["files"]))
        self.media.remote.request.assert_not_called()
        return destination

    async def test_http_download_to_real_local_media_files_without_external_asset_config(self):
        view = await self.post("view", {})
        self.assertEqual(view["targets"][0]["kind"], "media_server")
        self.upstream.parts[BV] = ["111111111"]
        preview = await self.post(
            "resolve", {"url": "https://www.bilibili.com/video/" + BV, "account_id": None}
        )
        result = await self.post(
            "enqueue",
            {
                "preview_id": preview["preview_id"],
                "part_cids": self.upstream.parts[BV],
                "account_id": None,
                "target_id": "test-server",
                "quality": dict(QUALITY),
                "client_id": str(uuid.uuid4()),
            },
        )
        job = await self.run_job(result["jobs"][0]["job_id"])
        destination = self.verify_output(job)
        self.assertTrue((destination / "movie.nfo").exists())
        view = await self.post("view", {})
        self.assertEqual(view["overview"]["jobs"]["published"], 1)

    async def test_subscription_new_parts_and_metadata_editions_keep_existing_videos(self):
        self.upstream.parts[BV] = ["111111111"]
        request = subscription()
        request["target_id"] = "test-server"
        sub = (await self.post("subscriptions/save", request))["subscription"]
        await self.media.subscriptions.scan(sub["subscription_id"])
        jobs, _ = self.media.repo.jobs()
        first = await self.run_job(jobs[0]["job_id"])
        self.verify_output(first)
        first_manifest = json.loads(base64.b64decode(first["package"]["manifest_base64"]))
        self.upstream.parts[BV].append("222222222")
        self.assertEqual((await self.media.subscriptions.scan(sub["subscription_id"]))["queued"], 1)
        jobs, _ = self.media.repo.jobs(states={"queued"})
        second = await self.run_job(jobs[0]["job_id"])
        self.verify_output(second)
        manifest = json.loads(base64.b64decode(second["package"]["manifest_base64"]))
        original = next(f for f in first_manifest["files"] if f["kind"] == "video")
        self.assertIn(original, manifest["files"])
        self.assertEqual(self.media.engine.calls.count("111111111"), 1)
        await self.post(
            "jobs/metadata",
            {
                "job_id": second["job_id"],
                "expected_revision": second["revision"],
                "title": "Updated local title",
                "description": "Updated local description",
                "client_id": str(uuid.uuid4()),
            },
        )
        third = await self.run_job(second["job_id"])
        destination = self.verify_output(third)
        self.assertIn(
            "Updated local title", (destination / "tvshow.nfo").read_text(encoding="utf-8")
        )
        self.assertEqual(len(self.media.engine.calls), 2)

    async def test_worker_recovers_installed_publication_after_receipt_write_loss(self):
        job = await self.enqueue()
        library = self.media.publisher.organizer
        save = library.save

        def lost_receipt(operation, **changes):
            if changes.get("status") == "indexed":
                raise OSError("synthetic lost receipt write")
            return save(operation, **changes)

        with patch.object(library, "save", side_effect=lost_receipt):
            failed = await self.run_job(job["job_id"])
        self.assertEqual(failed["state"], "failed")
        await self.post(
            "jobs/control",
            {"job_id": job["job_id"], "action": "retry", "client_id": str(uuid.uuid4())},
        )
        done = await self.run_job(job["job_id"])
        self.verify_output(done)
        self.assertEqual(len(self.media.engine.calls), 2)
        self.assertEqual(len(self.media.repo.list("file_transfer")), 1)

    async def test_local_files_are_verified_directly_by_emby_and_jellyfin_protocols(self):
        from services.platform.media.identity import media_key, single_directory
        from services.platform.media.publication import Remote

        from services.platform.media.servers import MediaServers

        self.media.servers = MediaServers(self.media.remote)
        self.upstream.parts[BV] = ["111111111"]
        self.media.worker.cover = AsyncMock(return_value=(self.upstream.cover_bytes, "png"))
        library = Path(self.config["media"]["targets"][0]["directory"])
        directory = library / single_directory(BV, "111111111")
        calls = []

        async def server(request):
            calls.append((request.method, request.path))
            self.assertTrue((directory / "video.mp4").is_file())
            self.assertTrue((directory / "movie.nfo").is_file())
            self.assertTrue(
                request.headers.get("X-Emby-Token") or request.headers.get("Authorization")
            )
            if request.path == "/Library/Refresh":
                return web.Response(status=204)
            if request.path == "/Items/fixture/Images/Primary":
                return web.Response(
                    body=(directory / "poster.png").read_bytes(), content_type="image/png"
                )
            return web.json_response(
                {
                    "Items": [
                        {
                            "Id": "fixture",
                            "Name": "合成投稿 " + BV,
                            "Overview": self.upstream.description,
                            "ProviderIds": {"bilibili": media_key(BV, "111111111")},
                            "People": [{"Name": "测试UP主"}],
                            "Path": (directory / "video.mp4").as_posix(),
                        }
                    ]
                }
            )

        app = web.Application()
        app.router.add_route("*", "/{path:.*}", server)
        runner, url = await start_http(app)
        self.addAsyncCleanup(runner.cleanup)
        remote = Remote()
        self.addAsyncCleanup(remote.close)

        async def server_only(config, method, path, **kwargs):
            self.assertTrue(kwargs.get("server"), "unexpected AssetLibrary call")
            return await remote.request(config, method, path, **kwargs)

        self.media.remote.request = server_only
        self.media.remote.image = remote.image
        with patch.dict(os.environ, {"LOCAL_MEDIA_SERVER_TOKEN": "synthetic-only"}):
            self.media.config["targets"]["test-server"]["servers"] = [
                {
                    "server_id": kind,
                    "kind": kind,
                    "base_url": url,
                    "token_env": "LOCAL_MEDIA_SERVER_TOKEN",
                    "library_id": "fixture-library",
                    "media_root": str(library),
                }
                for kind in ("emby", "jellyfin")
            ]
            job = await self.enqueue()
            done = await self.run_job(job["job_id"])
        self.assertEqual(done["state"], "completed", done.get("code"))
        self.assertEqual(
            [item["state"] for item in done["library_results"]], ["verified", "verified"]
        )
        self.assertEqual(calls.count(("POST", "/Library/Refresh")), 2)

    async def test_server_connections_plex_transfer_and_encrypted_restart(self):
        from services.platform.media.publication import Remote
        from services.platform.media.servers import MediaServers
        from services.platform.media.targets import Targets
        from services.platform.media.identity import single_directory

        root = Path(self.temp.name) / "plex-videos"
        video = root / single_directory(BV, "111111111") / "video.mp4"
        calls = []

        async def server(request):
            calls.append((request.path, dict(request.query)))
            self.assertTrue(
                request.headers.get("X-Plex-Token") == "test-plex-secret"
                or request.headers.get("X-Emby-Token") == "test-plex-secret"
                or "test-plex-secret" in request.headers.get("Authorization", "")
            )
            if request.path == "/Library/VirtualFolders":
                return web.json_response(
                    [{"ItemId": "1", "Name": "Network videos", "Locations": [str(root)]}]
                )
            if request.path == "/library/sections":
                return web.json_response(
                    {
                        "MediaContainer": {
                            "Directory": [
                                {
                                    "key": "1",
                                    "title": "Network videos",
                                    "type": "movie",
                                    "Location": [{"path": str(root)}],
                                }
                            ]
                        }
                    }
                )
            if request.path.endswith("/refresh"):
                self.assertTrue(video.exists())
                return web.Response(status=200)
            if request.path.endswith("/all"):
                return web.json_response(
                    {
                        "MediaContainer": {
                            "totalSize": 1,
                            "Metadata": [
                                {"ratingKey": "99", "Media": [{"Part": [{"file": str(video)}]}]}
                            ],
                        }
                    }
                )
            raise AssertionError(request.path)

        app = web.Application()
        app.router.add_route("*", "/{path:.*}", server)
        runner, url = await start_http(app)
        self.addAsyncCleanup(runner.cleanup)
        self.media.remote = Remote(self.media.targets.token)
        self.addAsyncCleanup(self.media.remote.close)
        self.media.servers = MediaServers(self.media.remote)
        for kind in ("emby", "jellyfin", "plex"):
            result = await self.post(
                "targets/test",
                {"kind": kind, "base_url": url, "token": "test-plex-secret", "target_id": None},
            )
            self.assertEqual(result["libraries"][0]["library_id"], "1")
        body = {
            "target_id": None,
            "expected_revision": 0,
            "kind": "plex",
            "base_url": url,
            "token": "test-plex-secret",
            "label": "Plex videos",
            "library_id": "1",
            "media_root": str(root),
            "directory": str(root),
            "client_id": str(uuid.uuid4()),
        }
        saved = await self.post("targets/save", body)
        replay = await self.post("targets/save", body)
        self.assertEqual(saved["target_id"], replay["target_id"])
        self.assertTrue(replay["replayed"])
        view = await self.post("view", {})
        self.assertNotIn("test-plex-secret", json.dumps(view))
        self.assertNotIn(b"test-plex-secret", self.media.repo.database.read_bytes())
        # Restore from the durable documents and use its encrypted credential on HTTP.
        self.media.config["targets"] = dict(self.media.targets.configured)
        self.media.targets = Targets(self.media)
        self.assertEqual(self.media.targets.token(saved["target_id"]), "test-plex-secret")
        self.upstream.parts[BV] = ["111111111"]
        resolved = await self.media.connector.resolve(BV, with_formats=False)
        job = self.media.repo.enqueue(
            job_payload(resolved, self.upstream.parts[BV], None, saved["target_id"], dict(QUALITY))
        )
        done = await self.run_job(job["job_id"])
        self.assertEqual(done["state"], "completed", done.get("code"))
        self.assertEqual(done["library_results"][0]["code"], "library_files_verified")
        self.assertTrue(video.exists())
        self.assertTrue(any(path.endswith("/refresh") for path, _ in calls))
        # Saved credentials can be reused for edits, but populated mappings cannot be repointed.
        edit = {
            **body,
            "target_id": saved["target_id"],
            "expected_revision": 1,
            "token": "",
            "label": "Renamed Plex",
            "client_id": str(uuid.uuid4()),
        }
        changed = await self.post("targets/save", edit)
        self.assertEqual(changed["revision"], 2)
        for operation, payload in [
            (
                "targets/save",
                {
                    **edit,
                    "expected_revision": 2,
                    "directory": str(root / "elsewhere"),
                    "client_id": str(uuid.uuid4()),
                },
            ),
            (
                "targets/remove",
                {
                    "target_id": saved["target_id"],
                    "expected_revision": 2,
                    "client_id": str(uuid.uuid4()),
                },
            ),
        ]:
            async with self.client.post(
                self.url + "/api/web/media/" + operation,
                json=payload,
                headers={"Origin": self.url, "X-CSRF-Token": self.csrf},
            ) as response:
                self.assertEqual(response.status, 409)
                self.assertEqual((await response.json())["code"], "target_mapping_in_use")

    async def test_unused_server_can_be_removed_without_deleting_files(self):
        root = Path(self.temp.name) / "unused-server"
        root.mkdir()
        (root / "existing.mp4").write_bytes(b"untouched")
        body = {
            "target_id": None,
            "expected_revision": 0,
            "kind": "emby",
            "base_url": "http://127.0.0.1:8096",
            "token": "synthetic",
            "label": "Unused",
            "library_id": "1",
            "media_root": "/videos",
            "directory": str(root),
            "client_id": str(uuid.uuid4()),
        }
        saved = await self.post("targets/save", body)
        await self.post(
            "targets/remove",
            {
                "target_id": saved["target_id"],
                "expected_revision": 1,
                "client_id": str(uuid.uuid4()),
            },
        )
        self.assertNotIn(saved["target_id"], self.media.config["targets"])
        self.assertEqual((root / "existing.mp4").read_bytes(), b"untouched")

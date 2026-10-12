"""Real Platform HTTP, SQLite, FFmpeg files and local publication; no AssetLibrary server."""

import base64
import asyncio
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
from services.platform.media.destination import head_key, snapshot
from services.platform.media.repository import Repository
from services.platform.media.retention import Retention
from services.platform.media.runtime import Media
from services.platform.contracts import digest
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

    async def test_global_download_once_two_directories_and_different_target_with_independent_nfo(
        self,
    ):
        target = self.media.config["targets"]["test-server"]
        resolved = await self.media.connector.resolve(BV, with_formats=False)
        jobs = []
        for folder, title in [("收藏夹/音乐", "音乐收藏"), ("订阅/学习", "学习订阅")]:
            payload = job_payload(
                resolved,
                self.upstream.parts[BV],
                None,
                "test-server",
                dict(QUALITY),
                **snapshot(target, folder),
            )
            payload["overrides"] = {"title": title, "description": "不同目录的独立简介"}
            jobs.append(self.media.repo.enqueue(payload))
        first = self.media.repo.claim("one")
        second = self.media.repo.claim("two")
        self.assertIsNotNone(second)  # Different destination directories do not serialize jobs.
        await asyncio.gather(
            self.media.worker.run(first, "one"), self.media.worker.run(second, "two")
        )
        done = [self.media.repo.job(job["job_id"]) for job in jobs]
        for job, (folder, title) in zip(
            done, [("收藏夹/音乐", "音乐收藏"), ("订阅/学习", "学习订阅")], strict=True
        ):
            destination = self.verify_output(job)
            self.assertTrue(job["asset_receipt"]["target_relative_path"].startswith(folder + "/"))
            self.assertEqual(job["destination_directory"], str(Path(target["directory"]) / folder))
            self.assertIn(title, next(destination.rglob("*.nfo")).read_text(encoding="utf-8"))
            self.assertEqual(
                self.media.repo.get("target_head", head_key(job))["job_id"], job["job_id"]
            )
        self.assertEqual(self.media.engine.calls, self.upstream.parts[BV])
        video_paths = []
        nfo_paths = []
        for job in done:
            directory = Path(target["directory"]) / job["asset_receipt"]["target_relative_path"]
            video_paths.append(next(directory.rglob("*.mp4")))
            nfo_paths.append(next(directory.rglob("*.nfo")))
        self.assertTrue(video_paths[0].samefile(video_paths[1]))
        self.assertFalse(nfo_paths[0].samefile(nfo_paths[1]))
        self.assertEqual(
            {
                call.args[1]["destination_subdirectory"]
                for call in self.media.servers.verify.call_args_list
            },
            {"收藏夹/音乐", "订阅/学习"},
        )
        # A new repository/cache instance and a second target still consume the same video sources.
        self.media.repo = Repository(self.media.repo.database)
        other = {
            **target,
            "target_id": "second-server",
            "directory": str(Path(self.temp.name) / "second-library"),
        }
        self.media.config["targets"]["second-server"] = other
        third = self.media.repo.enqueue(
            job_payload(
                resolved,
                self.upstream.parts[BV],
                None,
                "second-server",
                dict(QUALITY),
                **snapshot(other, "重启复用"),
            )
        )
        third = await self.run_job(third["job_id"])
        self.assertEqual(third["state"], "completed", third["code"])
        self.assertEqual(third["download_reused_cids"], self.upstream.parts[BV])
        self.assertEqual(self.media.engine.calls, self.upstream.parts[BV])

    async def test_subscription_directory_edit_preserves_queued_snapshot_and_reorganizes_existing_members(
        self,
    ):
        request = subscription()
        request.update(target_id="test-server", destination_subdirectory="旧目录/收藏")
        saved = (await self.post("subscriptions/save", request))["subscription"]
        await self.media.subscriptions.scan(saved["subscription_id"])
        old_jobs, _ = self.media.repo.jobs()
        old = old_jobs[0]
        request.update(
            subscription_id=saved["subscription_id"],
            expected_revision=saved["revision"],
            destination_subdirectory="新目录/收藏",
            client_id=str(uuid.uuid4()),
        )
        changed = (await self.post("subscriptions/save", request))["subscription"]
        self.assertEqual(
            (await self.media.subscriptions.scan(changed["subscription_id"]))["queued"], 1
        )
        self.assertEqual(
            self.media.repo.job(old["job_id"])["destination_subdirectory"], "旧目录/收藏"
        )
        await self.run_job(old["job_id"])
        pending, _ = self.media.repo.jobs(states={"queued"})
        second = await self.run_job(pending[0]["job_id"])
        self.assertEqual(second["destination_subdirectory"], "新目录/收藏")
        self.verify_output(second)
        self.assertEqual(self.media.engine.calls, self.upstream.parts[BV])
        # New P is independently added to both directory heads without re-downloading old P.
        self.upstream.parts[BV].append("333333333")
        await self.media.subscriptions.scan(changed["subscription_id"])
        pending, _ = self.media.repo.jobs(states={"queued"})
        latest = await self.run_job(pending[0]["job_id"])
        self.assertEqual(latest["selected_cids"], self.upstream.parts[BV])
        self.assertEqual(self.media.engine.calls, self.upstream.parts[BV])
        self.assertTrue(latest["asset_receipt"]["target_relative_path"].startswith("新目录/收藏/"))

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

    async def test_success_cleans_cache_and_packages_then_reuses_persistent_video_after_restart(
        self,
    ):
        self.upstream.parts[BV] = ["111111111"]
        first = await self.run_job((await self.enqueue())["job_id"])
        original = self.verify_output(first)
        staging = Path(self.media.config["staging_directory"])
        self.assertFalse((staging / first["package"]["staging_ref"]).exists())
        self.assertFalse(Path(first["downloads"]["111111111"]["file"]).exists())
        self.assertEqual(list((staging / ".downloads").rglob("download.mp4")), [])
        self.assertEqual(
            self.media.repo.get("media_retention", first["job_id"])["state"], "cleaned"
        )
        self.media.repo = Repository(self.media.repo.database)
        self.media.publisher.organizer.repo = self.media.repo
        self.media.retention = Retention(self.media)
        resolved = await self.media.connector.resolve(BV, with_formats=False)
        target = self.media.config["targets"]["test-server"]
        payload = job_payload(
            resolved,
            self.upstream.parts[BV],
            None,
            "test-server",
            dict(QUALITY),
            **snapshot(target, "第三个目录"),
        )
        second = await self.run_job(self.media.repo.enqueue(payload)["job_id"])
        destination = self.verify_output(second)
        self.assertEqual(self.media.engine.calls, ["111111111"])
        self.assertEqual(second["downloads"]["111111111"]["source"]["kind"], "library")
        self.assertTrue((original / "video.mp4").samefile(destination / "video.mp4"))

    async def test_cleanup_failure_preserves_success_and_recovers_historical_completed_jobs(self):
        self.upstream.parts[BV] = ["111111111"]
        with patch.object(self.media.retention, "cleanup", side_effect=PermissionError("fixture")):
            first = await self.run_job((await self.enqueue())["job_id"])
        self.assertEqual(first["state"], "completed")
        self.assertEqual(
            self.media.repo.get("media_retention", first["job_id"])["state"], "pending"
        )
        package = Path(self.media.config["staging_directory"]) / first["package"]["staging_ref"]
        self.assertTrue(package.exists())
        await self.media.retention.recover()
        self.assertFalse(package.exists())
        self.assertEqual(
            self.media.repo.get("media_retention", first["job_id"])["state"], "cleaned"
        )

    async def test_library_verification_failure_and_partial_download_preserve_staging(self):
        self.media.servers.verify.return_value = [{"server_id": "test-server", "state": "pending"}]
        first = await self.run_job((await self.enqueue())["job_id"])
        self.assertEqual(first["state"], "retry_wait")
        self.assertTrue(
            (
                Path(self.media.config["staging_directory"]) / first["package"]["staging_ref"]
            ).exists()
        )
        self.assertTrue(all(Path(d["file"]).exists() for d in first["downloads"].values()))
        self.assertEqual(self.media.repo.list("media_source"), [])
        self.media.repo.update_job(first["job_id"], {"state": "failed"})
        self.media.engine.fail_cid = "222222222"
        self.upstream.parts[BV] = ["111111111", "222222222"]
        resolved = await self.media.connector.resolve(BV, with_formats=False)
        target = self.media.config["targets"]["test-server"]
        second = self.media.repo.enqueue(
            job_payload(
                resolved,
                self.upstream.parts[BV],
                None,
                "test-server",
                dict(QUALITY),
                **snapshot(target, "部分失败"),
            )
        )
        second = await self.run_job(second["job_id"])
        self.assertEqual(second["state"], "retry_wait")
        self.assertTrue(Path(second["downloads"]["111111111"]["file"]).exists())
        self.assertIsNone(self.media.repo.get("media_retention", second["job_id"]))

    async def test_corrupt_library_video_redownloads_and_removed_mapping_is_not_read(self):
        self.upstream.parts[BV] = ["111111111"]
        first = await self.run_job((await self.enqueue())["job_id"])
        root = self.verify_output(first)
        (root / "video.mp4").write_bytes(b"corrupt")
        resolved = await self.media.connector.resolve(BV, with_formats=False)
        target = self.media.config["targets"]["test-server"]
        second = await self.run_job(
            self.media.repo.enqueue(
                job_payload(
                    resolved,
                    self.upstream.parts[BV],
                    None,
                    "test-server",
                    dict(QUALITY),
                    **snapshot(target, "损坏后新目录"),
                )
            )["job_id"]
        )
        self.verify_output(second)
        self.assertEqual(self.media.engine.calls, ["111111111", "111111111"])
        old_targets = dict(self.media.config["targets"])
        self.media.config["targets"] = {}
        with patch.object(
            self.media.publisher.organizer,
            "operation",
            side_effect=AssertionError("removed root must not be touched"),
        ):
            self.assertIsNone(self.media.retention.source(BV, "111111111", "80"))
        self.media.config["targets"] = old_targets

    async def test_multiple_configured_roots_reuse_source_and_repointed_roots_are_not_read(self):
        self.upstream.parts[BV] = ["111111111"]
        first = await self.run_job((await self.enqueue())["job_id"])
        target = self.media.config["targets"]["test-server"]
        self.media.config["targets"]["another-server"] = {
            **target,
            "target_id": "another-server",
            "directory": str(Path(self.temp.name) / "second-library"),
            "library_id": str(uuid.uuid4()),
        }
        resolved = await self.media.connector.resolve(BV, with_formats=False)
        second = await self.run_job(
            self.media.repo.enqueue(
                job_payload(
                    resolved,
                    self.upstream.parts[BV],
                    None,
                    "another-server",
                    dict(QUALITY),
                    **snapshot(self.media.config["targets"]["another-server"], "跨卷"),
                )
            )["job_id"]
        )
        self.assertEqual(second["state"], "completed", second["code"])
        self.assertEqual(len(self.media.engine.calls), 1)
        self.assertEqual(second["downloads"]["111111111"]["source"]["target_id"], "test-server")
        self.media.config["targets"] = {
            "test-server": {**target, "directory": str(Path(self.temp.name) / "repointed")}
        }
        with patch.object(
            self.media.publisher.organizer,
            "operation",
            side_effect=AssertionError("repointed root must not be touched"),
        ):
            self.assertIsNone(self.media.retention.source(BV, "111111111", "80"))
        self.assertTrue(self.media.repo.job(first["job_id"]))

    async def test_independent_pins_survive_cleanup_before_peer_records_download(self):
        self.upstream.parts[BV] = ["111111111"]
        with patch.object(self.media.retention, "finish"):
            first = await self.run_job((await self.enqueue())["job_id"])
        resolved = await self.media.connector.resolve(BV, with_formats=False)
        target = self.media.config["targets"]["test-server"]
        second = self.media.repo.enqueue(
            job_payload(
                resolved,
                self.upstream.parts[BV],
                None,
                "test-server",
                dict(QUALITY),
                **snapshot(target, "并发目录"),
            )
        )
        claimed = self.media.repo.claim("peer")
        downloaded, reused = await self.media.download_cache.obtain(
            claimed, resolved["snapshot"]["parts"][0], None, "peer", "80"
        )
        self.assertTrue(reused)
        self.assertNotIn("downloads", self.media.repo.job(second["job_id"]))
        self.media.retention.finish(first)
        self.assertTrue(Path(downloaded["file"]).exists())
        self.media.repo.update_job(second["job_id"], {"downloads": {"111111111": downloaded}})
        await self.media.worker.run(self.media.repo.job(second["job_id"]), "peer")
        done = self.media.repo.job(second["job_id"])
        self.assertEqual(done["state"], "completed", done["code"])
        self.assertEqual(len(self.media.engine.calls), 1)

    async def test_delete_receipt_hides_task_counts_and_reenqueue_preserves_publication_base(self):
        self.upstream.parts[BV] = ["111111111"]
        first = await self.run_job((await self.enqueue())["job_id"])
        directory = self.verify_output(first)
        command = {
            "job_id": first["job_id"],
            "expected_revision": first["revision"],
            "client_id": str(uuid.uuid4()),
        }
        result = await self.post("jobs/delete", command)
        self.assertTrue(result["deleted"])
        self.assertTrue((await self.post("jobs/delete", command))["replayed"])
        self.assertIsNone(self.media.repo.job(first["job_id"]))
        self.assertIsNone(self.media.record(first["job_id"]))
        self.assertEqual(self.media.repo.job_overview()["total"], 0)
        self.assertEqual(self.media.repo.jobs(), ([], False))
        self.assertTrue((directory / "video.mp4").is_file())
        second = await self.run_job((await self.enqueue())["job_id"])
        self.assertNotEqual(first["job_id"], second["job_id"])
        self.assertEqual(second["state"], "completed", second["code"])
        self.assertEqual(len(self.media.engine.calls), 1)

    async def test_failed_delete_releases_dedupe_and_rejects_stale_revision_and_active_jobs(self):
        job = await self.enqueue()
        for revision in [job["revision"], job["revision"] + 1]:
            async with self.client.post(
                self.url + "/api/web/media/jobs/delete",
                json={
                    "job_id": job["job_id"],
                    "expected_revision": revision,
                    "client_id": str(uuid.uuid4()),
                },
                headers={"Origin": self.url, "X-CSRF-Token": self.csrf},
            ) as response:
                self.assertEqual(response.status, 409)
        failed = self.media.repo.update_job(
            job["job_id"], {"state": "failed", "code": "download_failed"}
        )
        await self.post(
            "jobs/delete",
            {
                "job_id": failed["job_id"],
                "expected_revision": failed["revision"],
                "client_id": str(uuid.uuid4()),
            },
        )
        replacement = await self.enqueue()
        self.assertNotEqual(replacement["job_id"], failed["job_id"])
        self.assertEqual(replacement["state"], "queued")
        self.assertEqual(self.media.repo.job_count(), 1)

    async def test_completed_generation_cleanup_preserves_same_job_new_generation_pin(self):
        self.upstream.parts[BV] = ["111111111"]
        with patch.object(self.media.retention, "finish"):
            first = await self.run_job((await self.enqueue())["job_id"])
        self.media.retention.register(first)
        await self.post(
            "jobs/metadata",
            {
                "job_id": first["job_id"],
                "expected_revision": first["revision"],
                "title": "并发修订",
                "description": "新简介",
                "client_id": str(uuid.uuid4()),
            },
        )
        current = self.media.repo.claim("new-generation")
        self.assertGreater(current["generation"], first["generation"])
        part = current["snapshot"]["parts"][0]
        pin, reused = await self.media.download_cache.obtain(
            current, part, None, "new-generation", "80"
        )
        self.assertTrue(reused)
        self.media.repo.update_job(current["job_id"], {"downloads": {part["cid"]: pin}})
        self.media.retention.cleanup(first)
        self.assertTrue(Path(pin["file"]).exists())
        await self.media.worker.run(self.media.repo.job(current["job_id"]), "new-generation")
        done = self.media.repo.job(current["job_id"])
        self.assertEqual(done["state"], "completed", done["code"])
        self.assertEqual(len(self.media.engine.calls), 1)

    async def test_prepared_partial_input_is_recovered_after_later_success(self):
        self.upstream.parts[BV] = ["111111111"]
        with patch.object(self.media.retention, "finish"):
            first = await self.run_job((await self.enqueue())["job_id"])
        download = first["downloads"]["111111111"]
        abandoned = (
            Path(self.media.config["staging_directory"])
            / ".inputs"
            / first["job_id"]
            / "0"
            / "111111111.mp4"
        )
        identity = digest({"job": first["job_id"], "file": str(abandoned)})
        self.media.repo.put(
            "media_input",
            identity,
            {
                **download,
                "file": str(abandoned),
                "job_id": first["job_id"],
                "cid": "111111111",
                "generation": 0,
                "state": "prepared",
            },
        )
        abandoned.parent.mkdir(parents=True)
        abandoned.write_bytes(b"interrupted partial copy")
        unknown = abandoned.parent / "unknown-user-file.txt"
        unknown.write_bytes(b"preserved")
        await self.media.retention.recover()
        self.assertFalse(abandoned.exists())
        self.assertEqual(unknown.read_bytes(), b"preserved")
        self.assertEqual(
            self.media.repo.get("media_retention", first["job_id"])["state"], "cleaned"
        )

    async def test_history_without_pins_cleans_known_legacy_and_global_download_paths(self):
        self.upstream.parts[BV] = ["111111111"]
        with patch.object(self.media.retention, "finish"):
            first = await self.run_job((await self.enqueue())["job_id"])
        record = self.media.repo.download_source(BV, "111111111", "80")["download"]
        old = (
            Path(self.media.config["staging_directory"])
            / ".downloads"
            / first["job_id"]
            / "1"
            / "111111111"
            / "download.mp4"
        )
        old.parent.mkdir(parents=True)
        os.link(record["file"], old)
        Path(first["downloads"]["111111111"]["file"]).unlink()
        with self.media.repo.transaction() as db:
            db.execute("DELETE FROM documents WHERE kind='media_input'")
        first = self.media.repo.update_job(
            first["job_id"], {"downloads": {"111111111": {**record, "file": str(old)}}}
        )
        await self.media.retention.recover()
        self.assertFalse(old.exists())
        self.assertFalse(Path(record["file"]).exists())
        self.assertFalse(
            (
                Path(self.media.config["staging_directory"]) / first["package"]["staging_ref"]
            ).exists()
        )
        self.assertTrue(self.media.retention.source(BV, "111111111", "80"))

    async def test_start_schedules_immediate_recovery_without_waiting_for_large_file_hashes(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_recovery():
            entered.set()
            await release.wait()

        with patch.object(self.media.retention, "recover", side_effect=slow_recovery):
            await asyncio.wait_for(Media.start(self.media), 1)
            await asyncio.wait_for(entered.wait(), 1)
            self.assertFalse(release.is_set())
            await Media.close(self.media)

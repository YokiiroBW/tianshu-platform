import json
import unittest
import uuid
from pathlib import Path

from services.platform.contracts import Fault

import test_media_runtime as existing
from music_fixtures import SyntheticNetease


class MusicHttpTests(unittest.IsolatedAsyncioTestCase):
    # Reuse the existing authenticated, isolated platform HTTP fixture without inheriting its tests.
    asyncSetUp = existing.MediaRuntimeTests.asyncSetUp
    post = existing.MediaRuntimeTests.post

    async def test_maintenance_schedule_receipt_and_public_control_state(self):
        await self.login_music()
        view = await self.post("music/view", {})
        self.assertFalse(view["maintenance"]["settings"]["enabled"])
        command = {
            "enabled": True,
            "playlist_interval_seconds": 43200,
            "expected_revision": 0,
            "client_id": str(uuid.uuid4()),
        }
        saved = await self.post("music/maintenance/save", command)
        self.assertTrue(saved["settings"]["enabled"])
        self.assertTrue((await self.post("music/maintenance/save", command))["replayed"])
        await self.post("music/maintenance/save", {**command, "client_id": str(uuid.uuid4())}, 409)
        view = await self.post("music/view", {})
        self.assertEqual(view["maintenance"]["settings"]["playlist_interval_seconds"], 43200)
        self.assertNotIn("secret", json.dumps(view["maintenance"]))

    async def test_navidrome_connection_playlist_receipts_and_auth(self):
        from test_music_navidrome import SyntheticNavidrome

        server = SyntheticNavidrome()
        await server.start()
        self.addAsyncCleanup(server.close)
        account = await self.login_music()
        await self.post(
            "music/settings/save",
            {
                "directory": self.temp.name,
                "layout": "artist_album",
                "expected_revision": 0,
                "client_id": str(uuid.uuid4()),
            },
        )
        playlist = (
            await self.post(
                "music/playlists/subscribe",
                {
                    "account_id": account["account_id"],
                    "source_id": "900",
                    "interval_seconds": 3600,
                    "expected_revision": 0,
                    "client_id": str(uuid.uuid4()),
                },
            )
        )["playlist"]
        connection = {"base_url": server.url, "username": "synthetic", "password": server.password}
        checked = await self.post("music/navidrome/test", connection)
        self.assertEqual(checked["folders"][0]["id"], "1")
        command = {
            **connection,
            "music_folder_id": "1",
            "music_directory": "/music/音乐存档",
            "expected_revision": 0,
            "client_id": str(uuid.uuid4()),
        }
        saved = await self.post("music/navidrome/save", command)
        calls = len(server.calls)
        self.assertTrue((await self.post("music/navidrome/save", command))["replayed"])
        self.assertEqual(len(server.calls), calls)
        self.assertNotIn(server.password, json.dumps(saved))
        link_command = {
            "playlist_id": playlist["playlist_id"],
            "name": "我的本地歌单",
            "public": False,
            "auto_sync": True,
            "expected_revision": 0,
            "client_id": str(uuid.uuid4()),
        }
        linked = (await self.post("music/navidrome/playlists/save", link_command))["playlist"]
        self.assertTrue(
            (await self.post("music/navidrome/playlists/save", link_command))["replayed"]
        )
        await self.media.music.navidrome.tick()
        view = (await self.post("music/view", {}))["navidrome"]
        self.assertEqual(view["playlists"][0]["status"], "partial")
        self.assertEqual(len(server.playlists), 1)
        await self.post(
            "music/navidrome/playlists/control",
            {
                "link_id": linked["link_id"],
                "action": "pause",
                "expected_revision": linked["revision"],
                "client_id": str(uuid.uuid4()),
            },
        )
        await self.post(
            "music/navidrome/playlists/control",
            {
                "link_id": linked["link_id"],
                "action": "resume",
                "expected_revision": linked["revision"],
                "client_id": str(uuid.uuid4()),
            },
            409,
        )
        self.assertNotIn(server.password.encode(), self.media.repo.database.read_bytes())
        self.assertNotIn("secret", json.dumps(view))
        before = len(server.calls)
        async with self.client.post(
            self.url + "/api/web/media/music/navidrome/test",
            json=connection,
            headers={"Origin": self.url},
        ) as response:
            self.assertEqual(response.status, 403)
        self.client.cookie_jar.clear()
        await self.post("music/navidrome/test", connection, 401)
        self.assertEqual(len(server.calls), before)

    async def test_qq_qr_account_isolated_provider_and_catalog_routes(self):
        class ThirdParty(SyntheticNetease):
            async def qr_start(self, login_type):
                self.login_type = login_type
                return await super().qr_start()

            async def qr_poll(self, key, cookie):
                return "ready", {"cookie": "synthetic-qq-session"}

        client = ThirdParty()
        self.media.music.connectors["qq"] = client
        qr = await self.post("music/accounts/qr/start", {"provider": "qq", "login_type": "wx"})
        account = (await self.post("music/accounts/qr/poll", {"qr_id": qr["qr_id"]}))["account"]
        self.assertEqual(account["provider"], "qq")
        self.assertEqual(account["account_id"], "qq:123")
        self.assertEqual(client.login_type, "wx")
        playlists = await self.post("music/playlists/list", {"account_id": account["account_id"]})
        self.assertEqual(len(playlists["playlists"]), 1)
        view = await self.post("music/view", {})
        self.assertEqual(view["routing"], "automatic")
        self.assertEqual({p["provider"] for p in view["providers"]}, {"netease", "qq", "kugou"})
        self.assertNotIn("synthetic-qq-session", json.dumps(view))
        catalog = await self.post("music/library/view", {"offset": 0})
        self.assertEqual(catalog["items"], [])
        await self.post("music/accounts/qr/start", {"provider": "soda"}, 400)

    async def login_music(self):
        self.media.music.connector = SyntheticNetease()
        qr = await self.post("music/accounts/qr/start", {})
        self.media.music.connector.state = "ready"
        result = await self.post("music/accounts/qr/poll", {"qr_id": qr["qr_id"]})
        return result["account"]

    async def test_audio_queue_replay_controls_task_projection_and_export(self):
        account = await self.login_music()
        directory = Path(self.temp.name) / "music"
        directory.mkdir()
        await self.post(
            "music/settings/save",
            {
                "directory": str(directory),
                "layout": "artist_album",
                "expected_revision": 0,
                "client_id": str(uuid.uuid4()),
            },
        )
        playlist = (
            await self.post(
                "music/playlists/subscribe",
                {
                    "account_id": account["account_id"],
                    "source_id": "900",
                    "interval_seconds": 3600,
                    "expected_revision": 0,
                    "client_id": str(uuid.uuid4()),
                },
            )
        )["playlist"]
        command = {
            "playlist_id": playlist["playlist_id"],
            "quality": {"level": "lossless", "allow_fallback": False},
            "auto_download": True,
            "expected_revision": playlist["revision"],
            "client_id": str(uuid.uuid4()),
        }
        configured = await self.post("music/playlists/downloads", command)
        self.assertEqual(configured["queued"], 2)
        self.assertTrue((await self.post("music/playlists/downloads", command))["replayed"])
        self.assertEqual(self.media.repo.job_count(), 2)
        jobs = await self.post("jobs/list", {"states": None, "cursor": None, "page_size": 10})
        job = jobs["jobs"][0]
        self.assertEqual(job["media_kind"], "audio")
        self.assertIsNone(job["bvid"])
        detail = await self.post("jobs/detail", {"job_id": job["job_id"]})
        self.assertIsNone(detail["metadata"])
        await self.post(
            "jobs/refresh_metadata",
            {
                "job_id": job["job_id"],
                "expected_revision": job["revision"],
                "client_id": str(uuid.uuid4()),
            },
            409,
        )
        await self.post(
            "jobs/control",
            {"job_id": job["job_id"], "action": "cancel", "client_id": str(uuid.uuid4())},
        )
        self.assertEqual(self.media.repo.job(job["job_id"])["state"], "cancelled")
        await self.post(
            "jobs/control",
            {"job_id": job["job_id"], "action": "retry", "client_id": str(uuid.uuid4())},
        )

        async def forbidden(*args):
            raise Fault("music_download_forbidden", 409)

        self.media.music.connector.download = forbidden
        for _ in range(2):
            claimed = self.media.repo.claim("http-fixture")
            await self.media.worker.run(claimed, "http-fixture")
        detail = await self.post("jobs/detail", {"job_id": job["job_id"]})
        self.assertEqual(detail["job"]["code"], "music_download_forbidden")
        unlock = {"state": "key_required", "format": "mflac"}
        attempts = [
            {
                "provider": "qq",
                "source_id": "synthetic",
                "code": "music_decode_key_required",
                "unlock": unlock,
            }
        ]
        routing = {
            "policy": "highest_available",
            "providers": [
                {"provider": "qq", "matches": 1, "available_levels": ["lossless"], "failures": []},
                {
                    "provider": "netease",
                    "matches": 1,
                    "available_levels": ["hires"],
                    "failures": [],
                },
            ],
            "candidates": [],
        }
        self.media.repo.update_job(
            job["job_id"],
            {
                "audio_unlock": unlock,
                "audio_attempts": attempts,
                "audio_routing": routing,
                "code": "music_decode_key_required",
            },
        )
        detail = await self.post("jobs/detail", {"job_id": job["job_id"]})
        self.assertEqual(detail["job"]["audio_unlock"], unlock)
        self.assertEqual(detail["job"]["audio_attempts"], attempts)
        self.assertEqual(detail["job"]["audio_routing"], routing)
        listed = await self.post("jobs/list", {"states": None, "cursor": None, "page_size": 10})
        projected = next(row for row in listed["jobs"] if row["job_id"] == job["job_id"])
        self.assertEqual(projected["audio_unlock"], unlock)
        self.assertEqual(projected["audio_attempts"], attempts)
        self.assertEqual(projected["audio_routing"], routing)
        async with self.client.post(
            self.url + "/api/web/tasks/detail",
            json={"task_id": "media:" + job["job_id"]},
            headers={"Origin": self.url, "X-CSRF-Token": self.csrf},
        ) as response:
            self.assertEqual(response.status, 200)
            body = await response.json()
            self.assertIn("audio_archived", json.dumps(body))
            self.assertNotIn('"bvid"', json.dumps(body))
        exported = await self.post(
            "music/playlists/export", {"playlist_id": playlist["playlist_id"]}
        )
        self.assertEqual((exported["exported"], exported["total"]), (0, 4))
        self.assertTrue((Path(exported["directory"]) / "playlist.json").is_file())

    async def test_auto_download_without_directory_rolls_back_setting(self):
        account = await self.login_music()
        playlist = (
            await self.post(
                "music/playlists/subscribe",
                {
                    "account_id": account["account_id"],
                    "source_id": "900",
                    "interval_seconds": 3600,
                    "expected_revision": 0,
                    "client_id": str(uuid.uuid4()),
                },
            )
        )["playlist"]
        await self.post(
            "music/playlists/downloads",
            {
                "playlist_id": playlist["playlist_id"],
                "quality": {"level": "lossless", "allow_fallback": False},
                "auto_download": True,
                "expected_revision": 1,
                "client_id": str(uuid.uuid4()),
            },
            409,
        )
        saved = self.media.repo.get("music_playlist", playlist["playlist_id"])
        self.assertFalse(saved["auto_download"])
        self.assertEqual(saved["revision"], 1)

    async def test_qr_login_catalog_subscribe_and_persisted_detail(self):
        account = await self.login_music()
        self.assertEqual(account["state"], "ready")
        catalog = await self.post("music/playlists/list", {"account_id": account["account_id"]})
        self.assertEqual(len(catalog["playlists"]), 1)
        command = {
            "account_id": account["account_id"],
            "source_id": "900",
            "interval_seconds": 3600,
            "expected_revision": 0,
            "client_id": str(uuid.uuid4()),
        }
        result = await self.post("music/playlists/subscribe", command)
        replay = await self.post("music/playlists/subscribe", command)
        self.assertTrue(replay["replayed"])
        detail = await self.post(
            "music/playlists/detail",
            {"playlist_id": result["playlist"]["playlist_id"], "offset": 0},
        )
        self.assertEqual([track["position"] for track in detail["tracks"]], [1, 2, 3, 4])
        view = await self.post("music/view", {})
        self.assertNotIn("synthetic-music-session", json.dumps(view))
        self.assertNotIn("secret", json.dumps(view))
        self.assertEqual((await self.post("view", {}))["accounts"], [])

    async def test_music_uses_existing_csrf_and_login(self):
        async with self.client.post(
            self.url + "/api/web/media/music/accounts/qr/start",
            json={},
            headers={"Origin": self.url},
        ) as response:
            self.assertEqual(response.status, 403)
        self.client.cookie_jar.clear()
        await self.post("music/view", {}, 401)

    async def test_qr_binding_does_not_allow_other_console_session(self):
        self.media.music.connector = SyntheticNetease()
        qr = await self.post("music/accounts/qr/start", {})
        self.media.music.qr[qr["qr_id"]]["session"] = {"synthetic": "other session"}
        await self.post("music/accounts/qr/poll", {"qr_id": qr["qr_id"]}, 403)

    async def test_storage_config_can_be_saved_without_modifying_directory(self):
        path = self.temp.name
        result = await self.post(
            "music/settings/save",
            {
                "directory": path,
                "layout": "artist_album",
                "expected_revision": 0,
                "client_id": str(uuid.uuid4()),
            },
        )
        self.assertEqual(result["settings"]["directory"], path)
        result = await self.post("music/settings/inspect", {"directory": path})
        self.assertEqual(result["state"], "readable")
        await self.post(
            "music/settings/save",
            {
                "directory": path,
                "layout": "artist_album",
                "expected_revision": 0,
                "client_id": str(uuid.uuid4()),
            },
            409,
        )

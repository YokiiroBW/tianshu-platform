import json
import unittest
import uuid

import test_media_runtime as existing
from music_fixtures import SyntheticNetease


class MusicHttpTests(unittest.IsolatedAsyncioTestCase):
    # Reuse the existing authenticated, isolated platform HTTP fixture without inheriting its tests.
    asyncSetUp = existing.MediaRuntimeTests.asyncSetUp
    post = existing.MediaRuntimeTests.post

    async def login_music(self):
        self.media.music.connector = SyntheticNetease()
        qr = await self.post("music/accounts/qr/start", {})
        self.media.music.connector.state = "ready"
        result = await self.post("music/accounts/qr/poll", {"qr_id": qr["qr_id"]})
        return result["account"]

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

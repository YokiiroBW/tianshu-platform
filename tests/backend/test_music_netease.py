import json
import time
import unittest
from unittest.mock import AsyncMock

from aiohttp import web
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from services.platform.contracts import Fault
from services.platform.media.music.netease import Netease


class RecordedNetease(Netease):
    def __init__(self):
        super().__init__()
        self.details = 0
        self.changed = False
        self.incomplete = False
        self.requests = []

    async def request(self, path, data, cookie=None, **kwargs):
        self.requests.append((path, data))
        if path == "/api/user/playlist":
            start = data["offset"]
            rows = [
                {"id": i + 1, "name": "合成歌单", "trackCount": 0, "creator": {"userId": 123}}
                for i in range(start, min(start + 100, 105))
            ]
            return {"code": 200, "playlist": rows, "more": start == 0}, {}
        if path == "/api/v6/playlist/detail":
            self.details += 1
            return {
                "code": 200,
                "playlist": {
                    "id": 900,
                    "name": "测试",
                    "trackCount": 4 if self.incomplete else 3,
                    "updateTime": 2 if self.changed and self.details > 1 else 1,
                    "trackIds": [{"id": 101}, {"id": 102}, {"id": 101}],
                    "creator": {"userId": 123},
                },
            }, {}
        if path == "/api/v3/song/detail":
            return {
                "code": 200,
                "songs": [
                    {
                        "id": 101,
                        "name": "第一首",
                        "ar": [{"name": "歌手"}],
                        "al": {"id": 99, "name": "专辑"},
                        "no": 8,
                        "dt": 120000,
                    }
                ],
            }, {}
        raise AssertionError(path)


class MusicNeteaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_version_and_members_skip_track_detail_but_unknown_version_does_not(
        self,
    ):
        api = RecordedNetease()
        first = await api.snapshot("900", "123", {})
        previous = {
            **first["playlist"],
            "entries": first["entries"],
            "complete": True,
            "metadata_synced_at": time.time(),
        }
        api.requests.clear()
        unchanged = await api.snapshot_since("900", "123", {}, previous)
        self.assertTrue(unchanged["unchanged"])
        self.assertEqual(len(api.requests), 1)
        self.assertNotIn("tracks", unchanged)
        api.requests.clear()
        await api.snapshot_since("900", "123", {}, {**previous, "source_updated_at": None})
        self.assertTrue(any(path == "/api/v3/song/detail" for path, _ in api.requests))
        api.requests.clear()
        await api.snapshot_since(
            "900", "123", {}, {**previous, "metadata_synced_at": time.time() - 8 * 86400}
        )
        self.assertTrue(any(path == "/api/v3/song/detail" for path, _ in api.requests))

    async def test_download_request_and_denied_preview_unknown_quality(self):
        api = Netease()
        row = {
            "id": 101,
            "url": "http://m10.music.126.net/synthetic.mp3",
            "level": "lossless",
            "size": 12345,
            "md5": "0" * 32,
            "code": 200,
            "freeTrialPrivilege": None,
        }
        api.request = AsyncMock(return_value=({"data": row}, {}))
        found = await api.download("101", "best", {"MUSIC_U": "synthetic"})
        self.assertEqual(found["url"], "https://m10.music.126.net/synthetic.mp3")
        args, kwargs = api.request.call_args
        self.assertEqual(args[1]["level"], "hires")
        self.assertEqual(kwargs["mode"], "eapi")
        for changes, code in (
            ({"url": None}, "music_download_forbidden"),
            ({"freeTrialInfo": {"start": 0, "end": 30}}, "music_preview_rejected"),
            ({"level": "unknown"}, "music_quality_unknown"),
            ({"id": 102}, "music_upstream_invalid"),
        ):
            with self.subTest(code=code):
                api.request.return_value = ({"data": {**row, **changes}}, {})
                with self.assertRaises(Fault) as error:
                    await api.download("101", "lossless", {})
                self.assertEqual(error.exception.code, code)

    async def test_lyrics_and_album_normalize_optional_metadata(self):
        api = Netease()
        api.request = AsyncMock(
            return_value=({"lrc": {"lyric": "[00:00]正文"}, "tlyric": None}, {})
        )
        self.assertEqual((await api.lyrics("101", {}))["original"], "[00:00]正文")
        api.request.return_value = ({"lrc": "malformed"}, {})
        with self.assertRaises(Fault):
            await api.lyrics("101", {})
        api.request.return_value = (
            {
                "album": {
                    "id": 11,
                    "artists": [{"name": "专辑艺术家"}],
                    "publishTime": 1767283200000,
                }
            },
            {},
        )
        self.assertEqual(
            await api.album("11", {}), {"album_artists": ["专辑艺术家"], "date": "2026-01-02"}
        )

    async def test_full_playlist_catalog_pagination(self):
        api = RecordedNetease()
        result = await api.playlists("123", {})
        self.assertEqual(len(result), 105)
        self.assertEqual([data["offset"] for _, data in api.requests], [0, 100])

    async def test_detail_uses_full_ids_preserves_duplicates_and_missing(self):
        result = await RecordedNetease().snapshot("900", "123", {})
        self.assertEqual(result["entries"], ["101", "102", "101"])
        self.assertEqual(result["tracks"][0]["track_number"], 8)
        self.assertEqual(result["tracks"][1]["availability"], "unavailable")

    async def test_partial_and_mutating_source_not_accepted(self):
        for attribute, code in (
            ("incomplete", "music_scan_incomplete"),
            ("changed", "music_source_changed"),
        ):
            with self.subTest(attribute=attribute):
                api = RecordedNetease()
                setattr(api, attribute, True)
                with self.assertRaises(Fault) as failure:
                    await api.snapshot("900", "123", {})
                self.assertEqual(failure.exception.code, code)

    async def test_transport_envelope_cookies_and_response_errors(self):
        received = []
        code = 200

        async def handler(request):
            form = await request.post()
            decryptor = Cipher(algorithms.AES(b"e82ckenh8dichen8"), modes.ECB()).decryptor()
            data = decryptor.update(bytes.fromhex(form["params"])) + decryptor.finalize()
            unpad = padding.PKCS7(128).unpadder()
            plain = (unpad.update(data) + unpad.finalize()).decode()
            path, body, _ = plain.split("-36cd479b6b5-")
            received.append((path, json.loads(body), dict(request.cookies)))
            response = web.json_response({"code": code})
            response.set_cookie("MUSIC_U", "synthetic-response-cookie")
            return response

        app = web.Application()
        app.router.add_post("/eapi/test", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        api = Netease(api_origin=f"http://127.0.0.1:{port}")
        self.addAsyncCleanup(api.close)
        await api.request("/api/test", {"id": "1"}, {"MUSIC_U": "synthetic-A"}, mode="eapi")
        await api.request("/api/test", {"id": "2"}, {"MUSIC_U": "synthetic-B"}, mode="eapi")
        await api.request("/api/test", {}, mode="eapi")
        self.assertEqual(received[0][0], "/api/test")
        self.assertEqual(received[1][1]["header"]["MUSIC_U"], "synthetic-B")
        self.assertNotIn("MUSIC_U", received[2][2])
        self.assertFalse(received[0][1]["e_r"])
        code = 301
        with self.assertRaises(Fault) as failure:
            await api.request("/api/test", {}, mode="eapi")
        self.assertEqual(failure.exception.code, "music_auth_required")

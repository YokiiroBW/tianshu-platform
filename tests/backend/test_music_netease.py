import json
import unittest

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

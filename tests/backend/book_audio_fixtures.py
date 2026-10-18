"""A real HTTP synthesis endpoint returning valid synthetic tone MP3s, never paid speech."""

import asyncio
import base64
from collections import Counter
import json
import os
import subprocess

from aiohttp import web


class AudioSite:
    def __init__(self, folder):
        self.folder, self.hits, self.requests = folder, Counter(), []
        self.status, self.disconnect, self.delay = 200, False, 0
        self.disconnect_at = None

    async def start(self):
        path = self.folder / "tone.mp3"
        ffmpeg = os.environ.get("MEDIA_TEST_FFMPEG", "ffmpeg")
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=400:duration=1.5",
                "-ar",
                "24000",
                "-ac",
                "1",
                "-b:a",
                "48k",
                "-y",
                str(path),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        self.raw = path.read_bytes()
        app = web.Application()
        app.router.add_post("/v1/audio/speech", self.synthesize)
        app.router.add_post("/v1/tts/stream/with-timestamp", self.synthesize)
        app.router.add_get("/__fixture/status", self.status_view)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        return self

    async def synthesize(self, request):
        body = await request.json()
        self.hits[request.path] += 1
        self.requests.append(body)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.status != 200:
            return web.json_response({"error": "synthetic rejection"}, status=self.status)
        if self.disconnect or len(self.requests) == self.disconnect_at:
            request.transport.close()
            return web.Response()
        if request.path.endswith("speech"):
            return web.Response(body=self.raw, content_type="audio/mpeg")
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        midpoint = len(self.raw) // 2
        for index, raw in enumerate((self.raw[:midpoint], self.raw[midpoint:])):
            event = {
                "chunk_seq": 0,
                "chunk_audio_offset_sec": 0,
                "audio_base64": base64.b64encode(raw).decode(),
                "alignment": {
                    "audio_duration": 1.5,
                    "segments": [{"start": 0, "end": 1.3, "text": body["text"]}],
                },
            }
            encoded = ("data: " + json.dumps(event) + "\n\n").encode()
            await response.write(encoded[:30])
            await response.write(encoded[30:])
        await response.write_eof()
        return response

    async def status_view(self, request):
        return web.json_response({"hits": dict(self.hits)})


def ffmpeg_path():
    return os.environ.get("MEDIA_TEST_FFMPEG", "ffmpeg")

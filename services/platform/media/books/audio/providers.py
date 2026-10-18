"""Bounded provider transports. A request with an uncertain outcome is never retried here."""

import asyncio
import base64
import binascii
import json

import aiohttp

from ....contracts import Fault, require

MAX_AUDIO = 32 * 1024 * 1024


class ProviderRejected(Fault):
    """The remote endpoint explicitly rejected this request before synthesis."""


class Providers:
    def __init__(self, *, fish_endpoint="https://api.fish.audio"):
        self.fish_endpoint = fish_endpoint

    async def synthesize(self, profile, text, key):
        async with asyncio.timeout(180):
            if profile["provider"] == "edge":
                return await self.edge(profile, text)
            return await self.http(profile, text, key)

    async def edge(self, profile, text):
        import edge_tts

        stream = edge_tts.Communicate(
            text,
            profile["voice"],
            rate=f"{round((profile['speed'] - 1) * 100):+d}%",
            pitch=f"{profile['pitch']:+d}Hz",
            boundary="SentenceBoundary",
            connect_timeout=15,
            receive_timeout=90,
        )
        audio, boundaries = bytearray(), []
        async for event in stream.stream():
            if event["type"] == "audio":
                audio.extend(event["data"])
                require(len(audio) <= MAX_AUDIO, "book_audio_response_invalid", 502)
            elif event["type"] in {"SentenceBoundary", "WordBoundary"}:
                start = event["offset"] / 10000000
                boundaries.append(
                    {
                        "start": start,
                        "end": start + event["duration"] / 10000000,
                        "text": event["text"],
                    }
                )
        return bytes(audio), boundaries

    async def http(self, profile, text, key):
        fish = profile["provider"] == "fish"
        endpoint = self.fish_endpoint if fish else profile["endpoint"]
        path = "/v1/tts/stream/with-timestamp" if fish else "/v1/audio/speech"
        headers = {"Authorization": "Bearer " + key} if key else {}
        if fish:
            headers["model"] = profile["model"]
            body = {
                "text": text,
                "reference_id": profile["voice"],
                "format": "mp3",
                "prosody": {"speed": profile["speed"], "volume": 0},
            }
        else:
            body = {
                "model": profile["model"],
                "input": text,
                "voice": profile["voice"],
                "speed": profile["speed"],
                "response_format": "mp3",
            }
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=170, connect=15)
        ) as session:
            async with session.post(
                endpoint + path, json=body, headers=headers, allow_redirects=False
            ) as response:
                if response.status in {400, 401, 402, 403, 404, 413, 422, 429}:
                    code = {
                        401: "book_audio_auth",
                        403: "book_audio_auth",
                        402: "book_audio_quota",
                        429: "book_audio_rate_limited",
                    }.get(response.status, "book_audio_rejected")
                    raise ProviderRejected(code, 409)
                require(response.status == 200, "book_audio_remote_unknown", 502)
                if fish:
                    require(
                        response.content_type == "text/event-stream",
                        "book_audio_response_invalid",
                        502,
                    )
                    return await self.fish_stream(response)
                audio = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    audio.extend(chunk)
                    require(len(audio) <= MAX_AUDIO, "book_audio_response_invalid", 502)
                return bytes(audio), []

    async def fish_stream(self, response):
        audio, snapshots, lines, size = bytearray(), {}, [], 0

        def consume():
            if not lines:
                return
            data = "\n".join(lines)
            lines.clear()
            if data == "[DONE]":
                return
            try:
                event = json.loads(data)
                sequence = event["chunk_seq"]
                require(
                    type(sequence) is int and 0 <= sequence < 10000,
                    "book_audio_response_invalid",
                    502,
                )
                audio.extend(base64.b64decode(event.get("audio_base64", ""), validate=True))
                require(len(audio) <= MAX_AUDIO, "book_audio_response_invalid", 502)
                alignment = event.get("alignment")
                if alignment:
                    offset = float(event["chunk_audio_offset_sec"])
                    # Each alignment is a replacement cumulative snapshot for this chunk.
                    snapshots[sequence] = [
                        {
                            "text": item["text"],
                            "start": offset + float(item["start"]),
                            "end": offset + float(item["end"]),
                        }
                        for item in alignment["segments"]
                    ]
            except (ValueError, KeyError, TypeError, binascii.Error):
                raise Fault("book_audio_response_invalid", 502) from None

        # aiohttp's line reader preserves split TCP packets and rejects unbounded lines.
        async for raw in response.content:
            size += len(raw)
            require(size <= MAX_AUDIO * 2, "book_audio_response_invalid", 502)
            line = raw.decode("utf-8").rstrip("\r\n")
            if not line:
                consume()
            elif line.startswith("data:"):
                lines.append(line[5:].lstrip())
        consume()
        return bytes(audio), [item for seq in sorted(snapshots) for item in snapshots[seq]]

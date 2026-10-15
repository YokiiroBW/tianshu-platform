"""Synthetic music identities and explicit upstream failures; no real account material."""

import copy

from services.platform.contracts import Fault


def snapshot(ids=None):
    ids = ids if ids is not None else ["101", "102", "101", "103"]
    return {
        "playlist": {
            "source_id": "900",
            "name": "合成收藏",
            "description": "隔离歌单",
            "cover_url": None,
            "track_count": len(ids),
            "source_updated_at": 1,
            "kind": "created",
        },
        "entries": ids,
        "tracks": [
            {
                "source_id": i,
                "title": "歌曲 " + i if i != "103" else "",
                "artists": ["合成艺术家"],
                "album": "合成专辑",
                "availability": "unavailable" if i == "103" else "known",
                "audio_state": "not_downloaded",
            }
            for i in dict.fromkeys(ids)
        ],
    }


class SyntheticNetease:
    def __init__(self):
        self.state = "waiting"
        self.value = snapshot()
        self.error = None
        self.calls = 0
        self.waiting = None
        self.release = None

    async def close(self):
        pass

    async def qr_start(self):
        return {
            "key": "synthetic-key",
            "cookie": {},
            "url": "https://music.163.com/login?codekey=synthetic-key",
        }

    async def qr_poll(self, key, cookie):
        return self.state, {"MUSIC_U": "synthetic-music-session"} if self.state == "ready" else {}

    async def account(self, cookie):
        return {"user_id": "123", "label": "合成网易云账号"}

    async def playlists(self, uid, cookie):
        return [copy.deepcopy(self.value["playlist"])]

    async def snapshot(self, playlist_id, uid, cookie):
        self.calls += 1
        if self.waiting:
            self.waiting.set()
            await self.release.wait()
        if self.error:
            raise Fault(self.error, 502)
        return copy.deepcopy(self.value)

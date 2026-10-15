"""Provider-neutral music metadata, backed by the existing NetEase adapter and local tools."""

import re

from ...contracts import require
from . import tools
from .transfer import media_url

PROVIDERS = {"netease": "网易云音乐", "qq": "QQ 音乐", "kugou": "酷狗音乐"}


def identity(value):
    require(type(value) in (str, int), "invalid_music_id", 400)
    value = str(value)
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:_.-]{0,159}", value), "invalid_music_id", 400)
    return value


def playlist(row):
    source = identity(row["id"])
    extra = row.get("extra") or {}
    kind = "liked" if source == "profile:favorites" or extra.get("dirid") == "201" else "created"
    return {
        "source_id": source,
        "name": str(row.get("name") or source)[:256],
        "description": str(row.get("description") or "")[:8192],
        "cover_url": row.get("cover") or None,
        "track_count": row.get("track_count"),
        "source_updated_at": extra.get("update_time") or extra.get("publish_time"),
        "kind": kind,
    }


def track(row, provider):
    require(isinstance(row, dict), "music_upstream_invalid", 502)
    names = [
        value.strip() for value in re.split(r"、|, | / ", row.get("artist") or "") if value.strip()
    ]
    extra = row.get("extra") or {}
    safe_extra = {
        key: str(value)[:160]
        for key, value in extra.items()
        if key
        in {
            "songmid",
            "song_id",
            "media_mid",
            "hash",
            "sqhash",
            "hqhash",
            "album_id",
            "album_audio_id",
            "mixsongid",
        }
        and isinstance(value, (str, int))
    }
    artwork = None
    if row.get("cover"):
        try:
            artwork = media_url(row["cover"], provider, artwork=True)
        except Exception:
            pass
    duration = row.get("duration")
    return {
        "source_id": identity(row["id"]),
        "provider": provider,
        "title": str(row.get("name") or "")[:512],
        "artists": names,
        "album": str(row.get("album") or "")[:512],
        "album_id": str(row.get("album_id") or safe_extra.get("album_id") or "") or None,
        "cover_url": artwork,
        "duration_ms": duration * 1000
        if isinstance(duration, (int, float)) and duration > 0
        else None,
        "disc": "",
        "track_number": None,
        "availability": "known",
        "audio_state": "not_downloaded",
        "provider_data": safe_extra,
    }


def bridge_song(value, provider):
    return {
        "id": value["source_id"],
        "source": provider,
        "name": value.get("title", ""),
        "artist": "、".join(value.get("artists", [])),
        "album": value.get("album", ""),
        "album_id": value.get("album_id") or "",
        "duration": int((value.get("duration_ms") or 0) / 1000),
        "extra": value.get("provider_data", {}),
    }


class Bridge:
    def __init__(self, provider):
        self.provider = provider

    async def close(self):
        pass

    async def call(self, operation, cookie=None, **body):
        return await tools.invoke(
            "musicbridge",
            {
                "provider": self.provider,
                "operation": operation,
                "cookie": (cookie or {}).get("cookie", ""),
                **body,
            },
        )

    async def qr_start(self, login_type="qq"):
        value = await self.call("qr_start", login_type=login_type)
        require(value.get("key"), "music_upstream_invalid", 502)
        return {
            "key": value["key"],
            "cookie": {"login_type": login_type},
            "url": value.get("url") or "",
            "image": value.get("image_url"),
        }

    async def qr_poll(self, key, cookie):
        value = await self.call("qr_poll", key=key, login_type=cookie.get("login_type", "qq"))
        status = {"success": "ready", "failed": "expired"}.get(
            value.get("status"), value.get("status")
        )
        require(status in {"ready", "waiting", "scanned", "expired"}, "music_upstream_invalid", 502)
        return status, {"cookie": value.get("cookie") or ""} if status == "ready" else cookie

    async def account(self, cookie):
        value = await self.call("account", cookie)
        identity(value.get("user_id"))
        return {
            "user_id": str(value["user_id"]),
            "label": PROVIDERS[self.provider] + " " + str(value["user_id"]),
        }

    async def playlists(self, uid, cookie):
        result, seen = [], set()
        for page in range(1, 101):
            rows = await self.call("playlists", cookie, page=page, limit=100)
            require(isinstance(rows, list) or rows is None, "music_upstream_invalid", 502)
            rows = rows or []
            new = 0
            for raw in rows:
                item = playlist(raw)
                if item["source_id"] not in seen:
                    seen.add(item["source_id"])
                    result.append(item)
                    new += 1
            if len(rows) < 100:
                return result
            require(new > 0, "music_scan_incomplete", 502)
        require(False, "music_snapshot_too_large", 502)

    async def snapshot(self, source, uid, cookie):
        source = identity(source)
        catalog = await self.playlists(uid, cookie)
        info = next((p for p in catalog if p["source_id"] == source), None)
        if info:
            rows = await self.call("playlist_songs", cookie, id=source)
        else:
            value = await self.call("playlist", cookie, id=source)
            info, rows = playlist(value["playlist"]), value["songs"]
        rows = rows or []
        require(
            type(info["track_count"]) is int and len(rows) == info["track_count"] <= 20000,
            "music_scan_incomplete",
            502,
        )
        tracks = [track(row, self.provider) for row in rows]
        current = await self.call("playlist_songs", cookie, id=source)
        entries = [row["source_id"] for row in tracks]
        require([str(r["id"]) for r in (current or [])] == entries, "music_source_changed", 409)
        return {
            "playlist": info,
            "entries": entries,
            "tracks": list({t["source_id"]: t for t in tracks}.values()),
        }

    async def search(self, query, cookie):
        rows = await self.call("search", cookie, query=query)
        return [track(row, self.provider) for row in (rows or [])[:30]]

    async def download_track(self, value, level, cookie):
        offered = await self.call(
            "download", cookie, level=level, song=bridge_song(value, self.provider)
        )
        offered["url"] = media_url(offered["url"], self.provider)
        require(
            offered.get("level")
            in {"observed", "standard", "higher", "exhigh", "lossless", "hires"},
            "music_quality_unknown",
            409,
        )
        return offered

    async def lyrics_track(self, value, cookie):
        result = await self.call("lyrics", cookie, song=bridge_song(value, self.provider))
        return {"original": result[: 512 * 1024] if isinstance(result, str) else ""}

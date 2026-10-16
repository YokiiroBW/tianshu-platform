import copy
import unittest
from unittest.mock import AsyncMock

from services.platform.contracts import Fault
from services.platform.media.music.providers import Bridge, track


class RecordedBridge(Bridge):
    def __init__(self, source, rows):
        super().__init__("qq")
        self.source, self.rows = source, rows
        self.calls = []
        self.after = None
        self.next_count = None

    async def call(self, operation, cookie=None, **body):
        self.calls.append(operation)
        if operation == "playlists":
            count = (
                self.next_count
                if self.next_count is not None and self.calls.count(operation) > 1
                else len(self.rows)
            )
            return [{"id": self.source, "name": "Fixture playlist", "track_count": count}]
        if self.source.startswith("profile:dir:"):
            raise Fault("music_upstream_unavailable", 502)
        return copy.deepcopy(
            self.after if self.after is not None and self.calls.count(operation) > 1 else self.rows
        )


class MusicProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_kugou_download_uses_each_quality_hash_without_repeating_sq(self):
        api = Bridge("kugou")
        api.call = AsyncMock(
            return_value={
                "url": "http://fs.example.kugou.com/audio.mp3?key=fixture",
                "level": "observed",
            }
        )
        value = track(
            {
                "id": "a" * 32,
                "extra": {
                    "hash": "a" * 32,
                    "sq_hash": "a" * 32,
                    "hq_hash": "b" * 32,
                    "file_hash": "c" * 32,
                },
            },
            "kugou",
        )
        with self.assertRaises(Fault) as error:
            await api.download_track(value, "hires", {})
        self.assertEqual(error.exception.code, "music_quality_unavailable")
        api.call.assert_not_awaited()
        for level, expected in (
            ("lossless", "a" * 32),
            ("exhigh", "b" * 32),
            ("standard", "c" * 32),
        ):
            offer = await api.download_track(value, level, {})
            song = api.call.call_args.kwargs["song"]
            self.assertEqual(song["id"], expected)
            self.assertEqual(song["extra"]["hash"], expected)
            self.assertNotIn("sq_hash", song["extra"])
            self.assertTrue(offer["url"].startswith("http://"))

    async def test_missing_metadata_requires_shared_hash_and_keeps_playlist_identity(self):
        api = Bridge("kugou")
        original = track(
            {"id": "a" * 32, "name": "Local filename.mp3", "extra": {"hq_hash": "b" * 32}}, "kugou"
        )
        canonical = track(
            {
                "id": "c" * 32,
                "name": "Recording (Extended)",
                "artist": "Artist",
                "album": "Album",
                "duration": 300,
                "extra": {"hq_hash": "b" * 32},
            },
            "kugou",
        )
        api._search = AsyncMock(return_value=[{**canonical, "provider_data": {}}])
        self.assertEqual(await api.enrich(original, {}), original)
        api._search.return_value = [canonical]
        enriched = await api.enrich(original, {"_music_account_revision": 2})
        self.assertEqual(enriched["source_id"], original["source_id"])
        self.assertEqual(enriched["duration_ms"], 300000)
        self.assertEqual(enriched["title"], "Recording (Extended)")
        self.assertTrue(enriched["metadata_enriched"])
        self.assertEqual(await api.enrich(original, {"_music_account_revision": 2}), enriched)
        self.assertEqual(api._search.await_count, 2)

    async def test_missing_song_id_preserves_position_without_blocking_valid_members(self):
        api = RecordedBridge(
            "123",
            [
                {"id": "valid", "name": "Available song", "artist": "Artist", "duration": 180},
                {"id": "", "name": "Unavailable upload", "artist": "Unknown", "duration": 0},
                {"id": "valid", "name": "Available song", "artist": "Artist", "duration": 180},
            ],
        )
        first = await api.snapshot("123", "user", {})
        second = await api.snapshot("123", "user", {})
        self.assertEqual(first["entries"], second["entries"])
        self.assertEqual(first["entries"][::2], ["valid", "valid"])
        missing = next(t for t in first["tracks"] if t["source_id"] == first["entries"][1])
        self.assertEqual(missing["title"], "Unavailable upload")
        self.assertEqual(missing["availability"], "unavailable")
        self.assertEqual(len(first["tracks"]), 2)

    async def test_missing_member_changes_during_snapshot_are_rejected(self):
        api = RecordedBridge("123", [{"id": "", "name": "First unavailable song"}])
        api.after = [{"id": "", "name": "Different unavailable song"}]
        with self.assertRaises(Fault) as caught:
            await api.snapshot("123", "user", {})
        self.assertEqual(caught.exception.code, "music_source_changed")

    async def test_empty_qq_system_lists_use_reconfirmed_catalog_count(self):
        for source in ("profile:dir:205", "profile:dir:206"):
            with self.subTest(source=source):
                api = RecordedBridge(source, [])
                result = await api.snapshot(source, "user", {})
                self.assertEqual(result["entries"], [])
                self.assertEqual(result["tracks"], [])
                self.assertEqual(api.calls, ["playlists", "playlists"])

    async def test_empty_qq_system_list_becoming_nonempty_is_not_installed_as_empty(self):
        api = RecordedBridge("profile:dir:205", [])
        api.next_count = 1
        with self.assertRaises(Fault) as caught:
            await api.snapshot(api.source, "user", {})
        self.assertEqual(caught.exception.code, "music_source_changed")


if __name__ == "__main__":
    unittest.main()

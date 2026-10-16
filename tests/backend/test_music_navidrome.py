import asyncio
import copy
import hashlib
import json
import threading
import unittest
import uuid
from unittest.mock import patch

from aiohttp import web

from music_fixtures import snapshot
import test_music_foundation as foundation
from services.platform.contracts import Fault
from services.platform.media.music.navidrome.matching import mapped_members
from services.platform.media.music.navidrome.state import LINK, mark_dirty
from services.platform.media.organizer import library_lock


class SyntheticNavidrome:
    """Real loopback HTTP with official response and full-replacement semantics."""

    def __init__(self):
        self.password = "synthetic-nav-password"
        self.playlists = {}
        self.songs = []
        self.calls = []
        self.creates = 0
        self.drop_create = False
        self.reject_create = False
        self.readonly = False
        self.long_scan = False
        self.scanning = False
        self.bad_catalog = False

    async def start(self):
        app = web.Application()
        app.router.add_post("/rest/{method}.view", self.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])

    async def close(self):
        await self.runner.cleanup()

    async def handle(self, request):
        body = await request.post()
        assert not request.query
        method = request.match_info["method"]
        self.calls.append((method, dict(body)))
        expected = hashlib.md5((self.password + body.get("s", "")).encode()).hexdigest()
        result = {
            "status": "ok",
            "version": "1.16.1",
            "type": "navidrome",
            "serverVersion": "synthetic",
        }
        if body.get("t") != expected or body.get("u") != "synthetic":
            result.update(status="failed", error={"code": 40})
        elif method == "getMusicFolders":
            result["musicFolders"] = {"musicFolder": [{"id": 1, "name": "隔离音乐库"}]}
        elif method == "getUser":
            result["user"] = {"username": "synthetic", "adminRole": False}
        elif method == "search3":
            offset, count = int(body["songOffset"]), int(body["songCount"])
            result["searchResult3"] = {"song": self.songs[offset : offset + count]}
            if self.bad_catalog:
                result["searchResult3"] = None
        elif method == "getPlaylists":
            result["playlists"] = {"playlist": list(self.playlists.values())}
        elif method == "getPlaylist":
            if body["id"] not in self.playlists:
                result.update(status="failed", error={"code": 70})
            else:
                result["playlist"] = self.playlists[body["id"]]
        elif method == "createPlaylist":
            identity = body.get("playlistId")
            if not identity:
                self.creates += 1
                if self.reject_create:
                    return web.Response(status=503)
                identity = str(uuid.uuid4())
                self.playlists[identity] = {
                    "id": identity,
                    "name": body["name"],
                    "owner": "synthetic",
                    "public": False,
                    "readonly": self.readonly,
                }
            target = self.playlists[identity]
            # Observed Navidrome 0.64.2 semantics: empty replacement is a no-op.
            if not body.get("playlistId") or "songId" in body:
                target["entry"] = [{"id": i} for i in body.getall("songId", [])]
            if self.drop_create and not body.get("playlistId"):
                return web.Response(status=503)
            result["playlist"] = target
        elif method == "updatePlaylist":
            target = self.playlists[body["playlistId"]]
            positions = {int(i) for i in body.getall("songIndexToRemove", [])}
            target["entry"] = [
                row for i, row in enumerate(target.get("entry", [])) if i not in positions
            ]
            for key in ("name", "comment", "public"):
                if key in body:
                    target[key] = body[key] == "true" if key == "public" else body[key]
        elif method == "deletePlaylist":
            self.playlists.pop(body["id"], None)
        elif method in {"getScanStatus", "startScan"}:
            if method == "startScan" and self.long_scan:
                self.scanning = True
            result["scanStatus"] = {"scanning": self.scanning}
        elif method != "ping":
            raise AssertionError(method)
        return web.json_response({"subsonic-response": copy.deepcopy(result)})


class NavidromeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUpFoundation = foundation.MusicFoundationTests.asyncSetUp
    subscribe = foundation.MusicFoundationTests.subscribe

    async def asyncSetUp(self):
        await self.asyncSetUpFoundation()
        self.server = SyntheticNavidrome()
        await self.server.start()
        self.addAsyncCleanup(self.server.close)
        self.nav = self.music.navidrome
        self.directory = self.root / "music"
        self.directory.mkdir()
        self.repo.put(
            "music_settings",
            "library",
            {"directory": str(self.directory), "layout": "artist_album"},
        )
        self.playlist = self.subscribe()
        self.connection_body = {
            "base_url": self.server.url,
            "username": "synthetic",
            "password": self.server.password,
            "music_folder_id": "1",
            "music_directory": "/music/音乐存档",
            "expected_revision": 0,
        }
        connection = self.nav.state.connection(self.connection_body)
        checked = await self.nav.client.check(connection)
        with self.repo.transaction() as db:
            self.nav.state.save(self.connection_body, connection, checked, db)
        self.archive("101")
        self.archive("102")

    def archive(self, source):
        track = self.repo.get("music_track", self.account["account_id"] + ":track:" + source)
        relative = "合成艺术家/合成专辑/" + source + ".flac"
        path = self.directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("synthetic-" + source).encode())
        audio = {
            "kind": "audio",
            "path": relative,
            "size_bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        with self.repo.transaction() as db:
            self.library.catalog.store_file(
                track["recording_id"],
                {
                    "directory": str(self.directory),
                    "files": [audio],
                    "quality": {"duration_seconds": 10.5, "level": "lossless"},
                },
                db,
            )
        self.server.songs.append(
            {
                "id": source,
                "title": track["title"],
                "album": track["album"],
                "artist": "合成艺术家",
                "duration": 10,
                "size": audio["size_bytes"],
                "suffix": "flac",
                "path": "/music/音乐存档/" + relative,
            }
        )

    def link(self, **changes):
        with self.repo.transaction() as db:
            result = self.nav.state.link(
                {
                    "playlist_id": self.playlist["playlist_id"],
                    "name": "",
                    "public": False,
                    "auto_sync": True,
                    "expected_revision": 0,
                    **changes,
                },
                db,
            )
        self.link_id = result["playlist"]["link_id"]
        return self.repo.get(LINK, self.link_id)

    def control(self, action):
        row = self.repo.get(LINK, self.link_id)
        with self.repo.transaction() as db:
            return self.nav.state.control(
                {"link_id": self.link_id, "action": action, "expected_revision": row["revision"]},
                db,
            )

    async def test_sync_waits_for_in_process_archive_instead_of_failing_busy(self):
        self.music.downloads.storage.export(self.playlist["playlist_id"])
        self.media.local = asyncio.to_thread
        self.link()
        entered, released = threading.Event(), threading.Event()

        def publishing():
            with library_lock(self.directory):
                entered.set()
                released.wait(3)

        writer = asyncio.create_task(self.music.downloads.exclusive(publishing))
        sync = None
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            sync = asyncio.create_task(self.nav.tick())
            await asyncio.sleep(0.15)
            self.assertFalse(sync.done(), "Sync must wait for the active archive owner")
        finally:
            released.set()
            await asyncio.gather(writer, *([sync] if sync else []))
        self.assertEqual(self.repo.get(LINK, self.link_id)["status"], "partial")

    async def test_order_duplicates_updates_and_no_audio_copies(self):
        self.link()
        files = {str(p): p.read_bytes() for p in self.directory.rglob("*.flac")}
        self.server.playlists["unrelated"] = {
            "id": "unrelated",
            "name": "用户已有歌单",
            "owner": "synthetic",
            "entry": [{"id": "102"}],
        }
        await self.nav.tick()
        row = self.repo.get(LINK, self.link_id)
        self.assertEqual(row["status"], "partial")
        self.assertEqual(
            [r["id"] for r in self.server.playlists[row["remote_id"]]["entry"]],
            ["101", "102", "101"],
        )
        self.assertEqual(row["members"][-1]["code"], "not_downloaded")
        changed = snapshot(["102", "101", "102"])
        changed["playlist"]["name"] = "更新后的歌单"
        current = self.repo.get("music_playlist", self.playlist["playlist_id"])
        with self.repo.transaction() as db:
            self.library.install_snapshot(
                self.account, current["playlist_id"], changed, current, db
            )
        await self.nav.tick()
        updated = self.repo.get(LINK, self.link_id)
        remote = self.server.playlists[updated["remote_id"]]
        self.assertEqual(updated["status"], "synced")
        self.assertEqual(updated["remote_id"], row["remote_id"])
        self.assertEqual(remote["name"], "更新后的歌单")
        self.assertEqual([r["id"] for r in remote["entry"]], ["102", "101", "102"])
        writes = sum(m in {"createPlaylist", "updatePlaylist"} for m, _ in self.server.calls)
        self.control("sync")
        await self.nav.tick()
        self.assertEqual(
            sum(m in {"createPlaylist", "updatePlaylist"} for m, _ in self.server.calls), writes
        )
        self.assertEqual(self.server.creates, 1)
        self.assertEqual(self.server.playlists["unrelated"]["entry"], [{"id": "102"}])
        self.assertEqual({str(p): p.read_bytes() for p in self.directory.rglob("*.flac")}, files)
        self.assertEqual(self.repo.job_count(), 0)

    async def test_unknown_creation_recovers_original_after_restart(self):
        self.link()
        self.server.drop_create = True
        await self.nav.tick()
        self.assertEqual(self.repo.get(LINK, self.link_id)["code"], "navidrome_unavailable")
        self.server.drop_create = False
        self.control("sync")
        from services.platform.media.music.navidrome import Navidrome

        restored = Navidrome(self.music)
        self.addAsyncCleanup(restored.close)
        await restored.tick()
        self.assertEqual(self.repo.get(LINK, self.link_id)["status"], "partial")
        self.assertEqual(self.server.creates, 1)

    async def test_unknown_creation_without_observation_is_not_resent(self):
        self.link()
        self.server.reject_create = True
        await self.nav.tick()
        self.server.reject_create = False
        self.control("sync")
        await self.nav.tick()
        self.assertEqual(self.repo.get(LINK, self.link_id)["code"], "navidrome_creation_unverified")
        self.assertEqual(self.server.creates, 1)

    async def test_mapping_uses_path_or_unique_metadata_and_size_not_title(self):
        _, tracks = self.library.export_snapshot(self.playlist["playlist_id"])
        connection = self.nav.state.connection()
        songs = copy.deepcopy(self.server.songs)
        songs[0]["path"] = "synthetic/album/song.flac"
        members = mapped_members(tracks, songs, connection)
        self.assertEqual(members[0]["method"], "metadata_size")
        songs.append({**songs[0], "id": "another-copy"})
        self.assertEqual(mapped_members(tracks, songs, connection)[0]["code"], "ambiguous")
        songs = [{**self.server.songs[0], "path": "/different/file.flac"}]
        self.assertEqual(mapped_members(tracks, songs, connection)[0]["code"], "waiting_index")
        songs = [{**self.server.songs[0], "size": 2}]
        self.assertEqual(mapped_members(tracks, songs, connection)[0]["code"], "waiting_index")

    async def test_managed_archive_unknown_tags_match_exact_size_duration_and_unique_recording(
        self,
    ):
        _, tracks = self.library.export_snapshot(self.playlist["playlist_id"])
        track = {
            **tracks[0],
            "artists": [],
            "album": "",
            "artifact": {**tracks[0]["artifact"], "managed": True},
        }
        song = {
            **self.server.songs[0],
            "path": "未知艺术家/未知专辑/song.flac",
            "artist": "未知艺术家",
            "album": "未知专辑",
        }
        connection = self.nav.state.connection()
        self.assertEqual(mapped_members([track], [song], connection)[0]["code"], "matched")
        self.assertEqual(
            mapped_members([track], [song, {**song, "id": "duplicate"}], connection)[0]["code"],
            "ambiguous",
        )
        self.assertEqual(
            mapped_members([track], [{**song, "artist": "Different Artist"}], connection)[0][
                "code"
            ],
            "waiting_index",
        )
        unmanaged = {**track, "artifact": {**track["artifact"], "managed": False}}
        self.assertEqual(
            mapped_members([unmanaged], [song], connection)[0]["code"], "waiting_index"
        )

    async def test_later_index_and_archive_notification_complete_members(self):
        saved = self.server.songs.pop()
        self.link()
        await self.nav.tick()
        self.assertEqual(self.repo.get(LINK, self.link_id)["synced_count"], 2)
        self.server.songs.append(saved)
        with self.repo.transaction() as db:
            mark_dirty(self.repo, db)
        await self.nav.tick()
        self.assertEqual(self.repo.get(LINK, self.link_id)["synced_count"], 3)

    async def test_pause_during_remote_read_keeps_created_id_and_blocks_update(self):
        self.link()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.nav.client.songs

        async def waiting(*args):
            entered.set()
            await release.wait()
            return await original(*args)

        with patch.object(self.nav.client, "songs", waiting):
            task = asyncio.create_task(self.nav.tick())
            await asyncio.wait_for(entered.wait(), 5)
            self.control("pause")
            release.set()
            await task
        row = self.repo.get(LINK, self.link_id)
        self.assertEqual(row["status"], "paused")
        self.assertTrue(row["remote_id"])
        self.assertEqual(self.server.playlists[row["remote_id"]]["entry"], [])
        self.control("resume")
        await self.nav.tick()
        self.assertEqual(self.server.creates, 1)

    async def test_delete_recreate_and_external_deletion_are_explicit(self):
        self.link()
        await self.nav.tick()
        identity = self.repo.get(LINK, self.link_id)["remote_id"]
        self.server.playlists.pop(identity)
        self.control("sync")
        await self.nav.tick()
        self.assertEqual(self.repo.get(LINK, self.link_id)["code"], "navidrome_not_found")
        self.assertEqual(self.server.creates, 1)
        self.control("recreate")
        await self.nav.tick()
        self.assertEqual(self.server.creates, 2)
        self.control("delete")
        await self.nav.tick()
        self.assertEqual(self.repo.get(LINK, self.link_id)["status"], "deleted")
        self.assertEqual(len(list(self.directory.rglob("*.flac"))), 2)
        self.link()
        await self.nav.tick()
        self.assertEqual(self.server.creates, 3)

    async def test_saved_credentials_are_encrypted_and_bad_login_is_reported(self):
        self.assertNotIn(self.server.password, json.dumps(self.nav.state.view()))
        self.assertNotIn(self.server.password.encode(), self.repo.database.read_bytes())
        blank = {**self.connection_body, "password": ""}
        self.assertEqual(self.nav.state.connection(blank)["password"], self.server.password)
        with self.assertRaises(Fault):
            self.nav.state.connection({**blank, "base_url": "http://127.0.0.1:1"})
        with self.assertRaises(Fault) as failure:
            await self.nav.client.check({**self.nav.state.connection(), "password": "wrong"})
        self.assertEqual(failure.exception.code, "navidrome_auth_required")

    async def test_navidrome_alone_prevents_silent_credential_key_replacement(self):
        from services.platform.media.accounts import Accounts

        with self.repo.transaction() as db:
            db.execute("DELETE FROM documents WHERE kind='music_account'")
        key = self.repo.database.with_name(self.repo.database.name + ".key")
        key.unlink()
        with self.assertRaises(Fault) as failure:
            Accounts(self.repo)
        self.assertEqual(failure.exception.code, "media_credentials_unavailable")
        self.assertFalse(key.exists())

    async def test_lease_prevents_second_worker_and_directory_change_stops_writes(self):
        self.link()
        connection = self.nav.state.saved()
        key = "navidrome:" + connection["connection_id"]
        generation = self.repo.scan_claim(key, "other-worker")
        await self.nav.tick()
        self.assertEqual(self.server.creates, 0)
        self.repo.scan_release(key, "other-worker", generation)
        self.repo.put("music_settings", "library", {"directory": str(self.root / "other")})
        await self.nav.tick()
        self.assertEqual(self.server.creates, 0)
        self.assertEqual(self.repo.get(LINK, self.link_id)["code"], "navidrome_mapping_changed")

    async def test_long_scan_is_observed_on_retry_instead_of_started_again(self):
        from services.platform.media.music.navidrome.state import CONNECTION

        connection = self.nav.state.saved()
        self.repo.put(CONNECTION, "server", {**connection, "can_scan": True})
        self.server.long_scan = True
        self.link()
        await self.nav.tick()
        self.assertEqual(self.repo.get(LINK, self.link_id)["code"], "navidrome_scanning")
        self.server.scanning = False
        self.control("sync")
        await self.nav.tick()
        self.assertEqual(self.repo.get(LINK, self.link_id)["status"], "partial")
        self.assertEqual(sum(m == "startScan" for m, _ in self.server.calls), 1)

    async def test_malformed_catalog_preserves_last_remote_members(self):
        self.link()
        await self.nav.tick()
        before = copy.deepcopy(self.server.playlists)
        self.server.bad_catalog = True
        self.control("sync")
        await self.nav.tick()
        self.assertEqual(self.repo.get(LINK, self.link_id)["code"], "navidrome_invalid_response")
        self.assertEqual(self.server.playlists, before)

    async def test_managed_exports_are_excluded_without_ignoring_shared_audio(self):
        from services.platform.contracts import digest
        from services.platform.media.music.navidrome.exports import prepare_exports

        folder = "歌单/" + digest(self.playlist["playlist_id"])[:20]
        directory = self.directory / folder
        directory.mkdir(parents=True)
        playlist_file = directory / "playlist.m3u8"
        playlist_file.write_text("#EXTM3U\n../../合成艺术家/合成专辑/101.flac\n", encoding="utf-8")
        before = playlist_file.read_bytes()
        ignore = directory / ".ndignore"
        ignore.write_text("# User rule\n*.tmp\n", encoding="utf-8")
        original_ignore = ignore.read_bytes()
        self.repo.put(
            "music_export",
            "fixture-export",
            {
                "directory": str(self.directory),
                "relative": folder,
                "playlist_id": self.playlist["playlist_id"],
            },
        )
        prepare_exports(self.repo, self.nav.state.connection())
        first = ignore.read_bytes()
        prepare_exports(self.repo, self.nav.state.connection())
        self.assertEqual(ignore.read_bytes(), first)
        self.assertTrue(first.startswith(original_ignore))
        self.assertTrue(first.endswith(b"playlist.m3u8\n"))
        self.assertEqual(playlist_file.read_bytes(), before)
        self.assertFalse((self.directory / ".ndignore").exists())
        self.assertEqual(len(list(self.directory.rglob("*.flac"))), 2)

    async def test_source_becomes_empty_removes_all_remote_positions(self):
        self.link()
        await self.nav.tick()
        current = self.repo.get("music_playlist", self.playlist["playlist_id"])
        with self.repo.transaction() as db:
            self.library.install_snapshot(
                self.account, current["playlist_id"], snapshot([]), current, db
            )
        await self.nav.tick()
        row = self.repo.get(LINK, self.link_id)
        self.assertEqual(row["status"], "synced")
        self.assertEqual(row["total"], 0)
        self.assertEqual(self.server.playlists[row["remote_id"]]["entry"], [])
        self.assertEqual(len(list(self.directory.rglob("*.flac"))), 2)

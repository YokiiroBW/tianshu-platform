"""Real console and internal HTTP checks with isolated synthetic identities and speech."""

import base64
import copy
from pathlib import Path
import time
import unittest
import uuid

import aiohttp

import test_web_console
from book_audio_fixtures import AudioSite, ffmpeg_path
from fixtures import bearer
from services.platform.contracts import canonical


class BookReadingTests(unittest.IsolatedAsyncioTestCase):
    get = test_web_console.WebConsoleTests.get
    post = test_web_console.WebConsoleTests.post
    login = test_web_console.WebConsoleTests.login

    async def asyncSetUp(self):
        await test_web_console.WebConsoleTests.asyncSetUp(self)
        self.books = self.platform.media.books
        self.platform.auth.principals["companion_resolver"]["resolver"]["caller"] = "platform"
        self.service_client = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())
        self.addAsyncCleanup(self.service_client.close)
        account = self.config["principals"]["admin"]["account"]
        channel = self.config["entries"]["actor-a"]["channel"]
        # Explicit fixture ownership mapping; never a production or user database.
        with self.platform.store.connect(write=True) as db:
            db.execute(
                "INSERT OR REPLACE INTO identities VALUES(?,?,?)",
                (canonical(account), "person:reader", 1),
            )
            db.execute(
                "INSERT OR REPLACE INTO channels VALUES(?,?)",
                (canonical(channel), "conversation:reader"),
            )
        await self.login()
        self.csrf = (await self.get())[1]["csrf"]
        root = Path(self.temp.name) / "ebooks"
        root.mkdir()
        await self.book_call(
            "settings/save",
            {
                "directory": str(root),
                "expected_revision": 0,
                "auto_metadata": False,
                "open_library": False,
                "interval_seconds": 3600,
            },
        )
        result = await self.book_call(
            "library/import",
            {
                "filename": "合成阅读.txt",
                "content_base64": base64.b64encode(
                    (
                        "第一章 清晨\n"
                        + "这是一段合成小说正文，不是用户经历。\n" * 8
                        + "第二章 夜晚\n未来章节不会共享。\n"
                    ).encode()
                ).decode(),
            },
        )
        self.book_id = result["book"]["book_id"]
        self.chapter = self.books.catalog.directory(self.book_id)[0]
        self.content = self.books.catalog.chapter(self.book_id, self.chapter["chapter_id"])

    async def book_call(self, operation, body=None, expected=200):
        return await self.post(
            "media/books/" + operation,
            {**(body or {}), "client_id": str(uuid.uuid4())},
            self.csrf,
            expected=expected,
        )

    async def opening(self):
        view = await self.book_call("reading/view", {"book_id": self.book_id})
        return (
            await self.book_call(
                "reading/open",
                {
                    "book_id": self.book_id,
                    "chapter_id": self.chapter["chapter_id"],
                    "content_hash": self.content["content_hash"],
                    "expected_revision": (view["reader"] or {}).get("revision", 0),
                },
            )
        )["reader"]

    async def progress(self, reader, sequence=1, sharing=True, offset=20, expected=200):
        return await self.book_call(
            "reading/progress",
            {
                "reader_id": reader["reader_id"],
                "sequence": sequence,
                "offset": offset,
                "seconds": 1.2,
                "sharing": sharing,
                "conversation": "input-entry",
                "actor": "actor:a",
            },
            expected=expected,
        )

    async def context(self, actor="actor-a", grant=None, change=None, expected=200):
        origin = self.platform.origins.issue(bearer("ADMIN"), actor)
        with self.platform.store.connect() as db:
            scope = self.platform.origins.scope(db, self.platform.auth.entries[actor])
        body = {
            "schema_version": 1,
            "query": {
                "schema_version": 1,
                "request_id": "read:" + uuid.uuid4().hex,
                "origin": {"assertion_ref": origin["assertion_ref"]},
            },
            "scope": scope,
            "grant_id": grant,
        }
        body.update(change or {})
        async with self.service_client.post(
            self.url + "/internal/v1/books/reading",
            json=body,
            headers={"Authorization": bearer("COMPANION_RESOLVER")},
        ) as response:
            data = await response.json()
            self.assertEqual(response.status, expected, data)
            return data

    async def test_catalog_available_without_reading_and_grant_closes(self):
        before = await self.context()
        self.assertEqual(before["catalog_total"], 1)
        self.assertIsNone(before["active"])
        reader = await self.opening()
        self.assertIsNone((await self.context())["active"])
        await self.progress(reader)
        during = await self.context()
        self.assertEqual(during["active"]["excerpt"], self.content["content"][:20])
        self.assertNotIn("未来章节", during["active"]["excerpt"])
        self.assertTrue((await self.context(grant=during["active"]["grant_id"]))["grant_current"])
        await self.book_call("reading/close", {"reader_id": reader["reader_id"]})
        after = await self.context(grant=during["active"]["grant_id"])
        self.assertIsNone(after["active"])
        self.assertFalse(after["grant_current"])
        self.assertEqual(after["catalog_total"], 1)

    async def test_stale_tab_cannot_replace_position_or_reopen_closed_grant(self):
        first = await self.opening()
        await self.progress(first, sequence=2, offset=50)
        await self.progress(first, sequence=1, offset=10, expected=409)
        second = await self.opening()
        self.assertEqual(second["offset"], 50)
        await self.progress(first, sequence=3, expected=409)
        await self.book_call("reading/close", {"reader_id": first["reader_id"]})
        await self.progress(second)
        self.assertIsNotNone((await self.context())["active"])
        await self.book_call("reading/close", {"reader_id": second["reader_id"]})
        await self.progress(second, sequence=3, expected=409)

    async def test_expiry_logout_and_another_actor_never_get_current_text(self):
        reader = await self.opening()
        await self.progress(reader)
        self.assertIsNone((await self.context("actor-b"))["active"])
        now = time.time()
        self.books.repo.clock = lambda: now + 80
        self.assertIsNone((await self.context())["active"])
        self.books.repo.clock = time.time
        await self.post("logout", {}, self.csrf)
        self.assertIsNone((await self.context())["active"])

    async def test_other_person_group_and_forged_scope_cannot_read_private_catalog(self):
        entry = self.platform.auth.entries["actor-b"]
        entry["account"] = {"namespace": "web", "immutable_account_id": "other-person"}
        with self.platform.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO identities VALUES(?,?,?)",
                (canonical(entry["account"]), "person:other", 1),
            )
        self.assertEqual((await self.context("actor-b"))["catalog"], [])
        entry["audience"] = "group"
        self.assertEqual((await self.context("actor-b"))["catalog"], [])
        scope = copy.deepcopy((await self.context())["scope"])
        scope["person_id"] = "person:forged"
        await self.context(change={"scope": scope}, expected=403)

    async def test_audio_http_range_requires_live_console_session(self):
        speech = await AudioSite(Path(self.temp.name)).start()
        self.addAsyncCleanup(speech.runner.cleanup)
        self.books.audio.files.ffmpeg = ffmpeg_path()
        await self.book_call(
            "audio/settings/save",
            {
                "provider": "local",
                "endpoint": speech.url,
                "model": "fixture",
                "voice": "tone",
                "expected_revision": 0,
            },
        )
        body = {
            "book_id": self.book_id,
            "chapter_ids": [self.chapter["chapter_id"]],
            "provider": "local",
        }
        estimate = await self.book_call("audio/estimate", body)
        await self.book_call("audio/generate", {**body, "profile_id": estimate["profile_id"]})
        await self.books.audio.run_once()
        job = self.books.repo.list("book_audio_job")[0]
        self.assertEqual(job["state"], "ready", job["code"])
        url = self.url + (await self.book_call("audio/manifest", {"job_id": job["id"]}))["url"]
        async with self.client.get(url, headers={"Range": "bytes=0-99"}) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(len(await response.read()), 100)
        async with aiohttp.ClientSession() as anonymous:
            async with anonymous.get(url) as response:
                self.assertEqual(response.status, 401)
        await self.post("logout", {}, self.csrf)
        async with self.client.get(url) as response:
            self.assertEqual(response.status, 401)


if __name__ == "__main__":
    unittest.main()

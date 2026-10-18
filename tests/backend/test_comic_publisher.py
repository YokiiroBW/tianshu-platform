import asyncio
import hashlib
import os
import socket
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import aiohttp
from aiohttp.test_utils import TestServer

from services.platform.contracts import Fault
from services.platform.media.comics.publisher import CHUNK_BYTES, PREFIX, PublisherStore
from services.platform.media.comics.publisher_client import Publisher
from services.platform.media.comics.publisher_service import application


class FixtureResolver(aiohttp.abc.AbstractResolver):
    def __init__(self, port):
        self.port = port

    async def resolve(self, host, port=0, family=socket.AF_INET):
        return [
            {
                "hostname": host,
                "host": "127.0.0.1",
                "port": self.port,
                "family": socket.AF_INET,
                "proto": 0,
                "flags": 0,
            }
        ]

    async def close(self):
        pass


class PublisherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.socket_path = str(self.root / "comics.sock")
        self.output = self.root / "output"
        self.store = PublisherStore({"comic-main": str(self.output)})
        self.key = str(uuid.uuid4())
        self.data = b"first-chunk-" * (CHUNK_BYTES // 12 + 1) + b"remaining-bytes"
        self.source = self.root / "package.cbz"
        self.source.write_bytes(self.data)
        self.intent = {
            "target_id": "comic-main",
            "relative_path": "work/version/chapter.cbz",
            "length": str(len(self.data)),
            "sha256": hashlib.sha256(self.data).hexdigest(),
        }
        self.job = {
            "comic_target": {
                "asset_target_id": "comic-main",
                "publisher_socket": self.socket_path,
            },
            "comic_relative_path": self.intent["relative_path"],
            "comic_artifact": {
                "size_bytes": len(self.data),
                "sha256": self.intent["sha256"],
                "archive_key": self.key,
            },
        }
        # Windows covers the real HTTP protocol and client. Only Linux tests below exercise SO_PEERCRED.
        peer = patch(
            "services.platform.media.comics.publisher_service.peer_uid", return_value=10001
        )
        self.peer = peer.start()
        self.addCleanup(peer.stop)
        self.server = TestServer(application(self.store, 10001))
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        connector = aiohttp.TCPConnector(resolver=FixtureResolver(self.server.port))
        transport = patch(
            "services.platform.media.comics.publisher_client.aiohttp.UnixConnector",
            return_value=connector,
        )
        transport.start()
        self.addCleanup(transport.stop)
        self.client = Publisher()
        self.addAsyncCleanup(self.client.close)

    async def checkpoint(self):
        return None

    def prepare(self):
        return self.store.prepare("comic-main", self.key, self.intent)

    def append(self, offset, data):
        return self.store.append(
            "comic-main", self.key, offset, data, hashlib.sha256(data).hexdigest()
        )

    async def test_restart_recovers_durable_offset_and_unacknowledged_tail(self):
        self.prepare()
        self.append(0, self.data[:CHUNK_BYTES])
        partial = self.output / ".tianshu-transfer" / (self.key + ".part")
        with partial.open("ab") as stream:
            stream.write(b"a process died before journaling these bytes")
        result = await self.client.publish(self.job, self.source, self.checkpoint)
        final = self.output / self.intent["relative_path"]
        self.assertEqual(final.read_bytes(), self.data)
        self.assertEqual(final.stat().st_nlink, 1)
        self.assertFalse(partial.exists())
        self.assertFalse(result["reused"])
        original = final.stat()
        replay = await self.client.publish(self.job, self.source, self.checkpoint)
        self.assertEqual(result, replay)
        self.assertEqual(final.stat().st_ino, original.st_ino)
        self.assertEqual(self.source.read_bytes(), self.data)

    async def test_lost_chunk_and_completion_replies_keep_one_file(self):
        self.prepare()
        self.append(0, self.data[:CHUNK_BYTES])
        replay = self.append(0, self.data[:CHUNK_BYTES])
        self.assertEqual(replay["offset"], CHUNK_BYTES)
        self.append(CHUNK_BYTES, self.data[CHUNK_BYTES:])
        with patch.object(self.store, "save", side_effect=OSError("synthetic lost journal write")):
            with self.assertRaises(OSError):
                self.store.complete("comic-main", self.key)
        final = self.output / self.intent["relative_path"]
        original = final.stat()
        result = await self.client.publish(self.job, self.source, self.checkpoint)
        self.assertTrue(result["reused"])
        self.assertEqual(final.stat().st_ino, original.st_ino)
        self.assertEqual(final.stat().st_nlink, 1)
        self.assertEqual(self.store.get("comic-main", self.key)["state"], "published")

    async def test_changed_intent_bad_chunk_and_existing_file_are_preserved(self):
        self.prepare()
        with self.assertRaisesRegex(Fault, "comic_publisher_conflict"):
            self.store.prepare("comic-main", self.key, {**self.intent, "length": "1"})
        with self.assertRaisesRegex(Fault, "comic_publisher_chunk_mismatch"):
            self.store.append("comic-main", self.key, 0, b"wrong", "0" * 64)
        self.assertEqual(self.store.get("comic-main", self.key)["offset"], 0)
        with self.assertRaisesRegex(Fault, "comic_publisher_incomplete"):
            self.store.complete("comic-main", self.key)
        final = self.output / self.intent["relative_path"]
        final.parent.mkdir(parents=True)
        final.write_bytes(b"existing user data")
        with self.assertRaisesRegex(Fault, "comic_file_conflict"):
            await self.client.publish(self.job, self.source, self.checkpoint)
        self.assertEqual(final.read_bytes(), b"existing user data")

    async def test_uid_rejection_and_path_boundaries_do_not_publish(self):
        self.peer.return_value = 10002
        with self.assertRaisesRegex(Fault, "comic_publisher_forbidden"):
            await self.client.publish(self.job, self.source, self.checkpoint)
        self.assertFalse(self.output.exists())
        for relative in (
            "../version/chapter.cbz",
            ".tianshu-transfer/v/chapter.cbz",
            "a/b/../c.cbz",
            "a/b/%2f.cbz",
            "a/b/c\\d.cbz",
            "a/CON/c.cbz",
            "a/b/chapter.txt",
        ):
            with self.subTest(relative=relative), self.assertRaises(Fault):
                self.store.prepare(
                    "comic-main", self.key, {**self.intent, "relative_path": relative}
                )
        with self.assertRaisesRegex(Fault, "comic_publisher_target_missing"):
            self.store.prepare("comic-adult", self.key, {**self.intent, "target_id": "comic-adult"})

    async def test_cancelled_transfer_resumes_without_publishing_partial_file(self):
        checkpoints = 0

        async def cancel_after_first_chunk():
            nonlocal checkpoints
            checkpoints += 1
            if checkpoints == 5:
                raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await self.client.publish(self.job, self.source, cancel_after_first_chunk)
        self.assertEqual(self.store.get("comic-main", self.key)["offset"], CHUNK_BYTES)
        self.assertFalse((self.output / self.intent["relative_path"]).exists())
        await self.client.publish(self.job, self.source, self.checkpoint)
        self.assertEqual((self.output / self.intent["relative_path"]).read_bytes(), self.data)

    async def test_worker_packages_publishes_and_observes_the_same_cbz(self):
        from book_fixtures import LiveConsole
        from comic_fixtures import ComicSite, page_bytes
        from services.platform.media.runtime import Media
        from services.platform.media.web import WebMedia

        inputs = self.root / "inputs"
        work = inputs / "images"
        work.mkdir(parents=True)
        (work / "1.png").write_bytes(page_bytes(1))
        site = await ComicSite(self.output).start()
        self.addAsyncCleanup(site.runner.cleanup)
        media = Media(
            {"database_path": str(self.root / "state.sqlite")},
            clock=time.time,
            local=asyncio.to_thread,
        )
        self.addAsyncCleanup(media.close)
        media.comics.downloads.publisher = self.client
        web = WebMedia(media, LiveConsole())

        async def call(operation, body):
            return await web.route(
                "/api/web/media/comics/" + operation,
                {**body, "client_id": str(uuid.uuid4())},
                {"live": True},
            )

        await call(
            "settings/save", {**site.settings(inputs), "publisher_socket": self.socket_path}
        )
        saved = media.comics.settings.public()
        await call(
            "settings/save", {"expected_revision": saved["revision"], "interval_seconds": 7200}
        )
        self.assertEqual(media.comics.settings.public()["publisher_socket"], self.socket_path)
        inspected = await call("imports/inspect", {"relative_path": "images"})
        await call(
            "imports/enqueue",
            {
                "relative_path": "images",
                "input_digest": inspected["input_digest"],
                "library_kind": "general",
            },
        )
        job = media.repo.claim("publisher-worker", kinds=("comic",))
        await media.worker.run(job, "publisher-worker")
        completed = media.repo.job(job["job_id"])
        self.assertEqual(completed["state"], "completed", completed["code"])
        final = self.output / completed["comic_relative_path"]
        self.assertEqual(
            hashlib.sha256(final.read_bytes()).hexdigest(), completed["asset_receipt"]["sha256"]
        )
        self.assertEqual(completed["library_results"][0]["state"], "verified")
        self.assertEqual(final.stat().st_nlink, 1)
        self.assertTrue(completed["comic_cache_cleaned"])


@unittest.skipUnless(
    os.name == "posix" and hasattr(socket, "SO_PEERCRED"), "Linux kernel peer credentials required"
)
class UnixPublisherTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_unix_peer_uid_and_resumable_client(self):
        from aiohttp import web

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.cbz"
            data = b"linux transfer fixture" * 300000
            source.write_bytes(data)
            store = PublisherStore({"comic-main": str(root / "output")})
            socket_path = str(root / "publisher.sock")
            runner = web.AppRunner(application(store, os.getuid()))
            await runner.setup()
            await web.UnixSite(runner, socket_path).start()
            client = Publisher()
            try:
                job = {
                    "comic_target": {
                        "asset_target_id": "comic-main",
                        "publisher_socket": socket_path,
                    },
                    "comic_relative_path": "work/version/chapter.cbz",
                    "comic_artifact": {
                        "size_bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "archive_key": str(uuid.uuid4()),
                    },
                }
                await client.publish(job, source, lambda: asyncio.sleep(0))
                final = root / "output/work/version/chapter.cbz"
                self.assertEqual(final.read_bytes(), data)
                self.assertEqual(final.stat().st_uid, os.getuid())
                self.assertEqual(final.stat().st_nlink, 1)
                async with aiohttp.ClientSession(
                    connector=aiohttp.UnixConnector(path=socket_path)
                ) as session:
                    response = await session.get("http://comic-publisher" + PREFIX + "/health")
                    self.assertEqual(response.status, 200)
            finally:
                await client.close()
                await runner.cleanup()

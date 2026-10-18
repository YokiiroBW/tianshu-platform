import asyncio
import hashlib
import http.client
import tempfile
import time
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestServer

from book_fixtures import LiveConsole
from comic_fixtures import ComicSite, page_bytes
from services.platform.contracts import Fault
from services.platform.media.comics.source_client import RemoteInputs
from services.platform.media.comics.source_service import application
from services.platform.media.runtime import Media
from services.platform.media.web import WebMedia


class SourceServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inputs, self.output = self.root / "inputs", self.root / "output"
        self.work = self.inputs / "image work"
        self.work.mkdir(parents=True)
        for number in (1, 2, 10):
            (self.work / f"{number}.png").write_bytes(page_bytes(number))
        with zipfile.ZipFile(self.inputs / "old.cbz", "w") as archive:
            archive.writestr("1.png", page_bytes(1))
        self.originals = {
            p.relative_to(self.inputs): p.read_bytes()
            for p in self.inputs.rglob("*")
            if p.is_file()
        }
        self.socket_path = str(self.root / "inputs.sock")
        peer = patch("services.platform.media.comics.unix_service.peer_uid", return_value=10001)
        self.peer = peer.start()
        self.addCleanup(peer.stop)
        self.server = TestServer(application(str(self.inputs), 10001))
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        connection = patch(
            "services.platform.media.comics.source_client.UnixConnection",
            side_effect=lambda _: http.client.HTTPConnection(
                "127.0.0.1", self.server.port, timeout=10
            ),
        )
        connection.start()
        self.addCleanup(connection.stop)
        self.reader = RemoteInputs(self.socket_path, str(self.inputs))
        self.site = await ComicSite(self.output).start()
        self.addAsyncCleanup(self.site.runner.cleanup)
        self.media = Media(
            {"database_path": str(self.root / "state.sqlite")},
            clock=time.time,
            local=asyncio.to_thread,
        )
        self.addAsyncCleanup(self.media.close)
        self.web = WebMedia(self.media, LiveConsole())
        await self.call(
            "settings/save", {**self.site.settings(self.inputs), "import_socket": self.socket_path}
        )

    async def call(self, operation, body):
        return await self.web.route(
            "/api/web/media/comics/" + operation,
            {**body, "client_id": str(uuid.uuid4())},
            {"live": True},
        )

    async def test_folder_and_existing_archive_use_remote_bytes_without_changing_originals(self):
        settings = self.media.comics.settings.public()
        await self.call(
            "settings/save", {"expected_revision": settings["revision"], "interval_seconds": 7200}
        )
        self.assertEqual(self.media.comics.settings.public()["import_socket"], self.socket_path)
        listing = await self.call("imports/browse", {})
        self.assertEqual(listing["total"], 2)
        for relative in ("image work", "old.cbz"):
            inspected = await self.call("imports/inspect", {"relative_path": relative})
            await self.call(
                "imports/enqueue",
                {
                    "relative_path": relative,
                    "input_digest": inspected["input_digest"],
                    "library_kind": "general",
                },
            )
            job = self.media.repo.claim("source-service", kinds=("comic",))
            with patch(
                "services.platform.media.comics.imports.copy_page",
                side_effect=AssertionError("must read through source service"),
            ):
                await self.media.worker.run(job, "source-service")
            completed = self.media.repo.job(job["job_id"])
            self.assertEqual(completed["state"], "completed", completed["code"])
            self.assertTrue(completed["comic_cache_cleaned"])
            final = self.output / completed["comic_relative_path"]
            self.assertEqual(
                hashlib.sha256(final.read_bytes()).hexdigest(), completed["asset_receipt"]["sha256"]
            )
            with zipfile.ZipFile(final) as archive:
                expected = (1, 2, 10) if relative == "image work" else (1,)
                for index, number in enumerate(expected, 1):
                    self.assertEqual(archive.read(f"{index:06d}.png"), page_bytes(number))
        self.assertEqual(
            {
                p.relative_to(self.inputs): p.read_bytes()
                for p in self.inputs.rglob("*")
                if p.is_file()
            },
            self.originals,
        )

    async def test_changed_input_refuses_transfer_and_keeps_existing_cache(self):
        inspected = await asyncio.to_thread(self.reader.inspect, "image work")
        chapter = inspected["chapters"][0]
        destination = self.root / "cached.page"
        destination.write_bytes(b"previous verified cache")
        (self.work / "1.png").write_bytes(page_bytes(22))
        with self.assertRaisesRegex(Fault, "comic_input_changed"):
            await asyncio.to_thread(
                self.reader.copy_file, "image work/1.png", chapter["input_files"][0], destination
            )
        self.assertEqual(destination.read_bytes(), b"previous verified cache")
        with self.assertRaisesRegex(Fault, "comic_input_changed"):
            await asyncio.to_thread(self.reader.recheck, chapter)

    async def test_uid_root_and_relative_path_boundaries(self):
        with self.assertRaises(Fault):
            await asyncio.to_thread(self.reader.browse, "../")
        other = RemoteInputs(self.socket_path, str(self.root))
        with self.assertRaisesRegex(Fault, "comic_input_changed"):
            await asyncio.to_thread(other.browse)
        self.peer.return_value = 10002
        with self.assertRaisesRegex(Fault, "comic_input_service_forbidden"):
            await asyncio.to_thread(self.reader.browse)

    async def test_receive_limit_does_not_replace_cache(self):
        inspected = await asyncio.to_thread(self.reader.inspect, "old.cbz")
        destination = self.root / "cache.cbz"
        destination.write_bytes(b"old cache")
        with self.assertRaisesRegex(Fault, "comic_response_too_large"):
            await asyncio.to_thread(
                self.reader.copy_file,
                "old.cbz",
                inspected["chapters"][0]["input_files"][0],
                destination,
                1,
            )
        self.assertEqual(destination.read_bytes(), b"old cache")

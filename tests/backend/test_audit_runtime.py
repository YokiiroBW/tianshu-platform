import asyncio
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import aiohttp
from fixtures import ENV, start_http
from web_fixtures import PASSWORD, web_settings
from services.platform.contracts import Fault
from services.platform.local_work import LocalWork
from services.platform.service import Platform
from services.platform.server import create_app
from services.platform.web_console import WebConsole


class RuntimeFixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        settings = web_settings(self.temp.name)
        self.platform = Platform(settings)
        self.runner, self.url = await start_http(create_app(self.platform))
        settings["web"]["origin"] = self.url
        self.client = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))

    async def asyncTearDown(self):
        await self.client.close()
        await self.runner.cleanup()
        self.platform.close()
        self.env.stop()
        self.temp.cleanup()

    async def login(self, client):
        async with client.get(self.url + "/api/web/session") as response:
            self.assertEqual(200, response.status)
            session = await response.json()
        async with client.post(
            self.url + "/api/web/login",
            json={"username": "synthetic-admin", "password": PASSWORD},
            headers={"Origin": self.url, "X-CSRF-Token": session["csrf"]},
        ) as response:
            self.assertEqual(200, response.status, await response.text())
            return await response.json()

    async def test_database_write_lock_does_not_block_live_probe(self):
        await self.login(self.client)
        lock = sqlite3.connect(
            self.platform.store.path, check_same_thread=False, isolation_level=None
        )
        lock.execute("BEGIN IMMEDIATE")
        release = threading.Event()

        def unlock():
            release.wait(2)
            lock.rollback()
            lock.close()

        thread = threading.Thread(target=unlock)
        thread.start()
        pending = asyncio.create_task(self.client.get(self.url + "/api/web/session"))
        try:
            await asyncio.sleep(0.03)
            async with asyncio.timeout(0.4):
                async with self.client.get(self.url + "/health/live") as response:
                    self.assertEqual(200, response.status)
            self.assertFalse(pending.done(), "session should still be waiting on the held lock")
        finally:
            release.set()
            await asyncio.to_thread(thread.join)
        response = await pending
        self.assertEqual(200, response.status)
        await response.read()

    async def test_anonymous_churn_preserves_login_and_authenticated_session(self):
        await self.login(self.client)
        async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as anonymous:
            for _ in range(160):
                async with anonymous.get(self.url + "/api/web/session") as response:
                    self.assertEqual(200, response.status)
                    await response.read()
        async with self.client.get(self.url + "/api/web/session") as response:
            self.assertTrue((await response.json())["authenticated"])
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as newcomer:
            await self.login(newcomer)

    async def test_cancelled_waiter_retains_worker_capacity(self):
        work = LocalWork(workers=1, capacity=1)
        release = threading.Event()
        entered = threading.Event()

        def slow():
            entered.set()
            release.wait(2)
            return "done"

        task = asyncio.create_task(work.run(slow))
        try:
            await asyncio.to_thread(entered.wait)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            with self.assertRaises(Fault):
                await work.run(lambda: "must not run")
            self.assertEqual(1, work.active)
            release.set()
            for _ in range(100):
                if work.active == 0:
                    break
                await asyncio.sleep(0.005)
            self.assertEqual("ok", await work.run(lambda: "ok"))
        finally:
            release.set()
            work.close()

    async def test_cancelled_queued_mutation_never_runs(self):
        work = LocalWork(workers=1, capacity=2)
        entered, release = threading.Event(), threading.Event()
        mutations = []

        def slow():
            entered.set()
            release.wait(3)

        first = asyncio.create_task(work.run(slow))
        try:
            await asyncio.to_thread(entered.wait)
            queued = asyncio.create_task(work.run(mutations.append, "forbidden-late-write"))
            while work.active < 2:
                await asyncio.sleep(0.005)
            queued.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await queued
            release.set()
            await first
            self.assertEqual([], mutations)
            self.assertEqual(0, work.active)
        finally:
            release.set()
            work.close()

    async def test_queued_model_action_rechecks_session(self):
        console = WebConsole(self.platform)
        _, session = console.issue(True, "synthetic-fingerprint")
        console.sessions.clear()
        calls = []
        with self.assertRaises(Fault) as error:
            await self.platform.local_work.run(
                console.models._guarded, lambda *args: calls.append(1), {}, session
            )
        self.assertEqual("session_expired", error.exception.code)
        self.assertEqual([], calls)

    async def test_anonymous_churn_during_password_verification_preserves_login(self):
        entered, release = threading.Event(), threading.Event()
        original = WebConsole.verify_password

        def held(console, password):
            entered.set()
            release.wait(3)
            return original(console, password)

        with patch.object(WebConsole, "verify_password", held):
            pending = asyncio.create_task(self.login(self.client))
            try:
                async with asyncio.timeout(3):
                    while not entered.is_set():
                        await asyncio.sleep(0.005)
                async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as anonymous:
                    for _ in range(64):
                        async with anonymous.get(self.url + "/api/web/session") as response:
                            self.assertEqual(200, response.status)
                            await response.read()
            finally:
                release.set()
            self.assertTrue((await pending)["authenticated"])

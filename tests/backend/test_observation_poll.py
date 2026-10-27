"""Owned observation batches cannot starve the SDK poll lease or shutdown."""

import asyncio
import importlib
import json
import os
import sys
import threading
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_bot_observation as fixtures

from services.platform.bot_observation import REPLY_SCOPE_BATCH
from services.platform.bot_observation import BotObservation
from services.platform.bot_adapter_catalog import BotAdapterCatalog
from services.platform import backends
from services.platform.contracts import Fault


class ObservationPollTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fixture = fixtures.ObservationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.manager, self.platform = fixture.manager, fixture.platform
        self.event, self.row = fixture.event, fixture.row
        self.addAsyncCleanup(self.manager.close)

    async def sdk_pressure(self, stage):
        companion = os.environ.get("TS_OBSERVATION_COMPANION")
        if not companion:
            self.skipTest("set TS_OBSERVATION_COMPANION for the real isolated SDK")
        with patch.object(sys, "path", [str(Path(companion) / "integrations/shared"), *sys.path]):
            rpc = importlib.import_module("tianshu_adapter_rpc")
        now = [1000.0]

        async def accounts():
            return [{"id": "10001", "platform": "qq", "label": "synthetic"}]

        async def send(*_):
            self.fail("this lease test must never send a real or synthetic reply")

        host = rpc.AdapterService(
            Path(self.platform.store.path).parent / "host.sqlite", "onebot", accounts, send
        )
        self.addCleanup(host.db.close)
        host.instance_id = self.row["instance_id"]
        row = dict(
            self.row,
            group_policy=fixtures.policy(mode="blacklist", actor="actor:a"),
            private_policy=fixtures.policy(mode="blacklist", actor="actor:a"),
        )
        self.manager.catalog.put(row)
        entered, release = asyncio.Event(), asyncio.Event()
        processed, acknowledgements, polls = [], [], []

        async def plugin(_row, operation, request):
            status, response = await host.handle(
                rpc.PREFIX + "/observation/" + operation,
                "Bearer " + host.access_key,
                json.dumps(request).encode(),
            )
            if status != 200:
                raise Fault(response["code"], status)
            if operation == "poll":
                polls.append(now[0])
            if operation == "ack":
                acknowledgements.extend(request["event_ids"])
            return response

        async def companion(payload):
            processed.append(payload["event"]["event_id"])
            if stage == "companion":
                entered.set()
                await release.wait()
            return dict(
                source_ref=payload["source_ref"], state="accepted", archive_state="pending_memory"
            )

        async def reply(*_):
            if stage == "reply":
                entered.set()
                await release.wait()
            return True

        async def no_replies(_row):
            pass

        self.manager._plugin, self.manager._companion = plugin, companion
        self.manager._reply_event, self.manager._deliver_replies = reply, no_replies
        with patch.object(rpc, "time", SimpleNamespace(time=lambda: now[0])):
            await plugin(
                row,
                "apply",
                dict(
                    request_id="initial",
                    account_id="10001",
                    revision=1,
                    enabled=True,
                    group_policy={k: row["group_policy"][k] for k in ("observe", "mode", "list")},
                    private_policy={
                        k: row["private_policy"][k] for k in ("observe", "mode", "list")
                    },
                ),
            )
            await self.manager.pump_once()
            self.assertTrue(
                await host.capture_observation(
                    "10001",
                    "group:20002",
                    "30003",
                    "first",
                    "2026-10-23T00:00:00Z",
                    "hello",
                    True,
                )
            )
            self.assertTrue(
                await host.observation_claimed("10001", "group:20002", "30003", "first")
            )
            await self.manager.pump_once(wait=False)
            await asyncio.wait_for(entered.wait(), 3)
            batch = self.manager._batches[row["id"]]
            for _ in range(2):
                now[0] += 20
                await asyncio.wait_for(self.manager.pump_once(wait=False), 3)
                self.assertIs(self.manager._batches[row["id"]], batch)
            # Forty seconds of slow business, but each real SDK poll gap is
            # below its unchanged thirty-second lease. No second dispatch/ack.
            self.assertEqual(processed, ["first"])
            self.assertEqual(acknowledgements, [])
            self.assertEqual(polls[-2:], [1020.0, 1040.0])
            self.assertTrue(
                await host.capture_observation(
                    "10001",
                    "private:30003",
                    "30003",
                    "p0",
                    "2026-10-23T00:00:40Z",
                    "question",
                    False,
                )
            )
            self.assertTrue(await host.observation_claimed("10001", "private:30003", "30003", "p0"))
            release.set()
            await asyncio.wait_for(batch, 3)
            await self.manager.pump_once()
            self.assertEqual(processed, ["first", "p0"])
            self.assertEqual(len(acknowledgements), 2)
            self.assertEqual(
                host.db.execute("SELECT count(*) FROM observations WHERE acked=0").fetchone()[0], 0
            )

    async def test_slow_companion_keeps_real_sdk_lease_and_private_claim(self):
        await self.sdk_pressure("companion")

    async def test_slow_reply_event_keeps_real_sdk_lease_and_private_claim(self):
        await self.sdk_pressure("reply")

    async def test_reply_pages_are_bounded_fair_and_do_not_block_poll_on_local_io(self):
        seen = []
        self.platform.bots = SimpleNamespace(
            claim=lambda _auth, body: seen.append(body["connection_id"]) or {"deliveries": []}
        )
        self.manager._reply_enabled = lambda _: True
        with closing(self.manager._db()) as db, db:
            for i in range(REPLY_SCOPE_BATCH * 3):
                db.execute(
                    "INSERT INTO reply_connections VALUES(?,?,?,?)",
                    (
                        f"bot:{i:03d}",
                        self.row["id"],
                        f"private:{30000 + i}",
                        "actor:a",
                    ),
                )
        for _ in range(3):
            before = len(seen)
            await self.manager._deliver_replies(self.row)
            self.assertEqual(len(seen) - before, REPLY_SCOPE_BATCH)
        self.assertEqual(len(set(seen)), REPLY_SCOPE_BATCH * 3)
        await self.manager._deliver_replies(self.row)
        self.assertEqual(seen[:REPLY_SCOPE_BATCH], seen[-REPLY_SCOPE_BATCH:])

        entered, release = threading.Event(), threading.Event()
        page = self.manager._reply_page

        def slow_page(row):
            entered.set()
            release.wait(3)
            return page(row)

        async def plugin(_row, operation, _request):
            if operation == "poll":
                return {"events": []}
            return {"found": True, "account": {"revision": 1, "pending": 0, "dropped": 0}}

        self.manager._reply_page, self.manager._plugin = slow_page, plugin
        await self.manager.pump_once(wait=False)
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            batch = self.manager._batches[self.row["id"]]
            await asyncio.wait_for(self.manager.pump_once(wait=False), 1)
            self.assertIs(self.manager._batches[self.row["id"]], batch)
            self.assertFalse(batch.done())
        finally:
            release.set()
            await asyncio.wait_for(batch, 3)

    async def test_close_settles_started_sdk_write_as_unknown_and_drains_owner(self):
        entered, stopped = asyncio.Event(), asyncio.Event()
        acknowledgements, sends = [], []
        self.manager._reply_page = lambda _: [
            {"connection_id": "bot:fixture", "conversation": "group:20002"}
        ]
        self.manager._reply_enabled = self.manager.reply_connection_active = lambda _: True
        delivery = dict(reply_id="reply:fixture", attempt_id="attempt:fixture")
        self.platform.bots = SimpleNamespace(
            claim=lambda *_: {"deliveries": [delivery]},
            ack=lambda _auth, request: acknowledgements.append(request),
        )

        async def plugin(_row, operation, _request):
            if operation == "poll":
                return {"events": []}
            if operation == "status":
                return {"found": True, "account": {"revision": 1, "pending": 0, "dropped": 0}}
            if operation == "messages/send":
                sends.append(operation)
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()
            self.fail(operation)

        self.manager._plugin = plugin
        await self.manager.pump_once(wait=False)
        await asyncio.wait_for(entered.wait(), 3)
        await asyncio.wait_for(self.manager.close(), 3)
        self.assertTrue(stopped.is_set())
        self.assertEqual(self.manager._batches, {})
        self.assertEqual([r["state"] for r in acknowledgements], ["unknown"])
        self.assertEqual(sends, ["messages/send"])
        await self.manager.pump_once(wait=False)
        self.assertEqual(sends, ["messages/send"])

    async def test_close_waits_for_its_running_local_transaction_only(self):
        entered, release = threading.Event(), threading.Event()
        other_entered, other_release = threading.Event(), threading.Event()

        def local_page(row):
            with closing(self.manager._db()) as db, db:
                db.execute("BEGIN")
                db.execute(
                    "SELECT count(*) FROM reply_connections WHERE observation_id=?", (row["id"],)
                ).fetchone()
                entered.set()
                release.wait(3)
            return []

        def other_owner():
            other_entered.set()
            other_release.wait(3)

        async def plugin(_row, operation, _request):
            if operation == "poll":
                return {"events": []}
            return {"found": True, "account": {"revision": 1, "pending": 0, "dropped": 0}}

        self.manager._reply_page, self.manager._plugin = local_page, plugin
        other = asyncio.create_task(self.platform.local_work.run(other_owner))
        await self.manager.pump_once(wait=False)
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            self.assertTrue(await asyncio.to_thread(other_entered.wait, 2))
            closing_owner = asyncio.create_task(self.manager.close())
            await asyncio.sleep(0.02)
            self.assertFalse(closing_owner.done())
            self.assertGreaterEqual(self.platform.local_work.active, 2)
            release.set()
            await asyncio.wait_for(closing_owner, 3)
            self.assertFalse(other.done())
            self.assertEqual(self.manager._local_tasks, set())
            self.assertEqual(self.manager._batches, {})
        finally:
            release.set()
            other_release.set()
            await other

    async def test_server_observation_owner_does_not_wait_for_legacy_adapter_and_closes(self):
        from fixtures import ENV
        from test_bots import bot_settings
        from services.platform.server import create_app
        from services.platform.service import Platform

        with patch.dict(os.environ, {**ENV, "TS301_CORE": "synthetic-ts301-core-token-123456"}):
            directory = Path(self.platform.store.path).parent / "server"
            directory.mkdir()
            settings = bot_settings(str(directory))
            settings["core"] = dict(base_url="https://127.0.0.1:9443", token_env="TS301_CORE")
            settings["bot_adapter_self_service"] = dict(
                directory=str(Path(self.platform.store.path).parent / "server-adapters"),
                allowed_cidrs=["127.0.0.0/8"],
                actors=[dict(id="actor:a", label="A")],
            )
            platform = Platform(settings)
            self.addCleanup(platform.close)
            entered, polled, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()

            async def legacy():
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()

            async def poll(*, wait):
                self.assertFalse(wait)
                polled.set()

            platform.bot_adapters.pump_once = legacy
            platform.bot_observation.pump_once = poll
            app = create_app(platform)
            factory = next(c for c in app.cleanup_ctx if c.__name__ == "bot_pump_context")
            context = factory(app)
            await anext(context)
            try:
                await asyncio.wait_for(entered.wait(), 1)
                await asyncio.wait_for(polled.wait(), 1)
            finally:
                await context.aclose()
            self.assertTrue(stopped.is_set() and platform.bot_observation._closed)


@unittest.skipUnless(
    os.environ.get("TS231_TEST_POSTGRES"),
    "TS231_TEST_POSTGRES requires a disposable PostgreSQL database",
)
class PostgreSQLObservationPollTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await ObservationPollTests.asyncSetUp(self)
        runtime = backends.start(
            {
                "database_path": self.platform.store.path,
                "storage_backends": {
                    "postgresql_url": os.environ["TS231_TEST_POSTGRES"],
                    "namespace": "ts301_" + uuid.uuid4().hex,
                },
            }
        )
        self.addCleanup(runtime.close)
        self.platform.storage_runtime = runtime
        self.platform.bot_adapters.catalog = BotAdapterCatalog(
            Path(self.platform.store.path).parent / "adapters",
            runtime=runtime,
        )
        self.manager = BotObservation(self.platform)
        runtime.finalize()
        self.addAsyncCleanup(self.manager.close)

    sdk_pressure = ObservationPollTests.sdk_pressure
    test_slow_companion_keeps_real_sdk_lease_and_private_claim = (
        ObservationPollTests.test_slow_companion_keeps_real_sdk_lease_and_private_claim
    )
    test_slow_reply_event_keeps_real_sdk_lease_and_private_claim = (
        ObservationPollTests.test_slow_reply_event_keeps_real_sdk_lease_and_private_claim
    )
    test_reply_pages_are_bounded_fair_and_do_not_block_poll_on_local_io = (
        ObservationPollTests.test_reply_pages_are_bounded_fair_and_do_not_block_poll_on_local_io
    )
    test_close_settles_started_sdk_write_as_unknown_and_drains_owner = (
        ObservationPollTests.test_close_settles_started_sdk_write_as_unknown_and_drains_owner
    )
    test_close_waits_for_its_running_local_transaction_only = (
        ObservationPollTests.test_close_waits_for_its_running_local_transaction_only
    )

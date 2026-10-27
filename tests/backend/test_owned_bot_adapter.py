"""Explicit owned adapter identity uses the real local exchange without DNS fallback."""

import asyncio
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from unittest.mock import AsyncMock, patch

from aiohttp import web
from fixtures import ENV
from web_fixtures import web_settings

from services.platform.contracts import Fault
from services.platform.server import create_app
from services.platform.service import Platform

try:
    from deploy.single_container.exchange import LocalExchange
except ModuleNotFoundError:
    LocalExchange = None


@unittest.skipIf(LocalExchange is None, "joint test needs unified host on PYTHONPATH")
class OwnedAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        environment = patch.dict(os.environ, ENV)
        environment.start()
        self.addCleanup(environment.stop)
        self.exchange = LocalExchange(buffer_bytes=1024)
        self.requests = []
        self.held = asyncio.Event()
        self.finished = asyncio.Event()

        async def sdk(request):
            self.assertTrue(self.exchange.owns(request))
            self.assertFalse(request.secure)
            self.requests.append((request.path, await request.json()))
            if request.headers.get("Authorization") != "Bearer synthetic-native-adapter-key":
                return web.json_response({"code": "unauthorized"}, status=401)
            if request.path.endswith("/held"):
                self.held.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.finished.set()
            return web.json_response({"protocol": "tianshu.bot-adapter/v1", "received": True})

        app = web.Application()
        app.router.add_post("/tianshu/adapter/v1/{path:.*}", sdk)
        self.exchange.register("onebot", app, "aiohttp", "http://onebot.internal:18765")
        await self.exchange.open()
        self.addAsyncCleanup(self.exchange.close)
        factory = self.exchange.session_factory("onebot")
        self.p = Platform(
            web_settings(self.temporary.name), adapter_clients={"owned-onebot": factory}
        )
        self.addCleanup(self.p.close)
        self.adapters = self.p.bot_adapters
        self.adapters.config = {"allowed_cidrs": ["127.0.0.0/8"]}
        self.row = {
            "id": "owned-onebot",
            "address": "http://onebot.internal:18765",
            "access_key": "synthetic-native-adapter-key",
            "pins": ["127.0.0.1"],
            "ca_pem": None,
        }

    async def test_owned_identity_uses_local_rpc_and_preserves_sdk_credential(self):
        with patch(
            "services.platform.bot_adapters.reviewed_pins",
            side_effect=AssertionError("owned module must not resolve DNS"),
        ):
            result = await self.adapters._plugin(self.row, "/capabilities", {})
            self.assertTrue(result["received"])
            self.assertEqual(self.requests[0], ("/tianshu/adapter/v1/capabilities", {}))
            with self.assertRaises(Fault) as denied:
                await self.adapters._plugin(
                    {**self.row, "access_key": "wrong-synthetic-key"}, "/capabilities", {}
                )
            self.assertEqual(denied.exception.code, "adapter_unauthorized")

    async def test_same_address_with_another_connection_is_still_external(self):
        resolver = AsyncMock(side_effect=Fault("external_target_changed", 503))
        with (
            patch("services.platform.bot_adapters.reviewed_pins", resolver),
            self.assertRaises(Fault) as blocked,
        ):
            await self.adapters._plugin(
                {**self.row, "id": "external-at-same-url"}, "/capabilities", {}
            )
        self.assertEqual(blocked.exception.code, "external_target_changed")
        resolver.assert_awaited_once_with(self.row["address"], ["127.0.0.0/8"])
        self.assertFalse(self.requests)

    async def test_owned_target_origin_mismatch_does_not_fall_back_to_network(self):
        with self.assertRaises(Fault) as blocked:
            await self.adapters._plugin(
                {**self.row, "address": "http://another-sdk.invalid:18765"}, "/capabilities", {}
            )
        self.assertEqual(blocked.exception.code, "adapter_unreachable")
        self.assertFalse(self.requests)

    async def test_host_owned_instance_routes_future_conversation_rows(self):
        self.adapters.bind_owned_adapter(
            "owned-sdk-instance", self.exchange.session_factory("onebot")
        )
        with patch(
            "services.platform.bot_adapters.reviewed_pins",
            side_effect=AssertionError("explicit SDK ownership must not resolve DNS"),
        ):
            for row_id in ("new-group-row", "new-private-row"):
                result = await self.adapters._plugin(
                    {**self.row, "id": row_id, "instance_id": "owned-sdk-instance"},
                    "/capabilities",
                    {},
                )
                self.assertTrue(result["received"])
        self.assertEqual(len(self.requests), 2)

    async def test_cancel_waits_for_real_local_rpc_cleanup(self):
        call = asyncio.create_task(self.adapters._plugin(self.row, "/held", {}))
        await asyncio.wait_for(self.held.wait(), 1)
        call.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await call
        self.assertTrue(self.finished.is_set())
        self.assertFalse(self.exchange._calls)

    async def test_owned_instance_rebases_persisted_network_alias_without_rewriting_row(self):
        self.adapters.bind_owned_adapter(
            "owned-sdk-instance",
            self.exchange.session_factory("onebot"),
            origin="http://onebot.internal:18765",
        )
        row = {
            **self.row,
            "id": "persisted-private-row",
            "instance_id": "owned-sdk-instance",
            "address": "http://192.0.2.10:18765",
        }
        before = dict(row)
        with patch(
            "services.platform.bot_adapters.reviewed_pins",
            side_effect=AssertionError("owned SDK must not contact the persisted network alias"),
        ):
            result = await self.adapters._plugin(row, "/capabilities", {})
        self.assertTrue(result["received"])
        self.assertEqual(row, before)
        self.assertEqual(len(self.requests), 1)

    async def test_final_app_cleanup_waits_for_cancelled_waiters_database_write(self):
        runner = web.AppRunner(create_app(self.p), access_log=None)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        started, release = threading.Event(), threading.Event()

        def write():
            with self.p.media.repo.transaction() as database:
                started.set()
                if not release.wait(3):
                    raise TimeoutError("fixture writer was not released")
                self.p.media.repo.put("fixture", "owned-close", {"done": True}, db=database)

        writing = asyncio.create_task(self.p.local_work.run(write))
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        writing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await writing
        close_task = asyncio.create_task(runner.cleanup())
        try:
            await asyncio.sleep(0.05)
            self.assertFalse(close_task.done())
            self.assertNotEqual(self.p._runtime_state, "closed")
        finally:
            release.set()
            await asyncio.wait_for(close_task, 3)
        with closing(sqlite3.connect(str(self.p.media.repo.database))) as database:
            row = database.execute(
                "SELECT body FROM documents WHERE kind='fixture' AND id='owned-close'"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(self.p._runtime_state, "closed")

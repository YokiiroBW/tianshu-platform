"""Explicit owned adapter identity uses the real local exchange without DNS fallback."""

import asyncio
import os
import sqlite3
import tempfile
import threading
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from fixtures import ENV
from test_bots import bot_settings

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
        environment = patch.dict(
            os.environ, {**ENV, "TS_ADAPTER_CORE_TOKEN": "synthetic-core-token"}
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.exchange = LocalExchange(buffer_bytes=1024)
        self.requests = []
        self.held = asyncio.Event()
        self.finished = asyncio.Event()

        async def sdk(request):
            self.assertTrue(self.exchange.owns(request))
            self.assertFalse(request.secure)
            body = await request.json()
            self.requests.append((request.path, body))
            if request.headers.get("Authorization") != "Bearer synthetic-native-adapter-key":
                return web.json_response({"code": "unauthorized"}, status=401)
            if request.path.endswith("/held"):
                self.held.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.finished.set()
            accounts = [
                {"id": "10001", "platform": "qq", "label": "QQ one"},
                {"id": "10002", "platform": "qq", "label": "QQ two"},
            ]
            if request.path.endswith("/observation/capabilities"):
                return web.json_response(
                    {
                        "protocol": "tianshu.bot-observation/v2",
                        "instance_id": "owned-sdk-instance",
                        "adapter": "onebot",
                        "accounts": accounts,
                        "group_policy_version": 2,
                        "ordinary_claim_available": True,
                    }
                )
            if request.path.endswith("/observation/apply"):
                return web.json_response(
                    {
                        k: body[k]
                        for k in (
                            "account_id",
                            "revision",
                            "enabled",
                            "group_policy",
                            "private_policy",
                            "group_revisions",
                            "group_default_revision",
                        )
                    }
                )
            if request.path.endswith("/capabilities"):
                return web.json_response(
                    {
                        "protocol": "tianshu.bot-adapter/v1",
                        "adapter": "onebot",
                        "instance_id": "owned-sdk-instance",
                        "accounts": accounts,
                        "capabilities": ["text"],
                        "max_outbound_utf8_bytes": 32768,
                        "received": True,
                    }
                )
            return web.json_response({"protocol": "tianshu.bot-adapter/v1", "received": True})

        app = web.Application()
        app.router.add_post("/tianshu/adapter/v1/{path:.*}", sdk)
        self.exchange.register("onebot", app, "aiohttp", "http://onebot.internal:18765")
        await self.exchange.open()
        self.addAsyncCleanup(self.exchange.close)
        factory = self.exchange.session_factory("onebot")
        settings = bot_settings(self.temporary.name)
        settings["core"] = {
            "base_url": "https://127.0.0.1:9443",
            "token_env": "TS_ADAPTER_CORE_TOKEN",
        }
        settings["bot_adapter_self_service"] = {
            "directory": str(Path(self.temporary.name) / "owned-catalog"),
            "allowed_cidrs": ["127.0.0.0/8"],
            "actors": [{"id": "actor:a", "label": "A"}],
        }
        self.p = Platform(settings, adapter_clients={"owned-onebot": factory})
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

    async def test_server_catalog_adds_storage_without_changing_existing_rows(self):
        from services.platform.bot_adapter_catalog import BotAdapterCatalog
        from services.platform.onebot_servers import OneBotServers

        original = self.adapters.catalog
        original.put({"id": "legacy-row", "name": "unchanged"})
        with closing(original._db()) as db, db:
            db.execute("DROP TABLE onebot_servers")
        migrated = BotAdapterCatalog(original.root)
        manager = OneBotServers(SimpleNamespace(catalog=migrated))
        server = await manager.create(
            {"name": "synthetic", "token": "fixture-server-token", "client_id": "fixture-id"}
        )
        self.assertEqual(migrated.all(), [{"id": "legacy-row", "name": "unchanged"}])
        reopened = BotAdapterCatalog(original.root)
        self.assertEqual(reopened.all(), migrated.all())
        self.assertEqual(reopened.server(server["id"])["token"], "fixture-server-token")
        with closing(reopened._db()) as db:
            ciphertext = db.execute("SELECT document FROM onebot_servers").fetchone()[0]
        self.assertNotIn(b"fixture-server-token", ciphertext)

    async def test_server_handshake_binds_and_automatically_enrolls_each_account(self):
        live = {}

        async def status():
            return live.copy()

        self.adapters.bind_owned_adapter(
            "owned-sdk-instance",
            self.exchange.session_factory("onebot"),
            origin="http://onebot.internal:18765",
            onboarding=lambda: {"access_key": "synthetic-native-adapter-key", "ws_port": 18765},
            server_status=status,
        )
        console = SimpleNamespace(session_valid=lambda session: True)
        session = {}
        body = {"name": "QQ one", "token": "", "client_id": str(uuid.uuid4())}
        with patch.object(self.p.bots, "_web_code", return_value="management_required"):
            with self.assertRaises(Fault):
                await self.adapters.route(
                    console, "/api/web/bot-adapters/onebot-create", body, session
                )
        with (
            patch.object(self.p.bots, "_web_code", return_value="ready"),
            patch(
                "services.platform.bot_adapters.reviewed_pins", side_effect=AssertionError("DNS")
            ),
        ):
            first_view = await self.adapters.route(
                console, "/api/web/bot-adapters/onebot-create", body, session
            )
            first = first_view["servers"][0]
            self.assertTrue(first["token"])
            self.assertNotIn("access_key", first_view)
            self.assertFalse(first["connected"])
            repeated = await self.adapters.route(
                console, "/api/web/bot-adapters/onebot-create", body, session
            )
            self.assertEqual(len(repeated["servers"]), 1)
            second_view = await self.adapters.route(
                console,
                "/api/web/bot-adapters/onebot-create",
                {
                    "name": "QQ two",
                    "token": "another-synthetic-token",
                    "client_id": str(uuid.uuid4()),
                },
                session,
            )
            second = next(item for item in second_view["servers"] if item["name"] == "QQ two")
            manager = self.adapters.onebot_servers
            self.assertFalse(await manager.authorize(first["id"], "wrong", "10001"))
            self.assertTrue(await manager.authorize(first["id"], first["token"], "10001"))
            reservation = await manager.reserve(first["id"], first["token"], "10001")
            self.assertIsNone(await manager.reserve(first["id"], first["token"], "10002"))
            self.assertIsNone(manager.row(first["id"])["account_id"])
            self.assertTrue(await manager.claim(first["id"], first["token"], "10001", reservation))
            self.assertIsNone(await manager.reserve(first["id"], first["token"], "10002"))
            reservation = await manager.reserve(second["id"], second["token"], "10002")
            self.assertTrue(
                await manager.claim(second["id"], second["token"], "10002", reservation)
            )
            live.update({first["id"]: "10001", second["id"]: "10002"})
            await manager.synchronize()
            rows = self.p.bot_observation.rows()
            self.assertEqual({item["account_id"] for item in rows}, {"10001", "10002"})
            self.assertTrue(all(item["state"] == "ready" for item in rows))
            await manager.synchronize()
            self.assertEqual(len(self.p.bot_observation.rows()), 2)
            result = await manager.view()
            self.assertTrue(
                all(item["connected"] and item["observation_id"] for item in result["servers"])
            )
            live.clear()
            result = await manager.view()
            self.assertFalse(any(item["connected"] for item in result["servers"]))
            self.assertEqual({item["account_id"] for item in result["servers"]}, {"10001", "10002"})
            from services.platform.onebot_servers import OneBotServers

            restored = OneBotServers(self.adapters)
            self.assertTrue(await restored.authorize(first["id"], first["token"], "10001"))
            self.assertFalse(await restored.authorize(first["id"], first["token"], "10002"))

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

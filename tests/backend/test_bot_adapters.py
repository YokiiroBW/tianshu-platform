"""Recorded HTTP adapter and real Platform ledgers; Core management is a recorded peer."""

import os
import asyncio
import ipaddress
import socket
import time
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from contextlib import closing as closing_db
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web

from fixtures import ENV, bearer, start_http
from services.platform.contracts import Fault, canonical, utc
from services.platform.server import create_app
from services.platform.service import Platform
from test_bots import bot_settings
from web_fixtures import PASSWORD


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        environment = patch.dict(
            os.environ, {**ENV, "TS_ADAPTER_CORE_TOKEN": "synthetic-core-adapter-token-123456789"}
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.settings = bot_settings(self.temp.name)
        self.settings["core"] = {
            "base_url": "https://127.0.0.1:9443",
            "token_env": "TS_ADAPTER_CORE_TOKEN",
        }
        self.settings["bot_adapter_self_service"] = {
            "directory": str(Path(self.temp.name) / "private-adapters"),
            "allowed_cidrs": ["127.0.0.0/8"],
            "actors": [{"id": "actor:a", "label": "合成角色"}],
        }
        self.platform = Platform(self.settings)
        self.addAsyncCleanup(self._close)
        self.plugin_state = {}
        self.core_state = {}
        self.apply_calls = 0
        self.fail_next = False
        self.events = []
        self.acked = []
        self.sends = []
        self.receipts = {}

        async def handler(request):
            if request.headers.get("Authorization") != "Bearer synthetic-plugin-key-123456789":
                return web.json_response({"code": "unauthorized", "retryable": False}, status=401)
            body = await request.json()
            route = request.path.removeprefix("/tianshu/adapter/v1")
            if route == "/capabilities":
                return web.json_response(
                    {
                        "protocol": "tianshu.bot-adapter/v1",
                        "adapter": "nonebot",
                        "instance_id": "sdk:one",
                        "accounts": [{"id": "bot:9", "platform": "qq", "label": "Synthetic QQ"}],
                        "capabilities": ["text"],
                        "max_outbound_utf8_bytes": 32768,
                    }
                )
            if route == "/bindings/apply":
                self.apply_calls += 1
                result = {key: body[key] for key in ("connection_id", "revision", "enabled")}
                self.plugin_state[body["connection_id"]] = result
                if self.fail_next:
                    self.fail_next = False
                    return web.json_response(
                        {"code": "dependency_unavailable", "retryable": True}, status=503
                    )
                return web.json_response(result)
            if route == "/bindings/status":
                result = self.plugin_state.get(body["connection_id"])
                return web.json_response({"found": result is not None, "binding": result})
            if route == "/events/poll":
                return web.json_response({"events": self.events[:]})
            if route == "/events/ack":
                self.acked.extend(body["event_ids"])
                self.events = [item for item in self.events if item["id"] not in body["event_ids"]]
                return web.json_response({"acknowledged": body["event_ids"]})
            if route == "/messages/send":
                delivery = body["delivery"]
                self.sends.append(delivery)
                receipt = {
                    "reply_id": delivery["reply_id"],
                    "attempt_id": delivery["attempt_id"],
                    "state": "unknown",
                    "channel_message_ids": [],
                }
                self.receipts[(delivery["reply_id"], delivery["attempt_id"])] = receipt
                return web.json_response(receipt)
            if route == "/messages/status":
                receipt = self.receipts.get((body["reply_id"], body["attempt_id"]))
                return web.json_response({"found": receipt is not None, "receipt": receipt})
            return web.Response(status=404, text="missing")

        app = web.Application()
        app.router.add_post("/tianshu/adapter/v1/{tail:.*}", handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.address = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        self.addAsyncCleanup(self.runner.cleanup)
        self._install_core_peer()

    def _install_core_peer(self):
        async def core_peer(path, body):
            if path == "apply":
                result = {key: body[key] for key in ("connection_id", "revision", "enabled")}
                self.core_state[body["connection_id"]] = result
                return result
            result = self.core_state.get(body["connection_id"])
            return {"found": result is not None, "binding": result}

        self.platform.bot_adapters._core = core_peer

    async def _close(self):
        self.platform.close()

    async def _created(self):
        manager = self.platform.bot_adapters
        session = {}
        probe = await manager.probe(
            {
                "adapter": "nonebot",
                "address": self.address,
                "access_key": "synthetic-plugin-key-123456789",
                "allow_private_http": True,
                "ca_pem": None,
            },
            session,
        )
        body = {
            "draft_id": probe["draft_id"],
            "name": "合成插件",
            "account_id": "bot:9",
            "conversation": {"kind": "group", "id": "group:999"},
            "allowed_authors": ["user:1"],
            "actor_id": "actor:a",
            "client_id": str(uuid.uuid4()),
        }
        return await manager.create(body, session), body

    async def test_probe_persist_enable_unknown_reconcile_restart_disable(self):
        manager = self.platform.bot_adapters
        with self.assertRaises(Fault) as error:
            await manager.probe(
                {
                    "adapter": "nonebot",
                    "address": self.address,
                    "access_key": "wrong-synthetic-plugin-key-123456",
                    "allow_private_http": True,
                    "ca_pem": None,
                },
                {},
            )
        self.assertEqual(error.exception.code, "adapter_unauthorized")
        from services.platform.web_external_net import reviewed_pins

        with self.assertRaises(Fault) as error:
            await manager._call(
                self.address,
                "synthetic-plugin-key-123456789",
                await reviewed_pins(self.address, ["127.0.0.0/8"]),
                None,
                "/tianshu/adapter/v1/missing",
                {},
            )
        self.assertEqual(error.exception.code, "adapter_not_installed")
        row, body = await self._created()
        self.assertEqual(row["state"], "disabled")
        self.assertEqual((await manager.create(body, {}))["id"], row["id"])
        self.assertNotIn("access_key", manager._project(row))
        for key in ("account", "channel"):
            value = self.platform.sources.entries[f"{row['id']}:author:0"][key]
            self.platform.contracts.check(
                "common#" + ("account" if key == "account" else "channel_key"), value
            )
        self.fail_next = True
        enabled = await manager.change(
            "enable", {"id": row["id"], "expected_revision": 1, "client_id": str(uuid.uuid4())}
        )
        self.assertEqual(enabled["state"], "unknown")
        with closing_db(self.platform.bots._db()) as db:
            self.assertEqual(
                db.execute("SELECT enabled FROM connections WHERE id=?", (row["id"],)).fetchone()[
                    0
                ],
                0,
            )
        recovered = await manager._reconcile(enabled)
        self.assertEqual(recovered["state"], "ready")
        self.platform.close()
        self.platform = Platform(self.settings)
        self._install_core_peer()
        restarted = self.platform.bot_adapters.catalog.get(row["id"])
        self.assertEqual(restarted["state"], "unknown")
        self.assertEqual((await self.platform.bot_adapters._reconcile(restarted))["state"], "ready")
        stopped = await self.platform.bot_adapters.change(
            "disable", {"id": row["id"], "expected_revision": 2, "client_id": str(uuid.uuid4())}
        )
        self.assertEqual(stopped["state"], "disabled")
        with closing_db(self.platform.bots._db()) as db:
            self.assertEqual(
                db.execute("SELECT enabled FROM connections WHERE id=?", (row["id"],)).fetchone()[
                    0
                ],
                0,
            )

    async def test_browser_login_unlock_and_probe_route(self):
        app = create_app(self.platform)
        runner, url = await start_http(app)
        self.addAsyncCleanup(runner.cleanup)
        self.settings["web"]["origin"] = url
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
            async with client.get(url + "/api/web/session") as response:
                anonymous = await response.json()
            async with client.post(
                url + "/api/web/login",
                json={"username": "synthetic-admin", "password": PASSWORD},
                headers={"Origin": url, "X-CSRF-Token": anonymous["csrf"]},
            ) as response:
                self.assertEqual(response.status, 200)
                logged = await response.json()
            headers = {"Origin": url, "X-CSRF-Token": logged["csrf"]}
            async with client.post(
                url + "/api/web/bot-adapters/view", json={}, headers=headers
            ) as response:
                self.assertEqual(response.status, 200)
                self.assertFalse((await response.json())["unlocked"])
            async with client.post(
                url + "/api/web/bot-adapters/probe",
                json={
                    "adapter": "nonebot",
                    "address": self.address,
                    "access_key": "synthetic-plugin-key-123456789",
                    "allow_private_http": True,
                    "ca_pem": None,
                },
                headers=headers,
            ) as response:
                self.assertEqual(response.status, 403)
            async with client.post(
                url + "/api/web/bots/unlock", json={"password": PASSWORD}, headers=headers
            ) as response:
                self.assertEqual(response.status, 200)
            async with client.post(
                url + "/api/web/bot-adapters/probe",
                json={
                    "adapter": "nonebot",
                    "address": self.address,
                    "access_key": "synthetic-plugin-key-123456789",
                    "allow_private_http": True,
                    "ca_pem": None,
                },
                headers=headers,
            ) as response:
                self.assertEqual(response.status, 200)
                result = await response.json()
                self.assertEqual(result["accounts"][0]["id"], "bot:9")
                self.assertNotIn("access_key", str(result))
            async with client.post(
                url + "/api/web/bot-adapters/create",
                json={
                    "draft_id": result["draft_id"],
                    "name": "合成网页插件",
                    "account_id": "bot:9",
                    "conversation": {"kind": "group", "id": "group:web"},
                    "allowed_authors": ["user:web"],
                    "actor_id": "actor:a",
                    "client_id": str(uuid.uuid4()),
                },
                headers=headers,
            ) as response:
                self.assertEqual(response.status, 200)
                created = (await response.json())["connection"]
                self.assertEqual(created["state"], "disabled")
            self.fail_next = True
            async with client.post(
                url + "/api/web/bot-adapters/enable",
                json={
                    "id": created["id"],
                    "expected_revision": created["revision"],
                    "client_id": str(uuid.uuid4()),
                },
                headers=headers,
            ) as response:
                self.assertEqual(response.status, 200)
                unknown = (await response.json())["connection"]
                self.assertEqual(unknown["state"], "unknown")
            async with client.post(
                url + "/api/web/bot-adapters/reconcile",
                json={
                    "id": created["id"],
                    "expected_revision": unknown["revision"],
                    "client_id": str(uuid.uuid4()),
                },
                headers=headers,
            ) as response:
                self.assertEqual(response.status, 200)
                ready = (await response.json())["connection"]
                self.assertEqual(ready["state"], "ready")
            async with client.post(
                url + "/api/web/bot-adapters/disable",
                json={
                    "id": created["id"],
                    "expected_revision": ready["revision"],
                    "client_id": str(uuid.uuid4()),
                },
                headers=headers,
            ) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())["connection"]["state"], "disabled")

    async def test_explicit_reconcile_recovers_unknown_revision_without_new_write(self):
        row, _ = await self._created()
        self.fail_next = True
        unknown = await self.platform.bot_adapters.change(
            "enable", {"id": row["id"], "expected_revision": 1, "client_id": str(uuid.uuid4())}
        )
        self.assertEqual(unknown["state"], "unknown")
        with self.assertRaises(Fault) as error:
            await self.platform.bot_adapters.change(
                "enable", {"id": row["id"], "expected_revision": 2, "client_id": str(uuid.uuid4())}
            )
        self.assertEqual(error.exception.code, "result_unknown")
        self.plugin_state.pop(row["id"])
        before = self.apply_calls
        pending = await self.platform.bot_adapters._reconcile(unknown)
        self.assertEqual(pending["state"], "unknown")
        self.assertEqual(self.apply_calls, before)
        recovery = {"id": row["id"], "expected_revision": 2, "client_id": str(uuid.uuid4())}
        recovered = await self.platform.bot_adapters.reconcile(recovery)
        self.assertEqual(recovered["state"], "ready")
        self.assertEqual(recovered["revision"], 2)
        self.assertEqual(self.apply_calls, before + 1)
        self.assertEqual((await self.platform.bot_adapters.reconcile(recovery))["state"], "ready")
        self.assertEqual(self.apply_calls, before + 1)
        with self.assertRaises(Fault) as error:
            await self.platform.bot_adapters.reconcile(
                {"id": row["id"], "expected_revision": 1, "client_id": str(uuid.uuid4())}
            )
        self.assertEqual(error.exception.code, "version_conflict")
        with self.assertRaises(Fault) as error:
            await self.platform.bot_adapters.reconcile(
                {"id": row["id"], "expected_revision": 2, "client_id": str(uuid.uuid4())}
            )
        self.assertEqual(error.exception.code, "version_conflict")

    async def test_pump_reuses_event_dedup_and_unknown_send_is_never_resent(self):
        row, _ = await self._created()
        row = await self.platform.bot_adapters.change(
            "enable", {"id": row["id"], "expected_revision": 1, "client_id": str(uuid.uuid4())}
        )
        self.assertEqual(row["state"], "ready")
        event = {
            "schema_version": 1,
            "connection_id": row["id"],
            "platform_id": "sdk:one",
            "self_id": "bot:9",
            "event_id": "sdk:event:1",
            "revision": 1,
            "namespace": "qq",
            "conversation_id": "group:999",
            "thread_id": None,
            "account_id": "user:1",
            "sent_at": utc(time.time()),
            "text": "合成入站",
        }
        self.events = [{"id": "queue:1", "event": event}]
        self.platform.sources.dispatch = AsyncMock(
            return_value={"outcomes": [{"actor_id": "actor:a", "state": "accepted"}]}
        )
        await self.platform.bot_adapters.pump_once()
        self.assertEqual(self.acked, ["queue:1"])
        self.events = [{"id": "queue:1", "event": event}]
        await self.platform.bot_adapters.pump_once()
        self.assertEqual(self.platform.sources.dispatch.await_count, 1)
        channel = self.platform.sources.entries[f"{row['id']}:author:0"]["channel"]
        account = self.platform.sources.entries[f"{row['id']}:author:0"]["account"]
        with self.platform.store.connect(write=True) as db:
            db.execute(
                "INSERT OR REPLACE INTO identities VALUES(?,?,?)",
                (canonical(account), "person:adapter", 1),
            )
            db.execute(
                "INSERT OR REPLACE INTO channels VALUES(?,?)",
                (canonical(channel), "conversation:adapter"),
            )
        origin = self.platform.origins.issue(bearer("CONNECTOR"), f"{row['id']}:author:0:actor")
        send_request = {
            "command": {
                "schema_version": 1,
                "request_id": "request:adapter-reply",
                "idempotency_key": "command:adapter-reply",
                "origin": {"assertion_ref": origin["assertion_ref"]},
                "deadline_at": utc(time.time() + 30),
            },
            "conversation_id": "conversation:adapter",
            "turn_id": "turn:adapter",
            "turn_sequence": 1,
            "reply_id": "reply:adapter",
            "actor_id": "actor:a",
            "destination": channel,
            "segment_sequence": 1,
            "segment_count": 1,
            "text": "合成回复",
        }
        self.platform.bots.send(bearer("COMPANION"), send_request)
        await self.platform.bot_adapters.pump_once()
        self.assertEqual(len(self.sends), 1)
        await self.platform.bot_adapters.pump_once()
        self.assertEqual(len(self.sends), 1)
        self.assertEqual(
            self.platform.bots.reply_status(bearer("COMPANION"), send_request)["receipt"]["state"],
            "unknown",
        )

    async def test_optional_actual_platform_core_tls_and_plugin_joint(self):
        if not os.environ.get("TS_ADAPTER_JOINT_CORE"):
            self.skipTest("set TS_ADAPTER_JOINT_CORE=1 for actual Core TLS joint")
        import uvicorn
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        from tianshu_companion.app import build_runtime, create_app as core_app

        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.now(timezone.utc)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(private.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(
                x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                critical=False,
            )
            .sign(private, hashes.SHA256())
        )
        cert = Path(self.temp.name) / "core-ca.pem"
        key = Path(self.temp.name) / "core-key.pem"
        cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key.write_bytes(
            private.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        config = {
            "contracts_path": "C:/YOKI/Codex/tianshu-peiban-bot/contracts/text-dialogue/v1",
            "database_path": str(Path(self.temp.name) / "core.sqlite"),
            "roles": {"actor:a": {}},
            "bot_binding_management_enabled": True,
            "callers": {
                "platform": {
                    "token_env": "TS_ADAPTER_CORE_TOKEN",
                    "issuer": "platform",
                    "origin_service": "platform_origin",
                }
            },
            "services": {
                "platform_sender": {
                    "url": "https://platform.synthetic.invalid",
                    "token_env": "TS_ADAPTER_CORE_TOKEN",
                }
            },
        }
        core, _, clients, _ = build_runtime(config)
        application = core_app(core, {"platform": os.environ["TS_ADAPTER_CORE_TOKEN"]})
        server = uvicorn.Server(
            uvicorn.Config(
                application,
                host="127.0.0.1",
                port=port,
                ssl_certfile=str(cert),
                ssl_keyfile=str(key),
                access_log=False,
                log_config=None,
            )
        )
        task = asyncio.create_task(server.serve())

        async def stop_core():
            server.should_exit = True
            await asyncio.wait_for(task, 10)
            for client in clients:
                await client.close()

        self.addAsyncCleanup(stop_core)
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        self.assertTrue(server.started)
        self.settings["core"] = {
            "base_url": f"https://127.0.0.1:{port}",
            "token_env": "TS_ADAPTER_CORE_TOKEN",
            "ca_file": str(cert),
        }
        del self.platform.bot_adapters.__dict__["_core"]
        row, _ = await self._created()
        self.assertEqual(row["state"], "disabled")
        self.assertNotIn("binding:" + row["id"], core.bindings)
        enabled = await self.platform.bot_adapters.change(
            "enable", {"id": row["id"], "expected_revision": 1, "client_id": str(uuid.uuid4())}
        )
        self.assertEqual(enabled["state"], "ready")
        self.assertIn("binding:" + row["id"], core.bindings)
        stopped = await self.platform.bot_adapters.change(
            "disable", {"id": row["id"], "expected_revision": 2, "client_id": str(uuid.uuid4())}
        )
        self.assertEqual(stopped["state"], "disabled")
        self.assertNotIn("binding:" + row["id"], core.bindings)

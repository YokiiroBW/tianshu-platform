"""Real Platform/Core TLS and installed host RPC against synthetic QQ SDKs.

Run with the H adapter venv and the explicit integration PYTHONPATH in ADAPTER-JOINT.md.
The test refuses imports from stale editable product checkouts.
"""

import asyncio
import importlib
import ipaddress
import json
import os
import shutil
import socket
import ssl
import sys
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import uvicorn
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

PLATFORM_ROOT = Path(__file__).resolve().parents[2]
COMPANION_ROOT = PLATFORM_ROOT.parent / "tianshu-companion"
CONTRACTS = PLATFORM_ROOT.parents[2] / "contracts" / "text-dialogue" / "v1"
for source in (
    COMPANION_ROOT / "src",
    COMPANION_ROOT / "tests",
    COMPANION_ROOT / "integrations" / "nonebot",
):
    sys.path.insert(0, str(source))

from fixtures import ENV  # noqa: E402
from services.platform.server import create_app  # noqa: E402
from services.platform.service import Platform  # noqa: E402
from services.platform.contracts import Fault  # noqa: E402
from test_bots import bot_settings  # noqa: E402
from tianshu_companion.app import build_runtime, create_app as core_app  # noqa: E402
from support import FakeGateway, FakeMemory  # noqa: E402


def _port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _certificate(directory):
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
    cert = Path(directory) / "joint-ca.pem"
    key = Path(directory) / "joint-key.pem"
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    return cert, key, context


class AdapterHostJointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertTrue(CONTRACTS.is_dir())
        from services.platform import service as platform_module
        from tianshu_companion import app as core_module

        self.assertTrue(Path(platform_module.__file__).resolve().is_relative_to(PLATFORM_ROOT))
        self.assertTrue(Path(core_module.__file__).resolve().is_relative_to(COMPANION_ROOT))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ca, self.key, tls = _certificate(self.root)
        self.platform_tls = tls
        environment = patch.dict(
            os.environ,
            {
                **ENV,
                "TS012_CONTRACT_DIR": str(CONTRACTS),
                "TIANSHU_CONTRACTS": str(CONTRACTS),
                "TS_ADAPTER_CORE_TOKEN": "synthetic-joint-platform-core-token-123456",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        core_port = _port()
        self.settings = bot_settings(str(self.root))
        self.settings["principals"]["companion"]["actions"].append("origin.resolve")
        self.settings["principals"]["companion"]["resolver"] = {
            "caller": "platform",
            "purpose": "dialogue",
        }
        self.settings["core"] = {
            "base_url": f"https://127.0.0.1:{core_port}",
            "token_env": "TS_ADAPTER_CORE_TOKEN",
            "ca_file": str(self.ca),
        }
        self.settings["bot_adapter_self_service"] = {
            "directory": str(self.root / "private-adapters"),
            "allowed_cidrs": ["127.0.0.0/8"],
            "actors": [{"id": "actor:a", "label": "Synthetic actor"}],
        }
        self.platform = Platform(self.settings)
        self.addAsyncCleanup(self._close_platform)
        platform_app = create_app(self.platform)
        # The same production pump is driven explicitly to make each boundary observable.
        platform_app.cleanup_ctx.clear()
        self.platform_runner = web.AppRunner(platform_app, access_log=None)
        await self.platform_runner.setup()
        platform_port = _port()
        self.platform_port = platform_port
        await web.TCPSite(self.platform_runner, "127.0.0.1", platform_port, ssl_context=tls).start()
        self.platform_url = f"https://127.0.0.1:{platform_port}"
        self.addAsyncCleanup(self.platform_runner.cleanup)

        core_config = {
            "contracts_path": str(CONTRACTS),
            "database_path": str(self.root / "core.sqlite"),
            "roles": {"actor:a": {"version": 1, "persona": "Synthetic actor"}},
            "config_version": 1,
            "automatic_memory_candidates": False,
            "bot_binding_management_enabled": True,
            "callers": {
                "platform": {
                    "token_env": "TS_ADAPTER_CORE_TOKEN",
                    "issuer": "platform",
                    "origin_service": "platform_origin",
                }
            },
            "services": {
                "platform_origin": {
                    "url": self.platform_url,
                    "token_env": "TS012_COMPANION",
                    "ca_file": str(self.ca),
                },
                "platform_sender": {
                    "url": self.platform_url,
                    "token_env": "TS012_COMPANION",
                    "ca_file": str(self.ca),
                },
            },
        }
        self.core, _, self.core_clients, _ = build_runtime(core_config)
        self.memory = FakeMemory(time.time)
        self.gateway = FakeGateway()
        self.gateway.segments = ["Synthetic reply"]
        self.core.memory = self.memory
        self.core.gateway = self.gateway
        server = uvicorn.Server(
            uvicorn.Config(
                core_app(self.core, {"platform": os.environ["TS_ADAPTER_CORE_TOKEN"]}),
                host="127.0.0.1",
                port=core_port,
                ssl_certfile=str(self.ca),
                ssl_keyfile=str(self.key),
                log_level="error",
                access_log=False,
                log_config=None,
            )
        )
        self.core_server = server
        self.core_task = asyncio.create_task(server.serve())
        self.addAsyncCleanup(self._close_core)
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        self.assertTrue(server.started)

    async def _close_platform(self):
        self.platform.close()

    async def _close_core(self):
        self.core_server.should_exit = True
        await asyncio.wait_for(self.core_task, 10)
        for client in self.core_clients:
            await client.close()
        self.core.store.close()

    async def _restart_platform(self):
        await self.platform_runner.cleanup()
        self.platform.close()
        self.platform = Platform(self.settings)
        application = create_app(self.platform)
        application.cleanup_ctx.clear()
        self.platform_runner = web.AppRunner(application, access_log=None)
        await self.platform_runner.setup()
        await web.TCPSite(
            self.platform_runner,
            "127.0.0.1",
            self.platform_port,
            ssl_context=self.platform_tls,
        ).start()
        self.addAsyncCleanup(self.platform_runner.cleanup)

    async def _connect(self, adapter, address, key):
        manager = self.platform.bot_adapters
        session = {}
        probe = await manager.probe(
            {
                "adapter": adapter,
                "address": address,
                "access_key": key,
                "allow_private_http": True,
                "ca_pem": None,
            },
            session,
        )
        self.assertEqual(probe["accounts"], [{"id": "42", "platform": "qq", "label": "synthetic"}])
        created = await manager.create(
            {
                "draft_id": probe["draft_id"],
                "name": f"Synthetic {adapter}",
                "account_id": "42",
                "conversation": {"kind": "private", "id": "7"},
                "allowed_authors": ["7"],
                "actor_id": "actor:a",
                "client_id": str(uuid.uuid4()),
            },
            session,
        )
        self.assertEqual(created["state"], "disabled")
        ready = await manager.change(
            "enable",
            {"id": created["id"], "expected_revision": 1, "client_id": str(uuid.uuid4())},
        )
        self.assertEqual(ready["state"], "ready")
        self.assertIn("binding:" + ready["id"], self.core.bindings)
        return ready

    def _delivery_states(self, connection_id):
        with closing(self.platform.bots._db()) as db:
            return [
                item[0]
                for item in db.execute(
                    "SELECT state FROM replies WHERE connection_id=? ORDER BY rowid",
                    (connection_id,),
                )
            ]

    async def _assert_message_path(self, host, row):
        manager = self.platform.bot_adapters
        await host.emit("7", "501")
        queued = await manager._plugin(
            row, "/events/poll", {"connection_id": row["id"], "limit": 20}
        )
        self.assertEqual(len(queued["events"]), 1)
        event = queued["events"][0]["event"]
        channel = self.platform.sources.entries[f"{row['id']}:author:0"]["channel"]
        # These are both wire-level requirements. The first integrated candidates violate both.
        self.assertEqual(
            (event["revision"], event["conversation_id"]),
            (1, channel["channel_conversation_id"]),
        )
        await manager.pump_once()
        self.assertEqual(
            (await manager._plugin(row, "/events/poll", {"connection_id": row["id"], "limit": 20}))[
                "events"
            ],
            [],
        )
        # Core holds a newly admitted source for up to five seconds so nearby
        # messages can share a turn; wait through that real timer.
        for _ in range(100):
            await self.core.tick()
            await manager.pump_once()
            if host.sends:
                break
            await asyncio.sleep(0.1)
        self.assertEqual(len(host.sends), 1, self.platform.bots.view())
        self.assertEqual(host.sends[0]["text"], "Synthetic reply")
        self.assertEqual(self._delivery_states(row["id"]), ["sent"])
        for _ in range(20):
            await self.core.tick()
            if self.core.store.list("replies", states=["sent"]):
                break
            await asyncio.sleep(0.05)
        self.assertEqual(len(self.core.store.list("replies", states=["sent"])), 1)

        # The native SDK can repeat an event. Its stable message ID must be
        # journaled once all the way through Core, with no second reply.
        turns = len(self.core.store.list("turns"))
        await host.emit("7", "501")
        await manager.pump_once()
        await self.core.tick()
        self.assertEqual(len(self.core.store.list("turns")), turns)
        self.assertEqual(len(host.attempts), 1)

        # A native timeout may mean the SDK sent before losing its response.
        # The adapter records unknown, and neither pump nor Core may resend it.
        host.fail_next = True
        await host.emit("7", "502")
        await manager.pump_once()
        for _ in range(100):
            await self.core.tick()
            await manager.pump_once()
            if "unknown" in self._delivery_states(row["id"]):
                break
            await asyncio.sleep(0.1)
        self.assertEqual(self._delivery_states(row["id"]), ["sent", "unknown"])
        self.assertEqual(len(host.attempts), 2)
        for _ in range(3):
            await self.core.tick()
            await manager.pump_once()
        self.assertEqual(len(host.attempts), 2)
        self.assertEqual(len(host.sends), 1)

        # Rebuild the HTTP Platform service against the same encrypted catalog
        # and delivery ledger. Startup seals the connection until both
        # live peers confirm the persisted binding.
        await self._restart_platform()
        manager = self.platform.bot_adapters
        restarted = manager.catalog.get(row["id"])
        self.assertEqual((restarted["state"], restarted["enabled"]), ("unknown", False))
        self.assertEqual(self._delivery_states(row["id"]), ["sent", "unknown"])
        recovered = await manager.reconcile(
            {"id": row["id"], "expected_revision": row["revision"], "client_id": str(uuid.uuid4())}
        )
        self.assertEqual(recovered["state"], "ready")
        await manager.pump_once()
        self.assertEqual(len(host.attempts), 2)

        stopped = await manager.change(
            "disable",
            {
                "id": row["id"],
                "expected_revision": recovered["revision"],
                "client_id": str(uuid.uuid4()),
            },
        )
        self.assertEqual(stopped["state"], "disabled")
        self.assertNotIn("binding:" + row["id"], self.core.bindings)
        await host.emit("7", "503")
        self.assertEqual(
            (
                await manager._plugin(
                    stopped, "/events/poll", {"connection_id": row["id"], "limit": 20}
                )
            )["events"],
            [],
        )
        self.assertEqual(len(self.core.store.list("turns")), turns + 1)

        # The remote apply succeeds but its HTTP response is lost. Reconcile
        # uses the actual Core and plugin status RPCs without another apply.
        original_plugin = manager._plugin

        async def lose_apply_response(connection, path, payload):
            result = await original_plugin(connection, path, payload)
            if path == "/bindings/apply":
                raise Fault("adapter_unavailable", 502)
            return result

        with patch.object(manager, "_plugin", side_effect=lose_apply_response):
            uncertain = await manager.change(
                "enable",
                {
                    "id": row["id"],
                    "expected_revision": stopped["revision"],
                    "client_id": str(uuid.uuid4()),
                },
            )
        self.assertEqual(uncertain["state"], "unknown")
        recovered = await manager.reconcile(
            {
                "id": row["id"],
                "expected_revision": uncertain["revision"],
                "client_id": str(uuid.uuid4()),
            }
        )
        self.assertEqual(recovered["state"], "ready")
        self.assertIn("binding:" + row["id"], self.core.bindings)
        self.assertEqual(len(host.attempts), 2)

    async def test_nonebot_host_through_platform_core(self):
        before = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, before)
        import nonebot
        from nonebot.adapters.onebot.v11 import Adapter, Bot, PrivateMessageEvent
        from nonebot.message import handle_event

        nonebot.init(driver="~fastapi")
        driver = nonebot.get_driver()
        driver.register_adapter(Adapter)
        self.assertIsNotNone(nonebot.load_plugin("tianshu_nonebot.adapter_plugin"))
        host = importlib.import_module("tianshu_nonebot.adapter_plugin")
        self.assertTrue(Path(host.__file__).resolve().is_relative_to(COMPANION_ROOT))
        adapter = Adapter(driver)
        bot = Bot(adapter, "42")
        sends, attempts = [], []
        native = {"fail_next": False}

        async def login():
            return {"user_id": 42, "nickname": "synthetic"}

        async def send_private_msg(**kwargs):
            attempts.append(kwargs)
            if native["fail_next"]:
                native["fail_next"] = False
                raise TimeoutError("synthetic ambiguous SDK result")
            sends.append({"text": str(kwargs["message"]), "target": kwargs["user_id"]})
            return {"message_id": 88}

        bot.get_login_info = login
        bot.send_private_msg = send_private_msg
        adapter.bot_connect(bot)
        port = _port()
        server = uvicorn.Server(
            uvicorn.Config(
                nonebot.get_asgi(), host="127.0.0.1", port=port, log_level="error", log_config=None
            )
        )
        task = asyncio.create_task(server.serve())

        async def close_host():
            server.should_exit = True
            await asyncio.wait_for(task, 10)

        self.addAsyncCleanup(close_host)
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        self.assertTrue(server.started)

        class Host:
            def __init__(self):
                self.sends = sends
                self.attempts = attempts

            @property
            def fail_next(self):
                return native["fail_next"]

            @fail_next.setter
            def fail_next(self, value):
                native["fail_next"] = value

            async def emit(self, author, message_id):
                event = PrivateMessageEvent.model_validate(
                    {
                        "time": int(time.time()),
                        "self_id": 42,
                        "post_type": "message",
                        "sub_type": "friend",
                        "user_id": int(author),
                        "message_type": "private",
                        "message_id": int(message_id),
                        "message": [{"type": "text", "data": {"text": "hello"}}],
                        "original_message": [{"type": "text", "data": {"text": "hello"}}],
                        "raw_message": "hello",
                        "font": 0,
                        "sender": {"user_id": int(author)},
                        "to_me": True,
                    }
                )
                await handle_event(bot, event)

        row = await self._connect("nonebot", f"http://127.0.0.1:{port}", host.service.access_key)
        await self._assert_message_path(Host(), row)

    async def test_astrbot_host_through_platform_core(self):
        before = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, before)
        sys.path.insert(0, str(self.root))
        self.addCleanup(sys.path.remove, str(self.root))
        from astrbot.core.star.context import Context
        from astrbot.core.star.star_manager import PluginManager

        source = COMPANION_ROOT / "integrations" / "astrbot" / "astrbot_plugin_tianshu"
        target = self.root / "data" / "plugins" / "astrbot_plugin_tianshu"
        shutil.copytree(source, target)
        port = _port()
        config_dir = self.root / "data" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "astrbot_plugin_tianshu_config.json").write_text(
            json.dumps({"enabled": False, "adapter_port": port}), encoding="utf-8"
        )
        platforms = SimpleNamespace(platform_insts=[])
        context = Context(
            asyncio.Queue(),
            {"dashboard": {"username": "admin"}},
            None,
            None,
            platforms,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        manager = PluginManager(context, {})
        loaded, error = await manager.load(specified_dir_name="astrbot_plugin_tianshu")
        self.assertTrue(loaded, error)
        metadata = next(
            item
            for item in context.get_all_stars()
            if item.root_dir_name == "astrbot_plugin_tianshu"
        )
        plugin = metadata.star_cls
        await plugin.on_astrbot_loaded()
        for _ in range(100):
            if plugin._adapter.runner is not None:
                break
            await asyncio.sleep(0.05)
        self.assertIsNotNone(plugin._adapter.runner)
        sends, attempts = [], []
        native = {"fail_next": False}

        class Client:
            _wsr_api_clients = {"42": object()}

            async def call_action(self, _action, **_params):
                return {"user_id": 42, "nickname": "synthetic"}

            async def send_private_msg(self, **kwargs):
                attempts.append(kwargs)
                if native["fail_next"]:
                    native["fail_next"] = False
                    raise TimeoutError("synthetic ambiguous SDK result")
                sends.append(
                    {"text": kwargs["message"][0]["data"]["text"], "target": kwargs["user_id"]}
                )
                return {"message_id": 55}

        class SyntheticPlatform:
            def __init__(self):
                self.client = Client()

            def meta(self):
                return SimpleNamespace(name="aiocqhttp", id="host1")

            def get_client(self):
                return self.client

        platforms.platform_insts.append(SyntheticPlatform())

        async def close_host():
            await manager._terminate_plugin(metadata)
            await manager._unbind_plugin(metadata.name, metadata.module_path)
            from astrbot.core import db_helper, sp

            await sp.close()
            await db_helper.engine.dispose()

        self.addAsyncCleanup(close_host)

        class Host:
            def __init__(self):
                self.sends = sends
                self.attempts = attempts

            @property
            def fail_next(self):
                return native["fail_next"]

            @fail_next.setter
            def fail_next(self, value):
                native["fail_next"] = value

            async def emit(self, author, message_id):
                class Event:
                    stopped = False
                    message_obj = SimpleNamespace(
                        message_id=message_id,
                        raw_message={
                            "post_type": "message",
                            "message_type": "private",
                            "self_id": 42,
                            "user_id": int(author),
                            "message_id": int(message_id),
                            "time": int(time.time()),
                            "message": [{"type": "text", "data": {"text": "hello"}}],
                        },
                    )

                    def get_platform_name(self):
                        return "aiocqhttp"

                    def get_platform_id(self):
                        return "host1"

                    def get_self_id(self):
                        return "42"

                    def get_sender_id(self):
                        return author

                    def get_group_id(self):
                        return None

                    def stop_event(self):
                        self.stopped = True

                await plugin.on_message(Event())

        row = await self._connect(
            "astrbot", f"http://127.0.0.1:{port}", plugin._adapter.service.access_key
        )
        await self._assert_message_path(Host(), row)


if __name__ == "__main__":
    unittest.main()

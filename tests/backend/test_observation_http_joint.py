"""Three real HTTPS observation routes with a synthetic SDK account and no send."""

import asyncio
import ipaddress
import json
import os
import socket
import ssl
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import aiohttp
import uvicorn
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from fixtures import ENV
from services.platform.server import create_app
from services.platform.service import Platform
from test_bots import bot_settings

COMPANION = os.environ.get("TS_OBSERVATION_COMPANION")
MEMORY = os.environ.get("TS_OBSERVATION_MEMORY")
CONTRACTS = Path(os.environ.get("TS012_CONTRACT_DIR", ""))
if COMPANION and MEMORY:
    sys.path[:0] = [
        str(Path(COMPANION) / "src"),
        str(Path(COMPANION) / "integrations" / "shared"),
        str(Path(MEMORY) / "src"),
    ]
    from tianshu_adapter_rpc import AdapterService, PREFIX
    from tianshu_companion.app import build_runtime, create_app as companion_app
    from tianshu_memory.app import configured_app
    from tianshu_memory.contracts import Contracts
    from tianshu_memory.store import Store


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def certificate(root):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (
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
    cert_path, key_path = root / "ca.pem", root / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert_path), str(key_path))
    return cert_path, key_path, context


@unittest.skipUnless(
    COMPANION and MEMORY and CONTRACTS.is_dir(),
    "explicit product paths and published text contract required",
)
class ObservationHttpJointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ca, self.key, tls = certificate(self.root)
        environment = patch.dict(
            os.environ,
            {
                **ENV,
                "TS012_CONTRACT_DIR": str(CONTRACTS),
                "TS_OBS_PLATFORM_CORE": "synthetic-platform-companion-token-123456",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.platform_port, self.companion_port, self.memory_port, self.host_port = (
            port(),
            port(),
            port(),
            port(),
        )
        self.platform_url = f"https://127.0.0.1:{self.platform_port}"
        self.companion_url = f"https://127.0.0.1:{self.companion_port}"
        self.memory_url = f"https://127.0.0.1:{self.memory_port}"
        self.host_url = f"https://127.0.0.1:{self.host_port}"
        self.settings = bot_settings(str(self.root))
        self.settings["principals"]["memory"]["actions"].append("observation.verify")
        self.settings["principals"]["qqreader"] = {
            "kind": "service",
            "service": "companion",
            "token_env": "TS_OBS_QQ_CHECK",
            "actions": ["qq.admin.check"],
        }
        os.environ["TS_OBS_QQ_CHECK"] = "synthetic-companion-qq-check-123456"
        os.environ["TS_OBS_MEMORY_PROFILE"] = "synthetic-platform-profile-token-123456"
        self.settings["core"] = {
            "base_url": self.companion_url,
            "token_env": "TS_OBS_PLATFORM_CORE",
            "ca_file": str(self.ca),
        }
        self.settings["bot_adapter_self_service"] = {
            "directory": str(self.root / "adapters"),
            "allowed_cidrs": ["127.0.0.0/8"],
            "actors": [{"id": "actor:a", "label": "Synthetic actor"}],
        }
        self.settings["web_qq_profiles"] = {
            "base_url": self.memory_url,
            "token_env": "TS_OBS_MEMORY_PROFILE",
            "ca_file": str(self.ca),
        }
        self.platform = Platform(self.settings)
        self.addAsyncCleanup(self._close_platform)
        existing = self.platform.bots.create("qq-onebot-main", ["actor:a"])
        self.platform.bots.change(existing["connection_id"], "enable")
        self.existing_connection = existing["connection_id"]
        platform_http = create_app(self.platform)
        platform_http.cleanup_ctx.clear()
        self.platform_runner = web.AppRunner(platform_http, access_log=None)
        await self.platform_runner.setup()
        await web.TCPSite(
            self.platform_runner, "127.0.0.1", self.platform_port, ssl_context=tls
        ).start()
        self.addAsyncCleanup(self.platform_runner.cleanup)

        contracts = Contracts(CONTRACTS)
        self.store = Store(self.root / "memory.sqlite")
        self.store.migrate_profiles(self.root / "profiles.bak")
        contracts.load_sources()
        self.store.migrate_sources(self.root / "sources.bak", contracts)
        self.store.migrate_observations(self.root / "observations.bak")
        self.store.migrate_qq_aliases(self.root / "qq-aliases.bak")
        memory_config = self.root / "memory-config.json"
        memory_config.write_text(
            json.dumps(
                {
                    "contract_directory": str(CONTRACTS),
                    "database_path": str(self.root / "memory.sqlite"),
                    "mode": "local_fixture",
                    "callers": {
                        "companion": {
                            "token": "synthetic-companion-memory-token-123456",
                            "operations": ["observe_ingest", "observe_query"],
                        },
                        "platform_qq_profiles": {
                            "token": "synthetic-platform-profile-token-123456",
                            "operations": ["qq_profiles"],
                        },
                        "platform_qq_alias": {
                            "token": "synthetic-platform-alias-token-123456",
                            "operations": ["qq_alias"],
                        },
                    },
                    "observation_source": {
                        "verify_url": self.platform_url + "/internal/v2/observation-source/verify",
                        "verify_token": ENV["TS012_MEMORY"],
                        "ca_file": str(self.ca),
                    },
                }
            ),
            encoding="utf-8",
        )
        os.environ["TIANSHU_MEMORY_CONFIG"] = str(memory_config)
        self.memory_app = configured_app()
        self.assertIsNotNone(self.memory_app.state.memory.observations)
        self.memory_server = None
        self.memory_task = None

        companion_config = {
            "contracts_path": str(CONTRACTS),
            "database_path": str(self.root / "companion.sqlite"),
            "bot_observation_enabled": True,
            "callers": {
                "platform": {
                    "token_env": "TS_OBS_PLATFORM_CORE",
                    "issuer": "platform",
                    "origin_service": "platform_origin",
                }
            },
            "services": {
                "memory": {
                    "url": self.memory_url,
                    "token_env": "TS_OBS_COMPANION_MEMORY",
                    "ca_file": str(self.ca),
                },
                "platform_sender": {
                    "url": self.platform_url,
                    "token_env": "TS_OBS_PLATFORM_CORE",
                    "ca_file": str(self.ca),
                },
                "qq_admin": {
                    "url": self.platform_url,
                    "token_env": "TS_OBS_QQ_CHECK",
                    "ca_file": str(self.ca),
                },
            },
        }
        os.environ["TS_OBS_COMPANION_MEMORY"] = "synthetic-companion-memory-token-123456"
        core, incoming, clients, _ = build_runtime(companion_config)
        self.addAsyncCleanup(core.close)
        for client in clients:
            self.addAsyncCleanup(client.close)
        self.inbox = core.observations
        await self._start_uvicorn(
            companion_app(core, incoming),
            self.companion_port,
        )

        async def accounts():
            return [
                {"id": account, "platform": "qq", "label": "Synthetic bot"}
                for account in ("10001", "10002")
            ]

        self.sent = []

        async def send(*args):
            self.sent.append(args)
            return "receipt"

        self.host = AdapterService(self.root / "host.sqlite", "nonebot", accounts, send)
        self.addCleanup(self.host.close)

        async def host_rpc(request):
            status, result = await self.host.handle(
                request.path, request.headers.get("Authorization"), await request.read()
            )
            return web.json_response(result, status=status)

        host_http = web.Application()
        host_http.router.add_post(PREFIX + "/{tail:.*}", host_rpc)
        self.host_runner = web.AppRunner(host_http, access_log=None)
        await self.host_runner.setup()
        await web.TCPSite(self.host_runner, "127.0.0.1", self.host_port, ssl_context=tls).start()
        self.addAsyncCleanup(self.host_runner.cleanup)
        self.tls_client = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=ssl.create_default_context(cafile=str(self.ca))),
            trust_env=False,
        )
        self.addAsyncCleanup(self.tls_client.close)

    async def _start_uvicorn(self, app, listen_port):
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=listen_port,
                ssl_certfile=str(self.ca),
                ssl_keyfile=str(self.key),
                lifespan="off",
                timeout_graceful_shutdown=1,
                log_level="error",
                access_log=False,
                log_config=None,
            )
        )
        task = asyncio.create_task(server.serve())

        async def stop():
            if hasattr(self, "tls_client") and not self.tls_client.closed:
                await self.tls_client.close()
            server.should_exit = True
            server.force_exit = True
            await asyncio.wait_for(task, 10)

        self.addAsyncCleanup(stop)
        for _ in range(100):
            if server.started:
                return
            await asyncio.sleep(0.05)
        self.fail("TLS server did not start")

    async def _close_platform(self):
        self.platform.close()

    async def test_observation_v3_registers_qq_profiles_without_reply(self):
        admin = {"Authorization": "Bearer " + ENV["TS012_ADMIN"]}
        for account in ("10001", "10002"):
            config = {
                "adapter": "nonebot",
                "address": self.host_url,
                "access_key": self.host.access_key,
                "allow_private_http": False,
                "ca_pem": self.ca.read_text(encoding="utf-8"),
                "account_id": account,
                "name": "Synthetic observation",
            }
            async with self.tls_client.post(
                self.platform_url + "/internal/v2/observation-admin/enroll-default",
                json=config,
                headers=admin,
            ) as response:
                self.assertEqual(response.status, 200, await response.text())
                self.assertTrue((await response.json())["created"])
        events = [
            ("10001", "group:20002", "30003", "51", "同名", "甲群名片"),
            ("10001", "private:30003", "30003", "52", "同名", None),
            ("10002", "group:20004", "30003", "53", "同名", "乙群名片"),
            ("10002", "group:20004", "30004", "54", "同名", "同名片"),
        ]
        for bot, conversation, author, event_id, nickname, card in events[:1]:
            self.assertTrue(
                await self.host.capture_observation(
                    bot,
                    conversation,
                    author,
                    event_id,
                    "2026-09-29T01:00:00Z",
                    "observed only",
                    True,
                    identity_v3=True,
                    nickname=nickname,
                    group_card=card,
                )
            )
        await self.platform.bot_observation.pump_once()
        await self.inbox.flush()  # The real Memory HTTPS listener is deliberately offline.
        with closing(self.inbox._db()) as db:
            self.assertEqual(
                db.execute("SELECT archive_state FROM inbox").fetchone()[0], "pending_memory"
            )
        await self._start_uvicorn(self.memory_app, self.memory_port)
        with closing(self.inbox._db()) as db, db:
            db.execute("UPDATE inbox SET next_attempt_at=0")
        await self.inbox.flush()
        for bot, conversation, author, event_id, nickname, card in events[1:]:
            self.assertTrue(
                await self.host.capture_observation(
                    bot,
                    conversation,
                    author,
                    event_id,
                    "2026-09-29T01:00:01Z",
                    "observed only",
                    conversation.startswith("group:"),
                    identity_v3=True,
                    nickname=nickname,
                    group_card=card,
                )
            )
        await self.platform.bot_observation.pump_once()
        await self.inbox.flush()
        with closing(self.inbox._db()) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM inbox WHERE archive_state='archived'").fetchone()[
                    0
                ],
                4,
            )
        profiles = await self.platform.qq_admin._profiles({"limit": 10, "after": None})
        by_qq = {item["qq_id"]: item for item in profiles["items"]}
        self.assertEqual(set(by_qq), {"30003", "30004"})
        self.assertNotEqual(by_qq["30003"]["person_id"], by_qq["30004"]["person_id"])
        aliases = by_qq["30003"]["aliases"]
        self.assertEqual(
            {(a["bot_id"], a["group_id"]) for a in aliases if a["kind"] == "group_card"},
            {("10001", "20002"), ("10002", "20004")},
        )
        from tianshu_memory.store import Store as MemoryStore
        from tianshu_memory.qq_identity import profiles as memory_profiles

        self.assertEqual(
            len(memory_profiles(MemoryStore(self.store.path), limit=10, after=None)["items"]), 2
        )
        self.assertEqual(self.sent, [])

    async def test_offline_pause_recover_via_authenticated_https(self):
        admin = {"Authorization": "Bearer " + ENV["TS012_ADMIN"]}
        companion_header = {"Authorization": "Bearer " + os.environ["TS_OBS_PLATFORM_CORE"]}
        config = {
            "adapter": "nonebot",
            "address": self.host_url,
            "access_key": self.host.access_key,
            "allow_private_http": False,
            "ca_pem": self.ca.read_text(encoding="utf-8"),
            "account_id": "10001",
            "name": "Synthetic observation",
        }
        async with self.tls_client.post(
            self.platform_url + "/internal/v2/observation-admin/enroll-default",
            json=config,
            headers=admin,
        ) as response:
            self.assertEqual(response.status, 200, await response.text())
            enrollment = await response.json()
        self.assertTrue(enrollment["created"])
        self.assertEqual(enrollment["connection"]["group_policy"]["mode"], "observe_only")
        async with self.tls_client.post(
            self.platform_url + "/internal/v2/observation-admin/status", json={}, headers=admin
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(len((await response.json())["connections"]), 1)
        with closing(self.platform.bots._db()) as db:
            self.assertEqual(
                db.execute(
                    "SELECT enabled FROM connections WHERE id=?", (self.existing_connection,)
                ).fetchone()[0],
                1,
            )
        self.assertTrue(
            await self.host.capture_observation(
                "10001", "group:20002", "30003", "44", "2026-09-29T01:00:00Z", "hello", True
            )
        )
        self.assertFalse(await self.host.observation_claimed("10001", "group:20002", "30003", "44"))
        await self.platform.bot_observation.pump_once()
        with closing(self.inbox._db()) as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM inbox WHERE archive_state='pending_memory'"
                ).fetchone()[0],
                1,
            )
        await self.inbox.flush()  # Memory HTTPS listener is down.
        row = self.platform.bot_observation.get(enrollment["connection"]["id"])
        paused = dict(
            row,
            revision=row["revision"] + 1,
            host_revision=row["host_revision"] + 1,
            group_policy={**row["group_policy"], "observe": False},
        )
        self.platform.bot_observation.catalog.put(paused)
        await self._start_uvicorn(self.memory_app, self.memory_port)
        with closing(self.inbox._db()) as db, db:
            db.execute("UPDATE inbox SET next_attempt_at=0 WHERE archive_state='pending_memory'")
        await self.inbox.flush()
        with closing(self.inbox._db()) as db:
            self.assertEqual(
                db.execute("SELECT archive_state FROM inbox").fetchone()[0], "archived"
            )
        query = {
            "instance_id": self.host.instance_id,
            "self_id": "10001",
            "conversation_id": "group:20002",
            "limit": 20,
            "cursor": None,
            "archive_epoch": 1,
        }
        async with self.tls_client.post(
            self.companion_url + "/internal/v2/observations/query",
            json=query,
            headers=companion_header,
        ) as response:
            self.assertEqual(response.status, 200, await response.text())
            result = await response.json()
        self.assertEqual(result["archive_items"][0]["text"], "hello")
        # A new accepted event waits while Platform is saving policy. A 503
        # verifier answer must stay retryable, never become a revocation.
        self.assertTrue(
            await self.host.capture_observation(
                "10001", "group:20002", "30003", "45", "2026-09-29T01:00:01Z", "later", True
            )
        )
        await self.platform.bot_observation.pump_once()
        stable = self.platform.bot_observation.get(enrollment["connection"]["id"])
        saving = dict(stable, pending={"request_id": "saving", "enabled": True})
        self.platform.bot_observation.catalog.put(saving)
        await self.inbox.flush()
        with closing(self.inbox._db()) as db:
            state = db.execute(
                "SELECT archive_state,error_code FROM inbox WHERE event_id='45'"
            ).fetchone()
            self.assertEqual(state[0], "pending_memory")
            self.assertEqual(state[1], "dependency_unavailable")
        self.platform.bot_observation.catalog.put(stable)
        await self.platform.bot_observation.history(
            {
                "id": stable["id"],
                "expected_revision": stable["revision"],
                "read_enabled": False,
                "client_id": "revoke:synthetic",
            }
        )
        with closing(self.inbox._db()) as db, db:
            db.execute("UPDATE inbox SET next_attempt_at=0 WHERE event_id='45'")
        await self.inbox.flush()
        with closing(self.inbox._db()) as db:
            state = db.execute(
                "SELECT archive_state,error_code FROM inbox WHERE event_id='45'"
            ).fetchone()
            self.assertEqual(tuple(state), ("revoked", "scope_changed"))
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM inbox WHERE archive_state='pending_memory'"
                ).fetchone()[0],
                0,
            )
        revoked = self.platform.bot_observation.get(stable["id"])
        await self.platform.bot_observation.history(
            {
                "id": revoked["id"],
                "expected_revision": revoked["revision"],
                "read_enabled": True,
                "client_id": "resume:synthetic",
            }
        )
        query["archive_epoch"] = 2
        async with self.tls_client.post(
            self.companion_url + "/internal/v2/observations/query",
            json=query,
            headers=companion_header,
        ) as response:
            self.assertEqual(response.status, 200, await response.text())
            resumed = await response.json()
        self.assertEqual(resumed["archive_items"], [])  # Prior epoch stays hidden.
        self.assertEqual(resumed["items"], [])
        projected = await self.platform.bot_observation.archive(
            {
                "id": stable["id"],
                "conversation_id": "group:20002",
                "limit": 20,
                "cursor": None,
            }
        )
        self.assertEqual(projected["items"], [])
        self.assertEqual(projected["archive_items"], [])
        self.assertEqual(self.sent, [])
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM turn_inputs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)

"""Production transport shape using isolated account/catalog and contract responses, not a NAS."""

import hashlib
import json
import os
import socket
import ssl
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import aiohttp
from aiohttp import web

from fixtures import ENV
from infrastructure_fixture import domain_configuration
from make_tls_fixture import generate
from verify_infrastructure_deployment import verify
from web_fixtures import PASSWORD, web_settings
from services.platform.external_catalog import ExternalCatalog
from services.platform.server import create_app, create_console
from services.platform.service import Platform
from services.platform.transport import server_tls
from services.platform.web_account import WebAccount


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class InfrastructureDeploymentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ts182-public-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = patch.dict(
            os.environ,
            {
                **ENV,
                "TS182_TEST_USERNAME": "synthetic-admin",
                "TS182_TEST_PASSWORD": PASSWORD,
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        generate(self.root)
        self.cert = str(self.root / "localhost.pem")
        with socket.socket() as port:
            port.bind(("127.0.0.1", 0))
            public_port = port.getsockname()[1]
        self.origin = f"http://127.0.0.1:{public_port}"
        self.settings = web_settings(self.temp.name, origin="https://127.0.0.1:8443")
        self.settings.update(
            mode="service_https",
            tls={
                "certificate_file": self.cert,
                "private_key_file": str(self.root / "localhost-key.pem"),
            },
            web_access={
                "host": "127.0.0.1",
                "port": public_port,
                "certificates": {},
                "initial": {"mode": "http", "origin": self.origin, "certificate": None},
            },
            web_account={"mode": "claim"},
            infrastructure=domain_configuration(self.temp.name),
        )
        self.settings["principals"]["admin"]["actions"].append("external.manage")
        self.catalog = ExternalCatalog(self.root / "external", create=True)
        self.catalog.save(
            "weather",
            {"marker": "existing connection"},
            {"action": "replace", "value": "synthetic-peer-secret"},
            {"action": "clear"},
            0,
            str(uuid.uuid4()),
        )
        self.original_peer = self.catalog.snapshot()[1]["weather"]
        self.account = WebAccount(self.settings)
        self.account.create("synthetic-admin", self.settings["web"]["password_hash"])
        self.original_account = file_digest(self.account.path)
        self.original_seal = file_digest(self.account.seal)
        self.original_key = file_digest(self.catalog.key_file)
        self.original_actions = list(self.settings["principals"]["admin"]["actions"])
        (self.root / "mode.json").write_text(json.dumps({"containers": 201}))
        self.runners, self.platform = [], None
        self.addAsyncCleanup(self.stop)
        await self.start()

    async def start(self):
        self.platform = Platform(self.settings)
        self.console = create_console(self.platform)
        internal = web.AppRunner(create_app(self.platform, console=self.console), access_log=None)
        await internal.setup()
        self.runners.append(internal)
        site = web.TCPSite(internal, "127.0.0.1", 0, ssl_context=server_tls(self.settings["tls"]))
        await site.start()
        self.internal = "https://127.0.0.1:" + str(internal.addresses[0][1])
        public = web.AppRunner(
            create_app(self.platform, console=self.console, public=True), access_log=None
        )
        await public.setup()
        self.runners.append(public)
        await web.TCPSite(public, "127.0.0.1", self.settings["web_access"]["port"]).start()

    async def stop(self):
        for runner in reversed(self.runners):
            await runner.cleanup()
        self.runners.clear()
        if self.platform:
            self.platform.close()

    async def test_public_session_catalog_preservation_and_background_after_restart(self):
        context = ssl.create_default_context(cafile=self.cert)
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=context), trust_env=False
        ) as client:
            async with client.get(self.internal + "/health/live") as response:
                self.assertEqual(response.status, 200)
            async with client.get(self.internal + "/api/web/session") as response:
                self.assertEqual(response.status, 404)
            async with client.get(self.origin + "/health/live") as response:
                self.assertEqual(response.status, 404)
        settings = {
            "origin": self.origin,
            "node_id": "fixture-nas",
            "username_env": "TS182_TEST_USERNAME",
            "password_env": "TS182_TEST_PASSWORD",
            "register_if_missing": True,
            "timeout_seconds": 60,
            "log_container_id": f"{1:064x}",
        }
        report = await verify(settings)
        self.assertEqual(report["status"], "passed", report)
        self.assertEqual(report["inventory"]["count"], 201)
        self.assertEqual(report["inventory"]["pages"], 2)
        self.assertEqual(report["inventory"]["metrics_returned"], 201)
        self.assertEqual(
            report["inventory"]["tail"]["identity_sha256"],
            hashlib.sha256(f"{201:064x}".encode()).hexdigest(),
        )
        self.assertEqual(report["inventory"]["tail"]["cpu_state"], "ok")
        self.assertEqual(report["logs"]["state"], "observed")
        self.assertEqual(self.catalog.snapshot()[1]["weather"], self.original_peer)
        self.assertEqual(file_digest(self.catalog.key_file), self.original_key)
        self.assertEqual(file_digest(self.account.path), self.original_account)
        self.assertEqual(file_digest(self.account.seal), self.original_seal)
        self.assertEqual(self.settings["principals"]["admin"]["actions"], self.original_actions)
        encoded = json.dumps(report)
        for private in (
            PASSWORD,
            "synthetic-admin",
            "fixture-nas",
            "synthetic-peer-secret",
            self.origin,
        ):
            self.assertNotIn(private, encoded)
        await self.stop()
        await self.start()
        with patch.object(
            self.platform.infrastructure,
            "request",
            side_effect=AssertionError("No refresh expected"),
        ):
            report = await verify(
                {**settings, "phase": "after_restart", "register_if_missing": False}
            )
        self.assertEqual(report["status"], "passed", report)
        self.assertEqual(report["background_advances"], 2)
        self.assertEqual(report["logs"]["state"], "not_requested")
        self.assertEqual(self.catalog.snapshot()[1]["weather"], self.original_peer)
        self.assertEqual(file_digest(self.account.path), self.original_account)
        self.assertEqual(file_digest(self.catalog.key_file), self.original_key)


@unittest.skipUnless(
    os.environ.get("TS182_REQUIRE_INSTALLED"),
    "Installed wheel check requires TS182_REQUIRE_INSTALLED=1",
)
class InfrastructureInstalledWheelTests(unittest.TestCase):
    def test_installed_wheel_contains_infrastructure(self):
        import services.platform.infrastructure as infrastructure

        module = Path(infrastructure.__file__).resolve()
        self.assertIn("site-packages", module.parts, str(module))
        self.assertFalse(module.is_relative_to(Path(__file__).resolve().parents[2] / "services"))
        from services.platform.infrastructure.client import Executor
        from services.platform.infrastructure import supervisor

        self.assertTrue(callable(Executor.invoke))
        self.assertEqual(Path(supervisor.__file__).resolve().parent, module.parent)

"""Actual Platform HTTP -> fixed node runner -> OpenSSH -> loopback synthetic equipment."""

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import aiohttp

from fixtures import ENV, start_http
from infrastructure_fixture import domain_configuration
from test_infrastructure import eventually
from web_fixtures import PASSWORD, web_settings
from services.platform.external_catalog import ExternalCatalog
from services.platform.server import create_app
from services.platform.service import Platform
from services.platform.web_console import WebConsole


@unittest.skipUnless(
    os.environ.get("TS177_NODE_EXECUTOR_ROOT"),
    "Set TS177_NODE_EXECUTOR_ROOT to the fixed 172d20d export",
)
class InfrastructureJointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ts177-joint-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.export = Path(os.environ["TS177_NODE_EXECUTOR_ROOT"]).resolve()
        ready_path = self.root / "ready.json"
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper()
            in {"PATH", "SYSTEMROOT", "WINDIR", "PROGRAMDATA", "TEMP", "TMP", "LANG", "LC_ALL"}
        }
        environment.update(PYTHONPATH=str(self.export), PYTHONDONTWRITEBYTECODE="1")
        self.ssh = await asyncio.create_subprocess_exec(
            sys.executable,
            "-B",
            str(self.export / "tests/infrastructure/synthetic_ssh.py"),
            "--directory",
            str(self.root / "synthetic-ssh"),
            "--ready-file",
            str(ready_path),
            "--containers",
            "150",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.export,
            env=environment,
        )
        self.addAsyncCleanup(self.close_ssh)
        started = time.monotonic()
        while not ready_path.exists():
            if self.ssh.returncode is not None:
                self.fail(
                    "Synthetic SSH fixture exited before readiness: "
                    + (await self.ssh.stderr.read()).decode()
                )
            if time.monotonic() - started > 15:
                self.fail("Synthetic SSH fixture did not start")
            await asyncio.sleep(0.05)
        self.ready = json.loads(ready_path.read_text())
        env = patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)
        ExternalCatalog(self.root / "external", create=True)
        settings = web_settings(self.temp.name)
        settings["principals"]["admin"]["actions"].append("external.manage")
        settings["infrastructure"] = {
            **domain_configuration(self.temp.name, timeout_ms=30000),
            "contract_directory": str(self.export / "contracts/infrastructure-node/candidate-v1"),
            "executor": {
                "command": [
                    sys.executable,
                    "-B",
                    "-m",
                    "ops.infrastructure",
                    "--config",
                    self.ready["config_path"],
                ],
                "directory": str(self.export),
            },
            "poll_interval_seconds": 30,
            "stale_after_seconds": 120,
        }
        self.platform = Platform(settings)
        self.addCleanup(self.platform.close)
        self.console = WebConsole(self.platform)
        self.runner, self.url = await start_http(create_app(self.platform, console=self.console))
        self.addAsyncCleanup(self.runner.cleanup)
        self.console.config["origin"] = self.url
        self.client = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.addAsyncCleanup(self.client.close)
        async with self.client.get(self.url + "/api/web/session") as response:
            self.csrf = (await response.json())["csrf"]
        self.csrf = (
            await self.post("login", {"username": "synthetic-admin", "password": PASSWORD})
        )["csrf"]

    async def close_ssh(self):
        if self.ssh.returncode is None:
            self.ssh.stdin.close()
            try:
                await asyncio.wait_for(self.ssh.wait(), 5)
            except TimeoutError:
                self.ssh.kill()
                await self.ssh.wait()

    async def post(self, operation, body):
        async with self.client.post(
            self.url + "/api/web/" + operation,
            json=body,
            headers={"Origin": self.url, "X-CSRF-Token": self.csrf},
        ) as response:
            value = await response.json()
            self.assertEqual(response.status, 200, value)
            return value

    async def view(self):
        return await self.post("infrastructure/view", {})

    async def register(self, identity):
        view = await eventually(self.view, lambda value: bool(value["catalog"]["data"]), timeout=20)
        await self.post(
            "infrastructure/save",
            {
                "node_id": identity,
                "label": identity,
                "enabled": True,
                "expected_revision": view["revision"],
                "client_id": str(uuid.uuid4()),
            },
        )

    async def settle(self, identity):
        return await eventually(
            lambda: self.post("infrastructure/node", {"node_id": identity}),
            lambda value: bool(value["refresh"] and value["refresh"]["finished_at"]),
            timeout=95,
        )

    async def test_real_runner_ssh_150_container_pagination_openwrt_logs_and_offline_cache(self):
        await self.register("fixture-nas")
        nas = await self.settle("fixture-nas")
        self.assertEqual(nas["snapshot"]["status"], "ok", nas)
        self.assertEqual(nas["snapshot"]["data"]["identity"]["hostname"], "synthetic-node")
        listing = await self.post("infrastructure/containers", {"node_id": "fixture-nas"})
        self.assertEqual(listing["inventory_total"], 150, listing)
        self.assertEqual(len(listing["containers"]), 100)
        more = await self.post("infrastructure/containers", {"node_id": "fixture-nas", "page": 1})
        self.assertEqual(len(more["containers"]), 50)
        self.assertEqual(listing["inventory"]["completeness"]["state"], "complete")
        identity = self.ready["container_ids"][0]
        detail = await self.post(
            "infrastructure/container", {"node_id": "fixture-nas", "container_id": identity}
        )
        self.assertEqual(detail["container"]["health"], "none")
        self.assertEqual(detail["stats"]["memory"]["cache_adjusted_usage_bytes"], 9000)
        await self.post(
            "infrastructure/logs/refresh", {"node_id": "fixture-nas", "container_id": identity}
        )
        logs = await eventually(
            lambda: self.post(
                "infrastructure/logs", {"node_id": "fixture-nas", "container_id": identity}
            ),
            lambda value: value["state"] in {"observed", "failed"},
            timeout=35,
        )
        self.assertEqual(logs["state"], "observed", logs)
        self.assertIn("synthetic log", logs["data"]["stdout"])
        self.assertLessEqual(logs["data"]["returned_bytes"], 65536)
        await self.post("infrastructure/refresh", {"node_id": "fixture-nas"})
        sampled = await self.settle("fixture-nas")
        self.assertGreater(sampled["snapshot"]["data"]["cpu"]["usage_ratio"], 0)
        await self.register("fixture-router")
        router = await self.settle("fixture-router")
        self.assertEqual(
            router["snapshot"]["data"]["openwrt"]["board"]["model"], "Synthetic router"
        )
        self.assertEqual(router["snapshot"]["data"]["openwrt"]["interfaces"][0]["name"], "wan")
        self.assertEqual(
            router["snapshot"]["data"]["openwrt"]["per_device_traffic"]["state"], "unsupported"
        )
        await self.close_ssh()
        await self.post("infrastructure/refresh", {"node_id": "fixture-nas"})
        offline = await self.settle("fixture-nas")
        self.assertEqual(offline["connection_state"], "offline", offline)
        self.assertEqual(offline["snapshot"]["data"], sampled["snapshot"]["data"])
        self.assertEqual(offline["snapshot"]["observed_at"], sampled["snapshot"]["observed_at"])
        async with self.client.get(self.url + "/health/live") as response:
            self.assertEqual(response.status, 200)

"""Platform HTTP -> ControlClient -> OpenSSH -> relay -> remote/worker -> synthetic Docker.

Export TS-205 commit 0a6172463d2b1b8ae8521c80f13c3bcc92f1f635 to a private directory,
install its two existing infrastructure requirements in the test interpreter, and set
TS205_CONTROL_EXECUTOR_ROOT to that export. This test uses the existing canned read-only
observation fixture; every control projection and mutation comes from the actual node package.
It contacts only its own loopback SSH server, never a Docker daemon or registered equipment.
"""

import asyncio
import json
import os
import shutil
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
    os.environ.get("TS205_CONTROL_EXECUTOR_ROOT") and shutil.which("ssh"),
    "Set TS205_CONTROL_EXECUTOR_ROOT to the fixed 0a617246 export; OpenSSH is required",
)
class InfrastructureControlJointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ts206-control-joint-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.export = Path(os.environ["TS205_CONTROL_EXECUTOR_ROOT"]).resolve()
        self.fixture = self.root / "loopback-node"
        ready_path = self.root / "node-ready.json"
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper()
            in {"PATH", "SYSTEMROOT", "WINDIR", "PROGRAMDATA", "TEMP", "TMP", "LANG", "LC_ALL"}
        }
        environment.update(PYTHONPATH=str(self.export), PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
        self.ssh = await asyncio.create_subprocess_exec(
            sys.executable,
            "-B",
            str(self.export / "tests/infrastructure/control_ssh.py"),
            "--directory",
            str(self.fixture),
            "--ready-file",
            str(ready_path),
            cwd=self.export,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self.addAsyncCleanup(self.close_ssh)
        expires = time.monotonic() + 15
        while not ready_path.exists():
            if self.ssh.returncode is not None:
                self.fail(
                    "Loopback node fixture failed: "
                    + (await self.ssh.stderr.read()).decode("utf-8", errors="replace")[:1000]
                )
            if time.monotonic() >= expires:
                self.fail("Loopback node fixture did not become ready")
            await asyncio.sleep(0.05)
        self.ready = json.loads(ready_path.read_text(encoding="utf-8"))
        self.assertEqual(self.ready["scope"], "loopback_synthetic_docker")
        observation = json.loads(
            Path(self.ready["observation_config_path"]).read_text(encoding="utf-8")
        )
        self.assertTrue(all(row["host"] == "127.0.0.1" for row in observation["nodes"].values()))
        self.node, self.stack = self.ready["node_id"], self.ready["stack_id"]
        self.compose = self.fixture / "stacks/fixture-web/compose.yaml"
        self.dotenv = self.compose.with_name(".env")
        self.original_compose, self.original_env = (
            self.compose.read_bytes(),
            self.dotenv.read_bytes(),
        )
        self.data = self.compose.parent / "data"
        self.data.mkdir()
        (self.data / "keep.txt").write_text("synthetic durable data", encoding="utf-8")

        environment_patch = patch.dict(os.environ, ENV)
        environment_patch.start()
        self.addCleanup(environment_patch.stop)
        catalog = ExternalCatalog(self.root / "external", create=True)
        catalog.save(
            "infrastructure:" + self.node,
            {
                "node_id": self.node,
                "label": "实际控制链合成节点",
                "kind": "linux",
                "enabled": True,
            },
            {"action": "clear"},
            {"action": "clear"},
            0,
            str(uuid.uuid4()),
        )
        settings = web_settings(self.temp.name)
        settings["principals"]["admin"]["actions"].append("external.manage")
        settings["infrastructure"] = {
            **domain_configuration(self.temp.name, timeout_ms=30000),
            "poll_interval_seconds": 30,
            "stale_after_seconds": 120,
            "control": {
                "contract_directory": str(
                    self.export / "contracts/infrastructure-control/candidate-v1"
                ),
                "executor": {
                    "command": self.ready["control_command"],
                    "directory": str(self.export),
                },
            },
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
            await self.post(
                "login", {"username": "synthetic-admin", "password": PASSWORD}, prefix=""
            )
        )["csrf"]

    async def close_ssh(self):
        if self.ssh.returncode is None:
            self.ssh.stdin.close()
            try:
                await asyncio.wait_for(self.ssh.communicate(), 10)
            except TimeoutError:
                self.ssh.kill()
                await self.ssh.communicate()

    async def post(self, operation, body, *, prefix="infrastructure/control/"):
        async with self.client.post(
            self.url + "/api/web/" + prefix + operation,
            json=body,
            headers={"Origin": self.url, "X-CSRF-Token": self.csrf},
        ) as response:
            value = await response.json()
            self.assertEqual(response.status, 200, value)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            return value

    async def settled(self, identity):
        return (
            await eventually(
                lambda: self.post("operation", {"operation_id": identity}),
                lambda row: row["operation"]["settled"],
                timeout=45,
            )
        )["operation"]

    async def details(self, *, image=None, refresh=False):
        if refresh:
            read = await self.post(
                "detail", {"node_id": self.node, "stack_id": self.stack, "refresh": True}
            )
            await self.settled(read["refresh"]["operation_id"])
        return await eventually(
            lambda: self.post("detail", {"node_id": self.node, "stack_id": self.stack}),
            lambda row: (
                row["observation"]["freshness"] == "current"
                and row["stack"] is not None
                and (image is None or image in row["compose_content"])
            ),
            timeout=45,
        )

    def intent(self, **fields):
        return {
            "node_id": self.node,
            "expected_connection_revision": 1,
            "client_id": str(uuid.uuid4()),
            **fields,
        }

    def assert_no_secrets(self, value):
        raw = json.dumps(value, ensure_ascii=False)
        self.assertNotIn("synthetic-fixture-password", raw)
        self.assertNotIn("synthetic-fixture-dotenv", raw)

    def assert_execution_evidence(self, operation):
        self.assertEqual(operation["state"], "observed", operation)
        self.assertEqual(operation["acceptance"], "submitted")
        self.assertEqual(operation["result"]["command_state"], "succeeded")
        self.assertEqual(operation["result"]["runtime_state"], "running")
        self.assertEqual(operation["result"]["health_state"], "none")
        self.assertIs(operation["result"]["execution_active"], False)
        self.assert_no_secrets(operation)

    async def test_http_stack_replay_secret_preserving_deploy_and_version_restore(self):
        inventory = await eventually(
            lambda: self.post("view", {"node_id": self.node}),
            lambda row: row["observation"]["freshness"] == "current",
            timeout=45,
        )
        self.assertEqual(inventory["stacks"][0]["stack_id"], self.stack)
        before = await self.details()
        self.assertIn("{{tianshu.secret:", before["compose_content"])
        self.assert_no_secrets(before)

        intent = self.intent(
            action="stack.restart",
            stack_id=self.stack,
            expected_revision=before["stack"]["revision"],
        )
        accepted = await self.post("action", intent)
        replay = await self.post("action", intent)
        self.assertEqual(accepted["operation"]["operation_id"], replay["operation"]["operation_id"])
        restarted = await self.settled(accepted["operation"]["operation_id"])
        self.assert_execution_evidence(restarted)
        replay = await self.post("action", intent)
        self.assertEqual(replay["operation"], restarted)
        daemon = json.loads((self.fixture / "synthetic-daemon.json").read_text(encoding="utf-8"))
        self.assertEqual(daemon["calls"], [["compose.restart", "fixture-web"]])

        current = await self.details(refresh=True)
        edited = current["compose_content"].replace(
            "fixture-local:latest", "fixture-local:joint-v2"
        )
        planned = await self.post(
            "plan",
            self.intent(
                action="configure",
                stack_id=self.stack,
                expected_revision=current["stack"]["revision"],
                compose_content=edited,
            ),
        )
        plan = (await self.settled(planned["operation"]["operation_id"]))["result"]
        self.assertTrue(plan["executable"], plan)
        self.assert_no_secrets(plan)
        deployed = await self.post("execute", self.intent(plan_id=plan["plan_id"]))
        self.assert_execution_evidence(await self.settled(deployed["operation"]["operation_id"]))
        self.assertEqual(self.dotenv.read_bytes(), self.original_env)
        self.assertIn(b"synthetic-fixture-password", self.compose.read_bytes())
        deployed_detail = await self.details(image="fixture-local:joint-v2", refresh=True)
        self.assert_no_secrets(deployed_detail)
        # Select the complete ID returned by the producer for its recorded before version.
        version = next(
            row for row in deployed_detail["versions"] if row["version_id"].endswith(":before")
        )
        planned = await self.post(
            "plan",
            self.intent(
                action="restore",
                stack_id=self.stack,
                expected_revision=deployed_detail["stack"]["revision"],
                restore_version_id=version["version_id"],
            ),
        )
        restoration = (await self.settled(planned["operation"]["operation_id"]))["result"]
        self.assertTrue(restoration["executable"], restoration)
        restored = await self.post("execute", self.intent(plan_id=restoration["plan_id"]))
        final = await self.settled(restored["operation"]["operation_id"])
        self.assert_execution_evidence(final)
        self.assertEqual(self.compose.read_bytes(), self.original_compose)
        self.assertEqual(self.dotenv.read_bytes(), self.original_env)
        self.assertEqual(
            (self.data / "keep.txt").read_text(encoding="utf-8"), "synthetic durable data"
        )
        ledger = await self.post("operations", {"node_id": self.node})
        self.assertEqual(len([row for row in ledger["items"] if row["kind"] == "action"]), 1)
        self.assertEqual(len([row for row in ledger["items"] if row["kind"] == "execute"]), 2)
        self.assert_no_secrets(ledger)

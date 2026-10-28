import asyncio
import ctypes
import json
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import aiohttp

from fixtures import ENV, start_http
from infrastructure_fixture import domain_configuration
from web_fixtures import PASSWORD, web_settings
from services.platform.contracts import Fault
from services.platform.external_catalog import ExternalCatalog
from services.platform.infrastructure.client import Executor
from services.platform.infrastructure.config import configuration
from services.platform.infrastructure.process import ProcessTree
from services.platform.infrastructure.repository import Repository
from services.platform.infrastructure.runtime import Infrastructure
from services.platform.server import create_app
from services.platform.service import Platform
from services.platform.web_console import WebConsole


async def eventually(operation, predicate, timeout=12):
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        value = await operation()
        if predicate(value):
            return value
        await asyncio.sleep(0.05)
    raise AssertionError("Synthetic infrastructure operation did not settle")


class InfrastructureHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ts177-http-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)
        ExternalCatalog(self.root / "external", create=True)
        self.settings = web_settings(self.temp.name)
        self.settings["principals"]["admin"]["actions"].append("external.manage")
        self.settings["infrastructure"] = domain_configuration(self.temp.name)
        self.platform = Platform(self.settings)
        self.addCleanup(self.platform.close)
        self.console = WebConsole(self.platform)
        self.runner, self.url = await start_http(create_app(self.platform, console=self.console))
        self.console.config["origin"] = self.url
        self.client = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.addAsyncCleanup(self.client.close)
        self.addAsyncCleanup(self.runner.cleanup)
        async with self.client.get(self.url + "/api/web/session") as result:
            session = await result.json()
        self.csrf = session["csrf"]
        logged = await self.post("login", {"username": "synthetic-admin", "password": PASSWORD})
        self.csrf = logged["csrf"]

    async def post(self, path, body, expected=200, *, csrf=None):
        async with self.client.post(
            self.url + "/api/web/" + path,
            json=body,
            headers={"Origin": self.url, "X-CSRF-Token": self.csrf if csrf is None else csrf},
        ) as result:
            value = await result.json()
            self.assertEqual(result.status, expected, value)
            self.assertEqual(result.headers["Cache-Control"], "no-store")
            return value

    async def view(self):
        return await self.post("infrastructure/view", {})

    async def register(self, identity="fixture-nas"):
        view = await eventually(self.view, lambda value: bool(value["catalog"]["data"]))
        return await self.post(
            "infrastructure/save",
            {
                "node_id": identity,
                "label": "合成路由器" if identity == "fixture-router" else "合成 NAS",
                "enabled": True,
                "expected_revision": view["revision"],
                "client_id": str(uuid.uuid4()),
            },
        )

    async def settled(self, identity="fixture-nas"):
        return await eventually(
            lambda: self.post("infrastructure/node", {"node_id": identity}),
            lambda value: bool(value["refresh"] and value["refresh"]["finished_at"]),
        )

    def mode(self, **values):
        (self.root / "mode.json").write_text(json.dumps(values))

    async def test_real_platform_http_catalog_save_and_background_more_than_64_containers(self):
        self.assertIsNone(self.settings.get("web_external"))
        self.assertIsNone(self.settings.get("home"))
        await self.register()
        node = await self.settled()
        self.assertEqual(node["connection_state"], "current")
        self.assertIsNone(node["snapshot"]["data"]["cpu"]["usage_ratio"])
        first = await self.post("infrastructure/containers", {"node_id": "fixture-nas"})
        self.assertEqual(first["inventory_total"], 130)
        self.assertEqual(len(first["containers"]), 100)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["containers"][0]["health"], "none")
        second = await self.post("infrastructure/containers", {"node_id": "fixture-nas", "page": 1})
        self.assertEqual(len(second["containers"]), 30)
        self.assertFalse(
            {row["id"] for row in first["containers"]} & {row["id"] for row in second["containers"]}
        )
        filtered = await self.post(
            "infrastructure/containers", {"node_id": "fixture-nas", "health": "unhealthy"}
        )
        self.assertEqual(filtered["containers"][0]["state"], "running")
        self.assertEqual(filtered["total"], 1)
        restarted = Infrastructure(self.settings, local=self.platform.local_work.run)
        self.assertEqual(
            restarted.node("fixture-nas")["snapshot"]["data"], node["snapshot"]["data"]
        )

    async def test_continuous_samples_and_container_cpu_and_rates_are_not_fake_zero(self):
        await self.register()
        await self.settled()
        await self.post("infrastructure/refresh", {"node_id": "fixture-nas"})
        node = await self.settled()
        self.assertEqual(node["snapshot"]["data"]["cpu"]["usage_ratio"], 0.125)
        detail = await self.post(
            "infrastructure/container", {"node_id": "fixture-nas", "container_id": f"{1:064x}"}
        )
        self.assertEqual(detail["stats"]["cpu"]["usage_ratio"], 1.5)
        self.assertGreater(detail["stats"]["rates"]["receive_bytes_per_second"], 0)
        self.assertEqual(detail["stats"]["memory"]["raw_usage_bytes"], 10485760)
        old_count = int((self.root / "fixture-nas-snapshot.count").read_text())
        await eventually(
            lambda: asyncio.to_thread(
                lambda: int((self.root / "fixture-nas-snapshot.count").read_text())
            ),
            lambda count: count > old_count,
            timeout=8,
        )

    async def test_offline_preserves_old_readings_and_does_not_fail_platform_liveness(self):
        await self.register()
        before = await self.settled()
        self.mode(offline=True)
        await self.post("infrastructure/refresh", {"node_id": "fixture-nas"})
        after = await self.settled()
        self.assertEqual(after["connection_state"], "offline")
        self.assertEqual(after["snapshot"]["freshness"], "stale")
        self.assertEqual(after["snapshot"]["observed_at"], before["snapshot"]["observed_at"])
        self.assertEqual(after["snapshot"]["data"], before["snapshot"]["data"])
        self.assertIn("ssh_unavailable", after["snapshot"]["reason_codes"])
        async with self.client.get(self.url + "/health/live") as response:
            self.assertEqual(response.status, 200)

    async def test_openwrt_interfaces_and_per_device_limit_are_distinct(self):
        await self.register("fixture-router")
        node = await self.settled("fixture-router")
        router = node["snapshot"]["data"]["openwrt"]
        self.assertEqual(router["interfaces"][0]["name"], "wan")
        self.assertEqual(router["per_device_traffic"]["state"], "unsupported")
        self.assertEqual(node["compose"]["status"], "unsupported")

    async def test_save_cas_idempotency_and_arbitrary_node_not_allowed(self):
        await self.register()
        await self.settled()
        view = await self.view()
        body = {
            "node_id": "fixture-nas",
            "label": "first",
            "enabled": True,
            "expected_revision": view["revision"],
            "client_id": str(uuid.uuid4()),
        }

        async def save(value):
            async with self.client.post(
                self.url + "/api/web/infrastructure/save",
                json=value,
                headers={"Origin": self.url, "X-CSRF-Token": self.csrf},
            ) as response:
                return response.status, await response.json(), value

        results = await asyncio.gather(
            save(body), save({**body, "label": "second", "client_id": str(uuid.uuid4())})
        )
        self.assertEqual(sorted(row[0] for row in results), [200, 409])
        winner = next(row for row in results if row[0] == 200)
        replay = await self.post("infrastructure/save", winner[2])
        self.assertEqual(replay["revision"], winner[1]["revision"])
        view = await self.view()
        rejected = await self.post(
            "infrastructure/save",
            {
                **body,
                "node_id": "unregistered-host",
                "expected_revision": view["revision"],
                "client_id": str(uuid.uuid4()),
            },
            expected=409,
        )
        self.assertEqual(rejected["code"], "infrastructure_node_not_in_catalog")

    async def test_short_refresh_disable_cancels_and_discards_late_sample(self):
        await self.register()
        await self.settled()
        self.mode(delay=1)
        started = time.monotonic()
        await self.post("infrastructure/test", {"node_id": "fixture-nas"})
        self.assertLess(time.monotonic() - started, 0.8)
        await asyncio.sleep(0.15)
        view = await self.view()
        await self.post(
            "infrastructure/save",
            {
                "node_id": "fixture-nas",
                "label": "paused",
                "enabled": False,
                "expected_revision": view["revision"],
                "client_id": str(uuid.uuid4()),
            },
        )
        node = await self.post("infrastructure/node", {"node_id": "fixture-nas"})
        self.assertEqual(node["connection_state"], "disabled")
        self.assertIsNone(node["snapshot"]["data"])
        self.assertNotIn("fixture-nas", self.platform.infrastructure.jobs)
        await self.post("infrastructure/refresh", {"node_id": "fixture-nas"}, expected=409)

    async def test_log_job_uses_exact_container_and_limited_preview(self):
        await self.register()
        await self.settled()
        request = {"node_id": "fixture-nas", "container_id": f"{1:064x}"}
        response = await self.post("infrastructure/logs/refresh", request)
        self.assertIn(response["state"], {"queued", "in_progress"})
        result = await eventually(
            lambda: self.post("infrastructure/logs", request),
            lambda value: value["state"] == "observed",
        )
        self.assertEqual(result["data"]["tail"], 200)
        self.assertEqual(result["data"]["max_bytes"], 65536)
        self.assertEqual(result["data"]["returned_bytes"], len(result["data"]["stdout"].encode()))
        self.assertIn("合成容器日志", result["data"]["stdout"])
        await self.post(
            "infrastructure/logs/refresh", {**request, "container_id": "short"}, expected=400
        )
        await self.post(
            "infrastructure/logs/refresh", {**request, "container_id": f"{999:064x}"}, expected=404
        )

    async def test_platform_host_uses_explicit_registered_node_and_existing_gate(self):
        self.assertEqual(
            await self.post("infrastructure/host", {}), {"configured": False, "node": None}
        )
        await self.register()
        await self.settled()
        self.platform.infrastructure.config["platform_node_id"] = "fixture-nas"
        result = await self.post("infrastructure/host", {})
        self.assertTrue(result["configured"])
        self.assertEqual(result["node"]["id"], "fixture-nas")
        self.assertIsNotNone(result["node"]["snapshot"]["data"])
        await self.post("infrastructure/host", {}, expected=403, csrf="wrong-token")
        await self.post("infrastructure/host", {"node_id": "fixture-router"}, expected=400)
        self.platform.auth.principals["admin"]["actions"].remove("external.manage")
        await self.post("infrastructure/host", {}, expected=403)

    async def test_csrf_admin_and_session_revocation_remain_real_http_requirements(self):
        await self.post("infrastructure/view", {}, expected=403, csrf="wrong-token")
        self.platform.auth.principals["admin"]["actions"].remove("external.manage")
        await self.post("infrastructure/view", {}, expected=403)
        self.platform.auth.principals["admin"]["actions"].append("external.manage")
        await self.post("logout", {})
        await self.post("infrastructure/view", {}, expected=401)

    async def test_partial_inventory_keeps_explicit_completeness(self):
        self.mode(partial=True)
        await self.register()
        await self.settled()
        result = await self.post("infrastructure/containers", {"node_id": "fixture-nas"})
        self.assertEqual(result["inventory"]["completeness"]["state"], "partial")
        self.assertEqual(result["inventory_total"], 130)

    async def test_stats_partial_first_batch_keeps_earliest_observation(self):
        self.mode(containers=201)
        await self.register()
        await self.settled()
        result = await self.post("infrastructure/containers", {"node_id": "fixture-nas"})
        self.assertEqual(result["inventory_total"], 201)
        self.assertEqual(result["metrics"]["status"], "partial")
        self.assertEqual(result["metrics"]["completeness"]["state"], "partial")
        self.assertEqual(result["metrics"]["completeness"]["returned"], 201)
        self.assertIn("first_sample", result["metrics"]["reason_codes"])
        calls = [json.loads(line) for line in (self.root / "calls.jsonl").read_text().splitlines()]
        batches = [call for call in calls if call["operation"] == "docker.stats"]
        self.assertEqual(len(batches), 2)
        self.assertEqual(result["metrics"]["observed_at"], batches[0]["time"])
        self.assertLess(batches[0]["time"], batches[1]["time"])


class InfrastructureExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ts177-process-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        ExternalCatalog(self.root / "external", create=True)
        self.config = domain_configuration(self.temp.name, timeout_ms=500)
        self.executor = Executor(self.config)
        self.addAsyncCleanup(self.executor.close)

    async def test_fixed_contract_response_failure_and_identity_validation(self):
        (self.root / "mode.json").write_text(json.dumps({"offline": True}))
        result = await self.executor.invoke("fixture-nas", "node.snapshot")
        self.assertEqual(result["status"], "unavailable")
        (self.root / "mode.json").write_text(json.dumps({"wrong_node": True}))
        with self.assertRaises(Fault) as error:
            await self.executor.invoke("fixture-nas", "node.snapshot")
        self.assertEqual(error.exception.code, "infrastructure_response_invalid")
        with self.assertRaises(Fault):
            await self.executor.invoke("fixture-nas", "node.snapshot", {"command": "arbitrary"})

    async def test_malformed_and_over_limit_output_are_rejected(self):
        for mode, code in (
            ("invalid_json", "infrastructure_response_invalid"),
            ("oversize", "infrastructure_output_exceeded"),
        ):
            with self.subTest(mode=mode):
                (self.root / "mode.json").write_text(json.dumps({mode: True}))
                with self.assertRaises(Fault) as error:
                    await self.executor.invoke("fixture-nas", "node.snapshot")
                self.assertEqual(error.exception.code, code)
                self.assertFalse(self.executor.processes)

    async def test_timeout_kills_executor_and_ssh_style_descendant(self):
        (self.root / "mode.json").write_text(json.dumps({"spawn_child": True}))
        with self.assertRaises(Fault) as error:
            await self.executor.invoke("fixture-nas", "node.snapshot")
        self.assertEqual(error.exception.code, "infrastructure_executor_timeout")
        self.assertFalse(self.executor.processes)
        pid = int((self.root / "child.pid").read_text())
        if os.name == "nt":
            kernel = ctypes.windll.kernel32
            handle = kernel.OpenProcess(0x1000, False, pid)
            if handle:
                code = ctypes.c_ulong()
                kernel.GetExitCodeProcess(handle, ctypes.byref(code))
                kernel.CloseHandle(handle)
                self.assertNotEqual(code.value, 259)
        else:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    async def test_disabled_configuration_has_no_store_or_executor(self):
        self.assertIsNone(configuration({}))
        runtime = Infrastructure({}, local=asyncio.to_thread)
        self.assertFalse(runtime.view()["configured"])
        await runtime.start()
        self.assertIsNone(runtime.pump)
        await runtime.close()

    async def test_retired_revision_cannot_replace_a_newer_cache_commit(self):
        repository = Repository(self.root / "observations.sqlite")
        new = {"status": "ok", "data": {"marker": "current connection"}}
        old = {"status": "ok", "data": {"marker": "retired connection"}}
        repository.record("fixture-nas", "node.snapshot", 2, new)
        repository.record("fixture-nas", "node.snapshot", 1, old)
        saved = repository.read("fixture-nas", "node.snapshot", 2)
        self.assertEqual(saved["latest"], new)
        self.assertEqual(saved["sample"], new)
        self.assertIsNone(repository.read("fixture-nas", "node.snapshot", 1))

    @unittest.skipUnless(os.name == "nt", "Windows job lifetime regression")
    async def test_windows_descendant_is_killed_after_executor_exits_first(self):
        (self.root / "mode.json").write_text(json.dumps({"spawn_child": True, "exit_parent": True}))
        with self.assertRaises(Fault) as error:
            await self.executor.invoke("fixture-nas", "node.snapshot")
        self.assertIn(
            error.exception.code,
            {"infrastructure_executor_timeout", "infrastructure_response_invalid"},
        )
        pid = int((self.root / "child.pid").read_text())
        kernel = ctypes.windll.kernel32
        for _ in range(20):
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return
            code = ctypes.c_ulong()
            kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            kernel.CloseHandle(handle)
            if code.value != 259:
                return
            await asyncio.sleep(0.05)
        self.fail("Synthetic descendant remained alive after executor retirement")

    @unittest.skipUnless(os.name == "nt", "Windows suspended launch failure")
    async def test_windows_guard_failure_retires_root_before_executor_runs(self):
        started = []

        def reject_resume(tree):
            started.append(tree.pid)
            raise OSError("Synthetic thread resume failure")

        with patch.object(ProcessTree, "resume", reject_resume):
            with self.assertRaises(Fault) as error:
                await self.executor.invoke("fixture-nas", "node.snapshot")
        self.assertEqual(error.exception.code, "infrastructure_executor_unavailable")
        self.assertFalse((self.root / "calls.jsonl").exists())
        self.assertFalse(self.executor.processes)
        self.assertEqual(len(started), 1)
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, started[0])
        if handle:
            try:
                code = wintypes.DWORD()
                self.assertTrue(kernel.GetExitCodeProcess(handle, ctypes.byref(code)))
                self.assertNotEqual(code.value, 259)
            finally:
                kernel.CloseHandle(handle)

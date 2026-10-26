"""Actual plugin workers, durable role registrations, and role-bound action receipts."""

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from aiohttp import web

from fixtures import ENV, bearer, start_http
from services.platform.actor_capabilities import ActorCapabilities
from services.platform.contracts import Fault
from services.platform.plugins import PluginManager
from services.platform.role_runtime import RoleRuntime
from services.platform.service import Platform
from test_plugins import CODE, package
from web_fixtures import web_settings


EXTRA_CODE = """
    @action(id="no-effect", title="No effect", input_schema=EMPTY, output_schema=ANY, effect="write")
    async def no_effect(self, call, args):
        return ActionResult({"reason":"already-current"}, "not_applied")
    @action(id="inspect-job", title="Inspect job", input_schema={"type":"object"}, output_schema=ANY, required_capabilities=(("jobs.manage",1),))
    async def inspect_job(self, call, args):
        return await call.ctx.capability("jobs.manage").call("get", {"invocation_id":args["id"]})
    @action(id="late-write", title="Late write", input_schema=EMPTY, output_schema=ANY, effect="write", required_capabilities=(("state.kv",1),))
    async def late_write(self, call, args):
        await call.checkpoint({"step":"before-write"})
        await asyncio.sleep(.25)
        result = await call.ctx.state.compare_and_set("late", 1, expected_revision=None)
        return ActionResult(result.value, "applied")
    @action(id="oversized", title="Large result", input_schema=EMPTY, output_schema=ANY)
    async def oversized(self, call, args):
        return "x" * (1024 * 1024 - 256)
"""


def seed_roles(platform):
    """Seed synthetic confirmed peer registrations in the real isolated RoleRuntime store."""
    if platform.role_runtime.config is None:
        platform.settings["role_runtime"] = {
            "enabled": True,
            "memory": {"base_url": "https://memory.invalid", "token_env": "TS310_ROLE_MEMORY"},
        }
        platform.settings.setdefault(
            "core",
            {
                "base_url": "https://companion.invalid",
                "token_env": "TS310_ROLE_COMPANION",
            },
        )
        catalog = platform.provider_catalog
        platform.provider_catalog = object()
        try:
            platform.role_runtime = RoleRuntime(platform)
        finally:
            platform.provider_catalog = catalog
    for actor, name in [("actor:a", "合成角色甲"), ("actor:b", "合成角色乙")]:
        platform.role_runtime._put(
            {
                "actor_id": actor,
                "name": name,
                "version": 7,
                "companion_version": 19,
                "memory_version": 23,
                "enabled": True,
                "state": "active",
                "stage": "complete",
                "capabilities": [],
                "provider_id": None,
                "provider_revision": None,
            }
        )


class ActorFixture:
    async def prepare(self, *, postgres=None):
        self.temp = tempfile.TemporaryDirectory(prefix="ts310-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        environment = patch.dict(os.environ, ENV)
        environment.start()
        self.addCleanup(environment.stop)
        self.settings = web_settings(self.temp.name)
        self.settings["principals"]["companion"]["actions"].append("actor.capabilities")
        if postgres:
            self.settings["storage_backends"] = {
                "postgresql_url": postgres,
                "namespace": "actor_caps_" + uuid.uuid4().hex,
            }
        self.platform = Platform(self.settings)
        seed_roles(self.platform)
        self.service = self.platform.actor_capabilities
        self.host = self.platform.plugins
        await self.host.start()
        self.addAsyncCleanup(self.cleanup_platform)
        source = package(self.root / "source", code=CODE + EXTRA_CODE)
        operation = await self.host.install({"type": "directory", "path": str(source)}, "install")
        for _ in range(500):
            operation = self.host.repository.get("operations", operation["id"])
            if operation["execution"] not in {"accepted", "running"}:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(operation["execution"], "succeeded", operation)
        instance = await self.host.create(operation["package_id"], "Actor fixture", "instance")
        instance = await self.host.state(instance["id"], True, instance["revision"])
        self.instance = instance
        self.bindings = [self.binding("actor:a"), self.binding("actor:b")]
        await self.save_bindings()

    async def cleanup_platform(self):
        await self.host.close()
        self.platform.close()
        # The production close is non-blocking; let cancelled media fixture reads
        # release SQLite handles before TemporaryDirectory removes them on Windows.
        await asyncio.to_thread(self.platform.local_work.executor.shutdown, wait=True)

    def binding(self, actor, actions=None):
        return {
            "kind": "binding",
            "schema_version": 1,
            "binding_id": str(uuid.uuid4()),
            "instance_id": self.instance["id"],
            "revision": 1,
            "enabled": True,
            "scope": {"kind": "character", "character_id": actor},
            "overrides": {},
            "priority": 0,
            "entries": [
                {"kind": "action", "id": item}
                for item in (
                    actions
                    or [
                        "echo",
                        "increment",
                        "child",
                        "network",
                        "wait",
                        "crash",
                        "bad-output",
                        "no-effect",
                        "inspect-job",
                        "late-write",
                        "oversized",
                    ]
                )
            ],
        }

    async def save_bindings(self):
        current = await self.host.instance(self.instance["id"])
        self.instance = await self.host.bindings(current["id"], self.bindings, current["revision"])

    def request(self, operation, actor="actor:a", **fields):
        body = {
            "schema_version": 1,
            "request_id": "request:" + uuid.uuid4().hex,
            "operation": operation,
            "actor_id": actor,
            **fields,
        }
        if operation != "discover":
            body["role_version"] = self.platform.role_runtime.get(actor)["version"]
        return body

    def invoke_request(self, action="increment", actor="actor:a", intent="one", arguments=None):
        binding = next(row for row in self.bindings if row["scope"]["character_id"] == actor)
        return self.request(
            "invoke",
            actor,
            binding={
                "binding_id": binding["binding_id"],
                "binding_revision": self.instance["revision"],
                "instance_id": self.instance["id"],
                "action_id": action,
            },
            intent_key=intent,
            arguments=arguments or {},
        )

    async def operate(self, request, header=None, **kwargs):
        return await self.service.operate(header or bearer("COMPANION"), request, **kwargs)

    async def settled(self, invocation):
        for _ in range(500):
            value = self.host.repository.call(invocation)
            if value["execution"] not in {"accepted", "running"}:
                return value
            await asyncio.sleep(0.01)
        self.fail("Invocation did not settle")

    async def invoke(self, action="increment", **kwargs):
        receipt = await self.operate(self.invoke_request(action, **kwargs))
        await self.settled(receipt["invocation_id"])
        return await self.operate(
            self.request(
                "get", kwargs.get("actor", "actor:a"), invocation_id=receipt["invocation_id"]
            )
        )


class ActorCapabilityTests(ActorFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self.prepare()

    async def test_discovery_bootstraps_platform_version_and_filters_explicit_actions(self):
        self.bindings[0]["entries"] = [{"kind": "action", "id": "increment"}]
        await self.save_bindings()
        value = await self.operate(self.request("discover"))
        self.assertEqual(value["role_version"], 7)
        self.assertEqual(
            [item["binding"]["action_id"] for item in value["capabilities"]], ["increment"]
        )
        self.assertEqual(
            value["capabilities"][0]["binding"]["binding_revision"], self.instance["revision"]
        )
        self.assertEqual(value["capabilities"][0]["availability"], "ready")
        del self.bindings[0]["entries"]
        await self.save_bindings()
        self.assertEqual((await self.operate(self.request("discover")))["capabilities"], [])
        with self.assertRaises(Fault):
            await self.operate(self.invoke_request())

    async def test_foreground_background_retry_uses_one_intent_and_actor_ownership(self):
        one = self.invoke_request(intent="shared-origin-intent")
        two = {**one, "request_id": "background-retry"}
        a, b = await asyncio.gather(self.operate(one), self.operate(two))
        self.assertEqual(a["invocation_id"], b["invocation_id"])
        await self.settled(a["invocation_id"])
        other = await self.invoke(actor="actor:b", intent="shared-origin-intent")
        self.assertNotEqual(a["invocation_id"], other["invocation_id"])
        self.assertEqual(
            self.host.repository.state(self.instance["id"], "get", {"key": "count"})["value"], 2
        )
        for operation in ("get", "reconcile", "cancel"):
            with self.assertRaises(Fault) as caught:
                await self.operate(
                    self.request(operation, "actor:b", invocation_id=a["invocation_id"])
                )
            self.assertEqual(caught.exception.code, "not_found")
        mismatch = self.invoke_request("echo", intent="echo", arguments={"value": 1})
        await self.operate(mismatch)
        mismatch["arguments"] = {"value": 2}
        with self.assertRaises(Fault) as caught:
            await self.operate(mismatch)
        self.assertEqual(caught.exception.code, "idempotency_conflict")

    async def test_role_binding_instance_and_service_identity_are_not_caller_assertions(self):
        valid = self.invoke_request()
        for field, value in [("role_version", 19), ("actor_id", "actor:unknown")]:
            with self.assertRaises(Fault):
                await self.operate({**valid, field: value})
        for field, value in [
            ("binding_revision", 1),
            ("binding_id", self.bindings[1]["binding_id"]),
        ]:
            with self.assertRaises(Fault):
                await self.operate({**valid, "binding": {**valid["binding"], field: value}})
        for caller in ("ADMIN", "MEMORY", "CONNECTOR"):
            with self.assertRaises(Fault):
                await self.operate(valid, bearer(caller))
        self.assertEqual(self.host.repository.calls(), [])

    async def test_child_action_cannot_escape_parent_binding_and_job_read_cannot_cross_actor(self):
        self.bindings[0]["entries"] = [
            {"kind": "action", "id": "child"},
            {"kind": "action", "id": "inspect-job"},
        ]
        await self.save_bindings()
        value = await self.invoke("child")
        self.assertEqual(value["execution"], "failed")
        self.assertIsNone(self.host.repository.state(self.instance["id"], "get", {"key": "count"}))
        other = await self.invoke("increment", actor="actor:b")
        value = await self.invoke("inspect-job", arguments={"id": other["invocation_id"]})
        self.assertEqual(value["execution"], "failed")
        self.assertIsNone(value["result"])

    async def test_revoking_role_before_queued_execution_or_privileged_callback_prevents_write(
        self,
    ):
        lock = self.host.locks[self.instance["id"]]
        await lock.acquire()
        try:
            receipt = await self.operate(self.invoke_request())
            row = self.platform.role_runtime.get("actor:a")
            self.platform.role_runtime._put({**row, "enabled": False})
        finally:
            lock.release()
        value = await self.settled(receipt["invocation_id"])
        self.assertEqual(value["execution"], "failed")
        self.assertEqual(value["effect"], "not_applied")
        self.assertIsNone(self.host.repository.state(self.instance["id"], "get", {"key": "count"}))
        self.platform.role_runtime._put(row)
        receipt = await self.operate(self.invoke_request("late-write"))
        for _ in range(200):
            if self.host.repository.call(receipt["invocation_id"])["checkpoint"]:
                break
            await asyncio.sleep(0.01)
        self.platform.role_runtime._put({**row, "enabled": False})
        value = await self.settled(receipt["invocation_id"])
        self.assertEqual(value["execution"], "failed")
        self.assertIsNone(self.host.repository.state(self.instance["id"], "get", {"key": "late"}))

    async def test_receipt_keeps_not_applied_and_hides_internal_fields_and_credentials(self):
        value = await self.invoke("no-effect")
        self.assertEqual((value["execution"], value["effect"]), ("succeeded", "not_applied"))
        self.assertTrue(
            set(value).isdisjoint(
                {"snapshot", "config", "principal", "input", "checkpoint", "external"}
            )
        )
        instance = await self.host.configure(
            self.instance["id"], {}, {"api_key": "sensitive-value"}, self.instance["revision"]
        )
        self.instance = instance
        value = await self.invoke("echo", arguments={"text": "sensitive-value"}, intent="secret")
        self.assertNotIn("sensitive-value", json.dumps(value))

    async def test_result_schema_failure_and_response_budget_are_not_successful_content(self):
        value = await self.invoke("bad-output")
        self.assertEqual(value["execution"], "failed")
        self.assertIsNone(value["result"])
        receipt = await self.operate(self.invoke_request("oversized"))
        document = await self.settled(receipt["invocation_id"])
        # The worker frame fits; adding the actor receipt exceeds the wire budget.
        self.assertEqual(document["execution"], "succeeded")
        with self.assertRaises(Fault) as caught:
            await self.operate(self.request("get", invocation_id=receipt["invocation_id"]))
        self.assertEqual(caught.exception.code, "capacity_exceeded")

    async def test_unavailable_instance_or_remote_schema_never_reports_ready(self):
        await self.host.state(self.instance["id"], False, self.instance["revision"])
        value = await self.operate(self.request("discover"))
        self.assertTrue(
            all(item["availability"] == "unavailable" for item in value["capabilities"])
        )
        current = await self.host.instance(self.instance["id"])
        self.instance = await self.host.state(current["id"], True, current["revision"])
        key = self.instance["package_id"] + "@" + self.instance["version"]
        document = self.host.repository.get("packages", key)
        document["description"]["actions"][0]["input_schema"] = {
            "$ref": "https://never-fetch.invalid/schema"
        }
        self.host.repository.put("packages", document)
        value = await self.operate(self.request("discover"))
        action = document["description"]["actions"][0]["id"]
        self.assertEqual(
            next(item for item in value["capabilities"] if item["binding"]["action_id"] == action)[
                "availability"
            ],
            "unavailable",
        )

    async def test_lost_write_response_reconcile_uses_same_invocation_and_old_snapshot_is_fenced(
        self,
    ):
        writes = []

        async def write(request):
            writes.append(await request.json())
            await asyncio.sleep(0.3)
            return web.json_response({"ok": True})

        async def status(request):
            return web.json_response({"writes": len(writes)})

        app = web.Application()
        app.router.add_post("/write", write)
        app.router.add_get("/status", status)
        runner, url = await start_http(app)
        self.addAsyncCleanup(runner.cleanup)
        self.instance = await self.host.configure(
            self.instance["id"],
            {"base_url": url},
            {"api_key": "synthetic-account-a"},
            self.instance["revision"],
        )
        unknown = await self.invoke("network")
        self.assertEqual(unknown["effect"], "unknown")
        same = await self.operate(self.invoke_request("network"))
        self.assertEqual(same["invocation_id"], unknown["invocation_id"])
        await self.operate(self.request("reconcile", invocation_id=unknown["invocation_id"]))
        await self.settled(unknown["invocation_id"])
        value = await self.operate(self.request("get", invocation_id=unknown["invocation_id"]))
        self.assertEqual(value["result"], {"writes": 1})
        self.assertEqual(len(writes), 1)
        self.host.repository.update_call(
            value["invocation_id"], execution="failed", effect="unknown"
        )
        original_revision = value["binding"]["binding_revision"]
        self.instance = await self.host.configure(
            self.instance["id"],
            {"base_url": url},
            {"api_key": "synthetic-account-b"},
            self.instance["revision"],
        )
        value = await self.operate(self.request("get", invocation_id=value["invocation_id"]))
        self.assertEqual(value["binding"]["binding_revision"], original_revision)
        with self.assertRaises(Fault) as caught:
            await self.operate(self.request("reconcile", invocation_id=value["invocation_id"]))
        self.assertEqual(caught.exception.code, "version_conflict")

    async def test_restart_keeps_receipt_and_unknown_without_automatic_reexecution(self):
        value = await self.invoke()
        self.host.repository.update_call(
            value["invocation_id"], execution="running", dispatch="unknown"
        )
        await self.host.close()
        self.host = PluginManager(
            self.settings, local=self.platform.local_work.run, runtime=self.platform.storage_runtime
        )
        self.platform.plugins = self.host
        self.service = self.platform.actor_capabilities = ActorCapabilities(self.platform)
        await self.host.start()
        replay = await self.operate(self.invoke_request())
        self.assertEqual(replay["invocation_id"], value["invocation_id"])
        self.assertEqual(replay["effect"], "unknown")
        self.assertEqual(
            self.host.repository.state(self.instance["id"], "get", {"key": "count"})["value"], 1
        )

    async def test_cancel_keeps_original_receipt_and_does_not_create_another_call(self):
        value = await self.operate(self.invoke_request("wait"))
        result = await self.operate(self.request("cancel", invocation_id=value["invocation_id"]))
        self.assertEqual(result["invocation_id"], value["invocation_id"])
        self.assertEqual(result["execution"], "cancelled")
        self.assertEqual(result["effect"], "none")
        self.assertEqual(len(self.host.repository.calls()), 1)

    async def test_queued_write_cancellation_records_not_applied_in_original_ledger(self):
        lock = self.host.locks[self.instance["id"]]
        await lock.acquire()
        try:
            value = await self.operate(self.invoke_request())
            self.assertEqual(value["execution"], "accepted")
            result = await self.operate(
                self.request("cancel", invocation_id=value["invocation_id"])
            )
        finally:
            lock.release()
        self.assertEqual((result["execution"], result["effect"]), ("cancelled", "not_applied"))
        document = self.host.repository.call(value["invocation_id"])
        self.assertEqual(document["effect"], "not_applied")
        self.assertIsNone(self.host.repository.state(self.instance["id"], "get", {"key": "count"}))
        self.assertNotIn(value["invocation_id"], self.host.executions.authorizers)

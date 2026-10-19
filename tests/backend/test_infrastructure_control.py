"""Actual HTTP/SQLite/child-process tests with synthetic node protocol responses."""

import asyncio
import copy
import json
import os
import sqlite3
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import aiohttp

from dockge_fixture import COMPOSE, EXTERNAL, PROTECTED, STACK, configuration, state_read
from fixtures import ENV, start_http
from test_infrastructure import eventually
from web_fixtures import PASSWORD, web_settings
from services.platform.contracts import Fault
from services.platform.external_catalog import ExternalCatalog
from services.platform.infrastructure.control import Control
from services.platform.infrastructure.runtime import Infrastructure
from services.platform.server import create_app
from services.platform.service import Platform
from services.platform.web_console import WebConsole


class InfrastructureControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ts206-http-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        environment = patch.dict(os.environ, ENV)
        environment.start()
        self.addCleanup(environment.stop)
        self.catalog = ExternalCatalog(self.root / "external", create=True)
        self.catalog.save(
            "infrastructure:fixture-nas",
            {"node_id": "fixture-nas", "label": "合成 NAS", "kind": "synology", "enabled": True},
            {"action": "clear"},
            {"action": "clear"},
            0,
            str(uuid.uuid4()),
        )
        self.settings = web_settings(self.temp.name)
        self.settings["principals"]["admin"]["actions"].append("external.manage")
        self.settings["infrastructure"] = configuration(self.temp.name)
        self.platform = Platform(self.settings)
        self.addCleanup(self.platform.close)
        self.console = WebConsole(self.platform)
        self.runner, self.url = await start_http(create_app(self.platform, console=self.console))
        self.console.config["origin"] = self.url
        self.addAsyncCleanup(self.runner.cleanup)
        self.client = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.addAsyncCleanup(self.client.close)
        async with self.client.get(self.url + "/api/web/session") as response:
            self.csrf = (await response.json())["csrf"]
        logged = await self.post("login", {"username": "synthetic-admin", "password": PASSWORD})
        self.csrf = logged["csrf"]

    @property
    def control(self):
        return self.platform.infrastructure.control

    async def post(
        self, operation, body, expected=200, *, csrf=None, prefix="infrastructure/control/"
    ):
        if operation == "login":
            prefix = ""
        async with self.client.post(
            self.url + "/api/web/" + prefix + operation,
            json=body,
            headers={"Origin": self.url, "X-CSRF-Token": self.csrf if csrf is None else csrf},
        ) as response:
            value = await response.json()
            self.assertEqual(response.status, expected, value)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            return value

    def mode(self, **values):
        (self.root / "control-mode.json").write_text(json.dumps(values))

    def calls(self):
        path = self.root / "control-calls.jsonl"
        return [json.loads(row) for row in path.read_text().splitlines()] if path.exists() else []

    async def view(self):
        return await self.post("view", {"node_id": "fixture-nas"})

    async def inventory(self):
        return await eventually(self.view, lambda row: row["observation"]["freshness"] == "current")

    async def detail(self, *, stack_id=STACK, container_id=None):
        target = {"container_id": container_id} if container_id else {"stack_id": stack_id}
        return await eventually(
            lambda: self.post("detail", {"node_id": "fixture-nas", **target}),
            lambda row: row["observation"]["freshness"] == "current",
        )

    def action(
        self,
        action="stack.restart",
        *,
        client=None,
        stack_id=STACK,
        container_id=None,
        version=None,
    ):
        body = {
            "node_id": "fixture-nas",
            "expected_connection_revision": 1,
            "client_id": client or str(uuid.uuid4()),
            "action": action,
        }
        if container_id:
            body["container_id"] = container_id
        else:
            body.update(
                stack_id=stack_id,
                expected_revision=version or state_read(self.root)["stacks"][0]["revision"],
            )
        return body

    async def settled(self, operation_id, *, timeout=12):
        return (
            await eventually(
                lambda: self.post("operation", {"operation_id": operation_id}),
                lambda row: row["operation"]["settled"],
                timeout=timeout,
            )
        )["operation"]

    async def plan(self, action="configure", **extra):
        body = {
            "node_id": "fixture-nas",
            "expected_connection_revision": 1,
            "client_id": str(uuid.uuid4()),
            "action": action,
            "stack_id": STACK,
            "expected_revision": state_read(self.root)["stacks"][0]["revision"],
        }
        if action == "configure":
            body["compose_content"] = COMPOSE.replace("synthetic:v1", "synthetic:v2")
        body.update(extra)
        accepted = await self.post("plan", body)
        return await self.settled(accepted["operation"]["operation_id"])

    async def execute(self, plan):
        return await self.post(
            "execute",
            {
                "node_id": "fixture-nas",
                "expected_connection_revision": 1,
                "client_id": str(uuid.uuid4()),
                "plan_id": plan["plan_id"],
            },
        )

    def edit_node_state(self, operation):
        state = state_read(self.root)
        operation(state)
        with sqlite3.connect(self.root / "synthetic-dockge.sqlite") as db, db:
            db.execute("UPDATE fixture_state SET document=? WHERE id=1", (json.dumps(state),))

    async def test_view_does_not_wait_for_executor_and_repeated_reads_share_one_job(self):
        self.mode(read_delay=0.5)
        started = time.monotonic()
        first = await self.view()
        self.assertLess(time.monotonic() - started, 0.4)
        self.assertEqual(first["refresh"]["state"], "queued")
        second = await self.view()
        self.assertEqual(first["refresh"]["operation_id"], second["refresh"]["operation_id"])
        loaded = await self.inventory()
        self.assertEqual(loaded["total"], 3)
        self.assertTrue(loaded["capabilities"]["create"]["enabled"])
        protected = next(row for row in loaded["stacks"] if row["stack_id"] == PROTECTED)
        self.assertTrue(protected["maintenance"]["required"])
        self.assertFalse(protected["allowed_actions"]["stop"]["enabled"])
        self.assertEqual(len([row for row in self.calls() if row["operation"] == "stack.list"]), 1)

    async def test_container_details_and_full_id_action_produce_honest_task_evidence(self):
        await self.inventory()
        detail = await self.detail(container_id=f"{1:064x}")
        self.assertTrue(detail["container"]["allowed_actions"]["restart"]["enabled"])
        accepted = await self.post(
            "action", self.action("container.restart", container_id=f"{1:064x}")
        )
        result = await self.settled(accepted["operation"]["operation_id"])
        self.assertEqual(result["state"], "observed")
        self.assertEqual(result["result"]["command_state"], "succeeded")
        self.assertEqual(result["result"]["health_state"], "none")
        self.assertFalse(result["result"]["execution_active"])
        task = await self.post("detail", {"task_id": result["operation_id"]}, prefix="tasks/")
        self.assertEqual(task["task"]["source"], "platform.infrastructure")
        self.assertNotIn("compose_content", json.dumps(task))
        self.assertNotIn("payload", json.dumps(task))
        await self.post("action", self.action("container.restart", container_id="abc"), 400)

    async def test_ordinary_stack_action_can_start_from_inventory_without_detail_gate(self):
        await self.inventory()
        accepted = await self.post("action", self.action("stack.stop"))
        result = await self.settled(accepted["operation"]["operation_id"])
        self.assertEqual(result["result"]["runtime_state"], "stopped")

    async def test_validated_node_refusal_without_receipt_is_final_and_never_replayed(self):
        await self.inventory()
        for status in ("conflict", "unsupported", "invalid"):
            self.mode(reject_submit=status)
            accepted = await self.post("action", self.action())
            result = await self.settled(accepted["operation"]["operation_id"])
            self.assertEqual(result["state"], "failed")
            self.assertEqual(result["acceptance"], "rejected")
            self.assertFalse(result["can_refresh"])
        self.assertEqual(state_read(self.root)["submits"], 0)
        self.assertFalse(any(row["operation"] == "operation.get" for row in self.calls()))

    async def test_plan_body_supports_real_configuration_size_but_keeps_bounded_http_limit(self):
        await self.inventory()
        accepted = await self.post(
            "plan",
            {
                "node_id": "fixture-nas",
                "expected_connection_revision": 1,
                "client_id": str(uuid.uuid4()),
                "action": "create",
                "project_name": "large-fixture",
                "compose_content": COMPOSE + "\n#" + "synthetic-comment" * 2500,
            },
        )
        result = await self.settled(accepted["operation"]["operation_id"])
        self.assertEqual(result["state"], "observed")
        refused = await self.post("plan", {"padding": "x" * (1024 * 1024)}, 413)
        self.assertEqual(refused["code"], "budget_exceeded")
        await self.post("view", {"padding": "x" * 17000}, 413)

    async def test_csrf_and_administrator_capability_are_required(self):
        denied = await self.post("view", {"node_id": "fixture-nas"}, 403, csrf="wrong")
        self.assertIn("code", denied)
        self.platform.auth.principals["admin"]["actions"].remove("external.manage")
        denied = await self.post("view", {"node_id": "fixture-nas"}, 403)
        self.assertEqual(denied["code"], "external_manage_required")
        self.assertEqual(state_read(self.root)["submits"], 0)

    async def test_same_client_concurrency_replays_once_and_different_semantics_conflict(self):
        await self.inventory()
        body = self.action()
        results = await asyncio.gather(*(self.post("action", body) for _ in range(8)))
        self.assertEqual(len({row["operation"]["operation_id"] for row in results}), 1)
        await self.settled(results[0]["operation"]["operation_id"])
        self.assertEqual(state_read(self.root)["submits"], 1)
        other = {**body, "action": "stack.stop"}
        denied = await self.post("action", other, 409)
        self.assertEqual(denied["code"], "idempotency_conflict")
        looked = await self.post("operation", {"client_id": body["client_id"]})
        self.assertEqual(
            looked["operation"]["operation_id"], results[0]["operation"]["operation_id"]
        )

    async def test_different_clients_same_stack_wait_for_existing_worker(self):
        await self.inventory()
        self.mode(operation_seconds=1)
        first = await self.post("action", self.action())
        second = await self.post("action", self.action("stack.stop"))
        await eventually(
            lambda: self.post("operation", {"operation_id": first["operation"]["operation_id"]}),
            lambda row: row["operation"]["acceptance"] == "submitted",
        )
        self.assertEqual(state_read(self.root)["submits"], 1)
        queued = await self.post("operation", {"operation_id": second["operation"]["operation_id"]})
        self.assertEqual(queued["operation"]["state"], "queued")
        await self.settled(first["operation"]["operation_id"])
        await self.settled(second["operation"]["operation_id"])
        self.assertEqual(state_read(self.root)["submits"], 2)

    async def test_two_platform_instances_share_durable_claims(self):
        await self.inventory()
        second = Infrastructure(self.settings, local=self.platform.local_work.run)
        await second.control.start()
        self.addAsyncCleanup(second.close)
        accepted = await self.post("action", self.action())
        await self.settled(accepted["operation"]["operation_id"])
        self.assertEqual(state_read(self.root)["submits"], 1)

    async def test_lost_submit_response_and_restart_only_query_original_operation(self):
        await self.inventory()
        self.mode(disconnect_after_submit=True, operation_seconds=0.1)
        accepted = await self.post("action", self.action())
        identity = accepted["operation"]["operation_id"]
        await eventually(
            lambda: self.post("operation", {"operation_id": identity}),
            lambda row: row["operation"]["state"] == "unknown",
        )
        await self.control.close()
        self.platform.infrastructure.control = Control(self.platform.infrastructure)
        await self.control.start()
        result = await self.settled(identity)
        self.assertEqual(result["state"], "observed")
        self.assertEqual(state_read(self.root)["submits"], 1)
        self.assertEqual(sum(row["operation"] == "operation.submit" for row in self.calls()), 1)
        self.assertTrue(any(row["operation"] == "operation.get" for row in self.calls()))

    async def test_expired_sent_claim_never_resubmits_even_without_node_receipt(self):
        await self.inventory()
        await self.control.close()
        accepted = await self.post("action", self.action())
        identity = accepted["operation"]["operation_id"]
        row = self.control.repo.claim("crashed-owner", time.time(), 0)
        self.assertEqual(row["operation_id"], identity)
        self.assertTrue(self.control.repo.sending(identity, "crashed-owner", row["lease_until"]))
        await self.control.start()
        result = await eventually(
            lambda: self.post("operation", {"operation_id": identity}),
            lambda row: row["operation"]["state"] == "unknown",
        )
        self.assertFalse(result["operation"]["settled"])
        self.assertEqual(state_read(self.root)["submits"], 0)
        self.assertFalse(any(row["operation"] == "operation.submit" for row in self.calls()))

    async def test_changed_connection_does_not_send_queued_operation_to_new_registration(self):
        await self.inventory()
        await self.control.close()
        accepted = await self.post("action", self.action())
        revision, rows = self.catalog.snapshot()
        self.catalog.save(
            "infrastructure:fixture-nas",
            {**rows["infrastructure:fixture-nas"]["value"], "label": "重新登记"},
            {"action": "clear"},
            {"action": "clear"},
            revision,
            str(uuid.uuid4()),
        )
        await self.control.start()
        result = await self.settled(accepted["operation"]["operation_id"])
        self.assertEqual(result["state"], "failed")
        self.assertIn("revision_conflict", result["reason_codes"])
        self.assertEqual(state_read(self.root)["submits"], 0)

    async def test_configuration_preview_execute_version_restore_preserves_data_and_secrets(self):
        await self.inventory()
        data = self.root / "fixture-data"
        data.mkdir()
        (data / "keep.txt").write_text("synthetic persistent data")
        raw_secret = "synthetic-new-secret-never-public"
        planned = await self.plan(
            compose_content=COMPOSE.replace("synthetic:v1", "synthetic:v2").replace(
                '"{{tianshu.secret:fixture}}"', raw_secret
            )
        )
        self.assertEqual(planned["state"], "observed")
        plan = planned["result"]
        self.assertTrue(plan["executable"])
        self.assertTrue(plan["mounts"])
        self.assertNotIn(raw_secret, json.dumps(planned))
        accepted = await self.execute(plan)
        executed = await self.settled(accepted["operation"]["operation_id"])
        self.assertEqual(executed["state"], "observed")
        detail = await self.post(
            "detail", {"node_id": "fixture-nas", "stack_id": STACK, "refresh": True}
        )
        await self.settled(detail["refresh"]["operation_id"])
        detail = await self.detail()
        self.assertNotIn(raw_secret, json.dumps(detail))
        self.assertEqual(len(detail["versions"]), 1)
        restored = await self.plan(
            "restore", restore_version_id=detail["versions"][0]["version_id"]
        )
        accepted = await self.execute(restored["result"])
        await self.settled(accepted["operation"]["operation_id"])
        self.assertEqual(state_read(self.root)["stacks"][0]["compose_content"], COMPOSE)
        self.assertEqual((data / "keep.txt").read_text(), "synthetic persistent data")
        history = await self.post("operations", {})
        self.assertNotIn(raw_secret, json.dumps(history))

    async def test_stale_plan_after_external_edit_is_rejected_without_overwriting(self):
        await self.inventory()
        planned = await self.plan()
        self.edit_node_state(
            lambda state: state["stacks"][0].update(
                revision="f" * 64, compose_content="external-edit"
            )
        )
        accepted = await self.execute(planned["result"])
        result = await self.settled(accepted["operation"]["operation_id"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["acceptance"], "rejected")
        self.assertEqual(state_read(self.root)["stacks"][0]["compose_content"], "external-edit")

    async def test_blocked_port_plan_and_protected_targets_never_mutate(self):
        await self.inventory()
        self.mode(port_conflict=True)
        plan = await self.plan()
        self.assertFalse(plan["result"]["executable"])
        self.assertIn("port_conflict", plan["result"]["blockers"])
        await self.post(
            "execute",
            {
                "node_id": "fixture-nas",
                "expected_connection_revision": 1,
                "client_id": str(uuid.uuid4()),
                "plan_id": plan["result"]["plan_id"],
            },
            409,
        )
        protected = state_read(self.root)["stacks"][1]
        await self.post(
            "action", self.action(stack_id=PROTECTED, version=protected["revision"]), 409
        )
        detail = await self.detail(container_id=f"{2:064x}")
        self.assertTrue(detail["container"]["maintenance"]["required"])
        await self.post("action", self.action("container.stop", container_id=f"{2:064x}"), 409)
        self.assertEqual(state_read(self.root)["submits"], 0)

    async def test_create_uses_registered_project_name_and_external_configuration_stays_owned(self):
        await self.inventory()
        created = await self.post(
            "plan",
            {
                "node_id": "fixture-nas",
                "expected_connection_revision": 1,
                "client_id": str(uuid.uuid4()),
                "action": "create",
                "project_name": "fixture-created",
                "compose_content": COMPOSE,
            },
        )
        plan = (await self.settled(created["operation"]["operation_id"]))["result"]
        accepted = await self.execute(plan)
        await self.settled(accepted["operation"]["operation_id"])
        await eventually(self.view, lambda row: row["total"] == 4)
        external = state_read(self.root)["stacks"][2]
        blocked = await self.plan(stack_id=EXTERNAL, expected_revision=external["revision"])
        self.assertFalse(blocked["result"]["executable"])
        self.assertIn("stack_not_platform_owned", blocked["result"]["blockers"])

    async def test_pull_failure_and_partial_runtime_are_not_reported_as_success(self):
        await self.inventory()
        planned = await self.plan("update")
        self.mode(pull_failure=True)
        accepted = await self.execute(planned["result"])
        result = await self.settled(accepted["operation"]["operation_id"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["result"]["command_state"], "failed")
        self.mode(partial_start=True)
        accepted = await self.post("action", self.action())
        result = await self.settled(accepted["operation"]["operation_id"])
        self.assertEqual(result["state"], "partial")
        self.assertEqual(result["result"]["runtime_state"], "partial")

    async def test_known_inactive_unknown_remains_unknown_and_original_is_refreshable(self):
        await self.inventory()
        self.mode(worker_unknown=True)
        accepted = await self.post("action", self.action())
        result = await self.settled(accepted["operation"]["operation_id"])
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["result"]["command_state"], "unknown")
        self.assertFalse(result["result"]["execution_active"])
        self.assertTrue(result["can_refresh"])
        await self.post("operation", {"operation_id": result["operation_id"], "refresh": True})
        self.assertEqual(state_read(self.root)["submits"], 1)

    async def test_operations_page_filters_cursor_and_hides_read_jobs(self):
        await self.inventory()
        for _ in range(3):
            await self.plan("update")
        first = await self.post("operations", {"page_size": 2})
        self.assertEqual(len(first["items"]), 2)
        self.assertTrue(first["page"]["has_more"])
        second = await self.post(
            "operations", {"page_size": 2, "cursor": first["page"]["next_cursor"]}
        )
        self.assertEqual(len(second["items"]), 1)
        self.assertFalse(
            {row["operation_id"] for row in first["items"]}
            & {row["operation_id"] for row in second["items"]}
        )
        await self.post(
            "operations", {"state": "failed", "cursor": first["page"]["next_cursor"]}, 409
        )


class InfrastructureControlContractTests(unittest.TestCase):
    def test_configuration_is_optional_and_schema_is_pinned(self):
        from services.platform.infrastructure.config import configuration as validate

        with tempfile.TemporaryDirectory() as directory:
            ExternalCatalog(Path(directory) / "external", create=True)
            value = configuration(directory)
            wrong = copy.deepcopy(value)
            wrong["control"]["executor"]["command"] = ["relative-python"]
            with self.assertRaises(Fault):
                validate({"infrastructure": wrong})
            wrong = copy.deepcopy(value)
            wrong["control"]["contract_directory"] = str(Path(directory) / "missing")
            with self.assertRaises(Fault):
                validate({"infrastructure": wrong})
            value.pop("control")
            self.assertNotIn("control", validate({"infrastructure": value}))

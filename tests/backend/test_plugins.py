"""Real process, transport and restart tests for the public plugin authoring path."""

import asyncio
import base64
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid
import zipfile

from aiohttp import web

from services.platform.contracts import Fault, digest
from services.platform.plugins import PluginManager
from services.platform.plugins.process import RemoteError
from services.platform.plugins.scheduler import next_run


CODE = """
import asyncio
import os
from tianshu_plugin import Plugin, PluginError, ActionResult, action
EMPTY = {"type": "object", "additionalProperties": False}
ANY = {}
class Fixture(Plugin):
    @action(id="echo", title="Echo", input_schema={"type":"object"}, output_schema=ANY)
    async def echo(self, call, args):
        return {"value": args, "config": dict(call.ctx.config), "secret": dict(call.ctx.secret_configured),
                "inherited": os.environ.get("TS237_PARENT_SECRET")}
    @action(id="increment", title="Increment", input_schema=EMPTY, output_schema={"type":"integer"}, effect="write", required_capabilities=(("state.kv",1),))
    async def increment(self, call, args):
        value = await call.ctx.state.get("count")
        result = await call.ctx.state.compare_and_set("count", (value.value if value else 0)+1, expected_revision=value.revision if value else None)
        return ActionResult(result.value, "applied")
    @action(id="wait", title="Wait", input_schema=EMPTY, output_schema=ANY, timeout_seconds=2)
    async def wait(self, call, args):
        await call.checkpoint({"step": "waiting"})
        await asyncio.sleep(60)
        return {}
    @action(id="network", title="Network", input_schema={"type":"object"}, output_schema=ANY, effect="write", reconcile_handler="reconcile", required_capabilities=(("http.client",1),))
    async def network(self, call, args):
        response = await call.ctx.capability("http.client").call("request", {
            "method": args.get("method", "POST"), "url": call.ctx.config["base_url"] + args.get("path", "/write"),
            "body": {"key": call.intent_key}, "auth": {"secret_name": "api_key"}, "timeout_seconds": .15})
        if response["status"] != 200:
            raise PluginError("dependency_unavailable", effect="unknown")
        return ActionResult(response["body"], "applied", {"provider": "test", "status": "queued"})
    async def reconcile(self, call, recovery):
        response = await call.ctx.capability("http.client").call("request", {
            "method": "GET", "url": call.ctx.config["base_url"] + "/status", "auth": {"secret_name": "api_key"}})
        return ActionResult(response["body"], "applied", {"provider": "test", "status": "queued"})
    @action(id="child", title="Child", input_schema=EMPTY, output_schema=ANY, required_capabilities=(("jobs.manage",1),))
    async def child(self, call, args):
        return await call.ctx.actions.invoke("increment", {}, intent_key=call.intent_key+":child")
    @action(id="notify", title="Notify", input_schema=EMPTY, output_schema=ANY, required_capabilities=(("notifications.emit",1),))
    async def notify(self, call, args):
        return await call.ctx.capability("notifications.emit").call("publish", {"dedup_key": "one", "title": "Done", "body": "Complete"})
    @action(id="crash", title="Crash", input_schema=EMPTY, output_schema=ANY, effect="write")
    async def crash(self, call, args):
        os._exit(17)
    @action(id="bad-output", title="Bad output", input_schema=EMPTY, output_schema={"type":"integer"})
    async def bad_output(self, call, args):
        return "not an integer"
    @action(id="log", title="Log", input_schema=EMPTY, output_schema=ANY)
    async def log(self, call, args):
        print("print output must not corrupt the protocol" * 10000)
        call.ctx.log.info("test.finished", password="do-not-expose", detail="fine")
        await asyncio.sleep(.02)
        return {"ok":True}
"""


def package(directory, *, version="1.0.0", code=CODE, data_version=0):
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "id": "test.fixture",
        "name": "Fixture",
        "version": version,
        "description": "Isolated test plugin",
        "author": "Tests",
        "entrypoint": "main:Fixture",
        "sdk": {"min": "1.0.0", "max_exclusive": "2.0.0"},
        "config_schema": "config.json",
        "data_version": data_version,
        "binding_overrides": ["label"],
        "capabilities": {
            "required": [
                {"id": id, "major": 1}
                for id in ("state.kv", "jobs.manage", "http.client", "notifications.emit")
            ],
            "optional": [{"id": "secrets.read", "major": 1}],
        },
    }
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "label": {"type": "string", "default": "default"},
            "base_url": {"type": "string"},
            "api_key": {"type": "string", "x-secret": True, "minLength": 1},
        },
    }
    (path / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (path / "config.json").write_text(json.dumps(schema), encoding="utf-8")
    (path / "main.py").write_text(code, encoding="utf-8")
    return path


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = {"database_path": str(self.root / "platform.db")}
        self.host = PluginManager(self.settings)
        await self.host.start()
        self.source = package(self.root / "source")
        self.package = await self.install(self.source)
        self.instance = await self.host.create(self.package, "First", "instance-one")
        self.instance = await self.host.state(self.instance["id"], True, self.instance["revision"])

    async def asyncTearDown(self):
        await self.host.close()
        self.temp.cleanup()

    async def install(self, source):
        operation = await self.host.install(
            {"type": "directory", "path": str(source)}, str(uuid.uuid4())
        )
        for _ in range(300):
            document = self.host.repository.get("operations", operation["id"])
            if document["execution"] not in {"accepted", "running"}:
                self.assertEqual(document["execution"], "succeeded", document)
                return document["package_id"]
            await asyncio.sleep(0.01)
        self.fail("install did not finish")

    async def invoke(self, action, value=None, intent=None):
        result = await self.host.executions.invoke(
            self.instance["id"], action, value or {}, intent or str(uuid.uuid4()), "admin"
        )
        return await self.settled(result["id"])

    async def settled(self, identifier):
        for _ in range(400):
            result = self.host.repository.call(identifier)
            if result["execution"] not in {"accepted", "running"}:
                return result
            await asyncio.sleep(0.01)
        self.fail("call did not settle")

    async def test_real_worker_input_and_output_validation(self):
        result = await self.invoke("echo", {"hello": "real worker"})
        self.assertEqual(result["result"]["value"], {"hello": "real worker"})
        self.assertEqual(result["execution"], "succeeded")
        result = await self.invoke("bad-output")
        self.assertEqual(result["execution"], "failed")
        self.assertEqual(result["effect"], "none")
        with self.assertRaises(Fault):
            await self.invoke("increment", {"unexpected": True})

    async def test_same_intent_never_repeats_write_and_different_input_conflicts(self):
        first, second = await asyncio.gather(
            self.invoke("increment", intent="same"), self.invoke("increment", intent="same")
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(
            self.host.repository.state(self.instance["id"], "get", {"key": "count"})["value"], 1
        )
        await self.invoke("echo", {"value": 1}, "conflict")
        with self.assertRaises(Fault) as caught:
            await self.invoke("echo", {"value": 2}, "conflict")
        self.assertEqual(caught.exception.code, "idempotency_conflict")

    async def test_two_instances_keep_state_and_configuration_separate(self):
        await self.invoke("increment")
        second = await self.host.create(self.package, "Second", "instance-two")
        second = await self.host.configure(
            second["id"], {"label": "second"}, {"api_key": "second-secret"}, second["revision"]
        )
        await self.host.state(second["id"], True, second["revision"])
        receipt = await self.host.executions.invoke(second["id"], "echo", {}, "read", "admin")
        result = await self.settled(receipt["id"])
        self.assertEqual(result["result"]["config"]["label"], "second")
        self.assertNotIn("api_key", result["result"]["config"])
        self.assertIsNone(self.host.repository.state(second["id"], "get", {"key": "count"}))
        self.assertNotIn(b"second-secret", (self.root / "platform.db.plugins.sqlite").read_bytes())

    async def test_context_cannot_be_used_by_another_instance(self):
        context = await self.host.executions.context(
            await self.host.instance(self.instance["id"]), None
        )
        with self.assertRaises(Fault):
            await self.host.executions.callback(
                "another-instance",
                "host.capability",
                {
                    "context_ref": context["context_ref"],
                    "capability": "state.kv",
                    "major": 1,
                    "operation": "get",
                    "input": {"key": "count"},
                },
            )

    async def test_a_second_host_cannot_recover_the_live_hosts_calls(self):
        second = PluginManager(self.settings)
        try:
            await second.start()
            self.assertEqual(
                (await second.projection(await second.instance(self.instance["id"])))["state"],
                "managed_elsewhere",
            )
            with self.assertRaises(Fault) as caught:
                await second.executions.invoke(self.instance["id"], "echo", {}, "passive", "admin")
            self.assertEqual(caught.exception.code, "plugin_host_busy")
            self.assertEqual((await self.invoke("echo"))["execution"], "succeeded")
        finally:
            await second.close()

    async def test_resume_continues_original_checkpoint_without_repeating_completed_step(self):
        code = (
            CODE
            + """
    @action(id="recoverable", title="Recoverable", input_schema=EMPTY, output_schema={"type":"integer"}, effect="write", resumable=True, resume_handler="resume_work")
    async def recoverable(self, call, args):
        value = await call.ctx.state.get("steps")
        updated = await call.ctx.state.compare_and_set("steps", (value.value if value else 0)+1, expected_revision=value.revision if value else None)
        await call.checkpoint({"completed": updated.value})
        raise PluginError("outcome_unknown", effect="unknown")
    async def resume_work(self, call, recovery):
        return ActionResult(recovery.checkpoint.value["completed"], "applied")
"""
        )
        await self.install(package(self.root / "resumable", version="1.1.0", code=code))
        current = await self.host.instance(self.instance["id"])
        await self.host.upgrade(current["id"], "1.1.0", current["revision"])
        result = await self.invoke("recoverable")
        self.assertEqual(result["effect"], "unknown")
        await self.host.executions.recover(result["id"], "admin", lambda: True, resume=True)
        recovered = await self.settled(result["id"])
        self.assertEqual(recovered["execution"], "succeeded")
        self.assertEqual(recovered["result"], 1)
        self.assertEqual(
            self.host.repository.state(current["id"], "get", {"key": "steps"})["value"], 1
        )

    async def test_state_compare_and_set_rejects_stale_revision(self):
        await self.invoke("increment")
        with self.assertRaises(Fault):
            self.host.repository.state(
                self.instance["id"],
                "compare_and_set",
                {"key": "count", "value": 999, "expected_revision": None},
            )
        self.assertEqual(
            self.host.repository.state(self.instance["id"], "get", {"key": "count"})["value"], 1
        )

    async def test_child_job_does_not_deadlock_and_notification_has_separate_dedup(self):
        result = await self.invoke("child")
        child = await self.settled(result["result"]["invocation_id"])
        self.assertEqual(child["result"], 1)
        await self.invoke("notify")
        await self.invoke("notify")
        self.assertEqual(len(self.host.repository.list("notifications")), 1)

    async def test_cancel_releases_process_and_keeps_checkpoint(self):
        result = await self.host.executions.invoke(self.instance["id"], "wait", {}, "wait", "admin")
        for _ in range(50):
            if self.host.repository.call(result["id"])["checkpoint"]:
                break
            await asyncio.sleep(0.01)
        cancelled = await self.host.executions.cancel(result["id"])
        self.assertEqual(cancelled["execution"], "cancelled")
        self.assertEqual(cancelled["checkpoint"]["value"], {"step": "waiting"})
        self.assertEqual((await self.invoke("echo"))["execution"], "succeeded")

    async def test_crash_is_unknown_and_does_not_break_other_actions(self):
        result = await self.invoke("crash")
        self.assertEqual(result["effect"], "unknown")
        self.assertEqual(result["error"]["code"], "outcome_unknown")
        self.assertEqual((await self.invoke("echo"))["execution"], "succeeded")

    async def test_worker_inherits_no_platform_secret_and_print_cannot_block_rpc(self):
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"TS237_PARENT_SECRET": "not-for-plugins"}):
            result = await self.invoke("echo")
        self.assertIsNone(result["result"]["inherited"])
        self.assertEqual((await self.invoke("log"))["execution"], "succeeded")
        logs = self.host.repository.logs(self.instance["id"])
        self.assertNotIn("do-not-expose", json.dumps(logs))

    async def test_restart_keeps_configuration_and_marks_unfinished_write_unknown(self):
        instance = await self.host.instance(self.instance["id"])
        action = next(
            item
            for item in (await self.host.package(self.package))["description"]["actions"]
            if item["id"] == "increment"
        )
        receipt, _ = self.host.repository.accept(instance, action, {}, "interrupted", "admin")
        self.host.repository.update_call(receipt["id"], execution="running", dispatch="unknown")
        await self.host.close()
        self.host = PluginManager(self.settings)
        await self.host.start()
        result = self.host.repository.call(receipt["id"])
        self.assertEqual(result["execution"], "interrupted")
        self.assertEqual(result["effect"], "unknown")
        self.assertIsNone(self.host.repository.state(instance["id"], "get", {"key": "count"}))
        self.assertEqual(
            (await self.host.projection(await self.host.instance(instance["id"])))["state"], "ready"
        )

    async def test_uninstall_reinstall_preserves_instance_and_data(self):
        await self.invoke("increment")
        await self.host.uninstall("test.fixture")
        self.assertEqual(
            (await self.host.instance(self.instance["id"]))["desired_state"], "disabled"
        )
        await self.install(self.source)
        instance = await self.host.instance(self.instance["id"])
        await self.host.state(instance["id"], True, instance["revision"])
        self.assertEqual((await self.invoke("increment"))["result"], 2)

    async def test_failed_upgrade_restores_mutated_data_and_old_version(self):
        await self.invoke("increment")
        code = (
            CODE
            + """
    async def migrate_data(self, from_version, to_version):
        value = await self.ctx.state.get("count")
        await self.ctx.state.compare_and_set("count", 999, expected_revision=value.revision)
        raise PluginError("plugin_error", effect="none")
"""
        )
        await self.install(package(self.root / "v2", version="2.0.0", code=code, data_version=1))
        current = await self.host.instance(self.instance["id"])
        with self.assertRaises(RemoteError):
            await self.host.upgrade(current["id"], "2.0.0", current["revision"])
        self.assertEqual((await self.host.instance(current["id"]))["version"], "1.0.0")
        self.assertEqual((await self.invoke("increment"))["result"], 2)

    async def test_upgrade_crash_recovery_uses_persisted_snapshot(self):
        await self.invoke("increment")
        snapshot = self.host.repository.snapshot(self.instance["id"])
        self.host.repository.put(
            "operations",
            {
                "id": "upgrade:interrupted",
                "kind": "upgrade",
                "execution": "running",
                "instance_id": self.instance["id"],
                "snapshot": snapshot,
            },
        )
        self.host.repository.state(
            self.instance["id"],
            "compare_and_set",
            {"key": "count", "value": 999, "expected_revision": 1},
        )
        await self.host.close()
        self.host = PluginManager(self.settings)
        await self.host.start()
        self.assertEqual((await self.invoke("increment"))["result"], 2)

    async def test_schedule_coalesces_missed_runs_and_stop_prevents_new_ticks(self):
        await self.host.scheduler.manage(
            self.instance["id"],
            "schedule.put",
            {
                "schedule_id": "count",
                "action": "increment",
                "input": {},
                "expected_revision": None,
                "trigger": {
                    "kind": "interval",
                    "seconds": 60,
                    "timezone": "UTC",
                    "misfire": "coalesce",
                    "overlap": "skip",
                },
            },
        )
        schedule = self.host.repository.list("schedules")[0]
        await self.host.scheduler.tick(schedule["next_run"] + 10000)
        current = self.host.repository.list("schedules")[0]
        self.assertEqual((await self.settled(current["last_invocation"]))["result"], 1)
        instance = await self.host.instance(self.instance["id"])
        await self.host.state(instance["id"], False, instance["revision"])
        await self.host.scheduler.tick(current["next_run"] + 10000)
        self.assertEqual(
            self.host.repository.state(instance["id"], "get", {"key": "count"})["value"], 1
        )

    async def test_quiesce_drains_accepted_action_and_child_without_closing_worker(self):
        source = package(
            self.root / "quiesce-source",
            version="1.1.0",
            code=CODE.replace(
                "await asyncio.sleep(60)\n        return {}",
                "await asyncio.sleep(.15)\n"
                "        return await call.ctx.actions.invoke('increment', {}, "
                "intent_key=call.intent_key+':child')",
            ),
        )
        await self.install(source)
        current = await self.host.instance(self.instance["id"])
        await self.host.upgrade(current["id"], "1.1.0", current["revision"])
        call = await self.host.executions.invoke(current["id"], "wait", {}, "drain-parent", "admin")
        for _ in range(100):
            if self.host.repository.call(call["id"])["checkpoint"]:
                break
            await asyncio.sleep(0.01)
        self.assertIsNotNone(self.host.repository.call(call["id"])["checkpoint"])
        process = self.host.workers[current["id"]][0]
        await self.host.quiesce()
        self.assertTrue(self.host.scheduler_task.done())
        self.assertIs(self.host.workers[current["id"]][0], process)
        result = self.host.repository.call(call["id"])
        self.assertEqual(result["execution"], "succeeded")
        child = self.host.repository.call(result["result"]["invocation_id"])
        self.assertEqual(child["execution"], "succeeded")
        self.assertEqual(child["result"], 1)
        self.assertFalse(self.host.executions.running)
        self.assertTrue(self.host.started)

    async def schedule_pair(self):
        for name in ("count-a", "count-b"):
            await self.host.scheduler.manage(
                self.instance["id"],
                "schedule.put",
                {
                    "schedule_id": name,
                    "action": "increment",
                    "input": {},
                    "expected_revision": None,
                    "trigger": {
                        "kind": "interval",
                        "seconds": 60,
                        "timezone": "UTC",
                        "misfire": "coalesce",
                        "overlap": "queue_one",
                    },
                },
            )
        return self.host.repository.list("schedules")

    async def test_failed_schedule_keeps_due_and_does_not_starve_later_schedule(self):
        first, second = await self.schedule_pair()
        intent = "schedule:" + digest([first["id"], first["next_run"], first["revision"]])
        invoke = self.host.executions.invoke

        async def unavailable(identifier, action, value, key, principal):
            if key == intent:
                raise Fault("capability_unavailable", 409)
            return await invoke(identifier, action, value, key, principal)

        now = max(first["next_run"], second["next_run"]) + 1
        with patch.object(self.host.executions, "invoke", unavailable):
            await self.host.scheduler.tick(now)
        self.assertEqual(self.host.repository.get("schedules", first["id"]), first)
        progressed = self.host.repository.get("schedules", second["id"])
        self.assertEqual((await self.settled(progressed["last_invocation"]))["result"], 1)
        await self.host.scheduler.tick(now)
        retried = self.host.repository.get("schedules", first["id"])
        result = await self.settled(retried["last_invocation"])
        self.assertEqual(result["intent_key"], intent)
        self.assertEqual(result["result"], 2)
        self.assertEqual(len(self.host.repository.calls()), 2)

    async def test_schedule_receipt_save_failure_reuses_accepted_intent_on_retry(self):
        first, second = await self.schedule_pair()
        put = self.host.repository.put

        def unavailable(kind, document, *args, **kwargs):
            if kind == "schedules" and document["id"] == first["id"]:
                raise OSError("isolated schedule write failure")
            return put(kind, document, *args, **kwargs)

        now = max(first["next_run"], second["next_run"]) + 1
        with patch.object(self.host.repository, "put", unavailable):
            await self.host.scheduler.tick(now)
        self.assertEqual(self.host.repository.get("schedules", first["id"]), first)
        calls = self.host.repository.calls()
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertEqual((await self.settled(call["id"]))["execution"], "succeeded")
        await self.host.scheduler.tick(now)
        retried = self.host.repository.get("schedules", first["id"])
        self.assertIn(retried["last_invocation"], {call["id"] for call in calls})
        self.assertEqual(len(self.host.repository.calls()), 2)
        self.assertEqual(
            self.host.repository.state(self.instance["id"], "get", {"key": "count"})["value"],
            2,
        )

    async def test_write_response_loss_is_reconciled_without_resubmission(self):
        writes, received = [], []

        async def endpoint(request):
            received.append(request.headers.get("Authorization"))
            if request.method == "POST":
                writes.append(await request.json())
                await asyncio.sleep(0.4)
            return web.json_response({"count": len(writes)})

        app = web.Application()
        app.router.add_route("*", "/{path:.*}", endpoint)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        try:
            instance = await self.host.instance(self.instance["id"])
            await self.host.configure(
                instance["id"],
                {"base_url": url},
                {"api_key": "isolated-api-secret"},
                instance["revision"],
            )
            first = await self.invoke("network", intent="write-once")
            self.assertEqual(first["effect"], "unknown")
            self.assertEqual(len(writes), 1)
            replay = await self.invoke("network", intent="write-once")
            self.assertEqual(replay["id"], first["id"])
            await self.host.executions.recover(first["id"], "admin", lambda: True)
            result = await self.settled(first["id"])
            self.assertEqual(result["effect"], "applied")
            self.assertEqual(len(writes), 1)
            self.assertTrue(all(header == "Bearer isolated-api-secret" for header in received))
            self.assertNotIn("isolated-api-secret", json.dumps(self.host.executions.public(result)))
        finally:
            await runner.cleanup()

    async def test_unavailable_required_capability_is_not_fake_ready(self):
        source = package(self.root / "missing", version="3.0.0")
        path = source / "plugin.json"
        manifest = json.loads(path.read_text())
        manifest["capabilities"]["required"].append({"id": "channel.group", "major": 1})
        path.write_text(json.dumps(manifest))
        key = await self.install(source)
        instance = await self.host.create(key, "Missing", "missing")
        with self.assertRaises(Fault) as caught:
            await self.host.state(instance["id"], True, instance["revision"])
        self.assertEqual(caught.exception.code, "capability_unavailable")
        self.assertEqual(
            (await self.host.projection(await self.host.instance(instance["id"])))["state"],
            "blocked",
        )

    async def test_configuration_cas_and_binding_override_limits(self):
        current = await self.host.instance(self.instance["id"])
        updated = await self.host.configure(
            current["id"], {"label": "new"}, {}, current["revision"]
        )
        with self.assertRaises(Fault):
            await self.host.configure(current["id"], {"label": "stale"}, {}, current["revision"])
        binding = {
            "kind": "binding",
            "schema_version": 1,
            "binding_id": str(uuid.uuid4()),
            "instance_id": current["id"],
            "revision": 1,
            "enabled": True,
            "scope": {"kind": "workspace", "workspace_id": "main"},
            "overrides": {"api_key": "forbidden"},
            "priority": 1,
        }
        with self.assertRaises(Fault):
            await self.host.bindings(current["id"], [binding], updated["revision"])
        binding["overrides"] = {"label": "scope-specific"}
        await self.host.bindings(current["id"], [binding], updated["revision"])
        receipt = await self.host.executions.invoke(
            current["id"], "echo", {}, "scope", "admin", binding_id=binding["binding_id"]
        )
        result = await self.settled(receipt["id"])
        self.assertEqual(result["result"]["config"]["label"], "scope-specific")
        self.assertEqual((await self.host.instance(current["id"]))["config"]["label"], "new")

    async def test_revoked_session_blocks_queued_invocation(self):
        allowed = True
        result = await self.host.executions.invoke(
            self.instance["id"], "echo", {}, "revoked", "admin", authorize=lambda: allowed
        )
        allowed = False
        receipt = await self.settled(result["id"])
        self.assertEqual(receipt["execution"], "failed")
        self.assertEqual(receipt["error"]["code"], "session_expired")


class PluginPackageTests(unittest.IsolatedAsyncioTestCase):
    async def test_interrupted_dependency_environment_is_rebuilt_before_inspection(self):
        from services.platform.plugins.process import Process

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host = PluginManager({"database_path": str(root / "platform.db")})
            source = package(root / "source")
            prepared = await asyncio.to_thread(
                host.packages.prepare, {"type": "directory", "path": str(source)}
            )
            executable = Path(prepared["python"])
            (executable.parents[1] / ".complete").unlink()
            executable.write_bytes(b"interrupted environment")
            repaired = await asyncio.to_thread(
                host.packages.prepare, {"type": "directory", "path": str(source)}
            )
            process = Process(repaired, host.reject_capability)
            try:
                await process.start()
                self.assertTrue(process.alive)
                self.assertTrue((await process.call("plugin.describe", {}))["actions"])
            finally:
                await process.close()
                await host.close()

    async def test_sdk_range_respects_prerelease_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host = PluginManager({"database_path": str(root / "platform.db")})
            try:
                source = package(root / "source")
                path = source / "plugin.json"
                manifest = json.loads(path.read_text())
                manifest["sdk"]["min"] = "1.0.0-rc.1"
                manifest["sdk"]["max_exclusive"] = "2.0.0"
                path.write_text(json.dumps(manifest))
                prepared = await asyncio.to_thread(
                    host.packages.prepare, {"type": "directory", "path": str(source)}
                )
                self.assertEqual(prepared["manifest"]["id"], "test.fixture")
                manifest["sdk"]["max_exclusive"] = "1.0.0-rc.2"
                path.write_text(json.dumps(manifest))
                with self.assertRaises(Fault) as caught:
                    await asyncio.to_thread(
                        host.packages.prepare, {"type": "directory", "path": str(source)}
                    )
                self.assertEqual(caught.exception.code, "plugin_incompatible")
            finally:
                await host.close()

    def test_view_assets_do_not_rewrite_javascript_assignments(self):
        from services.platform.plugins.views import document

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "page.html").write_text(
                '<script>const src="https://example.invalid/resource";</script><img src="pixel.svg">',
                encoding="utf-8",
            )
            (root / "pixel.svg").write_text(
                '<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8"
            )
            html = document({"root": directory}, {"entry": "page.html"}, str(uuid.uuid4()))
            self.assertIn('const src="https://example.invalid/resource";', html)
            self.assertIn("data:image/svg+xml;base64,", html)

    async def test_zip_traversal_rejected_without_writing_outside_package_root(self):
        with tempfile.TemporaryDirectory() as directory:
            host = PluginManager({"database_path": str(Path(directory) / "platform.db")})
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("../escape.txt", "must not be written")
            with self.assertRaises(Fault):
                await asyncio.to_thread(
                    host.packages.prepare,
                    {"type": "zip", "data": base64.b64encode(buffer.getvalue()).decode()},
                )
            self.assertFalse((Path(directory) / "escape.txt").exists())
            await host.close()

    def test_cron_uses_timezone_and_does_not_replay_missed_minutes(self):
        stamp = next_run({"kind": "cron", "cron": "*/15 * * * *", "timezone": "UTC"}, 0)
        self.assertEqual(stamp, 900)

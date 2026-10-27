"""Existing plugin workers own only the runtime's two fixed comic file roles."""

import asyncio
import copy
import os
from pathlib import Path
import tempfile
import unittest
import uuid

from aiohttp import web

from services.platform.contracts import Fault
from services.platform.media.comics.plugin import (
    ComicFileWorkers,
    capability,
    configuration,
)
from services.platform.plugins import PluginManager
from services.platform.plugins.ports import capability as host_capability


class FileRoles:
    def __init__(self):
        self.owner = None
        self.running = False
        self.failed = False
        self.calls = []

    def result(self):
        state = "failed" if self.failed else "ready" if self.running else "stopped"
        role = "failed" if self.failed else "running" if self.running else "stopped"
        return {"state": state, "roles": {"inputs": role, "publisher": role}}

    def check(self, owner):
        if self.owner is not None and self.owner != owner:
            raise Fault("comic_file_owner_conflict", 409)

    async def start(self, owner):
        self.calls.append(("start", owner))
        self.check(owner)
        self.owner, self.running, self.failed = owner, True, False
        return self.result()

    async def stop(self, owner):
        self.calls.append(("stop", owner))
        self.check(owner)
        self.owner, self.running, self.failed = None, False, False
        return self.result()

    async def status(self, owner):
        self.calls.append(("status", owner))
        self.check(owner)
        return self.result()


class ComicPluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = {"database_path": str(Path(self.temp.name) / "platform.db")}
        self.roles = FileRoles()
        self.host = PluginManager(self.settings, comic_files=self.roles)
        self.addAsyncCleanup(self.close_host)
        await self.host.start()
        self.instance = self.host.repository.list("instances")[0]

    async def close_host(self):
        await self.host.close()

    async def test_default_uses_normal_ledger_and_real_worker_status_is_read_only(self):
        self.assertEqual(self.instance["desired_state"], "enabled")
        self.assertEqual(len(self.host.repository.list("instances")), 1)
        view = await self.host.projection(self.instance)
        self.assertEqual(view["state"], "ready", view)
        self.assertEqual(view["file_runtime"], self.roles.result())
        starts = self.roles.calls.count(("start", self.instance["id"]))
        call = await self.host.executions.invoke(
            self.instance["id"], "status", {}, "status-1", "synthetic-admin"
        )
        for _ in range(200):
            document = self.host.repository.call(call["id"])
            if document["execution"] not in {"accepted", "running"}:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(document["execution"], "succeeded", document)
        self.assertEqual(document["result"], self.roles.result())
        self.assertEqual(self.roles.calls.count(("start", self.instance["id"])), starts)

    async def test_disable_is_persistent_and_restart_does_not_enable_it(self):
        result = await self.host.state(self.instance["id"], False, self.instance["revision"])
        self.assertEqual(result["state"], "stopped")
        self.assertIsNone(self.roles.owner)
        starts = sum(operation == "start" for operation, _ in self.roles.calls)
        await self.host.close()
        self.host = PluginManager(self.settings, comic_files=self.roles)
        await self.host.start()
        self.assertEqual(len(self.host.repository.list("instances")), 1)
        self.assertEqual(sum(op == "start" for op, _ in self.roles.calls), starts)
        self.assertEqual((await self.host.view())["instances"][0]["state"], "stopped")

    async def test_enabled_restart_recovers_same_instance_and_shutdown_releases_owner(self):
        identifier = self.instance["id"]
        await self.host.close()
        self.assertIsNone(self.roles.owner)
        self.host = PluginManager(self.settings, comic_files=self.roles)
        await self.host.start()
        self.assertEqual(self.roles.owner, identifier)
        self.assertEqual((await self.host.view())["instances"][0]["state"], "ready")
        self.assertEqual(len(self.host.repository.list("instances")), 1)

    async def test_second_instance_cannot_stop_or_steal_original_file_roles(self):
        second = await self.host.create("tianshu.comics-files@1.0.0", "Second", "second")
        with self.assertRaises(Fault) as error:
            await self.host.state(second["id"], True, second["revision"])
        self.assertEqual(error.exception.code, "comic_file_owner_conflict")
        self.assertEqual(self.roles.owner, self.instance["id"])
        view = await self.host.projection(await self.host.instance(second["id"]))
        self.assertEqual(view["state"], "error")
        self.assertEqual(view["desired_state"], "disabled")
        self.assertTrue(self.roles.running)

    async def test_rejected_second_owner_does_not_take_over_after_host_restart(self):
        second = await self.host.create("tianshu.comics-files@1.0.0", "Second", "second")
        with self.assertRaises(Fault):
            await self.host.state(second["id"], True, second["revision"])
        await self.host.close()
        self.host = PluginManager(self.settings, comic_files=self.roles)
        await self.host.start()
        self.assertEqual(self.roles.owner, self.instance["id"])
        self.assertEqual((await self.host.instance(second["id"]))["desired_state"], "disabled")

    async def test_stop_failure_keeps_actual_running_state_visible(self):
        original = self.roles.stop

        async def fail(owner):
            raise Fault("comic_file_runtime_unavailable", 503)

        self.roles.stop = fail
        try:
            with self.assertRaises(Fault):
                await self.host.state(self.instance["id"], False, self.instance["revision"])
            view = await self.host.projection(await self.host.instance(self.instance["id"]))
            self.assertEqual(view["desired_state"], "disabled")
            self.assertEqual(view["state"], "error")
            self.assertEqual(view["file_runtime"]["state"], "ready")
        finally:
            self.roles.stop = original
            await self.roles.stop(self.instance["id"])

    async def test_helper_failure_is_not_reported_as_ready_and_status_never_restarts(self):
        self.roles.failed = True
        starts = sum(operation == "start" for operation, _ in self.roles.calls)
        view = await self.host.projection(self.instance)
        self.assertEqual(view["state"], "error")
        self.assertEqual(view["file_runtime"]["state"], "failed")
        self.assertEqual(sum(op == "start" for op, _ in self.roles.calls), starts)

    async def test_plugin_crash_releases_helpers_without_replaying_file_work(self):
        process = self.host.workers[self.instance["id"]][0]
        process.process.kill()
        await process.process.wait()
        for _ in range(200):
            if self.roles.owner is None:
                break
            await asyncio.sleep(0.01)
        self.assertIsNone(self.roles.owner)
        self.assertFalse(self.roles.running)
        view = await self.host.projection(self.instance)
        self.assertEqual(view["state"], "error")
        self.assertEqual(self.host.repository.calls(), [])

    async def test_capability_rejects_uploaded_clone_and_invocation_lifecycle(self):
        package = await self.host.package("tianshu.comics-files@1.0.0")
        context = {"package": package, "call": None, "instance": self.instance["id"]}
        forged = copy.deepcopy(context)
        forged["package"]["source"] = {"type": "zip"}
        with self.assertRaises(Fault):
            await capability(self.host, forged, "start")
        with self.assertRaises(Fault):
            await capability(self.host, {**context, "call": "synthetic-call"}, "stop")
        for key in ("command", "uid", "path", "socket"):
            with self.assertRaises(Fault):
                await host_capability(
                    self.host.executions, context, "comics.files", "start", {key: "untrusted"}
                )
        self.assertEqual(self.roles.owner, self.instance["id"])

    async def test_absent_provider_keeps_original_plugins_and_blocks_comic_instance(self):
        await self.host.close()
        self.host = PluginManager(self.settings)
        await self.host.start()
        self.assertNotIn("comics.files", self.host.available_capabilities)
        view = await self.host.projection(await self.host.instance(self.instance["id"]))
        self.assertEqual(view["state"], "blocked")


class ComicRuntimeClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_polls_fixed_pair_until_ready_and_status_does_not_start(self):
        calls = []

        class Pending(ComicFileWorkers):
            async def request(self, owner, operation):
                calls.append(operation)
                return {
                    "state": "starting" if len(calls) == 1 else "ready",
                    "roles": {"inputs": "running", "publisher": "running"},
                }

        client = Pending("unused")
        self.assertEqual((await client.start(str(uuid.uuid4())))["state"], "ready")
        self.assertEqual(calls, ["start", "status"])
        await client.status(str(uuid.uuid4()))
        self.assertEqual(calls[-1], "status")

    async def test_start_deadline_fails_instead_of_inventing_ready(self):
        class Pending(ComicFileWorkers):
            async def request(self, owner, operation):
                return {
                    "state": "starting",
                    "roles": {"inputs": "running", "publisher": "stopped"},
                }

        with self.assertRaises(Fault) as error:
            await Pending("unused", startup_timeout=0.01).start(str(uuid.uuid4()))
        self.assertEqual(error.exception.code, "comic_file_runtime_unavailable")

    async def test_only_runtime_socket_is_configurable(self):
        self.assertIsNone(configuration({}))
        self.assertEqual(
            configuration({"plugins": {"comic_files": {"runtime_socket": "/run/comic.sock"}}}),
            {"runtime_socket": "/run/comic.sock"},
        )
        for value in ({}, {"runtime_socket": "relative"}, {"runtime_socket": "/x", "uid": 0}):
            with self.assertRaises(Fault):
                configuration({"plugins": {"comic_files": value}})

    @unittest.skipUnless(os.name == "posix", "aiohttp Unix server requires POSIX event loop")
    async def test_actual_unix_control_body_status_and_response_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "control.sock")
            roles = FileRoles()
            seen = []

            async def handle(request):
                body = await request.json()
                seen.append((request.match_info["operation"], body))
                return web.json_response(
                    await getattr(roles, request.match_info["operation"])(body["owner"])
                )

            app = web.Application()
            app.router.add_post("/v1/comics/{operation}", handle)
            runner = web.AppRunner(app)
            await runner.setup()
            self.addAsyncCleanup(runner.cleanup)
            await web.UnixSite(runner, path).start()
            client = ComicFileWorkers(path)
            owner = str(uuid.uuid4())
            self.assertEqual((await client.start(owner))["state"], "ready")
            self.assertEqual((await client.status(owner))["state"], "ready")
            self.assertEqual((await client.stop(owner))["state"], "stopped")
            self.assertTrue(all(body == {"owner": owner} for _, body in seen))

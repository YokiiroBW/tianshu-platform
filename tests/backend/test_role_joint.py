"""Real TLS owners and gateway; only the paid model endpoint is recorded."""

import asyncio
import json
import os
import secrets
import shutil
import subprocess
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

from aiohttp import web
from services.platform.contracts import Fault, utc
from services.platform.provider_catalog import ProviderCatalog
from services.platform.server import create_app
from services.platform.web_console import WebConsole
from ts050_source_support import reserve
from ts050_support import CONTRACT
from web_joint_scenarios import WebJoint
from tianshu_gateway.config import ClientGrant
from tianshu_gateway.server import Settings

WebJoint.__test__ = False


class RoleJoint(WebJoint):
    __test__ = True
    test_real_web_login_source_core_memory_gateway_and_persistent_sender = None

    @property
    def life_fixture(self):
        return self._testMethodName == "test_new_runtime_role_life_reads_and_generation_without_dialogue"

    def make_platform_app(self):
        self.console = WebConsole(self.platform)
        app = create_app(self.platform, console=self.console)
        self.provider_calls = getattr(self, "provider_calls", [])

        @web.middleware
        async def record_provider(request, handler):
            response = await handler(request)
            if "provider" in request.path:
                self.provider_calls.append((request.path, response.status))
            if self.life_fixture and request.path == "/api/web/life/retry" and response.status == 200:
                self.life_retry_unlocked = True
            return response

        app.middlewares.insert(0, record_provider)
        return app

    def platform_settings(self):
        settings = super().platform_settings()
        self.set_env("TS_ROLE_GATEWAY_MANAGE", "synthetic-role-" + secrets.token_urlsafe(30))
        self.set_env("TS_ROLE_MEMORY_ADMIN", "synthetic-role-" + secrets.token_urlsafe(30))
        self.set_env("TS_ROLE_PERSONA", "synthetic-role-" + secrets.token_urlsafe(30))
        self.set_env("TS_ROLE_PROVIDER_KEY", self.tokens["MODEL"].removeprefix("synthetic-source-"))
        self.set_env("TS_ROLE_BOT_ADMIN", "synthetic-role-" + secrets.token_urlsafe(30))
        self.set_env("SSL_CERT_FILE", str(self.ca))
        catalog_path = self.directory / "provider-catalog"
        catalog = ProviderCatalog(catalog_path, create=True)
        self.providers = []
        for label in ("A", "B"):
            provider = catalog.save(
                client_id=str(uuid.uuid4()),
                name="Synthetic " + label,
                base_url=self.model_url + "/v1",
                model_id="role-model-" + label,
                api_key=self.tokens["MODEL"],
            )
            catalog.record_test(
                client_id=str(uuid.uuid4()),
                provider_id=provider["provider_id"],
                expected_revision=1,
                outcome="succeeded",
            )
            self.providers.append(provider)
        catalog.set_default(
            client_id=str(uuid.uuid4()),
            provider_id=self.providers[0]["provider_id"],
            expected_revision=1,
            expected_default_revision=0,
        )
        settings["provider_self_service"] = {
            "directory": str(catalog_path),
            "gateway_url": "https://127.0.0.1:1",
            "gateway_token_env": "TS_ROLE_GATEWAY_MANAGE",
        }
        settings["role_runtime"] = {
            "enabled": True,
            "memory": {
                "base_url": self.memory_url,
                "token_env": "TS_ROLE_MEMORY_ADMIN",
                "ca_file": str(self.ca),
                "timeout_seconds": 15,
            },
        }
        settings["principals"]["operator"]["actions"].append("role.manage")
        settings["principals"]["core"]["actions"].extend(["config.select", "qq.admin.check"])
        settings["principals"]["gateway"]["actions"].append("provider.runtime")
        settings["principals"]["operator"]["actions"].append("bot.manage")
        settings["principals"]["bot-admin"] = {
            "kind": "service",
            "service": "platform",
            "token_env": "TS_ROLE_BOT_ADMIN",
            "actions": ["source.register", "source.dispatch", "mapping.prepare"],
        }
        settings["bot_connections"] = {"principal": "bot-admin", "slots": {}}
        settings["bot_adapter_self_service"] = {
            "directory": str(self.directory / "bot-adapters"),
            "allowed_cidrs": ["127.0.0.0/8"],
            "actors": [{"id": "actor:a", "label": "Existing synthetic actor"}],
        }
        if self.life_fixture:
            self.set_env("TS_ROLE_LIFE_READ", "synthetic-life-" + secrets.token_urlsafe(30))
            settings["principals"]["operator"]["actions"].append("life.read")
            settings["web_life"] = {
                "enabled": True, "base_url": self.core_url,
                "token_env": "TS_ROLE_LIFE_READ", "ca_file": str(self.ca),
            }
        return settings

    def make_memory_config(self):
        config = super().make_memory_config()
        config["role_grants_database_path"] = str(self.directory / "memory-role-grants.sqlite")
        config["callers"]["companion"]["allow_runtime_roles"] = True
        config["callers"]["platform"] = {
            "token": os.environ["TS_ROLE_MEMORY_ADMIN"],
            "role_admin": True,
        }
        return config

    def make_core_config(self):
        config = super().make_core_config()
        config["personas"] = {"admin_token_env": "TS_ROLE_PERSONA"}
        config["services"]["qq_admin"] = dict(config["services"]["platform"])
        config["provider_self_service"] = True
        config["bot_binding_management_enabled"] = True
        config["services"]["provider_selector"] = {
            "url": self.platform_url,
            "token_env": "TS050_SOURCE_CORE_PLATFORM",
            "ca_file": str(self.ca),
        }
        # Other role scenarios isolate their existing chat/model assertions from
        # the independent background life worker. The life scenario uses normal
        # default generation through the configured provider selector.
        if self.life_fixture:
            config["roles"] = {}
            config["bindings"] = {}
            config["bot_binding_management_enabled"] = False
            config["callers"]["platform_life"] = {"token_env": "TS_ROLE_LIFE_READ"}
            config["life_readers"] = {
                "platform_life": {
                    "reader_id": "reader:platform-life", "actor_ids": [], "runtime_roles": True,
                },
            }
        else:
            config["life_writing"] = False
        return config

    async def start_gateway(self):
        settings = Settings(
            str(CONTRACT),
            str(self.directory / "gateway.sqlite"),
            self.platform_url,
            "secret-ref:source/platform",
            "TS050_SOURCE_CONFIG_ORIGIN",
            {
                "secret-ref:source/platform": "TS050_SOURCE_GATEWAY_PLATFORM",
                "secret-ref:source/core": "TS050_SOURCE_GATEWAY_CORE",
                "secret-ref:fixture/provider-a": "TS050_SOURCE_MODEL",
            },
            [
                {"base_url": url, "addresses": ["127.0.0.1"], "allow_private_http": False}
                for url in (self.platform_url, self.model_url + "/v1")
            ],
            [ClientGrant("companion", "secret-ref:source/core", "provider-fixture", 7, True)],
            config_refresh_seconds=0,
            provider_self_service=True,
        )
        path = self.directory / "gateway-settings.json"
        configure = getattr(self, "configure_gateway", None)
        if configure is not None:
            configure(settings)
        path.write_text(json.dumps(asdict(settings)), "utf-8")
        sock = reserve()
        port = sock.getsockname()[1]
        sock.close()
        self.gateway_url = f"https://127.0.0.1:{port}"
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(Path(__file__).with_name("gateway_loopback_fixture.py")),
                "--settings",
                str(path),
                "--port",
                str(port),
                "--tls-cert",
                str(self.cert),
                "--tls-key",
                str(self.key),
            ],
            cwd=Path(os.environ["TS_ROLE_GATEWAY_ROOT"]),
            env=dict(os.environ, SSL_CERT_FILE=str(self.ca)),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.gateway_process = process

        async def stop():
            if process.poll() is None:
                process.terminate()
                await asyncio.to_thread(process.wait, 5)

        self.resources.append(stop)
        async with asyncio.timeout(10):
            while True:
                self.assertIsNone(process.poll(), "gateway exited")
                try:
                    response = await self.client.get(
                        self.gateway_url + "/internal/v1/model-requests/startup-probe",
                        headers={"Authorization": self.bearer("GATEWAY_CORE")},
                    )
                    self.assertEqual(response.status_code, 404)
                    break
                except Exception:
                    await asyncio.sleep(0.05)

    async def record_model(self, request):
        if self.life_fixture:
            self.assertEqual(request.headers.get("Authorization"), self.bearer("MODEL"))
            body = await request.json()
            material = json.loads(body["messages"][-1]["content"])
            plan = body["messages"][0]["content"].startswith("Create today's fictional intentions")
            content = json.dumps({"entries": [
                {"minute": entry["minute"], "activity": entry["activity"],
                 "detail": "按自己的节奏在虚构小屋里安排这一阶段。"}
                for entry in material["schedule"]
            ]}, ensure_ascii=False) if plan else "我在书桌前整理今天的想法，翻开笔记本记下一段温和的感受。"
            if plan and body["model"] == "role-model-B" and not getattr(self, "life_retry_unlocked", False):
                content = '{"entries":[]}'
            self.model_requests.append({"body": body, "kind": "plan" if plan else "stage"})
            return web.json_response({
                "id": "synthetic-life-model", "object": "chat.completion", "created": 1,
                "model": body["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            })
        return await super(WebJoint, self).record_model(request)

    async def test_new_runtime_role_life_reads_and_generation_without_dialogue(self):
        await self.browser_roles([], "role_life_browser.mjs")
        actor = self.trace["browser"]["actor"]
        role = self.platform.role_runtime.get(actor)
        self.assertEqual(role["state"], "disabled")
        self.assertEqual(role["capabilities"], [])
        self.assertFalse(self.core._role_allows(actor, "dialogue"))
        self.assertTrue(self.life_retry_unlocked, "real UI retry was never accepted")
        self.assertTrue(any(
            row["body"]["model"] == "role-model-B" and row["kind"] == "plan"
            for row in self.model_requests
        ))
        self.assertTrue(any(
            row["body"]["model"] == "role-model-B" and row["kind"] == "stage"
            for row in self.model_requests
        ))
        for name in ("role-life-desktop.png", "role-life-mobile.png"):
            shutil.copy2(self.directory / name, Path(os.environ["TS050_RUNTIME"]) / "results" / name)

    async def test_real_role_apply_and_model_routing(self):
        profiles = []
        for label in ("A", "B"):
            result = await self.client.post(
                self.core_url + "/internal/v1/persona/manage",
                headers={"Authorization": "Bearer " + os.environ["TS_ROLE_PERSONA"]},
                json={
                    "operation": "create_profile",
                    "request_id": str(uuid.uuid4()),
                    "operator": "joint-test",
                    "reason": "synthetic",
                    "name": "Profile " + label,
                    "description": "",
                    "content": {"persona": "Role " + label + " | synthetic persona"},
                },
            )
            self.assertEqual(result.status_code, 200, result.text)
            profiles.append(result.json()["item"])
        await self.browser_roles(profiles)
        self.assertEqual(len(self.platform.role_runtime.active_actors()), 1)
        self.assertEqual(
            {r["body"]["model"] for r in self.model_requests}, {"role-model-A", "role-model-B"}
        )
        self.assertEqual({r["actor"] for r in self.model_requests},
                         {"Role A", "Role B", "actor:a"})
        adopted = self.platform.role_runtime.get("actor:a")
        self.assertTrue(adopted["legacy"])
        self.assertIsNone(adopted["profile_id"])
        self.assertEqual(adopted["provider_id"], self.providers[1]["provider_id"])
        self.assertEqual(adopted["state"], "disabled")
        self.assertNotIn("actor:a", self.core.roles)
        memory_status = await self.client.post(
            self.memory_url + "/internal/v1/role-runtime/authorize",
            headers={"Authorization": "Bearer " + os.environ["TS_ROLE_MEMORY_ADMIN"]},
            json={"operation": "status", "actor_id": "actor:a"},
        )
        self.assertEqual(memory_status.status_code, 200, memory_status.text)
        self.assertFalse(memory_status.json()["enabled"])
        self.assertGreaterEqual(
            sum(path.endswith("/select") and status == 200 for path, status in self.provider_calls),
            2,
        )
        self.assertGreaterEqual(
            sum(
                path.endswith("/runtime") and status == 200 for path, status in self.provider_calls
            ),
            2,
        )
        await self.exercise_bot_connections()
        evidence = Path(os.environ["TS050_RUNTIME"]) / "results"
        evidence.mkdir(exist_ok=True)
        for name in ("roles-desktop.png", "roles-mobile.png", "roles-existing.png"):
            shutil.copy2(self.directory / name, evidence / name)
        (evidence / "joint-summary.json").write_text(
            json.dumps(
                {
                    "models": sorted(r["body"]["model"] for r in self.model_requests),
                    "personas": sorted(r["actor"] for r in self.model_requests),
                    "browser": self.trace["browser"],
                    "provider_calls": self.provider_calls,
                    "bot_deliveries": self.bot_deliveries,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    async def test_lost_memory_receipt_and_profile_edit_reuse_approved_snapshot(self):
        profile = self.core.personas.manage({
            "operation": "create_profile", "request_id": str(uuid.uuid4()),
            "operator": "joint-test", "reason": "synthetic", "name": "Pinned source",
            "description": "", "content": {"persona": "First approved persona", "tone": "calm"},
        })["item"]
        client = str(uuid.uuid4())
        manager = self.platform.role_runtime
        body = {
            "client_id": client, "actor_id": None, "expected_version": 0,
            "name": "Pinned role", "profile_id": profile["id"],
            "profile_version": profile["version"],
            "provider_id": self.providers[0]["provider_id"], "provider_revision": 1,
            "enabled": True, "capabilities": ["dialogue", "memory.read"],
        }
        row = manager._begin(body)
        original_remote = manager._remote
        lost = False

        async def lose_after_memory_apply(settings, path, payload):
            nonlocal lost
            answer = await original_remote(settings, path, payload)
            if payload.get("request_id") == client + ":memory" and not lost:
                lost = True
                raise Fault("dependency_unavailable", 503)
            return answer

        manager._remote = lose_after_memory_apply
        try:
            with self.assertRaises(Fault):
                await manager._resume(row)
            self.assertEqual(row["stage"], "core_paused")
            actor = row["actor_id"]
            paused = self.core.role_runtime.get(actor)
            self.assertFalse(paused["enabled"])
            self.assertEqual(paused["application_id"], client)
            self.assertEqual(paused["profile_version"], profile["version"])
            approvals = self.core.store.list("persona_approvals", actor)
            publications = self.core.store.list("persona_publications", actor)
            self.assertEqual(len(approvals), 1)
            self.assertEqual(approvals[0]["operator"], self.console.config["principal"])
            self.assertEqual(len(publications), 1)
            self.assertEqual(publications[0]["operator"], self.console.config["principal"])
            self.assertEqual(publications[0]["kind"], "publish")
            linked = self.core.personas._profile(profile["id"])
            self.assertEqual(linked["last_applied_target"], actor)
            self.core.personas.manage({
                "operation": "save_profile", "request_id": str(uuid.uuid4()),
                "operator": "joint-test", "reason": "synthetic edit",
                "subject": profile["id"], "expected": linked["version"],
                "name": "Pinned source", "description": "",
                "content": {"persona": "Second persona", "tone": "warm"},
            })
        finally:
            manager._remote = original_remote
        active = await manager._resume(row)
        self.assertEqual(active["state"], "active")
        self.assertEqual(active["profile_revision"], paused["profile_revision"])
        self.assertEqual(self.core.role_runtime.pin(actor)["persona"], "First approved persona")
        self.assertEqual(len(self.core.store.list("persona_approvals", actor)), 1)
        self.assertEqual(len(self.core.store.list("persona_publications", actor)), 1)
        memory_status = await original_remote(
            manager.config["memory"], "/internal/v1/role-runtime/authorize",
            {"operation": "status", "actor_id": actor},
        )
        self.assertEqual((memory_status["version"], memory_status["enabled"]), (1, True))
        def revoked_provider(_):
            raise AssertionError("Disabling an active role must not inspect its provider")

        manager._provider = revoked_provider
        disable = {**body, "client_id": str(uuid.uuid4()), "actor_id": actor,
                   "expected_version": 1, "enabled": False,
                   "profile_version": linked["version"] + 1,
                   "provider_revision": 999}
        disabled = await manager._resume(manager._begin(disable))
        self.assertEqual(disabled["state"], "disabled")
        self.assertEqual(disabled["profile_version"], profile["version"])
        self.assertNotIn(actor, self.core.roles)
        denied_memory = await original_remote(
            manager.config["memory"], "/internal/v1/role-runtime/authorize",
            {"operation": "status", "actor_id": actor},
        )
        self.assertFalse(denied_memory["enabled"])
        self.assertEqual(manager._begin(body)["state"], "disabled")
        stale_enable = {
            "operation": "apply", "request_id": client + ":enable",
            "application_id": client, "operator": self.console.config["principal"],
            "actor_id": actor, "expected_version": 1,
            "name": body["name"], "profile_id": body["profile_id"],
            "profile_version": body["profile_version"], "enabled": True,
            "capabilities": body["capabilities"],
        }
        self.assertTrue(self.core.manage_role("platform", stale_enable)["enabled"])
        self.assertNotIn(actor, self.core.roles)

    async def test_browser_failed_retry_then_edit_uses_new_intent(self):
        profile = self.core.personas.manage({
            "operation": "create_profile", "request_id": str(uuid.uuid4()),
            "operator": "joint-test", "reason": "synthetic", "name": "Retry source",
            "description": "", "content": {"persona": "Retry persona"},
        })["item"]
        manager = self.platform.role_runtime
        original_remote = manager._remote
        lost = False

        async def lose_first_memory_receipt(settings, path, payload):
            nonlocal lost
            answer = await original_remote(settings, path, payload)
            if path.endswith("/role-runtime/authorize") and payload.get("request_id", "").endswith(":memory") and not lost:
                lost = True
                raise Fault("dependency_unavailable", 503)
            return answer

        manager._remote = lose_first_memory_receipt
        try:
            await self.browser_roles([profile], "role_retry_browser.mjs")
        finally:
            manager._remote = original_remote
        self.assertTrue(lost)
        self.assertEqual(self.trace["browser"]["flow"], "pending-retry-edit")

    async def test_cancel_first_static_adoption_denies_core_before_memory_and_restart(self):
        manager = self.platform.role_runtime
        original_remote = manager._remote
        originals = {}
        for actor, committed in (("actor:a", False), ("actor:b", True)):
            self.assertTrue(self.core._role_allows(actor, "dialogue"))
            original_persona = self.core.personas.pin(actor)
            original_bindings = {
                key: tuple(value["actor_ids"])
                for key, value in self.core.bindings.items()
                if actor in value["actor_ids"]
            }
            originals[actor] = (original_persona["revision_id"], original_bindings)
            client = str(uuid.uuid4())
            body = {
                "client_id": client, "actor_id": actor, "expected_version": 0,
                "name": "Adopt " + actor, "profile_id": None, "profile_version": None,
                "provider_id": self.providers[0]["provider_id"], "provider_revision": 1,
                "enabled": True, "capabilities": ["dialogue", "memory.read"],
            }
            row = manager._begin(body)

            async def lose_first_pause(settings, path, payload):
                if payload.get("request_id") == client + ":pause":
                    if committed:
                        await original_remote(settings, path, payload)
                    raise Fault("dependency_unavailable", 503)
                return await original_remote(settings, path, payload)

            manager._remote = lose_first_pause
            try:
                with self.assertRaises(Fault) as interrupted:
                    await manager._resume(row)
                manager._record_error(row, interrupted.exception)
            finally:
                manager._remote = original_remote
            self.assertEqual(row["stage"], "start")
            self.assertEqual(row["state"], "pending")
            self.assertEqual(self.core.role_runtime.get(actor) is not None, committed)
            cancel = manager._begin_cancel({
                "client_id": str(uuid.uuid4()), "actor_id": actor,
                "expected_version": row["version"],
            })

            async def core_unavailable(settings, path, payload):
                if path.endswith("/role-runtime/manage") and payload.get("operation") == "list":
                    raise Fault("dependency_unavailable", 503)
                return await original_remote(settings, path, payload)

            manager._remote = core_unavailable
            try:
                with self.assertRaises(Fault) as unavailable:
                    await manager._resume(cancel)
                manager._record_error(cancel, unavailable.exception)
            finally:
                manager._remote = original_remote
            self.assertEqual(manager.get(actor)["state"], "pending")
            stopped = await manager._resume(cancel)
            self.assertEqual(stopped["state"], "disabled")
            self.assertFalse(self.core.role_runtime.get(actor)["enabled"])
            self.assertFalse(self.core._role_allows(actor, "dialogue"))
            self.assertEqual(self.core.personas.pin(actor)["revision_id"],
                             original_persona["revision_id"])
            catalog = self.core.manage_role("platform", {"operation": "list"})
            self.assertNotIn(actor, [item["id"] for item in catalog["legacy_roles"]])
            for key, bindings in original_bindings.items():
                self.assertEqual(tuple(self.core.bindings[key]["actor_ids"]), bindings)
            grant = await original_remote(
                manager.config["memory"], "/internal/v1/role-runtime/authorize",
                {"operation": "status", "actor_id": actor},
            )
            self.assertFalse(grant["enabled"])
            self.assertEqual(manager._begin(body)["state"], "disabled")

        await self.restart_core()
        for actor, (revision, bindings) in originals.items():
            self.assertFalse(self.core._role_allows(actor, "dialogue"))
            self.assertFalse(self.core.role_runtime.get(actor)["enabled"])
            self.assertEqual(self.core.personas.pin(actor)["revision_id"], revision)
            for key, original in bindings.items():
                self.assertEqual(tuple(self.core.bindings[key]["actor_ids"]), original)

    async def test_cancel_static_adoption_with_unapplied_selected_profile(self):
        actor = "actor:a"
        original_persona = self.core.personas.pin(actor)
        manager = self.platform.role_runtime
        original_provider = manager._provider
        initial = {
            "client_id": str(uuid.uuid4()), "actor_id": actor, "expected_version": 0,
            "name": "Original static role", "profile_id": None, "profile_version": None,
            "provider_id": self.providers[0]["provider_id"], "provider_revision": 1,
            "enabled": True, "capabilities": ["dialogue", "memory.read"],
        }
        first = manager._begin(initial)

        def unavailable_provider(_):
            raise Fault("provider_unavailable", 409)

        manager._provider = unavailable_provider
        try:
            with self.assertRaises(Fault) as failed:
                await manager._resume(first)
            manager._record_error(first, failed.exception)
        finally:
            manager._provider = original_provider
        self.assertEqual(manager.get(actor)["state"], "failed")
        self.assertIsNone(self.core.role_runtime.get(actor))
        profile = self.core.personas.manage({
            "operation": "create_profile", "request_id": str(uuid.uuid4()),
            "operator": "joint-test", "reason": "synthetic", "name": "Proposed profile",
            "description": "", "content": {"persona": "Must remain unapplied"},
        })["item"]
        selected = {
            **initial, "client_id": str(uuid.uuid4()), "expected_version": 1,
            "profile_id": profile["id"], "profile_version": profile["version"],
        }
        pending = manager._begin(selected)
        original_remote = manager._remote

        async def unavailable_before_pause(settings, path, payload):
            if payload.get("request_id") == selected["client_id"] + ":pause":
                raise Fault("dependency_unavailable", 503)
            return await original_remote(settings, path, payload)

        manager._remote = unavailable_before_pause
        try:
            with self.assertRaises(Fault) as interrupted:
                await manager._resume(pending)
            manager._record_error(pending, interrupted.exception)
        finally:
            manager._remote = original_remote
        self.assertEqual(pending["stage"], "start")
        self.assertEqual(pending["profile_id"], profile["id"])
        self.assertIsNone(self.core.role_runtime.get(actor))
        stopped = await manager._resume(manager._begin_cancel({
            "client_id": str(uuid.uuid4()), "actor_id": actor,
            "expected_version": pending["version"],
        }))
        self.assertEqual(stopped["state"], "disabled")
        core_fact = self.core.role_runtime.get(actor)
        self.assertFalse(core_fact["enabled"])
        self.assertIsNone(core_fact["profile_id"])
        self.assertFalse(self.core._role_allows(actor, "dialogue"))
        self.assertEqual(self.core.personas.pin(actor)["revision_id"],
                         original_persona["revision_id"])
        self.assertEqual(self.core.store.list("persona_approvals", actor), [])
        self.assertIsNone(self.core.personas._profile(profile["id"]).get("last_applied_target"))
        grant = await original_remote(
            manager.config["memory"], "/internal/v1/role-runtime/authorize",
            {"operation": "status", "actor_id": actor},
        )
        self.assertFalse(grant["enabled"])

    async def test_optional_persona_browser_and_real_owners(self):
        await self.browser_roles([], "optional_persona_browser.mjs")
        profile = self.core.personas.manage({
            "operation": "create_profile", "request_id": str(uuid.uuid4()),
            "operator": "joint-test", "reason": "synthetic", "name": "Optional source",
            "description": "", "content": {"persona": "Distinct profile", "tone": "warm"},
        })["item"]
        await self.browser_roles([profile], "optional_persona_browser.mjs")
        self.assertEqual(len(self.core.store.list("persona_revisions", profile["id"])), 1)
        self.assertEqual(self.core.personas._profile(profile["id"])["version"], 3)
        roles = self.core.role_runtime.list()
        self.assertEqual(len(roles), 2)
        for role in roles:
            self.assertEqual(len(self.core.store.list("persona_revisions", role["actor_id"])),
                             3 if role["name"] == "Optional with profiles" else 1)
            self.assertFalse(role["enabled"])
            self.assertIsNone(role["profile_id"])
            self.assertEqual(self.core.personas._revision(role["persona_revision"])["content"],
                             {"persona": "Respond to the user's request clearly and accurately."})
            self.assertNotIn(role["actor_id"], self.core.roles)
        self.assertEqual(self.platform.role_runtime.active_actors(), [])
        self.assertEqual(len(self.model_requests), 0)

    async def browser_roles(self, profiles, script="role_joint_browser.mjs"):
        env = dict(
            os.environ,
            TS_ROLE_WEB_URL=self.platform_url,
            TS_ROLE_WEB_PASSWORD=self.password,
            TS_ROLE_OUTPUT=str(self.directory),
            TS_ROLE_PROFILES=json.dumps([p["id"] for p in profiles]),
            TS_ROLE_PROVIDERS=json.dumps([p["provider_id"] for p in self.providers]),
        )
        node = "C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
        result = await asyncio.to_thread(
            subprocess.run,
            [node, str(Path(__file__).with_name(script))],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=150 if self.life_fixture else 120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if self.life_fixture and result.returncode:
            result.stderr += "\nlife model requests: " + json.dumps([
                {"kind": row["kind"], "model": row["body"]["model"]}
                for row in self.model_requests
            ])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.trace["browser"] = json.loads(result.stdout)

    async def exercise_bot_connections(self):
        actors = [entry["actor"] for entry in self.trace["browser"]["roles"]]
        session = next(s for s in self.console.sessions.values() if s["authenticated"])
        old = self.platform.role_runtime.get(actors[0])
        body = {
            key: old[key]
            for key in (
                "actor_id",
                "name",
                "profile_id",
                "profile_version",
                "provider_id",
                "provider_revision",
                "capabilities",
            )
        }
        body.update(client_id=str(uuid.uuid4()), expected_version=old["version"], enabled=True)
        repaired = await self.platform.role_runtime.route(
            self.console, "/api/web/roles/apply", body, session
        )
        self.assertEqual(repaired["role"]["state"], "active")
        self.bot_events = {}
        self.bot_deliveries = []
        self.bot_bindings = {}
        self.bot_actor_for_connection = {}
        plugin_key = "synthetic-plugin-key-roles-123456789"

        async def plugin(request):
            self.assertEqual(request.headers.get("Authorization"), "Bearer " + plugin_key)
            body = await request.json()
            path = request.path.removeprefix("/tianshu/adapter/v1")
            if path == "/capabilities":
                return web.json_response(
                    {
                        "protocol": "tianshu.bot-adapter/v1",
                        "adapter": "nonebot",
                        "instance_id": "sdk:roles",
                        "capabilities": ["text"],
                        "max_outbound_utf8_bytes": 32768,
                        "accounts": [
                            {"id": number, "platform": "qq", "label": "Synthetic " + number}
                            for number in ("42", "43")
                        ],
                    }
                )
            if path == "/bindings/apply":
                result = {key: body[key] for key in ("connection_id", "revision", "enabled")}
                self.bot_bindings[body["connection_id"]] = result
                return web.json_response(result)
            if path == "/bindings/status":
                result = self.bot_bindings.get(body["connection_id"])
                return web.json_response({"found": result is not None, "binding": result})
            if path == "/events/poll":
                return web.json_response({"events": self.bot_events.get(body["connection_id"], [])})
            if path == "/events/ack":
                self.bot_events[body["connection_id"]] = []
                return web.json_response({"acknowledged": body["event_ids"]})
            if path == "/messages/send":
                delivery = body["delivery"]
                self.bot_deliveries.append(
                    {
                        "connection_id": body["connection_id"],
                        "actor_id": self.bot_actor_for_connection[body["connection_id"]],
                        "text": delivery["text"],
                    }
                )
                return web.json_response(
                    {
                        "reply_id": delivery["reply_id"],
                        "attempt_id": delivery["attempt_id"],
                        "state": "sent",
                        "channel_message_ids": ["sdk:" + delivery["reply_id"]],
                    }
                )
            if path == "/messages/status":
                return web.json_response({"found": False, "receipt": None})
            return web.json_response({"code": "not_found"}, status=404)

        app = web.Application()
        app.router.add_post("/tianshu/adapter/v1/{tail:.*}", plugin)
        address = await self.start_aio(app)
        adapter = self.platform.bot_adapters
        session["bot_management"] = self.console.clock() + 1800
        rows = []
        for index, actor in enumerate(actors):
            draft = await adapter.probe(
                {
                    "adapter": "nonebot",
                    "address": address,
                    "access_key": plugin_key,
                    "allow_private_http": True,
                    "ca_pem": self.ca.read_text(encoding="utf-8"),
                },
                session,
            )
            row = await adapter.create(
                {
                    "draft_id": draft["draft_id"],
                    "name": f"Synthetic bot {index}",
                    "account_id": str(42 + index),
                    "conversation": {"kind": "private", "id": "7"},
                    "allowed_authors": ["7"],
                    "actor_id": actor,
                    "client_id": str(uuid.uuid4()),
                },
                session,
            )
            self.assertEqual(row["state"], "disabled")
            row = await adapter.change(
                "enable",
                {
                    "id": row["id"],
                    "expected_revision": row["revision"],
                    "client_id": str(uuid.uuid4()),
                },
            )
            self.assertEqual(row["state"], "ready")
            self.bot_actor_for_connection[row["id"]] = actor
            rows.append(row)
        for index, row in enumerate(rows):
            event = {
                "schema_version": 1,
                "connection_id": row["id"],
                "platform_id": "sdk:roles",
                "self_id": row["account_id"],
                "event_id": f"sdk:roles:{index}",
                "revision": 1,
                "namespace": "qq",
                "conversation_id": "private:7",
                "thread_id": None,
                "account_id": "7",
                "sent_at": utc(self.platform.origins.clock()),
                "text": f"synthetic bot parallel {index}",
            }
            self.bot_events[row["id"]] = [{"id": f"event:{index}", "event": event}]
        try:
            async with asyncio.timeout(30):
                while len(self.bot_deliveries) < 2:
                    await adapter.pump_once()
                    await asyncio.sleep(0.15)
        except TimeoutError:
            self.fail(repr({
                "connections": [(r["state"], r["last_error"]) for r in adapter.catalog.all()],
                "events": self.bot_events,
                "deliveries": self.bot_deliveries,
                "model_requests": len(self.model_requests),
            }))
        self.assertEqual(len(self.bot_deliveries), 2)
        for index, row in enumerate(rows):
            matching = [item for item in self.bot_deliveries if item["connection_id"] == row["id"]]
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0]["actor_id"], actors[index])
            label = "B" if index else "A"
            self.assertTrue(matching[0]["text"].startswith(f"Role {label} recorded reply"))
        self.assertEqual(len(self.model_requests), 5)

"""Actual TLS Platform, Core, Memory, Knowledge and Gateway; synthetic external model only.

The fixture also supports a browser lifetime (TS_C4_HOLD_FILE): only a loopback URL,
synthetic login and temporary CA are written there, never any production information.
"""

import asyncio
import copy
import hashlib
import json
import os
import secrets
import time
import uuid
from pathlib import Path

import test_role_joint as role_fixture
from tianshu_memory.contracts import Contracts as MemoryContracts
from tianshu_memory.knowledge_content_migration import migrate as migrate_content
from tianshu_memory.knowledge_http import create_app as knowledge_app
from tianshu_memory.knowledge_migration import migrate as migrate_knowledge
from tianshu_memory.store import Store as MemoryStore
from ts050_source_support import reserve
from ts050_support import AppServer

from services.platform.role_runtime import RoleRuntime


class CompleteJoint(role_fixture.RoleJoint):
    @property
    def life_fixture(self):
        return True

    def platform_settings(self):
        self.knowledge_socket = reserve()
        self.knowledge_url = f"https://127.0.0.1:{self.knowledge_socket.getsockname()[1]}"
        self.resources.append(self.close_knowledge_socket)
        settings = super().platform_settings()
        self.set_env("TS_C4_KNOWLEDGE_ADMIN", "synthetic-knowledge-" + secrets.token_urlsafe(30))
        self.set_env("TS_C4_KNOWLEDGE_CORE", "synthetic-knowledge-" + secrets.token_urlsafe(30))
        self.set_env("TS_C4_KNOWLEDGE_ORIGIN", "synthetic-knowledge-" + secrets.token_urlsafe(30))
        owner = {
            "base_url": self.knowledge_url,
            "token_env": "TS_C4_KNOWLEDGE_ADMIN",
            "ca_file": str(self.ca),
        }
        settings["role_runtime"]["knowledge"] = owner
        settings["web_knowledge"] = {
            **owner,
            "enabled": True,
            "projects": [{"project_id": "fixture", "label": "合成资料库"}],
        }
        settings["principals"]["web-content-resolver"] = {
            "kind": "service",
            "service": "memory",
            "token_env": "TS_C4_KNOWLEDGE_ORIGIN",
            "actions": ["origin.resolve"],
            "resolver": {"caller": "platform", "purpose": "dialogue"},
        }
        for entry in settings["entries"].values():
            if entry["actor_id"] != "source:config":
                route = {"caller": "platform", "receiver": "memory", "purpose": "dialogue"}
                if route not in entry["routes"]:
                    entry["routes"].append(route)
        if (
            self._testMethodName
            == "test_association_requires_actual_second_author_and_memory_receipt"
        ):
            self.set_env("TS_C4_OTHER", "synthetic-other-" + secrets.token_urlsafe(30))
            self.other_account = {"namespace": "web", "immutable_account_id": "synthetic-other"}
            settings["principals"]["other"] = {
                "kind": "operator",
                "service": "platform",
                "token_env": "TS_C4_OTHER",
                "actions": ["source.register", "origin.issue"],
                "account": self.other_account,
            }
            source = copy.deepcopy(settings["input_entries"]["self_private"])
            original = source["actor_entries"][0]
            self.other_channel = {
                **source["channel"],
                "binding_id": "binding:other-private",
                "channel_conversation_id": "private:synthetic-other",
            }
            source.update(
                owner="other",
                account=self.other_account,
                channel=self.other_channel,
                actor_entries=["other:actor"],
                default_actor_ids=["actor:a"],
            )
            settings["input_entries"]["other-input"] = source
            actor = copy.deepcopy(settings["entries"][original])
            actor.update(owner="other", account=self.other_account, channel=self.other_channel)
            settings["entries"]["other:actor"] = actor
        settings["web"]["static_directory"] = os.environ.get(
            "TS_C4_STATIC_DIR",
            str(Path(__file__).resolve().parents[3] / "platform-ui/apps/web/dist"),
        )
        return settings

    async def close_knowledge_socket(self):
        self.knowledge_socket.close()

    def make_core_config(self):
        config = super().make_core_config()
        # The deployed web channel exists before any chat; role owner appends the new actor.
        config["bindings"] = role_fixture.WebJoint.make_core_config(self)["bindings"]
        for binding in config["bindings"].values():
            binding["actor_ids"] = []
        if hasattr(self, "other_channel"):
            config["bindings"][self.other_channel["binding_id"]] = copy.deepcopy(
                next(
                    item
                    for item in config["bindings"].values()
                    if item["audience"] == "self_private"
                )
            )
        config["life_readers"]["platform"] = {
            "reader_id": "reader:platform-manager",
            "actor_ids": [],
            "runtime_roles": True,
        }
        config["life_writing"] = False
        config["services"]["knowledge"] = {
            "url": self.knowledge_url,
            "token_env": "TS_C4_KNOWLEDGE_CORE",
            "ca_file": str(self.ca),
        }
        return config

    async def start_asgi(self, app, owner, sock):
        if owner == "memory":
            self.memory.store.migrate_context(self.directory / "backups/context.sqlite")
            self.memory.contracts.load_context()
            self.memory_config["memory_context_proofs"] = {
                "url": self.platform_url + "/internal/v1/memory-context/proof/verify",
                "token": self.tokens["MEMORY_PLATFORM"],
                "ca_file": str(self.ca),
            }
            self.memory_config["callers"]["companion"]["operations"].extend(
                [
                    "context_" + name
                    for name in ("query", "propose", "receipt", "batch", "association")
                ]
            )
            self.memory_config["callers"]["platform"].update(
                issuer="platform",
                issuer_url=self.platform_url + "/internal/v1/origins/resolve",
                issuer_token=os.environ["TS_C4_KNOWLEDGE_ORIGIN"],
                issuer_ca_file=str(self.ca),
                operations=["context_association"],
                allow_runtime_roles=True,
            )
            self.write_memory_config()
            await self.start_knowledge()
        # Binary originals and SSE must traverse the real ASGI server without the old JSON-only recorder.
        server = AppServer()
        self.resources.append(server.close)
        await server.start(app, self.cert, self.key, sock=sock)
        self.ports.append(server.sock.getsockname()[1])
        return server

    async def start_knowledge(self):
        config = copy.deepcopy(self.memory_config)
        config.update(
            database_path=str(self.directory / "knowledge/data.sqlite"),
            role_grants_database_path=str(self.directory / "knowledge/roles.sqlite"),
        )
        config["source_sync"]["recovery_path"] = str(self.directory / "knowledge/source-guard.json")
        config.pop("memory_context_proofs", None)
        operations = [
            "content_" + name
            for name in ("acquire", "read", "original", "uploads", "upload_status", "access")
        ]
        config["callers"] = {
            "companion": {
                **config["callers"]["companion"],
                "token": os.environ["TS_C4_KNOWLEDGE_CORE"],
                "operations": operations[:5],
                "runtime_content": True,
            },
            "platform": {
                **config["callers"]["platform"],
                "token": os.environ["TS_C4_KNOWLEDGE_ADMIN"],
                "operations": operations,
                "runtime_content": True,
            },
        }
        # Preserve the production Knowledge /execute registration alongside its new private routes.
        config["knowledge"] = {
            "projects": {},
            "clients": {
                "platform": {
                    "credential_sha256": hashlib.sha256(
                        os.environ["TS_C4_KNOWLEDGE_ADMIN"].encode()
                    ).hexdigest(),
                    "permissions": ["read"],
                    "projects": [],
                }
            },
        }
        self.knowledge_config_path = self.directory / "knowledge-config.json"
        self.knowledge_config_path.write_text(json.dumps(config), encoding="utf-8")
        contracts = MemoryContracts(config["contract_directory"])
        contracts.load_sources()
        store = MemoryStore(
            config["database_path"], recovery_path=config["source_sync"]["recovery_path"]
        )
        store.migrate_profiles(self.directory / "backups/knowledge-profile.sqlite")
        store.migrate_sources(self.directory / "backups/knowledge-sources.sqlite", contracts)
        migrate_knowledge(store, self.directory / "backups/knowledge.sqlite")
        migrate_content(store, self.directory / "backups/knowledge-content.sqlite")
        app = knowledge_app(
            self.knowledge_config_path, "platform", self.knowledge_socket.getsockname()[1]
        )
        self.knowledge_server = await self.start_asgi(app, "knowledge", self.knowledge_socket)

    async def login(self):
        response = await self.client.get(self.platform_url + "/api/web/session")
        self.assertEqual(response.status_code, 200, response.text)
        headers = {"Origin": self.platform_url, "X-CSRF-Token": response.json()["csrf"]}
        response = await self.client.post(
            self.platform_url + "/api/web/login",
            headers=headers,
            json={"username": "integration-admin", "password": self.password},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.web_headers = {"Origin": self.platform_url, "X-CSRF-Token": response.json()["csrf"]}

    async def web(self, path, body, expected=200):
        response = await self.client.post(
            self.platform_url + "/api/web/" + path, headers=self.web_headers, json=body
        )
        self.assertEqual(response.status_code, expected, response.text)
        return response.json()

    async def add_role(self):
        await self.login()
        self.role_body = {
            "client_id": str(uuid.uuid4()),
            "actor_id": None,
            "expected_version": 0,
            "name": "联合生活角色",
            "profile_id": None,
            "profile_version": None,
            "provider_id": None,
            "provider_revision": None,
            "enabled": True,
            "capabilities": [],
        }
        result = await self.web("roles/apply", self.role_body)
        self.actor = result["role"]["actor_id"]
        self.assertEqual(result["role"]["state"], "active", result)
        return self.actor

    async def manage(self, operation, value, version=0):
        return await self.web(
            "life/runtime/manage",
            {
                "actor_id": self.actor,
                "client_id": str(uuid.uuid4()),
                "operation": operation,
                "expected_version": version,
                "value": value,
            },
        )

    async def read(self, resource, object_id=None, scope=None, **extra):
        return await self.web(
            "life/runtime/read",
            {
                "actor_id": self.actor,
                "resource": resource,
                "object_id": object_id,
                "expected_version": None,
                "limit": 20,
                "after": None,
                **extra,
            },
        )

    async def test_first_no_chat_upload_independent_knowledge_restart_disable(self):
        await self.add_role()
        raw = "实际原文：首次上传不需要先聊天。\n".encode()
        descriptor = await self.web(
            "content/uploads",
            {
                "actor_id": self.actor,
                "client_id": str(uuid.uuid4()),
                "value": {
                    "filename": "first.txt",
                    "media_type": "text/plain",
                    "size": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                },
            },
        )
        self.assertEqual(descriptor["state"], "pending")
        response = await self.client.post(
            self.platform_url + "/api/web/content/upload/" + descriptor["upload_id"],
            content=raw,
            headers={
                **self.web_headers,
                "Content-Type": "application/octet-stream",
                "X-Tianshu-Actor-Id": self.actor,
                "X-Tianshu-Request-Id": "upload:first",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        acquired = await self.manage(
            "content.acquire",
            {
                "query": {},
                "scope": {},
                "source": {"kind": "upload", "upload_id": descriptor["upload_id"]},
                "purpose": "read",
            },
        )
        reference = acquired["result"]["content_ref"]
        original = await self.client.post(
            self.platform_url + "/api/web/content/original",
            headers=self.web_headers,
            json={
                "actor_id": self.actor,
                "client_id": str(uuid.uuid4()),
                "value": {"content_ref": reference, "range": None},
            },
        )
        self.assertEqual(original.status_code, 200, original.text)
        self.assertEqual(original.content, raw)
        self.assertEqual(original.headers["X-Content-SHA256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(len(self.model_requests), 0)
        session = (await self.client.get(self.platform_url + "/api/web/session")).json()
        self.assertFalse(
            any(self.actor in item["actors"] for item in session["conversations"]),
            "life entry must not grant dialogue",
        )
        await self.restart_core()
        again = await self.client.post(
            self.platform_url + "/api/web/content/original",
            headers=self.web_headers,
            json={
                "actor_id": self.actor,
                "client_id": str(uuid.uuid4()),
                "value": {"content_ref": reference, "range": None},
            },
        )
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.content, raw)
        body = {
            **self.role_body,
            "actor_id": self.actor,
            "client_id": str(uuid.uuid4()),
            "expected_version": 1,
            "enabled": False,
        }
        disabled = await self.web("roles/apply", body)
        self.assertEqual(disabled["role"]["state"], "disabled", disabled)
        await self.web(
            "content/read",
            {
                "actor_id": self.actor,
                "client_id": str(uuid.uuid4()),
                "value": {
                    "content_ref": reference,
                    "range": {"unit": "characters", "start": 0, "end": 10},
                    "budget_bytes": 1024,
                },
            },
            expected=403,
        )
        status = await self.client.post(
            self.knowledge_url + "/internal/v1/role-runtime/authorize",
            headers={"Authorization": "Bearer " + os.environ["TS_C4_KNOWLEDGE_ADMIN"]},
            json={"operation": "status", "actor_id": self.actor},
        )
        self.assertEqual(status.status_code, 200, status.text)
        self.assertFalse(status.json()["enabled"])

    async def test_browser_lifetime(self):
        hold = os.environ.get("TS_C4_HOLD_FILE")
        if not hold:
            self.skipTest("only the explicit temporary browser fixture lifetime")
        await self.add_role()
        Path(hold).write_text(
            json.dumps(
                {
                    "url": self.platform_url,
                    "username": "integration-admin",
                    "password": self.password,
                    "actor_id": self.actor,
                    "ca_file": str(self.ca),
                }
            ),
            encoding="utf-8",
        )
        stop = Path(hold + ".stop")
        while not stop.exists():
            await asyncio.sleep(1)

    async def test_upgrade_existing_active_and_disabled_knowledge_enrolment(self):
        # Create real role facts under the old two-peer configuration, then upgrade
        # just the control-plane configuration. Knowledge starts with no grants.
        manager = self.platform.role_runtime
        knowledge = manager.config.pop("knowledge")
        active = await self.add_role()
        disabled = await self.add_role()
        result = await self.web(
            "roles/apply",
            {
                **self.role_body,
                "actor_id": disabled,
                "client_id": str(uuid.uuid4()),
                "expected_version": 1,
                "enabled": False,
            },
        )
        self.assertEqual(result["role"]["state"], "disabled")
        for actor in (active, disabled):
            legacy = manager.get(actor)
            self.assertIsNone(legacy.pop("knowledge_version"))
            manager._put(legacy)
            self.assertNotIn("knowledge_version", manager.get(actor))
        manager.config["knowledge"] = knowledge
        upgraded = RoleRuntime(self.platform)
        upgraded.console = self.console
        self.platform.role_runtime = upgraded
        self.assertFalse(upgraded.active(active))
        await upgraded.resume_pending()
        self.assertTrue(upgraded.active(active))
        self.assertEqual(upgraded.get(disabled)["state"], "disabled")
        for actor, enabled in ((active, True), (disabled, False)):
            status = await self.client.post(
                self.knowledge_url + "/internal/v1/role-runtime/authorize",
                headers={"Authorization": "Bearer " + os.environ["TS_C4_KNOWLEDGE_ADMIN"]},
                json={"operation": "status", "actor_id": actor},
            )
            self.assertEqual(status.status_code, 200, status.text)
            self.assertEqual(status.json()["enabled"], enabled)
            self.assertEqual(status.json()["version"], upgraded.get(actor)["knowledge_version"])
        self.actor = active
        self.assertEqual((await self.read("image_backend"))["resource"], "image_backend")
        day = await self.web("life/today", {"actor_id": active})
        self.assertEqual(day["actor_id"], active)
        self.assertEqual(len(self.model_requests), 0)

    async def test_association_requires_actual_second_author_and_memory_receipt(self):
        await self.add_role()
        self.platform.auth.entries["other:actor"]["actor_id"] = self.actor
        self.platform.sources.entries["other-input"]["default_actor_ids"] = [self.actor]
        header = "Bearer " + os.environ["TS_C4_OTHER"]
        origin = self.platform.origins.issue(header, "other:actor")
        ensured = await self.client.post(
            self.core_url + "/internal/v2/life/conversation/ensure",
            headers={"Authorization": "Bearer " + os.environ["TS_ROLE_LIFE_READ"]},
            json={
                "schema_version": 2,
                "actor_id": self.actor,
                "query": {
                    "schema_version": 1,
                    "request_id": "ensure:other",
                    "origin": {"assertion_ref": origin["assertion_ref"]},
                },
            },
        )
        self.assertEqual(ensured.status_code, 200, ensured.text)
        self.console.life_management._confirm_scope("other:actor", ensured.json())
        challenge = await self.web(
            "memory-links/begin",
            {
                "actor_id": self.actor,
                "target_account": self.other_account,
                "client_id": str(uuid.uuid4()),
            },
        )
        lookup = {"actor_id": self.actor, "challenge_id": challenge["challenge_id"]}
        pending = await self.web("memory-links/status", lookup)
        self.assertEqual(pending["consent_sentence"], challenge["consent_sentence"])
        refused = await self.web("memory-links/complete", lookup, expected=409)
        self.assertEqual(refused["code"], "consent_required")
        physical = {
            "message_key": {
                "channel": self.other_channel,
                "message_id": "consent:actual",
                "revision": 1,
            },
            "author": self.other_account,
            "sent_at": role_fixture.utc(time.time()),
            "kind": "message",
            "parts": [{"kind": "text", "text": challenge["consent_sentence"]}],
            "reply_refs": [],
            "mentioned_accounts": [],
        }
        forged = {**physical, "author": self.account}
        with self.assertRaises(role_fixture.Fault):
            self.platform.sources.register_input(header, "other-input", forged)
        self.platform.sources.register_input(header, "other-input", physical)
        status = await self.web("memory-links/status", lookup)
        self.assertEqual(status["state"], "consented")
        self.assertNotIn("consent_sentence", status)
        linked = await self.web("memory-links/complete", lookup)
        self.assertEqual(linked["state"], "linked", linked)
        self.assertEqual(await self.web("memory-links/complete", lookup), linked)
        revoked = await self.web(
            "memory-links/revoke",
            {
                **lookup,
                "client_id": "revoke:actual",
                "expected_version": linked["version"],
            },
        )
        self.assertEqual(revoked["state"], "revoked", revoked)
        self.assertEqual(len(self.model_requests), 0)


# This extends startup, not the unrelated old browser/model scenarios.
for name in dir(role_fixture.RoleJoint):
    if name.startswith("test_") and name not in CompleteJoint.__dict__:
        setattr(CompleteJoint, name, None)

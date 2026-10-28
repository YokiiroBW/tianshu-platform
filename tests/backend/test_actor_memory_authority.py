"""Actual registered role, live origin revocation and service-only actor memory checks."""

import copy
import os
import tempfile
import unittest
from unittest.mock import patch

import aiohttp

from fixtures import ENV, bearer, start_http
from services.platform.actor_memory_authority import PATH
from services.platform.contracts import canonical
from services.platform.origins import channel_key
from services.platform.server import create_app
from services.platform.service import Platform
from test_actor_capabilities import seed_roles
from web_fixtures import web_settings


class ActorMemoryAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ts316-authority-")
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)
        settings = web_settings(self.temp.name)
        for identity in ("companion", "memory", "gateway"):
            settings["principals"][identity]["actions"].append("actor.memory.authorize")
        settings["entries"]["chat-entry"]["actor_id"] = "actor:a"
        self.p = Platform(settings)
        seed_roles(self.p)
        self.role = self.p.role_runtime.get("actor:a")
        self.role["capabilities"] = ["memory.read", "memory.write"]
        self.p.role_runtime._put(self.role)
        entry = self.p.auth.entries["chat-entry"]
        with self.p.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO identities VALUES(?,?,?)",
                (canonical(entry["account"]), "person:requester", 1),
            )
            db.execute(
                "INSERT INTO channels VALUES(?,?)", (channel_key(entry), "conversation:group-a")
            )
        self.origin = self.p.origins.issue(bearer("CONNECTOR"), "chat-entry")["assertion_ref"]
        self.runner, self.url = await start_http(create_app(self.p))
        self.client = aiohttp.ClientSession()
        self.addAsyncCleanup(self.cleanup)

    async def cleanup(self):
        await self.client.close()
        await self.runner.cleanup()
        self.p.close()
        await __import__("asyncio").to_thread(self.p.local_work.executor.shutdown, wait=True)

    def request(self, operation="actor_check", **fields):
        value = {
            "schema_version": 1,
            "request_id": "synthetic-authority",
            "actor_id": "actor:a",
            "role_version": 7,
            "operation": operation,
        }
        if operation == "actor_check":
            value["requested_operation"] = "append_episode"
        else:
            value["context"] = {
                "purpose": "dialogue",
                "audience": {
                    "kind": "group",
                    "conversation_id": "conversation:group-a",
                    "assertion_ref": self.origin,
                },
            }
        return {**value, **fields}

    async def post(self, value, status=200, **headers):
        async with self.client.post(
            self.url + PATH, json=value, headers={"Authorization": bearer("MEMORY"), **headers}
        ) as response:
            answer = await response.json()
            self.assertEqual(response.status, status, answer)
            return answer

    async def test_current_role_operations_do_not_borrow_admin_or_affinity(self):
        first = await self.post(self.request())
        self.assertIn("append_episode", first["allowed_operations"])
        self.assertIn("query", first["allowed_operations"])
        self.role["capabilities"] = ["memory.read"]
        self.p.role_runtime._put(self.role)
        second = await self.post(self.request())
        self.assertNotIn("append_episode", second["allowed_operations"])
        self.assertNotEqual(first["policy_version"], second["policy_version"])
        self.role["enabled"] = False
        self.p.role_runtime._put(self.role)
        disabled = await self.post(self.request())
        self.assertEqual((disabled["state"], disabled["allowed_operations"]), ("disabled", []))
        missing = await self.post(self.request(actor_id="actor:missing"))
        self.assertEqual((missing["state"], missing["allowed_operations"]), ("missing", []))

    async def test_fresh_group_context_checks_actor_conversation_audience_and_revocation(self):
        value = self.request("context_check")
        self.assertTrue((await self.post(value))["allowed"])
        for audience in (
            {**value["context"]["audience"], "conversation_id": "conversation:other"},
            {
                "kind": "private",
                "person_id": "person:requester",
                "conversation_id": "conversation:group-a",
                "assertion_ref": self.origin,
            },
        ):
            await self.post(
                {**value, "context": {"purpose": "dialogue", "audience": audience}}, 403
            )
        self.p.role_runtime._put(
            {**self.p.role_runtime.get("actor:b"), "capabilities": ["memory.read"]}
        )
        await self.post({**value, "actor_id": "actor:b"}, 403)
        self.p.origins.revoke(bearer("ADMIN"), "origin", self.origin)
        await self.post(value, 403)

    async def test_actor_only_needs_role_read_and_version_but_no_external_origin(self):
        value = self.request(
            "context_check", context={"purpose": "plan", "audience": {"kind": "actor_only"}}
        )
        self.assertTrue((await self.post(value))["allowed"])
        await self.post({**value, "role_version": 8}, 409)
        self.role["capabilities"] = ["memory.write"]
        self.p.role_runtime._put(self.role)
        self.assertFalse((await self.post(value))["allowed"])

    async def test_transport_identity_is_actual_and_browser_or_forged_authority_is_rejected(self):
        for headers in (
            {"Authorization": bearer("GATEWAY")},
            {"Cookie": "session=fake"},
            {"Origin": self.url},
        ):
            await self.post(self.request(), 403, **headers)
        await self.post(self.request(), 401, Authorization="Bearer invalid")
        await self.post({**self.request(), "allowed_operations": ["append_episode"]}, 400)
        self.p.auth.principals["memory"]["actions"].remove("actor.memory.authorize")
        await self.post(self.request(), 403)

    async def test_public_context_is_role_read_only_and_has_no_fabricated_origin(self):
        value = self.request(
            "context_check", context={"purpose": "diary", "audience": {"kind": "public"}}
        )
        with patch.object(self.p.origins, "context", side_effect=AssertionError("no origin")):
            first = await self.post(value)
            self.assertTrue(first["allowed"])
            self.assertEqual(first["context"], value["context"])
            management = await self.post(
                {**value, "context": {"purpose": "management", "audience": {"kind": "public"}}}
            )
            self.assertNotEqual(first["access_version"], management["access_version"])
        await self.post({**value, "role_version": 8}, 409)
        self.assertFalse((await self.post({**value, "actor_id": "actor:missing"}))["allowed"])
        self.role["capabilities"] = ["memory.write"]
        self.p.role_runtime._put(self.role)
        self.assertFalse((await self.post(value))["allowed"])
        self.role["capabilities"] = ["memory.read"]
        self.role["enabled"] = False
        self.p.role_runtime._put(self.role)
        self.assertFalse((await self.post(value))["allowed"])

    async def test_public_context_rejects_non_diary_purpose_and_origin_fields(self):
        for purpose in ("dialogue", "plan", "proactive"):
            await self.post(
                self.request(
                    "context_check", context={"purpose": purpose, "audience": {"kind": "public"}}
                ),
                400,
            )
        await self.post(
            self.request(
                "context_check",
                context={
                    "purpose": "diary",
                    "audience": {"kind": "public", "person_id": "person:requester"},
                },
            ),
            400,
        )

    async def test_private_person_is_checked_and_contract_unavailable_is_local(self):
        entry = copy.deepcopy(self.p.auth.entries["chat-entry"])
        entry["audience"] = "self_private"
        entry["channel"]["channel_conversation_id"] = "private:requester"
        self.p.auth.entries["private-fixture"] = entry
        with self.p.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO channels VALUES(?,?)", (channel_key(entry), "conversation:private")
            )
        ref = self.p.origins.issue(bearer("CONNECTOR"), "private-fixture")["assertion_ref"]
        value = self.request(
            "context_check",
            context={
                "purpose": "dialogue",
                "audience": {
                    "kind": "private",
                    "person_id": "person:requester",
                    "conversation_id": "conversation:private",
                    "assertion_ref": ref,
                },
            },
        )
        self.assertTrue((await self.post(value))["allowed"])
        value["context"]["audience"]["person_id"] = "person:other"
        await self.post(value, 403)
        protocol = self.p.actor_memory_authority.protocol
        protocol.schema = None
        protocol.manifest = "0" * 64
        await self.post(self.request(), 503)
        async with self.client.get(self.url + "/health/live") as response:
            self.assertEqual(response.status, 200)

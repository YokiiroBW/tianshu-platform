"""Real browser sessions and owner HTTP transport with a declared synthetic Memory peer."""

import os
import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

import aiohttp
from aiohttp import web

from fixtures import ENV, start_http
from services.platform.server import create_app, create_console
from services.platform.service import Platform
from test_actor_capabilities import seed_roles
from web_fixtures import PASSWORD, web_settings


class ActorMemoryBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        environment = patch.dict(
            os.environ,
            {
                **ENV,
                "TS396_MEMORY": "synthetic-actor-memory-token-000000000001",
                "TS396_LIFE": "synthetic-life-token-0000000000000001",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        settings = web_settings(self.temp.name)
        settings["principals"]["admin"]["actions"] += ["role.manage", "life.read"]
        settings["web_life"] = {
            "enabled": True,
            "base_url": "https://life.invalid:443",
            "token_env": "TS396_LIFE",
        }
        self.p = Platform(settings)
        seed_roles(self.p)
        self.role = self.p.role_runtime.get("actor:a")
        self.role.update(operator="admin", capabilities=["memory.read", "memory.write"])
        self.p.role_runtime._put(self.role)
        self.calls = []
        self.revoke = False
        self.wrong_actor = False
        self.impact_changed = False
        self.unknown_write = False
        self.write_delay = 0
        self.wrong_ref = False
        peer = web.Application()
        peer.router.add_post("/internal/v1/actor-self-memory/operate", self.peer)
        peer.router.add_post("/internal/v2/life/read", self.life_peer)
        self.life_calls = []
        self.life_change = None
        self.life_denied = False
        self.life_wrong_request = False
        self.peer_runner, peer_url = await start_http(peer)
        self.p.role_runtime.config["memory"] = {"base_url": peer_url, "token_env": "TS396_MEMORY"}
        self.console = create_console(self.p)
        self.console.actor_memory.session_factory = aiohttp.ClientSession
        self.console.life.config["base_url"] = peer_url
        self.p.settings["core"].update(base_url=peer_url, token_env="TS396_MEMORY")
        self.console.life_management.session_factory = aiohttp.ClientSession
        self.runner, self.url = await start_http(create_app(self.p, console=self.console))
        settings["web"]["origin"] = self.url
        self.client = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.addAsyncCleanup(self.cleanup)
        async with self.client.get(self.url + "/api/web/session") as response:
            csrf = (await response.json())["csrf"]
        async with self.client.post(
            self.url + "/api/web/login",
            json={"username": "synthetic-admin", "password": PASSWORD},
            headers={"Origin": self.url, "X-CSRF-Token": csrf},
        ) as response:
            self.assertEqual(response.status, 200, await response.text())
            self.csrf = (await response.json())["csrf"]
        self.assertTrue(
            self.console.life_management.available(),
            (
                self.p.settings.get("core"),
                self.console.life.code(),
                self.p.auth.principals["admin"]["actions"],
            ),
        )
        self.assertTrue(self.p.role_runtime.active("actor:a"), self.p.role_runtime.get("actor:a"))

    async def cleanup(self):
        await self.client.close()
        await self.runner.cleanup()
        await self.peer_runner.cleanup()
        self.p.close()

    async def peer(self, request):
        self.assertEqual(
            request.headers["Authorization"], "Bearer synthetic-actor-memory-token-000000000001"
        )
        body = await request.json()
        self.p.actor_memory_authority.protocol.check("request", body)
        self.calls.append(body)
        if self.unknown_write and body["operation"] == "forget_memory":
            return web.json_response(
                {
                    "schema_version": 1,
                    "request_id": body["request_id"],
                    "code": "unknown",
                    "retryable": False,
                    "execution": "unknown",
                },
                status=503,
            )
        if self.write_delay and body["operation"] == "forget_memory":
            await asyncio.sleep(self.write_delay)
        if self.revoke:
            self.role["capabilities"] = []
            self.p.role_runtime._put(self.role)
        answer = {
            key: body[key]
            for key in (
                "schema_version",
                "request_id",
                "actor_id",
                "role_version",
                "operation",
                "context",
            )
        }
        answer.update(domain_version=4, checked_at="2026-10-28T04:00:00Z")
        if body["operation"] == "manage_list":
            answer.update(items=[], next_cursor=None, omitted=False)
        elif body["operation"] == "memory_impact":
            answer.update(
                ref=body["ref"],
                dependent_refs=[],
                source_refs=[],
                can_revise=True,
                can_forget=True,
                owner_originals_affected=False,
                omitted=False,
            )
            if self.impact_changed:
                answer["ref"] = {**body["ref"], "revision": body["ref"]["revision"] + 1}
                answer.update(can_revise=False, can_forget=False)
        elif body["operation"] == "forget_memory":
            answer = {
                "schema_version": 1,
                "request_id": body["request_id"],
                "actor_id": body["actor_id"],
                "command": "forget_memory",
                "idempotency_key": body["idempotency_key"],
                "state": "withdrawn",
                "ref": {**body["ref"], "revision": body["ref"]["revision"] + 1},
                "domain_version": 5,
            }
        elif body["operation"] == "get_episode":
            examples = json.loads(
                (self.p.contracts.root / "actor-self-memory/candidate-v1/examples.json").read_text(
                    encoding="utf-8"
                )
            )
            episode = next(
                item["document"]["episode"]
                for item in examples["positive"]
                if item["definition"] == "get_episode_result"
            )
            episode["actor_id"] = body["actor_id"]
            episode["episode_id"] = body["episode_id"]
            episode["revision"] = body["expected_revision"]
            episode["fact"]["actor_id"] = body["actor_id"]
            answer.pop("checked_at")
            answer["episode"] = episode
        if self.wrong_actor:
            answer["actor_id"] = "actor:b"
        if self.wrong_ref and body["operation"] == "forget_memory":
            answer["ref"]["id"] = "derived:other"
        return web.json_response(answer)

    async def post(self, name, body, status=200, *, csrf=True):
        headers = {"Origin": self.url}
        if csrf:
            headers["X-CSRF-Token"] = self.csrf
        async with self.client.post(
            self.url + "/api/web/actor-memory/" + name,
            json={"actor_id": "actor:a", **body},
            headers=headers,
        ) as response:
            result = await response.json()
            self.assertEqual(response.status, status, result)
            return result

    async def test_list_uses_current_owned_role_and_management_context(self):
        await self.post("list", {"states": ["active"], "kinds": ["episode"], "cursor": None})
        self.assertEqual(self.calls[-1]["role_version"], 7)
        self.assertEqual(
            self.calls[-1]["context"], {"purpose": "management", "audience": {"kind": "actor_only"}}
        )
        await self.post(
            "list", {"states": ["active"], "kinds": ["episode"], "cursor": None, "context": {}}, 400
        )
        await self.post(
            "list", {"states": ["active"], "kinds": ["episode"], "cursor": None}, 403, csrf=False
        )
        await self.post(
            "list",
            {"actor_id": "actor:b", "states": ["active"], "kinds": ["episode"], "cursor": None},
            403,
        )
        self.assertEqual(len(self.calls), 1)

    async def test_write_capability_and_fresh_role_are_enforced(self):
        ref = {"kind": "derived", "id": "derived:synthetic", "revision": 1}
        self.role["capabilities"] = ["memory.read"]
        self.p.role_runtime._put(self.role)
        result = await self.post("impact", {"ref": ref})
        self.assertFalse(result["can_forget"])
        await self.post(
            "forget", {"ref": ref, "client_id": "synthetic-key", "expected_domain_version": 4}, 403
        )
        self.revoke = True
        await self.post("list", {"states": ["active"], "kinds": ["episode"], "cursor": None}, 403)

    async def test_owner_mismatch_and_write_receipt_binding(self):
        self.wrong_actor = True
        await self.post("list", {"states": ["active"], "kinds": ["episode"], "cursor": None}, 502)
        self.wrong_actor = False
        result = await self.post(
            "forget",
            {
                "ref": {"kind": "derived", "id": "derived:synthetic", "revision": 1},
                "client_id": "synthetic-key",
                "expected_domain_version": 4,
            },
        )
        self.assertEqual(result["state"], "withdrawn")
        self.assertEqual(self.calls[-1]["idempotency_key"], "synthetic-key")

    async def test_impact_source_invalidation_requires_refresh_of_selected_revision(self):
        self.impact_changed = True
        result = await self.post(
            "impact", {"ref": {"kind": "derived", "id": "derived:synthetic", "revision": 1}}, 409
        )
        self.assertEqual(result["code"], "version_conflict")

    async def test_owner_unknown_write_and_network_timeout_preserve_uncertainty(self):
        body = {
            "ref": {"kind": "derived", "id": "derived:synthetic", "revision": 1},
            "client_id": "stable-write-key",
            "expected_domain_version": 4,
        }
        self.unknown_write = True
        unknown = await self.post("forget", body, 503)
        self.assertEqual(unknown["code"], "unknown")
        self.assertEqual(unknown["execution_state"], "unknown")
        self.unknown_write = False
        self.write_delay = 0.1
        self.p.role_runtime.config["memory"]["timeout_seconds"] = 0.02
        timeout = await self.post("forget", body, 503)
        self.assertEqual(timeout["execution_state"], "unknown")
        self.assertFalse(timeout["retryable"])
        self.write_delay = 0
        self.p.role_runtime.config["memory"]["timeout_seconds"] = 10
        await self.post("forget", body)
        self.assertEqual([row["idempotency_key"] for row in self.calls], ["stable-write-key"] * 3)
        self.wrong_ref = True
        await self.post("forget", body, 502)

    async def test_episode_projects_coverage_without_internal_source_authority(self):
        answer = await self.post(
            "episode", {"ref": {"kind": "episode", "id": "episode:fixture", "revision": 1}}
        )
        fact = answer["episode"]["fact"]
        self.assertIn("content_reads", fact)
        self.assertNotIn("access_version", fact)
        self.assertNotIn("source_generation", fact)
        self.assertNotIn("source_constraints", fact)

    async def test_autonomy_owner_projection_and_import_keep_role_write_boundary(self):
        @asynccontextmanager
        async def scoped(actor, session, **kwargs):
            yield (
                {"assertion_ref": "origin:synthetic"},
                {
                    "actor_id": actor,
                    "person_id": "person:operator",
                    "conversation_id": "conversation:operator",
                    "audience": "self_private",
                },
            )

        calls = []

        async def owner(path, payload, **kwargs):
            calls.append((path, payload, kwargs))
            return {"synthetic_owner": True}

        self.console.life_management.scoped_actor = scoped
        self.console.life_management._call = owner
        session = next(row for row in self.console.sessions.values() if row["authenticated"])
        await self.console.life_management.runtime(
            "read",
            {
                "actor_id": "actor:a",
                "resource": "autonomy",
                "object_id": None,
                "expected_version": None,
                "limit": 32,
                "after": None,
            },
            session,
        )
        self.assertIsNone(calls[-1][1]["scope"])
        self.assertFalse(calls[-1][2]["management"])
        body = {
            "actor_id": "actor:a",
            "operation": "actor-memory.import",
            "client_id": "import:synthetic",
            "expected_version": 0,
            "value": {"owner": "activities", "after": None, "limit": 32},
        }
        await self.console.life_management.runtime("manage", body, session)
        self.role["capabilities"] = ["memory.read"]
        self.p.role_runtime._put(self.role)
        from services.platform.contracts import Fault

        with self.assertRaises(Fault) as failure:
            await self.console.life_management.runtime("manage", body, session)
        self.assertEqual(failure.exception.status, 403)
        self.assertEqual(len(calls), 2)

    async def life_peer(self, request):
        body = await request.json()
        self.p.contracts.check("life-runtime#read_request", body)
        self.life_calls.append((request.headers["Authorization"], body))
        request_id = "wrong-request" if self.life_wrong_request else body["query"]["request_id"]
        # Synthetic owner grants only the household/web-life reader for autonomy.
        denied = self.life_denied or (
            body["resource"] == "autonomy"
            and request.headers["Authorization"] != "Bearer " + os.environ["TS396_LIFE"]
        )
        if denied:
            return web.json_response(
                {
                    "code": "not_found",
                    "execution_state": "not_started",
                    "request_id": request_id,
                },
                status=404,
            )
        if self.life_change == "capability":
            self.role["capabilities"] = []
        elif self.life_change == "owner":
            self.role["operator"] = "other"
        elif self.life_change == "version":
            self.role["version"] += 1
        elif self.life_change == "guard":
            self.p.auth.principals["admin"]["actions"].remove("role.manage")
        if self.life_change:
            self.p.role_runtime._put(self.role)
        return web.json_response(
            {
                "schema_version": 2,
                "request_id": request_id,
                "actor_id": body["actor_id"],
                "resource": body["resource"],
                "items": [],
                "next_cursor": None,
            }
        )

    async def life_read(self, status=200, **extra):
        async with self.client.post(
            self.url + "/api/web/life/runtime/read",
            json={
                "actor_id": "actor:a",
                "resource": "autonomy",
                "object_id": None,
                "expected_version": None,
                "limit": 32,
                "after": None,
                **extra,
            },
            headers={"Origin": self.url, "X-CSRF-Token": self.csrf},
        ) as response:
            result = await response.json()
            self.assertEqual(response.status, status, result)
            return result

    async def test_autonomy_reader_grant_and_management_credentials_are_independent(self):
        await self.life_read()
        self.assertEqual(self.life_calls[-1][0], "Bearer " + os.environ["TS396_LIFE"])
        self.assertIsNone(self.life_calls[-1][1]["scope"])
        for resource, extra in (("image_backend", {}), ("chapter", {"chapter_view": "current"})):
            await self.life_read(resource=resource, **extra)
            self.assertEqual(self.life_calls[-1][0], "Bearer " + os.environ["TS396_MEMORY"])
        self.life_denied = True
        self.assertEqual((await self.life_read(404))["code"], "not_found")
        self.life_wrong_request = True
        self.assertEqual((await self.life_read(502))["code"], "invalid_upstream")

    async def test_autonomy_requires_memory_read_and_operator_ownership(self):
        for change in ({"capabilities": []}, {"operator": "other"}):
            original = dict(self.role)
            self.role.update(change)
            self.p.role_runtime._put(self.role)
            await self.life_read(403)
            self.role = original
        self.assertEqual(self.life_calls, [])

    async def test_autonomy_rechecks_capability_owner_role_version_and_guard_after_read(self):
        for change, status in (
            ("capability", 403),
            ("owner", 403),
            ("version", 409),
            ("guard", 403),
        ):
            with self.subTest(change=change):
                self.role.update(operator="admin", capabilities=["memory.read", "memory.write"])
                self.p.role_runtime._put(self.role)
                self.life_change = change
                await self.life_read(status)
        self.assertEqual(len(self.life_calls), 4)

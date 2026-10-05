"""Real loopback proxy and encrypted catalog with isolated image-owner responses."""

import os
import tempfile
import unittest
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
from aiohttp import web

from fixtures import CONTRACT, start_http
from services.platform.contracts import Contracts, Fault, require
from services.platform.external_catalog import ExternalCatalog
from services.platform.service_credentials import ServiceCredentials
from services.platform.web_life_management import WebLifeManagement


class LocalWork:
    async def run(self, function, *args):
        return function(*args)


class ScopedLife(WebLifeManagement):
    @asynccontextmanager
    async def scoped_actor(self, actor_id, session):
        require(actor_id in {"actor:a", "actor:b"}, "forbidden", 403)
        self.guard(session)
        yield
        self.guard(session)


class WebImageBackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.calls = []
        self.revoke = False
        self.mismatched = False
        self.upstream_error = None
        self.session_valid = True
        self.connection = {
            "base_url": None,
            "credential_ref": None,
            "version": 0,
            "state": "not_configured",
        }
        self.receipts = {}
        self.token = "synthetic-comfy-management-token-0001"
        environment = patch.dict(os.environ, {"TS_COMFY_TEST_TOKEN": self.token})
        environment.start()
        self.addCleanup(environment.stop)
        app = web.Application()
        app.router.add_post("/internal/v1/image-backend/{operation}", self.owner)
        runner, url = await start_http(app)
        self.addAsyncCleanup(runner.cleanup)
        self.p = SimpleNamespace(
            settings={
                "core": {"base_url": url, "token_env": "TS_COMFY_TEST_TOKEN"},
                "web_external": {"directory": str(Path(self.temp.name) / "external")},
            },
            contracts=Contracts(CONTRACT),
            local_work=LocalWork(),
            auth=SimpleNamespace(principals={"admin": {"actions": ["role.manage", "life.read"]}}),
        )
        ExternalCatalog(self.p.settings["web_external"]["directory"], create=True)
        self.p.service_credentials = ServiceCredentials(self.p)
        console = SimpleNamespace(
            config={"principal": "admin"},
            session_valid=lambda session: session == "session:valid" and self.session_valid,
            life=SimpleNamespace(code=lambda: "ready"),
        )
        self.life = ScopedLife(self.p, console)

    async def owner(self, request):
        self.assertEqual(request.headers["Authorization"], "Bearer " + self.token)
        body = await request.json()
        self.calls.append((request.match_info["operation"], body))
        if self.revoke:
            self.session_valid = False
        if self.upstream_error:
            return web.json_response({"code": self.upstream_error}, status=409)
        result = {"credential_ref": "companion-images:opaque", "state": "configured"}
        if body.get("resource") == "status":
            result = dict(self.connection)
        if body.get("operation") == "connection.configure":
            if body["request_id"] not in self.receipts:
                if body["expected_version"] != self.connection["version"]:
                    return web.json_response({"code": "version_conflict"}, status=409)
                self.connection.update(body["value"])
                self.connection["version"] += 1
                self.connection["state"] = "configured"
                self.receipts[body["request_id"]] = dict(self.connection)
            result = dict(self.receipts[body["request_id"]])
        return web.json_response(
            {
                "schema_version": 1,
                "request_id": body["request_id"] + (":wrong" if self.mismatched else ""),
                "actor_id": body["actor_id"],
                "result": result,
            }
        )

    async def route(self, name, body, session="session:valid"):
        return await self.life.image_backend(name, body, session)

    def save_body(self):
        return {
            "actor_id": "actor:a",
            "value": {"base_url": "http://127.0.0.1:8188", "enabled": True},
            "credential": {"action": "replace", "value": "synthetic-backend-secret-0001"},
            "catalog_revision": 0,
            "expected_version": 0,
            "client_id": str(uuid.uuid4()),
        }

    async def test_connection_reuses_encrypted_credentials_and_replay(self):
        body = self.save_body()
        result = await self.route("save", body)
        self.assertNotIn("credential_ref", result["result"])
        self.assertEqual(await self.route("save", body), result)
        sent = next(
            body for _, body in self.calls if body.get("operation") == "connection.configure"
        )
        self.assertEqual(sent["operation"], "connection.configure")
        self.assertEqual(sent["request_id"], body["client_id"])
        self.assertTrue(sent["value"]["credential_ref"].startswith("companion-images:"))
        self.assertNotIn("credential", sent)
        catalog = self.p.service_credentials.catalog
        self.assertNotIn(body["credential"]["value"].encode(), catalog.database.read_bytes())
        self.assertEqual(self.p.service_credentials.view("actor:a")["revision"], 1)

    async def test_shared_connection_preserves_empty_credentials_across_two_actors(self):
        first = self.save_body()
        await self.route("save", first)
        reference = self.connection["credential_ref"]
        second = {
            **first,
            "actor_id": "actor:b",
            "credential": {"action": "keep"},
            "catalog_revision": 1,
            "expected_version": 1,
            "client_id": str(uuid.uuid4()),
        }
        result = await self.route("save", second)
        self.assertEqual(self.connection["credential_ref"], reference)
        self.assertEqual(await self.route("save", second), result)
        status = await self.route("status", {"actor_id": "actor:b"})
        self.assertTrue(status["credential_configured"])
        self.assertNotIn(reference, str(status))
        _, rows = self.p.service_credentials.catalog.snapshot()
        self.assertEqual(list(rows), ["images:connection"])
        self.assertEqual(rows["images:connection"]["credential"], first["credential"]["value"])

    async def test_new_origin_has_no_secret_transfer_and_explicit_clear_removes_shared_token(self):
        first = self.save_body()
        await self.route("save", first)
        second = {
            **first,
            "actor_id": "actor:b",
            "value": {"base_url": "http://127.0.0.1:8199", "enabled": True},
            "credential": {"action": "keep"},
            "catalog_revision": 1,
            "expected_version": 1,
            "client_id": str(uuid.uuid4()),
        }
        await self.route("save", second)
        self.assertIsNone(self.connection["credential_ref"])
        self.assertFalse(
            (await self.route("status", {"actor_id": "actor:b"}))["credential_configured"]
        )
        revision, rows = self.p.service_credentials.catalog.snapshot()
        self.assertEqual(revision, 1)
        self.assertEqual(rows["images:connection"]["value"]["base_url"], first["value"]["base_url"])
        self.assertEqual(rows["images:connection"]["credential"], first["credential"]["value"])
        await self.route(
            "save",
            {
                **second,
                "credential": {"action": "clear"},
                "expected_version": 2,
                "client_id": str(uuid.uuid4()),
            },
        )
        _, rows = self.p.service_credentials.catalog.snapshot()
        self.assertIsNone(rows["images:connection"]["credential"])

    async def test_legacy_reference_migrates_only_from_authoritative_matching_origin(self):
        first = self.save_body()
        old = self.p.service_credentials.save(
            "actor:a", first["value"], first["credential"], 0, str(uuid.uuid4())
        )
        self.connection.update(base_url=first["value"]["base_url"], credential_ref=old, version=1)
        await self.route(
            "save",
            {
                **first,
                "actor_id": "actor:b",
                "credential": {"action": "keep"},
                "catalog_revision": 1,
                "expected_version": 1,
            },
        )
        self.assertNotEqual(self.connection["credential_ref"], old)
        _, rows = self.p.service_credentials.catalog.snapshot()
        self.assertEqual(rows["images:actor:a"]["value"]["credential_ref"], old)
        self.assertEqual(
            rows["images:actor:a"]["credential"], rows["images:connection"]["credential"]
        )
        # A catalog row with a different origin cannot supply the current owner's token.
        forged = {"base_url": "http://127.0.0.1:8199", "credential_ref": old}
        self.assertFalse(
            self.p.service_credentials.view_connection(forged)["credential_configured"]
        )

    async def test_actor_workflow_and_compile_use_fixed_owner_paths(self):
        for actor in ("actor:a", "actor:b"):
            await self.route(
                "read", {"actor_id": actor, "resource": "workflows", "workflow_id": None}
            )
            await self.route(
                "manage",
                {
                    "actor_id": actor,
                    "operation": "workflow.select",
                    "expected_version": 3,
                    "value": {"workflow_id": "角色/澄汐.json", "bindings": None},
                    "client_id": str(uuid.uuid4()),
                },
            )
            await self.route(
                "compile",
                {
                    "actor_id": actor,
                    "intent": {"camera": "全身", "background": "窗边"},
                    "parameters": {"width": 1024, "height": 768},
                    "assist_model": False,
                },
            )
        self.assertEqual([path for path, _ in self.calls], ["read", "manage", "compile"] * 2)
        self.assertEqual(
            [body["actor_id"] for _, body in self.calls], ["actor:a"] * 3 + ["actor:b"] * 3
        )
        self.assertEqual(self.calls[1][1]["expected_version"], 3)

    async def test_forbidden_actor_session_and_browser_credential_reference_never_forward(self):
        for body, session in (
            (
                {"actor_id": "actor:foreign", "resource": "status", "workflow_id": None},
                "session:valid",
            ),
            ({"actor_id": "actor:a", "resource": "status", "workflow_id": None}, "session:expired"),
        ):
            with self.assertRaises(Fault):
                await self.route("read", body, session)
        with self.assertRaises(Fault):
            await self.route(
                "manage",
                {
                    "actor_id": "actor:a",
                    "operation": "connection.configure",
                    "expected_version": 0,
                    "value": {
                        "base_url": "http://127.0.0.1:8188",
                        "enabled": True,
                        "credential_ref": "forged",
                    },
                    "client_id": str(uuid.uuid4()),
                },
            )
        self.assertEqual(self.calls, [])

    async def test_invalid_compile_and_save_are_rejected_before_mutation(self):
        with self.assertRaises(Fault):
            await self.route(
                "compile",
                {
                    "actor_id": "actor:a",
                    "intent": {},
                    "parameters": {"width": 4097},
                    "assist_model": False,
                },
            )
        body = self.save_body()
        body["value"]["enabled"] = "yes"
        with self.assertRaises(Fault):
            await self.route("save", body)
        self.assertEqual(self.p.service_credentials.view("actor:a")["revision"], 0)
        self.assertEqual(self.calls, [])

    async def test_response_identity_and_post_call_session_are_verified(self):
        body = {"actor_id": "actor:a", "resource": "status", "workflow_id": None}
        self.mismatched = True
        with self.assertRaises(Fault) as failed:
            await self.route("read", body)
        self.assertEqual(failed.exception.code, "invalid_upstream")
        self.mismatched = False
        self.revoke = True
        with self.assertRaises(Fault) as failed:
            await self.route("read", body)
        self.assertEqual(failed.exception.code, "session_expired")

    async def test_owner_conflicts_are_preserved_without_retry(self):
        self.upstream_error = "version_conflict"
        with self.assertRaises(Fault) as failed:
            await self.route(
                "read", {"actor_id": "actor:a", "resource": "actor", "workflow_id": None}
            )
        self.assertEqual(
            (failed.exception.code, failed.exception.status), ("version_conflict", 409)
        )
        self.assertEqual(len(self.calls), 1)

    async def test_model_assistance_has_one_total_budget_without_retry(self):
        timeouts = []
        factory = aiohttp.ClientSession

        def session(*args, **kwargs):
            timeouts.append(kwargs["timeout"].total)
            return factory(*args, **kwargs)

        with patch(
            "services.platform.web_life_management.aiohttp.ClientSession", side_effect=session
        ):
            await self.route(
                "read", {"actor_id": "actor:a", "resource": "actor", "workflow_id": None}
            )
            await self.route(
                "compile",
                {"actor_id": "actor:a", "intent": {}, "parameters": {}, "assist_model": True},
            )
            await self.route(
                "manage",
                {
                    "actor_id": "actor:a",
                    "operation": "workflow.analyze",
                    "expected_version": 1,
                    "value": {"workflow_id": None, "goal": "竖幅生活照", "assist_model": True},
                    "client_id": str(uuid.uuid4()),
                },
            )
        self.assertEqual(timeouts, [10, 120, 120])
        self.assertEqual(len(self.calls), 3)

    async def test_stale_new_form_never_mutates_credentials_while_original_id_replays(self):
        body = self.save_body()
        result = await self.route("save", body)
        before = self.p.service_credentials.catalog.snapshot()
        self.assertEqual(await self.route("save", body), result)
        stale = {
            **body,
            "client_id": str(uuid.uuid4()),
            "credential": {"action": "replace", "value": "synthetic-stale-secret-9999"},
        }
        with self.assertRaises(Fault) as failed:
            await self.route("save", stale)
        self.assertEqual(failed.exception.code, "version_conflict")
        self.assertEqual(self.p.service_credentials.catalog.snapshot(), before)

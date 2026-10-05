"""Frozen Skills contract, real HTTP owner proxy and isolated encrypted credentials."""

import copy
import json
import os
import tempfile
import unittest
import uuid
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web

from fixtures import CONTRACT, start_http
from services.platform.contracts import Contracts, Fault, require
from services.platform.external_catalog import ExternalCatalog
from services.platform.service_credentials import ServiceCredentials
from services.platform.web_skills import WebSkills
from test_web_image_backend import LocalWork, ScopedLife


def skill(identifier="game.guides", handler="gscore.query"):
    definition = {
        "id": identifier,
        "version": "1",
        "title": "Synthetic game guides",
        "description": "Isolated command query adapter",
        "domain": "game",
        "handler_id": handler,
        "operations": ["game.query"],
    }
    return {
        **{key: value for key, value in definition.items() if key != "handler_id"},
        "definition": definition,
        "source_id": "builtin",
        "enabled": False,
        "installed": True,
        "revision": "1" * 64,
        "availability": {
            "state": "not_configured",
            "can_execute": False,
            "reason_code": "connection_missing",
        },
        "config": {
            "provider": None,
            "base_url": None,
            "options": {},
            "credential_configured": False,
        },
    }


class WebSkillsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.calls, self.receipts = [], {}
        self.valid, self.revoke, self.bad_reply = True, False, False
        self.reject_configuration = False
        self.states = {
            actor: {
                "actor_version": 1,
                "catalog_version": "0" * 64,
                "skills": [skill()],
                "sources": [],
            }
            for actor in ["actor:a", "actor:b"]
        }
        environment = patch.dict(os.environ, {"TS_SKILLS_TEST": "synthetic-skills-peer-token-0001"})
        environment.start()
        self.addCleanup(environment.stop)
        app = web.Application()
        app.router.add_post("/internal/v1/skills/{operation}", self.owner)
        runner, url = await start_http(app)
        self.addAsyncCleanup(runner.cleanup)

        def authenticate(header, db, action):
            require(header == "Bearer synthetic-resolver", "unauthorized", 401)
            return None, {"kind": "service", "service": "companion"}

        self.p = SimpleNamespace(
            settings={
                "core": {"base_url": url, "token_env": "TS_SKILLS_TEST"},
                "web_external": {"directory": str(Path(self.temp.name) / "external")},
            },
            contracts=Contracts(CONTRACT),
            local_work=LocalWork(),
            store=SimpleNamespace(connect=lambda: nullcontext()),
            auth=SimpleNamespace(
                authenticate=authenticate,
                principals={"admin": {"actions": ["role.manage", "life.read"]}},
            ),
        )
        ExternalCatalog(self.p.settings["web_external"]["directory"], create=True)
        self.p.service_credentials = ServiceCredentials(self.p)
        console = SimpleNamespace(
            config={"principal": "admin"},
            session_valid=lambda session: self.valid and session == "session:valid",
            life=SimpleNamespace(code=lambda: "ready"),
        )
        self.api = WebSkills(ScopedLife(self.p, console))

    async def owner(self, request):
        self.assertEqual(
            request.headers["Authorization"], "Bearer synthetic-skills-peer-token-0001"
        )
        body = await request.json()
        self.calls.append(body)
        state = self.states[body["actor_id"]]
        if request.match_info["operation"] == "manage":
            if self.reject_configuration and body["operation"] == "skill.update":
                return web.json_response({"code": "invalid_input"}, status=400)
            key = (body["actor_id"], body["request_id"])
            if key in self.receipts:
                state = self.receipts[key]
            else:
                if body["expected_version"] != state["actor_version"]:
                    return web.json_response({"code": "version_conflict"}, status=409)
                value, operation = body["value"], body["operation"]
                if operation == "skill.update":
                    item = state["skills"][0]
                    item["config"] = {
                        key: value["config"][key] for key in ["provider", "base_url", "options"]
                    }
                    item["config"]["credential_configured"] = bool(
                        value["config"]["credential_ref"]
                    )
                elif operation in {"skill.enable", "skill.disable"}:
                    state["skills"][0]["enabled"] = operation == "skill.enable"
                elif operation == "source.configure":
                    old = next(
                        (row for row in state["sources"] if row["source_id"] == value["source_id"]),
                        None,
                    )
                    source = {
                        key: value[key]
                        for key in [
                            "source_id",
                            "name",
                            "manifest_url",
                            "enabled",
                            "expected_sha256",
                        ]
                    }
                    source.update(
                        credential_configured=bool(value["credential_ref"]),
                        version=(old["version"] if old else 0) + 1,
                        state="configured",
                        last_refreshed_at=None,
                        content_sha256=None,
                        error_code=None,
                    )
                    state["sources"] = [source]
                elif operation == "source.refresh":
                    state["sources"][0].update(state="unreachable", error_code="connection_failed")
                state["actor_version"] += 1
                self.receipts[key] = copy.deepcopy(state)
        if self.revoke:
            self.valid = False
        answer = {
            "schema_version": 1,
            "request_id": body["request_id"],
            "actor_id": body["actor_id"],
            "result": state,
        }
        if self.bad_reply:
            answer["request_id"] += ":wrong"
        return web.json_response(answer)

    async def route(self, name, body):
        return await self.api.route(name, body, "session:valid")

    def configure(self, actor="actor:a", credential=None, version=1, revision=0):
        return {
            "actor_id": actor,
            "operation": "skill.update",
            "expected_version": version,
            "value": {
                "definition": self.states[actor]["skills"][0]["definition"],
                "enabled": False,
                "config": {
                    "provider": "gscore",
                    "base_url": "http://127.0.0.1:28765",
                    "options": {},
                },
            },
            "credential": credential
            or {"action": "replace", "value": "synthetic-private-gscore-token"},
            "catalog_revision": revision,
            "client_id": str(uuid.uuid4()),
        }

    def resolve(
        self,
        reference,
        audience="http://127.0.0.1:28765",
        purpose="companion.skills",
        header="Bearer synthetic-resolver",
    ):
        return self.p.service_credentials.resolve(
            header,
            {
                "schema_version": 1,
                "request_id": "credential:test",
                "credential_ref": reference,
                "audience": audience,
                "purpose": purpose,
            },
        )

    async def test_actor_cas_enable_and_catalog_read(self):
        read = await self.route("read", {"actor_id": "actor:a", "resource": "list"})
        self.assertIsNone(read["result"]["skills"][0]["config"]["provider"])
        body = {
            "actor_id": "actor:a",
            "operation": "skill.enable",
            "value": {"skill_id": "game.guides"},
            "expected_version": 1,
            "client_id": str(uuid.uuid4()),
        }
        result = await self.route("manage", body)
        self.assertTrue(result["result"]["skills"][0]["enabled"])
        self.assertFalse(self.states["actor:b"]["skills"][0]["enabled"])
        await self.route(
            "manage",
            {
                **body,
                "operation": "skill.disable",
                "expected_version": 2,
                "client_id": str(uuid.uuid4()),
            },
        )
        self.assertFalse(self.states["actor:a"]["skills"][0]["enabled"])
        with self.assertRaises(Fault) as error:
            await self.route("manage", {**body, "client_id": str(uuid.uuid4())})
        self.assertEqual(error.exception.code, "version_conflict")

    async def test_first_config_and_exact_purpose_origin_resolution(self):
        body = self.configure()
        result = await self.route("configure", body)
        sent = self.calls[-1]["value"]["config"]
        reference = sent["credential_ref"]
        self.assertEqual(sent["provider"], "gscore")
        self.assertNotIn(reference, json.dumps(result))
        self.assertNotIn(
            body["credential"]["value"].encode(),
            self.p.service_credentials.catalog.database.read_bytes(),
        )
        self.assertEqual(self.resolve(reference)["token"], body["credential"]["value"])
        for fields in [
            {"audience": "http://127.0.0.1:28766"},
            {"purpose": "companion.images"},
            {"header": "Bearer invalid"},
        ]:
            with self.assertRaises(Fault):
                self.resolve(reference, **fields)
        # Role enablement does not revoke credentials from existing operations.
        await self.route(
            "manage",
            {
                "actor_id": "actor:a",
                "operation": "skill.enable",
                "value": {"skill_id": "game.guides"},
                "expected_version": 2,
                "client_id": str(uuid.uuid4()),
            },
        )
        self.assertEqual(self.resolve(reference)["token"], body["credential"]["value"])

    async def test_two_actor_credentials_stay_separate(self):
        first = self.configure()
        await self.route("configure", first)
        ref_a = self.calls[-1]["value"]["config"]["credential_ref"]
        second = self.configure(
            "actor:b", {"action": "replace", "value": "synthetic-other-role-key"}, revision=1
        )
        await self.route("configure", second)
        ref_b = self.calls[-1]["value"]["config"]["credential_ref"]
        self.assertNotEqual(ref_a, ref_b)
        self.assertEqual(self.resolve(ref_a)["token"], first["credential"]["value"])
        self.assertEqual(self.resolve(ref_b)["token"], second["credential"]["value"])

    async def test_stale_form_cannot_edit_secret_and_original_replay_works(self):
        body = self.configure()
        result = await self.route("configure", body)
        snapshot = self.p.service_credentials.catalog.snapshot()
        self.assertEqual(await self.route("configure", body), result)
        with self.assertRaises(Fault) as error:
            await self.route(
                "configure",
                {
                    **body,
                    "client_id": str(uuid.uuid4()),
                    "credential": {"action": "replace", "value": "synthetic-stale-replacement"},
                },
            )
        self.assertEqual(error.exception.code, "version_conflict")
        self.assertEqual(snapshot, self.p.service_credentials.catalog.snapshot())

    async def test_rejected_new_origin_configuration_keeps_old_reference_usable(self):
        original = self.configure()
        await self.route("configure", original)
        old_reference = self.calls[-1]["value"]["config"]["credential_ref"]
        candidate = self.configure(version=2, revision=1)
        candidate["value"]["config"]["base_url"] = "http://127.0.0.1:28766"
        candidate["credential"] = {"action": "replace", "value": "synthetic-new-origin-token-0001"}
        self.reject_configuration = True
        with self.assertRaises(Fault) as error:
            await self.route("configure", candidate)
        self.assertEqual(error.exception.code, "invalid_input")
        self.assertEqual(
            self.states["actor:a"]["skills"][0]["config"]["base_url"], "http://127.0.0.1:28765"
        )
        self.assertEqual(self.resolve(old_reference)["token"], original["credential"]["value"])

    async def test_keep_clear_and_changed_origin(self):
        await self.route("configure", self.configure())
        kept = self.configure(credential={"action": "keep"}, version=2, revision=1)
        await self.route("configure", kept)
        reference = self.calls[-1]["value"]["config"]["credential_ref"]
        self.assertIsNotNone(reference)
        changed = self.configure(credential={"action": "keep"}, version=3, revision=2)
        changed["value"]["config"]["base_url"] = "http://127.0.0.1:28766"
        await self.route("configure", changed)
        self.assertIsNone(self.calls[-1]["value"]["config"]["credential_ref"])
        self.assertEqual(self.p.service_credentials.catalog.snapshot()[0], 3)
        self.assertEqual(self.resolve(reference)["token"], "synthetic-private-gscore-token")
        cleared = self.configure(credential={"action": "clear"}, version=4, revision=3)
        await self.route("configure", cleared)
        with self.assertRaises(Fault):
            self.resolve(reference)

    async def test_source_refresh_preserves_credentials_and_catalog_on_failure(self):
        body = {
            "actor_id": "actor:a",
            "operation": "source.configure",
            "expected_version": 1,
            "value": {
                "source_id": "source:synthetic",
                "name": "Synthetic source",
                "manifest_url": "http://127.0.0.1:28765/skills.json",
                "enabled": True,
                "expected_sha256": "a" * 64,
            },
            "credential": {"action": "replace", "value": "synthetic-private-source-token-0001"},
            "catalog_revision": 0,
            "client_id": str(uuid.uuid4()),
        }
        await self.route("configure", body)
        reference = self.calls[-1]["value"]["credential_ref"]
        snapshot = self.p.service_credentials.catalog.snapshot()
        result = await self.route(
            "manage",
            {
                "actor_id": "actor:a",
                "operation": "source.refresh",
                "value": {"source_id": "source:synthetic"},
                "expected_version": 2,
                "client_id": str(uuid.uuid4()),
            },
        )
        self.assertEqual(result["result"]["sources"][0]["state"], "unreachable")
        self.assertEqual(len(result["result"]["skills"]), 1)
        self.assertEqual(snapshot, self.p.service_credentials.catalog.snapshot())
        self.assertEqual(self.resolve(reference)["token"], "synthetic-private-source-token-0001")
        status = await self.route(
            "credentials", {"actor_id": "actor:a", "source_id": "source:synthetic"}
        )
        self.assertEqual(status, {"credential_configured": True, "revision": 1})

    async def test_reject_browser_secret_refs_and_duplicate_image_configuration(self):
        body = self.configure()
        body["value"]["config"]["credential_ref"] = "browser:forged"
        with self.assertRaises(Fault):
            await self.route("configure", body)
        self.assertEqual(self.calls, [])
        self.states["actor:a"]["skills"][0] = skill(handler="image.generate")
        with self.assertRaises(Fault):
            await self.route("configure", self.configure())
        self.assertEqual(self.p.service_credentials.catalog.snapshot()[0], 0)

    async def test_scope_session_and_correlated_response_checks(self):
        with self.assertRaises(Fault):
            await self.route("read", {"actor_id": "actor:unauthorized", "resource": "list"})
        self.assertEqual(self.calls, [])
        self.bad_reply = True
        with self.assertRaises(Fault) as error:
            await self.route("read", {"actor_id": "actor:a", "resource": "list"})
        self.assertEqual(error.exception.code, "invalid_upstream")
        self.bad_reply, self.revoke = False, True
        with self.assertRaises(Fault) as error:
            await self.route("read", {"actor_id": "actor:a", "resource": "list"})
        self.assertEqual(error.exception.code, "session_expired")


if __name__ == "__main__":
    unittest.main()

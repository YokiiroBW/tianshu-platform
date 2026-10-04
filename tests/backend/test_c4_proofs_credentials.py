"""Actual source bindings and narrow credential RPCs; no live account or upstream."""

import copy
import json
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import aiohttp
from fixtures import CONTRACT, ENV, bearer, start_http
from source_fixtures import SOURCE_DOCS, input_request, source_settings

from services.platform.contracts import canonical, utc
from services.platform.external_catalog import ExternalCatalog
from services.platform.memory_proofs import operation_digest
from services.platform.server import create_app
from services.platform.service import Platform


class ProofCredentialHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)
        self.now = time.time()
        self.p = Platform(source_settings(self.temp.name), clock=lambda: self.now)
        self.addCleanup(self.p.close)
        runner, self.url = await start_http(create_app(self.p))
        self.addAsyncCleanup(runner.cleanup)
        self.client = aiohttp.ClientSession()
        self.addAsyncCleanup(self.client.close)

    async def post(self, path, body, identity="COMPANION"):
        async with self.client.post(
            self.url + path, json=body, headers={"Authorization": bearer(identity)}
        ) as response:
            return response.status, await response.json()

    def source_and_proposal(self):
        ingress = copy.deepcopy(SOURCE_DOCS["group/request"])
        physical = ingress["input"]
        physical["sent_at"] = utc(self.now)
        physical["parts"] = [{"kind": "text", "text": "忘掉之前关于我喝咖啡的那条记录。"}]
        with self.p.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO identities VALUES(?,?,?)",
                (canonical(physical["author"]), "person:actual", 1),
            )
            db.execute(
                "INSERT INTO channels VALUES(?,?)",
                (canonical(physical["message_key"]["channel"]), "conversation:actual"),
            )
        issued = self.p.sources.register_input(bearer("CONNECTOR"), "input-entry", physical)
        ingress["command"]["origin"] = {"assertion_ref": issued["assertion_ref"]}
        ingress["command"]["deadline_at"] = utc(self.now + 30)
        authority = self.p.sources.read(bearer("COMPANION"), input_request(ingress))
        context = next(
            item
            for item in authority["actor_contexts"]
            if item["allowed_scope"]["actor_id"] == "actor:a"
        )
        examples = json.loads(
            (CONTRACT.parents[1] / "memory-context/v1/examples.json").read_text(encoding="utf-8")
        )
        proposal = copy.deepcopy(
            next(
                item["document"]["proposal"]
                for item in examples
                if item["definition"] == "issue_request"
                and item["document"]["proposal"]["kind"] == "forget"
            )
        )
        origin = {"assertion_ref": context["assertion_ref"]}
        proposal["scope"] = context["allowed_scope"]
        proposal["command"].update(
            origin=origin,
            request_id="revision:actual",
            idempotency_key="revision:actual",
            deadline_at=utc(self.now + 30),
        )
        proposal["evidence_refs"][0]["message_key"] = physical["message_key"]
        request = {
            "schema_version": 1,
            "request_id": "issue:actual",
            "origin": origin,
            "source": physical,
            "proposal": proposal,
        }
        return physical, request

    async def test_actual_source_issue_complete_digest_verify_and_retract(self):
        physical, request = self.source_and_proposal()
        status, issued = await self.post("/internal/v1/memory-context/proof/issue", request)
        self.assertEqual(status, 200, issued)
        self.assertEqual(issued["operation_digest"], operation_digest(request["proposal"]))
        self.assertNotIn(physical["parts"][0]["text"], canonical(issued))
        self.assertEqual(
            (await self.post("/internal/v1/memory-context/proof/issue", request))[1], issued
        )
        verify = {
            "schema_version": 1,
            "request_id": "revision:actual",
            "origin": request["origin"],
            "proof_ref": issued["proof_ref"],
            "purpose": "revision",
            "operation_digest": issued["operation_digest"],
        }
        status, result = await self.post(
            "/internal/v1/memory-context/proof/verify", verify, "MEMORY"
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result["accounts"], [physical["author"]])
        self.assertEqual(result["scopes"], [request["proposal"]["scope"]])
        self.assertEqual(
            (
                await self.post(
                    "/internal/v1/memory-context/proof/verify",
                    {**verify, "operation_digest": "f" * 64},
                    "MEMORY",
                )
            )[0],
            403,
        )
        edited = copy.deepcopy(physical)
        edited["message_key"]["revision"] = 2
        edited.update(kind="retract", parts=[])
        self.p.sources.register_input(bearer("CONNECTOR"), "input-entry", edited)
        self.assertEqual(
            (await self.post("/internal/v1/memory-context/proof/verify", verify, "MEMORY"))[0], 409
        )

    async def test_forged_source_plain_origin_and_foreign_scope_cannot_issue(self):
        _, request = self.source_and_proposal()
        for changed in (
            {
                **request,
                "source": {
                    **request["source"],
                    "parts": [{"kind": "text", "text": "model invented"}],
                },
            },
            {
                **request,
                "proposal": {
                    **request["proposal"],
                    "scope": {**request["proposal"]["scope"], "person_id": "person:foreign"},
                },
            },
        ):
            self.assertEqual(
                (await self.post("/internal/v1/memory-context/proof/issue", changed))[0], 403
            )
        plain = self.p.origins.issue(bearer("CONNECTOR"), "actor-a")
        changed = copy.deepcopy(request)
        changed["origin"] = changed["proposal"]["command"]["origin"] = {
            "assertion_ref": plain["assertion_ref"]
        }
        self.assertEqual(
            (await self.post("/internal/v1/memory-context/proof/issue", changed))[0], 403
        )
        self.assertEqual(
            (await self.post("/internal/v1/memory-context/proof/issue", request, "MEMORY"))[0], 403
        )
        self.p.origins.revoke(bearer("ADMIN"), "entry", "actor-a")
        self.assertEqual(
            (await self.post("/internal/v1/memory-context/proof/issue", request))[0], 403
        )

    async def test_narrow_encrypted_backend_credential_bound_origin_purpose_and_revoke(self):
        credentials = self.p.service_credentials
        credentials.catalog = ExternalCatalog(Path(self.temp.name) / "external", create=True)
        token = "synthetic-image-backend-secret-0000001"
        value = {"base_url": "http://127.0.0.1:8188", "enabled": True}
        key = str(uuid.uuid4())
        reference = credentials.save(
            "actor:a", value, {"action": "replace", "value": token}, 0, key
        )
        self.assertEqual(
            credentials.save("actor:a", value, {"action": "replace", "value": token}, 0, key),
            reference,
        )
        self.assertNotIn(token.encode(), credentials.catalog.database.read_bytes())
        status = credentials.view("actor:a")
        self.assertTrue(status["credential_configured"])
        self.assertNotIn(reference, canonical(status))
        request = {
            "schema_version": 1,
            "request_id": "credential:read",
            "credential_ref": reference,
            "purpose": "companion.images",
            "audience": value["base_url"],
        }
        code, answer = await self.post("/internal/v1/service-credentials/resolve", request)
        self.assertEqual(code, 200, answer)
        self.assertEqual(answer["token"], token)
        self.assertEqual(
            (await self.post("/internal/v1/service-credentials/resolve", request, "MEMORY"))[0], 403
        )
        self.assertEqual(
            (
                await self.post(
                    "/internal/v1/service-credentials/resolve",
                    {**request, "audience": "http://127.0.0.1:8189"},
                )
            )[0],
            404,
        )
        credentials.save(
            "actor:a", {**value, "enabled": False}, {"action": "keep"}, 1, str(uuid.uuid4())
        )
        self.assertEqual(
            (await self.post("/internal/v1/service-credentials/resolve", request))[0], 404
        )

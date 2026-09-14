"""Real Platform adapter and source confirmation; explicitly synthetic Core stand-ins."""

import json
import os
import tempfile
import time
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from fixtures import CONTRACT, ENV, bearer, config
from services.platform.contracts import Fault
from services.platform.service import Platform
from services.platform.transport import CoreFault
from services.platform.web_dialogue import WebDialogue
from source_fixtures import core_fixture, input_request
from web_fixtures import web_settings


class WebDialogueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)
        self.now = time.time()
        self.settings = web_settings(self.temp.name)
        self.settings["web"]["dialogue_enabled"] = True
        self.settings["core"] = {
            "base_url": "https://synthetic-core.invalid",
            "token_env": "TS014_CORE",
        }
        self.p = Platform(self.settings, clock=lambda: self.now)
        self.p.models.publish(bearer("ADMIN"), config(self.now))
        self.dialogue = WebDialogue(self.p)
        self.body = {
            "conversation": "input-entry",
            "actor": "actor:a",
            "text": "合成原文",
            "client_id": str(uuid.uuid4()),
        }

    async def ingest(self, settings, request, contracts):
        self.assertEqual(request["input"]["parts"], [{"kind": "text", "text": "合成原文"}])
        authority = self.p.sources.read(bearer("COMPANION"), input_request(request))
        return core_fixture(request, authority, self.now, "self_private")

    async def accepted(self):
        with patch("services.platform.transport.core_post", side_effect=self.ingest):
            return await self.dialogue.send(self.body)

    def projection(self, request):
        data = json.loads(
            (CONTRACT.parents[1] / "web-conversation/v1/examples.json").read_text(encoding="utf-8")
        )[1]["document"]
        data.update(
            request_id=request["query"]["request_id"],
            conversation_id=request["conversation_id"],
            actor_id=request["actor_id"],
        )
        return data

    async def test_original_input_server_identity_real_confirmation_and_dedup(self):
        result = await self.accepted()
        self.assertEqual(result["state"], "accepted")
        self.assertNotIn("receipt", str(result))
        self.assertNotIn("assertion_ref", str(result))
        with patch(
            "services.platform.transport.core_post", side_effect=AssertionError("must not replay")
        ):
            self.assertEqual(await self.dialogue.send(self.body), result)
            restarted = WebDialogue(Platform(self.settings, clock=lambda: self.now))
            self.assertEqual(await restarted.send(self.body), result)
        with self.assertRaises(Fault) as caught:
            await self.dialogue.send({**self.body, "text": "changed"})
        self.assertEqual(caught.exception.code, "idempotency_conflict")

    async def test_uncertain_delivery_persists_and_never_replays(self):
        call = AsyncMock(side_effect=Fault("dependency_unavailable", 503))
        with patch("services.platform.transport.core_post", call):
            result = await self.dialogue.send(self.body)
            self.assertEqual(result["state"], "unknown")
            self.assertEqual(await self.dialogue.send(self.body), result)
        self.assertEqual(call.await_count, 1)

    async def test_all_forbidden_outcomes_are_not_an_admission(self):
        with patch.object(
            self.p.sources,
            "dispatch",
            AsyncMock(
                return_value={
                    "routing_state": "routed",
                    "outcomes": [{"actor_id": "actor:a", "state": "forbidden"}],
                }
            ),
        ):
            result = await self.dialogue.send(self.body)
        self.assertEqual(result["state"], "not_started")
        self.assertEqual(result["result"]["outcomes"][0]["state"], "forbidden")

    async def test_forged_scope_wrong_actor_and_missing_model(self):
        for body in (
            {**self.body, "verified": True},
            {**self.body, "actor": "other"},
            {**self.body, "conversation": "other"},
        ):
            with self.assertRaises(Fault):
                await self.dialogue.send(body)
        self.p.models.revoke(bearer("ADMIN"), 7)
        with self.assertRaises(Fault) as caught:
            await self.dialogue.send(self.body)
        self.assertEqual(caught.exception.code, "model_not_configured")

    async def test_snapshot_preserves_unknown_and_viewer_scope(self):
        await self.accepted()

        async def read(settings, path, request, contracts, schema):
            self.assertEqual(path, "web-snapshot")
            self.assertTrue(request["query"]["origin"]["assertion_ref"].startswith("origin:"))
            result = self.projection(request)
            turn = result["history"][0]
            turn["turn"].update(
                phase="closed_unknown", delivery_state="unknown", unresolved_delivery=True
            )
            turn["replies"][0].update(state="unknown", content_state="unavailable", text=None)
            contracts.check(schema, result)
            return result

        with patch("services.platform.web_dialogue.core_web_call", side_effect=read):
            result = await self.dialogue.snapshot(
                {"conversation": "input-entry", "actor": "actor:a", "before": None}
            )
        self.assertEqual(result["snapshot"]["history"][0]["turn"]["phase"], "closed_unknown")
        self.assertIsNone(result["snapshot"]["history"][0]["replies"][0]["text"])

    async def test_inflight_revocation_and_mismatched_snapshot_rejected(self):
        await self.accepted()

        async def read(settings, path, request, contracts, schema):
            self.p.origins.revoke(bearer("ADMIN"), "entry", "actor-a")
            return self.projection(request)

        with patch("services.platform.web_dialogue.core_web_call", side_effect=read):
            with self.assertRaises(Fault):
                await self.dialogue.snapshot(
                    {"conversation": "input-entry", "actor": "actor:a", "before": None}
                )

    async def test_cancel_keeps_current_version_error_and_server_origin(self):
        await self.accepted()

        async def read(settings, path, request, contracts, schema):
            if path == "web-snapshot":
                return self.projection(request)
            self.assertEqual(request["reason"], "explicit_user_cancel")
            self.assertEqual(request["expected_version"], 2)
            contracts.check("conversation#cancel_request", request)
            raise CoreFault(
                {
                    "schema_version": 1,
                    "request_id": request["command"]["request_id"],
                    "code": "version_conflict",
                    "execution_state": "not_started",
                    "retryable": False,
                    "current_version": 3,
                },
                409,
            )

        with patch("services.platform.web_dialogue.core_web_call", side_effect=read):
            with self.assertRaises(CoreFault) as caught:
                await self.dialogue.cancel(
                    {
                        "conversation": "input-entry",
                        "actor": "actor:a",
                        "turn_id": "turn:1",
                        "expected_version": 2,
                    }
                )
        self.assertEqual(caught.exception.document["current_version"], 3)

"""Actual verified TLS transport boundaries; the response owner here is synthetic."""

import asyncio
import copy
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web

from services.platform.contracts import Fault
from services.platform.relationships.client import Client
from services.platform.relationships.contract import Contract
from services.platform.transport import server_tls

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = (
    Path(os.environ["TS012_CONTRACT_DIR"]).parents[2]
    / "contracts/role-relationship/candidate-v1/schema.json"
)
PAIR = {"actor_id": "actor:a", "person_id": "person:synthetic"}


def projection():
    return {
        "view": "private",
        "pair": dict(PAIR),
        "version": 1,
        "policy_version": "synthetic-policy",
        "relationship_type": "unspecified",
        "display_label": "",
        "score": 0,
        "stage": "acquaintance",
        "frozen": False,
        "frozen_since": None,
        "decay_cursor": "2026-10-01T00:00:00Z",
        "checked_at": "2026-10-01T00:00:00Z",
    }


@unittest.skipUnless(
    os.environ.get("TS013_TLS_PYTHON"), "ephemeral certificate tool not configured"
)
class RelationshipTLS(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ts116-tls-")
        self.addCleanup(self.temp.cleanup)
        await asyncio.to_thread(
            subprocess.run,
            [
                os.environ["TS013_TLS_PYTHON"],
                str(ROOT / "tests/backend/make_tls_fixture.py"),
                self.temp.name,
            ],
            check=True,
            capture_output=True,
            timeout=20,
        )
        self.cert = str(Path(self.temp.name) / "localhost.pem")
        tls = {
            "certificate_file": self.cert,
            "private_key_file": str(Path(self.temp.name) / "localhost-key.pem"),
        }
        self.calls, self.traps = [], 0
        self.mode = "ok"
        self.mutation = None
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.token = "synthetic-ts116-manager-tls-credential"
        env = patch.dict(os.environ, {"TS116_TLS_MANAGER": self.token})
        env.start()
        self.addCleanup(env.stop)

        async def respond(request):
            payload = await request.json()
            self.calls.append((request.path, copy.deepcopy(payload)))
            if request.headers.get("Authorization") != "Bearer " + self.token:
                return web.json_response(
                    {"request_id": payload["request_id"], "code": "unauthorized"}, status=401
                )
            if payload["origin"] != {"assertion_ref": "synthetic-current-origin"}:
                return web.json_response(
                    {"request_id": payload["request_id"], "code": "forbidden"}, status=403
                )
            if self.mode == "redirect":
                return web.Response(status=307, headers={"Location": self.url + "/trap"})
            if self.mode == "pause":
                self.entered.set()
                await self.release.wait()
            if self.mode == "malformed":
                return web.Response(text="{", content_type="application/json")
            if self.mode == "oversize":
                return web.Response(text=" " * 32769, content_type="application/json")
            if self.mode == "disconnect":
                request.transport.close()
                return web.Response()
            if self.mode in {"version_conflict", "result_unknown", "untrusted"}:
                status = 409 if self.mode == "version_conflict" else 503
                return web.json_response(
                    {
                        "request_id": payload["request_id"],
                        "code": self.mode,
                        "detail": "synthetic peer detail must never reach caller",
                    },
                    status=status,
                )
            history = {
                "projection": projection(),
                "items": [
                    {
                        "id": "a" * 64,
                        "kind": "management",
                        "delta": 0,
                        "outcome": "accepted",
                        "at": "2026-10-01T00:00:00Z",
                        "valid": True,
                        "operation": "set_binding",
                        "reason": None,
                    }
                ],
                "has_more": False,
            }
            field = "history" if request.path.endswith("/history") else "projection"
            answer = {
                "schema_version": 1,
                "request_id": payload["request_id"],
                field: history if field == "history" else projection(),
            }
            if self.mutation:
                self.mutation(answer)
            headers = {"Content-Encoding": "gzip"} if self.mode == "encoding" else {}
            return web.json_response(answer, headers=headers)

        async def trap(request):
            self.traps += 1
            return web.json_response({})

        app = web.Application()
        app.router.add_post("/internal/v1/relationships/{operation}", respond)
        app.router.add_post("/trap", trap)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        self.addAsyncCleanup(self.runner.cleanup)
        site = web.TCPSite(self.runner, "127.0.0.1", 0, ssl_context=server_tls(tls))
        await site.start()
        self.url = "https://127.0.0.1:" + str(self.runner.addresses[0][1])
        self.client = Client(
            {
                "base_url": self.url,
                "token_env": "TS116_TLS_MANAGER",
                "ca_file": self.cert,
                "timeout_seconds": 1,
            },
            Contract(SCHEMA),
            [],
        )
        self.payload = {
            "schema_version": 1,
            "request_id": "synthetic-request",
            "origin": {"assertion_ref": "synthetic-current-origin"},
            "pair": PAIR,
            "managed": True,
        }

    async def assert_fault(self, code, operation="history"):
        with self.assertRaises(Fault) as caught:
            await self.client.call(operation, self.payload)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    async def test_real_tls_authentication_origin_and_private_history(self):
        assert (await self.client.call("history", self.payload))["projection"]["pair"] == PAIR
        with patch.dict(os.environ, {"TS116_TLS_MANAGER": "synthetic-wrong-credential"}):
            await self.assert_fault("unauthorized")
        self.payload["origin"]["assertion_ref"] = "synthetic-revoked-origin"
        await self.assert_fault("forbidden")
        assert len(self.calls) == 3

    async def test_untrusted_certificate_is_rejected_before_peer_receives_request(self):
        self.client.config.pop("ca_file")
        await self.assert_fault("dependency_unavailable")
        assert self.calls == []

    async def test_redirect_is_not_followed_and_write_is_unknown(self):
        self.mode = "redirect"
        await self.assert_fault("invalid_upstream")
        await self.assert_fault("result_unknown", "manage")
        assert len(self.calls) == 2 and self.traps == 0

    async def test_malformed_oversize_and_encoded_responses_are_opaque(self):
        for mode in ["malformed", "oversize", "encoding"]:
            with self.subTest(mode=mode):
                self.mode = mode
                await self.assert_fault("invalid_upstream")
                await self.assert_fault("result_unknown", "manage")
        assert len(self.calls) == 6

    async def test_closed_envelope_pair_and_history_types(self):
        mutations = [
            lambda a: a.update(request_id="wrong"),
            lambda a: a.update(schema_version=True),
            lambda a: a.update(private_source_text="must be rejected"),
            lambda a: a["history"]["projection"]["pair"].update(person_id="person:other"),
            lambda a: a["history"]["projection"].update(checked_at="not-a-time"),
            lambda a: a["history"]["projection"].update(frozen=True),
            lambda a: a["history"]["items"][0].update(id="raw-source-reference"),
            lambda a: a["history"]["items"][0].update(delta=True),
            lambda a: a["history"]["items"][0].update(kind=[]),
            lambda a: a["history"]["items"][0].update(outcome={}),
            lambda a: a["history"]["items"][0].update(operation=[]),
            lambda a: a["history"]["items"][0].update(at="2026-10-01"),
            lambda a: a["history"]["items"][0].update(reason="x" * 201),
            lambda a: a["history"].update(items=[a["history"]["items"][0]] * 21),
        ]
        for mutate in mutations:
            self.mutation = mutate
            await self.assert_fault("invalid_upstream")
        assert len(self.calls) == len(mutations)

    async def test_conflict_and_unknown_are_not_automatically_retried(self):
        for mode in ["version_conflict", "result_unknown", "untrusted", "disconnect"]:
            self.mode = mode
            error = await self.assert_fault(
                "version_conflict" if mode == "version_conflict" else "result_unknown", "manage"
            )
            assert "synthetic peer detail" not in str(error)
        assert len(self.calls) == 4

    async def test_timeout_and_cancellation_close_without_replay(self):
        self.mode = "pause"
        self.client.config["timeout_seconds"] = 0.1
        try:
            await self.assert_fault("result_unknown", "manage")
            self.client.config["timeout_seconds"] = 1
            self.entered.clear()
            task = asyncio.create_task(self.client.call("history", self.payload))
            await asyncio.wait_for(self.entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            self.release.set()
        assert len(self.calls) == 2

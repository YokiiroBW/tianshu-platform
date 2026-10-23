"""Actual loopback Platform routes and the one durable adapter claim/ack queue."""

import base64
import copy
import hashlib
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from unittest.mock import patch

import aiohttp
import test_bots as bot_fixtures
from fixtures import ENV, bearer, start_http

from services.platform.contracts import Fault
from services.platform.server import create_app
from services.platform.service import Platform


class DeliveryHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)
        self.now = time.time()
        self.settings = bot_fixtures.bot_settings(self.temp.name)
        self.platform = Platform(self.settings, clock=lambda: self.now)
        self.addCleanup(lambda: self.platform.close())
        registered = self.platform.bots.create("qq-onebot-main", ["actor:a"])
        self.connection_id = registered["connection_id"]
        self.token = "Bearer " + registered["token"]
        self.platform.bots.change(self.connection_id, "enable")
        original = bot_fixtures.BotTests.send_request(self)
        with self.platform.store.connect() as db:
            _, _, context = self.platform.origins.context(
                db,
                original["command"]["origin"]["assertion_ref"],
                "companion",
                "platform",
                "dialogue",
            )
        self.scope = context["allowed_scope"]
        self.channel = original["destination"]
        self.origin = {
            "kind": "response",
            "actor_id": "actor:a",
            "turn_id": "turn:c4",
            "origin": original["command"]["origin"],
            "sources": [],
        }
        runner, self.url = await start_http(create_app(self.platform))
        self.addAsyncCleanup(runner.cleanup)
        self.client = aiohttp.ClientSession()
        self.addAsyncCleanup(self.client.close)

    async def post(self, operation, body, *, token=None, bot=False):
        prefix = "/internal/v1/bot/replies/" if bot else "/internal/v2/bot-delivery/"
        async with self.client.post(
            self.url + prefix + operation,
            json=body,
            headers={"Authorization": token or bearer("COMPANION")},
        ) as response:
            return response.status, await response.json()

    def request(self, number=1, *, final=False, expression="expression:c4", origin=None):
        return {
            "schema_version": 2,
            "request_id": expression + ":" + str(number),
            "expression_id": expression,
            "origin": origin or self.origin,
            "scope": self.scope,
            "channel": self.channel,
            "final": final,
            "segments": [
                {
                    "segment_id": expression + ":segment:" + str(number),
                    "reply_id": expression + ":reply:" + str(number),
                    "segment_sequence": number,
                    "text": "actual segment " + str(number),
                    "content_refs": [],
                }
            ],
        }

    def lookup(self, expression="expression:c4", request_id="query:c4"):
        return {"schema_version": 2, "request_id": request_id, "expression_id": expression}

    async def claim(self, limit=20):
        status, result = await self.post(
            "claim",
            {"connection_id": self.connection_id, "instance_id": "c4:host", "limit": limit},
            token=self.token,
            bot=True,
        )
        self.assertEqual(status, 200, result)
        return result["deliveries"]

    async def ack(self, delivery):
        status, result = await self.post(
            "ack",
            {
                "connection_id": self.connection_id,
                "reply_id": delivery["reply_id"],
                "attempt_id": delivery["attempt_id"],
                "state": "sent",
                "channel_message_ids": ["sdk:" + delivery["reply_id"]],
            },
            token=self.token,
            bot=True,
        )
        self.assertEqual(status, 200, result)

    async def test_append_real_ack_finalize_and_all_three_origins(self):
        request = self.request()
        status, queued = await self.post("send", request)
        self.assertEqual((status, queued["state"]), (200, "queued"), queued)
        self.assertEqual((await self.post("send", request))[1], queued)
        self.assertEqual((await self.post("send", self.request(2)))[0], 200)
        deliveries = await self.claim()
        self.assertEqual(
            [item["text"] for item in deliveries], ["actual segment 1", "actual segment 2"]
        )
        for delivery in deliveries:
            await self.ack(delivery)
        status, intermediate = await self.post("query", self.lookup())
        self.assertEqual((status, intermediate["receipt"]["state"]), (200, "sending"))
        status, closed = await self.post(
            "finalize", {**self.lookup(request_id="final:c4"), "origin": self.origin}
        )
        self.assertEqual((status, closed["receipt"]["state"]), (200, "sent"), closed)
        self.assertEqual((await self.post("send", request))[0], 200)
        self.assertEqual((await self.post("send", self.request(3)))[0], 409)
        for kind in ("direct", "proactive"):
            origin = {"kind": kind, "actor_id": "actor:a", "sources": []}
            if kind == "direct":
                origin.update(direct_request_id="direct:c4", origin=self.origin["origin"])
            else:
                origin.update(motive_id="motive:c4", motive_version=1)
            request = self.request(final=True, expression="expression:" + kind, origin=origin)
            self.assertEqual((await self.post("send", request))[0], 200)
            await self.ack((await self.claim())[0])
            self.assertEqual(
                (await self.post("query", self.lookup("expression:" + kind)))[1]["receipt"][
                    "state"
                ],
                "sent",
            )
        with closing(self.platform.bots._db()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM replies").fetchone()[0], 4)

    async def test_cancel_pending_preserves_claimed_unknown_and_late_ack(self):
        self.assertEqual((await self.post("send", self.request()))[0], 200)
        self.assertEqual((await self.post("send", self.request(2)))[0], 200)
        claimed = (await self.claim(1))[0]
        status, result = await self.post("cancel", self.lookup(request_id="cancel:c4"))
        self.assertEqual(status, 200, result)
        self.assertEqual(
            [part["state"] for part in result["receipt"]["segments"]], ["sending", "cancelled"]
        )
        self.now += 61
        self.assertEqual(await self.claim(), [])
        result = (await self.post("query", self.lookup()))[1]
        self.assertEqual(result["receipt"]["state"], "unknown")
        await self.ack(claimed)
        result = (await self.post("query", self.lookup()))[1]
        self.assertEqual(result["receipt"]["state"], "partial")
        self.assertTrue(all(not part["retry_safe"] for part in result["receipt"]["segments"]))
        self.assertEqual((await self.post("query", self.lookup("missing")))[1]["receipt"], None)

    async def test_committed_append_error_is_unknown_and_original_claim_is_not_replayed(self):
        original = self.platform.bots.delivery.send
        failures = [OSError("synthetic exit"), Fault("dependency_unavailable", 503), TypeError()]
        for index, failure in enumerate(failures):
            with self.subTest(failure=type(failure).__name__):
                expression = f"expression:committed:{index}"
                request = self.request(expression=expression, final=True)
                calls = []

                def committed_then_failed(*args):
                    calls.append(args[1]["request_id"])
                    original(*args)
                    # The complete real queue operation has committed. A later
                    # authority-context/response failure cannot undo that fact.
                    raise failure

                with patch.object(self.platform.bots.delivery, "send", committed_then_failed):
                    status, result = await self.post("send", request)
                self.assertEqual(status, 503, result)
                self.assertEqual(result["execution_state"], "unknown")
                self.assertFalse(result["retryable"])
                self.assertEqual(calls, [request["request_id"]])
                lookup = self.lookup(expression)
                status, found = await self.post("query", lookup)
                self.assertEqual((status, found["receipt"]["state"]), (200, "queued"))
                deliveries = await self.claim()
                self.assertEqual(
                    [item["reply_id"] for item in deliveries], [request["segments"][0]["reply_id"]]
                )
                self.assertEqual(await self.claim(), [])
                await self.ack(deliveries[0])
                self.assertEqual(await self.claim(), [])
                self.assertEqual((await self.post("query", lookup))[1]["receipt"]["state"], "sent")
        with closing(self.platform.bots._db()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM replies").fetchone()[0], 3)

    async def test_committed_finalize_and_cancel_errors_remain_unknown(self):
        original = self.platform.bots.delivery.control
        for operation in ("finalize", "cancel"):
            with self.subTest(operation=operation):
                expression = "expression:control:" + operation
                self.assertEqual(
                    (await self.post("send", self.request(expression=expression)))[0], 200
                )
                request = self.lookup(expression, "control:" + operation)
                if operation == "finalize":
                    request["origin"] = self.origin
                calls = []

                def committed_then_failed(*args):
                    calls.append(args[2])
                    original(*args)
                    raise OSError("synthetic authority exit")

                with patch.object(self.platform.bots.delivery, "control", committed_then_failed):
                    status, result = await self.post(operation, request)
                self.assertEqual(status, 503, result)
                self.assertEqual(result["execution_state"], "unknown")
                self.assertFalse(result["retryable"])
                self.assertEqual(calls, [operation])
                receipt = (await self.post("query", self.lookup(expression)))[1]["receipt"]
                self.assertTrue(receipt["final"])
                self.assertEqual(
                    receipt["state"], "queued" if operation == "finalize" else "cancelled"
                )
                deliveries = await self.claim()
                self.assertEqual(len(deliveries), 1 if operation == "finalize" else 0)
                if deliveries:
                    await self.ack(deliveries[0])
                self.assertEqual(await self.claim(), [])

    async def test_response_encoding_failure_after_append_is_unknown(self):
        from services.platform import server

        original = server.web.json_response

        def encode(body, *args, **kwargs):
            if isinstance(body, dict) and body.get("expression_id") == "expression:c4":
                raise TypeError("synthetic response encoding failure")
            return original(body, *args, **kwargs)

        with patch.object(server.web, "json_response", encode):
            status, result = await self.post("send", self.request())
        self.assertEqual(status, 503, result)
        self.assertEqual(result["execution_state"], "unknown")
        self.assertFalse(result["retryable"])
        self.assertEqual((await self.post("query", self.lookup()))[1]["receipt"]["state"], "queued")
        self.assertEqual(len(await self.claim()), 1)
        self.assertEqual(await self.claim(), [])

    async def test_explicit_mutation_rejections_are_still_not_started(self):
        invalid = self.request()
        invalid.pop("segments")
        foreign = self.request()
        foreign["scope"] = dict(self.scope, person_id="person:foreign")
        for request, token, expected in (
            (invalid, None, 400),
            (self.request(), "Bearer invalid", 401),
            (self.request(), bearer("MEMORY"), 403),
            (foreign, None, 403),
        ):
            with self.subTest(expected=expected, token=token is not None):
                status, result = await self.post("send", request, token=token)
                self.assertEqual(status, expected, result)
                self.assertEqual(result["execution_state"], "not_started")
                self.assertFalse(result["retryable"])
        self.assertIsNone((await self.post("query", self.lookup()))[1]["receipt"])
        self.assertEqual(await self.claim(), [])

    async def test_log_or_executor_refusal_does_not_claim_mutation_started(self):
        from services.platform import diagnostics

        with patch.object(diagnostics, "admitted", return_value=False):
            status, result = await self.post("send", self.request())
        self.assertEqual(status, 503, result)
        self.assertEqual(result["execution_state"], "not_started")
        self.assertTrue(result["retryable"])
        with (
            patch.object(self.platform.local_work.executor, "submit", side_effect=RuntimeError()),
            patch.object(
                self.platform.bots.delivery, "send", wraps=self.platform.bots.delivery.send
            ) as send,
        ):
            status, result = await self.post("send", self.request())
            self.assertEqual(send.call_count, 0)
        self.assertEqual(status, 503, result)
        self.assertEqual(result["execution_state"], "not_started")
        self.assertTrue(result["retryable"])
        self.assertIsNone((await self.post("query", self.lookup()))[1]["receipt"])

    async def test_read_only_delivery_errors_keep_their_existing_contract(self):
        with patch.object(self.platform.bots.delivery, "control", side_effect=OSError()):
            status, result = await self.post("query", self.lookup())
        self.assertEqual(status, 503, result)
        self.assertEqual(result["execution_state"], "not_started")
        self.assertTrue(result["retryable"])
        request = dict(
            schema_version=2,
            request_id="context:failure",
            origin=self.origin,
            scope=self.scope,
            channel=self.channel,
        )
        with patch.object(self.platform.bots.delivery, "context", side_effect=OSError()):
            status, result = await self.post("context", request)
        self.assertEqual(status, 503, result)
        self.assertEqual(result["execution_state"], "not_started")
        self.assertTrue(result["retryable"])

    async def test_scope_and_connection_revoke_not_receipt_admission(self):
        request = self.request()
        self.assertEqual((await self.post("send", request, token=bearer("MEMORY")))[0], 403)
        changed = copy.deepcopy(request)
        changed["scope"]["person_id"] = "person:foreign"
        self.assertEqual((await self.post("send", changed))[0], 403)
        self.assertEqual((await self.post("send", request))[0], 200)
        self.platform.bots.change(self.connection_id, "disable")
        self.assertEqual((await self.post("query", self.lookup()))[0], 403)
        self.assertEqual((await self.post("send", self.request(2)))[0], 403)

    async def test_media_materialization_and_expression_total_not_append_total(self):
        raw = b"isolated original bytes" * 150000
        reference = {
            "owner": "memory",
            "object_id": "original:c4",
            "version": 1,
            "kind": "image",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "sources": [],
            "coverage": {"unit": "bytes", "start": 0, "end": len(raw), "total": len(raw)},
        }
        media = {
            "content_ref": reference,
            "media_type": "image/png",
            "encoding": "base64",
            "data": base64.b64encode(raw).decode(),
            "sha256": reference["sha256"],
        }
        first = self.request()
        first["segments"][0].update(content_refs=[reference], media=[media])
        status, result = await self.post("send", first)
        self.assertEqual(status, 200, result)
        # Replay does not count the original twice, even with a fresh transport request id.
        self.assertEqual((await self.post("send", {**first, "request_id": "replay:c4"}))[0], 200)
        for number in (2, 3, 4):
            request = self.request(number)
            request["segments"][0].update(content_refs=[reference], media=[media])
            self.assertEqual((await self.post("send", request))[0], 200)
        request = self.request(5)
        request["segments"][0].update(content_refs=[reference], media=[media])
        self.assertEqual((await self.post("send", request))[0], 413)
        deliveries = await self.claim()
        self.assertEqual(len(deliveries), 4)
        self.assertEqual(base64.b64decode(deliveries[0]["media"][0]["data"]), raw)
        missing = self.request(expression="missing-bytes")
        missing["segments"][0]["content_refs"] = [reference]
        self.assertEqual((await self.post("send", missing))[0], 400)
        large_raw = b"x" * (17 * 1024 * 1024)
        reference = {**reference, "sha256": hashlib.sha256(large_raw).hexdigest()}
        media = {
            **media,
            "content_ref": reference,
            "sha256": reference["sha256"],
            "data": base64.b64encode(large_raw).decode(),
        }
        for number, expected in ((1, 200), (2, 413)):
            request = self.request(number, expression="total-bytes")
            request["segments"][0].update(content_refs=[reference], media=[media])
            self.assertEqual((await self.post("send", request))[0], expected)

    async def test_current_proactive_context_after_original_expired_and_revocation(self):
        self.now += 3600
        request = {
            "schema_version": 2,
            "request_id": "current:proactive",
            "origin": {
                "kind": "proactive",
                "actor_id": "actor:a",
                "motive_id": "motive:actual",
                "motive_version": 1,
                "sources": [],
            },
            "scope": self.scope,
            "channel": self.channel,
        }
        status, context = await self.post("context", request)
        self.assertEqual(status, 200, context)
        with self.platform.store.connect() as db:
            _, entry, current = self.platform.origins.context(
                db, context["origin"]["assertion_ref"], "companion", "memory", "dialogue"
            )
            self.assertEqual(current["allowed_scope"], self.scope)
            self.assertEqual(entry["account"]["immutable_account_id"], "1001")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_inputs").fetchone()[0], 0)
        self.assertEqual((await self.post("context", {**request, "origin": self.origin}))[0], 200)
        self.platform.origins.revoke(bearer("ADMIN"), "entry", "bot-actor-1")
        self.assertEqual((await self.post("context", request))[0], 403)

    async def test_migrate_existing_queue_preserves_nonempty_receipts_once(self):
        self.assertEqual((await self.post("send", self.request()))[0], 200)
        with closing(self.platform.bots._db()) as db, db:
            db.execute("PRAGMA user_version=1")
        # A rollback-compatible old queue preserves all rows; migration is additive.
        from services.platform.bots import Bots

        migrated = Bots(self.platform)
        with closing(migrated._db()) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM replies").fetchone()[0], 1)
        from pathlib import Path

        backups = list(Path(self.temp.name).glob("*.pre-delivery-*.sqlite"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM replies").fetchone()[0], 1)
        Bots(self.platform)
        self.assertEqual(len(list(Path(self.temp.name).glob("*.pre-delivery-*.sqlite"))), 1)

    async def test_expired_response_continues_same_reference_and_sends_once(self):
        self.now += 3600
        request = dict(
            schema_version=2,
            request_id="resume",
            origin=self.origin,
            scope=self.scope,
            channel=self.channel,
        )
        self.assertEqual((await self.post("send", self.request(final=True)))[0], 403)
        status, result = await self.post("context", request)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["origin"], self.origin["origin"])
        self.assertEqual((await self.post("send", self.request(final=True)))[0], 200)
        self.now += 3600
        self.assertEqual((await self.post("context", request))[0], 200)
        self.assertEqual((await self.post("send", self.request(final=True)))[0], 200)
        self.assertEqual(len(await self.claim()), 1)

    async def test_continuation_does_not_revive_revoked_or_retarget_origin(self):
        self.now += 3600
        request = dict(
            schema_version=2,
            request_id="resume",
            origin=self.origin,
            scope=self.scope,
            channel=self.channel,
        )
        bad = copy.deepcopy(request)
        bad["scope"]["person_id"] = "person:other"
        self.assertEqual((await self.post("context", bad))[0], 403)
        ref = self.origin["origin"]["assertion_ref"]
        self.platform.origins.revoke(bearer("ADMIN"), "origin", ref)
        self.assertEqual((await self.post("context", request))[0], 403)

    async def test_direct_continuation_still_checks_disabled_connection(self):
        self.now += 3600
        origin = dict(
            kind="direct",
            actor_id="actor:a",
            direct_request_id="direct:long",
            origin=self.origin["origin"],
            sources=[],
        )
        request = dict(
            schema_version=2,
            request_id="resume-direct",
            origin=origin,
            scope=self.scope,
            channel=self.channel,
        )
        self.assertEqual((await self.post("context", request))[0], 200)
        self.platform.bots.change(self.connection_id, "disable")
        self.assertEqual((await self.post("context", request))[0], 403)

    @unittest.skipUnless(
        os.environ.get("TIANSHU_COMPANION_REPO"), "Set companion checkout for joint test"
    )
    async def test_actual_companion_sender_recovers_expired_origin(self):
        import sys
        from pathlib import Path
        import httpx

        sys.path.insert(0, str(Path(os.environ["TIANSHU_COMPANION_REPO"]) / "src"))
        from tianshu_companion.clients import JsonService, PlatformBotSender
        from tianshu_companion.contracts import Contracts
        from fixtures import CONTRACT

        async def forward(request):
            async with self.client.post(
                self.url + request.url.path,
                data=request.content,
                headers={
                    "Authorization": request.headers["Authorization"],
                    "Content-Type": "application/json",
                },
            ) as response:
                return httpx.Response(response.status, json=await response.json())

        client = JsonService(
            "https://fixture.invalid",
            bearer("COMPANION").removeprefix("Bearer "),
            transport=httpx.MockTransport(forward),
        )
        self.addAsyncCleanup(client.close)
        sender = PlatformBotSender(Contracts(CONTRACT), client)
        self.now += 3600
        request = self.request(final=True)
        first = await sender.send_expression(request)
        self.assertEqual(first["state"], "queued")
        self.now += 3600
        second = await sender.send_expression(request)
        self.assertEqual(first["expression_id"], second["expression_id"])
        self.assertEqual(len(await self.claim()), 1)

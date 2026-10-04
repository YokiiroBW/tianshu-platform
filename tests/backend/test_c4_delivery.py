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
        self.assertEqual((await self.post("context", {**request, "origin": self.origin}))[0], 400)
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

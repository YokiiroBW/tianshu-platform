import asyncio
import copy
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from unittest.mock import patch

import aiohttp

from fixtures import ENV, bearer, start_http
from services.platform.contracts import Fault, utc
from services.platform.service import Platform
from services.platform.server import create_app
from services.platform.web_sender import WebSender
from source_fixtures import SOURCE_DOCS, core_fixture, input_request
from web_fixtures import web_settings


class WebSenderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)
        self.now = time.time()
        self.settings = web_settings(self.temp.name)
        self.p = Platform(self.settings, clock=lambda: self.now)
        self.sender = WebSender(self.p)
        ingest = copy.deepcopy(SOURCE_DOCS["self_private/request"])
        ref = self.p.sources.register_input(bearer("ADMIN"), "input-entry", ingest["input"])
        ingest["command"]["origin"] = {"assertion_ref": ref["assertion_ref"]}
        ingest["command"]["deadline_at"] = utc(self.now + 30)
        ticket = self.p.sources.prepare_mapping(bearer("ADMIN"), ingest)
        authority = self.p.sources.read(bearer("COMPANION"), input_request(ingest))
        response = core_fixture(ingest, authority, self.now, "self_private")
        self.p.sources.confirm_mapping(bearer("COMPANION"), ticket, response)
        origin = self.p.origins.issue(bearer("ADMIN"), "actor-a")
        self.request = {
            "command": {
                "schema_version": 1,
                "request_id": "request:reply",
                "idempotency_key": "command:reply",
                "origin": {"assertion_ref": origin["assertion_ref"]},
                "deadline_at": utc(self.now + 30),
            },
            "conversation_id": response["conversation_id"],
            "turn_id": "turn:web",
            "turn_sequence": 1,
            "reply_id": "reply:web",
            "actor_id": "actor:a",
            "destination": ingest["input"]["message_key"]["channel"],
            "segment_sequence": 1,
            "segment_count": 2,
            "text": "合成确认回复",
        }

    def reject(self, request, code="forbidden", header=None):
        with self.assertRaises(Fault) as caught:
            self.sender.send(header or bearer("COMPANION"), request)
        self.assertEqual(caught.exception.code, code)

    def test_persisted_sent_restart_dedup_and_segments(self):
        receipt = self.sender.send(bearer("COMPANION"), self.request)
        self.assertEqual(receipt["state"], "sent")
        restarted = WebSender(Platform(self.settings, clock=lambda: self.now))
        self.assertEqual(restarted.send(bearer("COMPANION"), self.request), receipt)
        changed = copy.deepcopy(self.request)
        changed["command"]["request_id"] = "request:retry"
        retried = restarted.send(bearer("COMPANION"), changed)
        self.assertEqual(retried, {**receipt, "request_id": "request:retry"})
        second = {
            **self.request,
            "reply_id": "reply:second",
            "segment_sequence": 2,
            "text": "第二段",
            "command": {**self.request["command"], "idempotency_key": "command:second"},
        }
        self.assertEqual(restarted.send(bearer("COMPANION"), second)["state"], "sent")
        with closing(sqlite3.connect(self.sender.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM replies").fetchone()[0], 2)

    def test_conflicting_text_or_duplicate_segment_rejected(self):
        self.sender.send(bearer("COMPANION"), self.request)
        self.reject({**self.request, "text": "changed"}, "idempotency_conflict")
        self.reject({**self.request, "reply_id": "reply:other"}, "idempotency_conflict")
        self.reject({**self.request, "segment_sequence": 3}, "invalid_input")

    def test_actor_destination_conversation_and_service_boundaries(self):
        self.reject({**self.request, "actor_id": "actor:b"})
        self.reject({**self.request, "conversation_id": "conversation:other"})
        self.reject(
            {
                **self.request,
                "destination": {**self.request["destination"], "channel_conversation_id": "other"},
            }
        )
        self.reject(self.request, header=bearer("ADMIN"))
        with closing(sqlite3.connect(self.sender.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM replies").fetchone()[0], 0)

    def test_revoke_expiry_and_deadline_before_existing_receipt(self):
        self.sender.send(bearer("COMPANION"), self.request)
        self.now += 31
        self.reject(self.request, "timeout")
        current = copy.deepcopy(self.request)
        current["command"]["deadline_at"] = utc(self.now + 100)
        self.now += 30
        self.reject(current)

    def test_input_revocation_blocks_delivery(self):
        self.p.origins.revoke(bearer("ADMIN"), "entry", "input-entry")
        self.reject(self.request)

    def test_durable_write_failure_never_reports_sent(self):
        with patch(
            "services.platform.web_sender.sqlite3.connect",
            side_effect=sqlite3.OperationalError("synthetic disk failure"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.sender.send(bearer("COMPANION"), self.request)

    def test_actual_service_http_sender_and_browser_rejection(self):
        async def run():
            runner, url = await start_http(create_app(self.p))
            try:
                async with aiohttp.ClientSession() as client:
                    async with client.post(
                        url + "/internal/v1/conversation/send",
                        json=self.request,
                        headers={"Authorization": bearer("COMPANION")},
                    ) as response:
                        self.assertEqual(response.status, 200)
                        self.assertEqual((await response.json())["state"], "sent")
                    for extra in ({"Cookie": "tianshu_session=synthetic"}, {"Origin": url}):
                        async with client.post(
                            url + "/internal/v1/conversation/send",
                            json=self.request,
                            headers={"Authorization": bearer("COMPANION"), **extra},
                        ) as response:
                            self.assertEqual(response.status, 403)
            finally:
                await runner.cleanup()

        asyncio.run(run())

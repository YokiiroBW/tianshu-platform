"""Real Platform event/admission/archive indexes; Core replies are explicit fixtures."""

import copy
import os
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fixtures import ENV, bearer
from services.platform.contracts import Fault, utc
from services.platform.service import Platform
from source_fixtures import SOURCE_DOCS, core_fixture, input_request
from test_bot_observation import policy
from test_bots import bot_settings


class SegmentMappingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        environment = patch.dict(
            os.environ, {**ENV, "TS_ADAPTER_CORE_TOKEN": "synthetic-core-adapter-token-123456789"}
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.now = time.time()
        settings = bot_settings(self.temp.name)
        settings["core"] = {
            "base_url": "https://127.0.0.1:9443",
            "token_env": "TS_ADAPTER_CORE_TOKEN",
        }
        settings["principals"]["memory"]["actions"].append("observation.verify")
        settings["bot_adapter_self_service"] = {
            "directory": str(Path(self.temp.name) / "adapters"),
            "allowed_cidrs": ["127.0.0.0/8"],
            "actors": [{"id": "actor:a", "label": "Synthetic"}],
        }
        self.platform = Platform(settings, clock=lambda: self.now)
        self.addAsyncCleanup(self._close)
        self.manager = self.platform.bot_observation
        self.row = dict(
            kind="observation",
            id="obs:segments",
            name="Synthetic",
            adapter="nonebot",
            instance_id="sdk:segment",
            account_id="42",
            enabled=True,
            revision=1,
            host_revision=1,
            observation_epoch=1,
            archive_epoch=1,
            read_enabled=True,
            state="ready",
            pending=None,
            last_error=None,
            last_checked_at=None,
            group_policy=policy(mode="blacklist", actor="actor:a"),
            private_policy=policy(actor="actor:a"),
        )
        self.manager.catalog.put(self.row)
        with closing(self.manager._db()) as db, db:
            db.execute(
                "INSERT INTO observation_versions VALUES(?,?,?,?,?)", (self.row["id"], 1, 1, 1, 1)
            )
        self.frames, self.core_bindings, self.calls = [], {}, []

        async def core_management(operation, body):
            self.calls.append(operation)
            if operation == "learning-frame":
                self.frames.append(copy.deepcopy(body))
                return dict(
                    schema_version=1,
                    valid=True,
                    frame=dict(
                        actor_id=body["actor_id"],
                        audience="group",
                        conversation_id=SOURCE_DOCS["group/response"]["conversation_id"],
                    ),
                    channel=body["channel"],
                    delivery_channel=body["delivery_channel"],
                )
            if operation == "apply":
                value = {key: body[key] for key in ("connection_id", "revision", "enabled")}
                self.core_bindings[body["connection_id"]] = value
                return value
            value = self.core_bindings.get(body["connection_id"])
            return dict(found=value is not None, binding=value)

        self.platform.bot_adapters._core = core_management
        self.connection, self.token = await self.manager._ensure_reply_scope(
            self.row, "group:999", "7", "actor:a"
        )
        self.ingests = []

        async def core_response(_settings, ingest, _contracts):
            self.calls.append("ingest")
            authority = self.platform.sources.read(bearer("COMPANION"), input_request(ingest))
            response = core_fixture(ingest, authority, self.now)
            for outcome in response["outcomes"]:
                if outcome["receipt"] is not None:
                    outcome["receipt"]["collection_key"] = {
                        "channel": copy.deepcopy(ingest["input"]["message_key"]["channel"]),
                        "author": copy.deepcopy(ingest["input"]["author"]),
                    }
            self.ingests.append(copy.deepcopy(ingest))
            return response

        core = patch("services.platform.transport.core_post", core_response)
        core.start()
        self.addCleanup(core.stop)

    async def _close(self):
        self.platform.close()

    def event(self, native="native:1"):
        return dict(
            schema_version=1,
            connection_id=self.connection,
            platform_id="sdk:segment",
            self_id="42",
            event_id=native,
            revision=1,
            namespace="qq",
            conversation_id="group:999",
            thread_id=None,
            account_id="7",
            sent_at=utc(self.now),
            text="Synthetic source",
        )

    def archive(self, event):
        return self.manager.record(
            self.row,
            {
                key: value
                for key, value in dict(
                    event, schema_version=2, scope_revision=1, content_state="text", mentioned=True
                ).items()
                if key not in {"connection_id", "thread_id"}
            },
        )

    async def accepted(self, *, archive=True):
        event = self.event()
        proof = self.archive(event) if archive else None
        result = await self.platform.bots.event("Bearer " + self.token, event)
        self.assertEqual(result["state"], "accepted")
        with self.platform.store.connect() as db:
            saved = db.execute("SELECT admission FROM admission_history").fetchall()
        self.assertEqual(len(saved), 1)
        from services.platform.contracts import loads

        admission = loads(saved[0][0])
        request = dict(
            operation="segment_resolve",
            actor_id="actor:a",
            sources=[dict(source=admission["source"], scope=admission["scope"])],
        )
        return event, proof, admission, request

    def resolve(self, request):
        return self.manager.verify(bearer("MEMORY"), request)

    async def test_real_native_mapping_uses_receipt_and_alias_before_ingest(self):
        event, proof, admission, request = await self.accepted()
        resolved = self.resolve(request)
        self.assertLess(self.calls.index("learning-frame"), self.calls.index("ingest"))
        self.assertEqual(resolved["sources"][0]["source"], admission["source"])
        self.assertEqual(resolved["sources"][0]["scope"], admission["scope"])
        self.assertEqual(
            resolved["sources"][0]["observation"],
            dict(**proof, instance_id="sdk:segment", self_id="42", conversation_id="group:999"),
        )
        self.assertNotEqual(event["event_id"], admission["source"]["message_key"]["message_id"])
        # A lost outer ACK does not erase an already confirmed Core admission.
        with closing(self.platform.bots._db()) as db, db:
            db.execute("UPDATE events SET state='unknown',result=NULL")
        self.assertEqual(self.resolve(request), resolved)

    async def test_known_mapping_waits_for_archive_or_confirmation_without_forking(self):
        event, _, _, request = await self.accepted(archive=False)
        with self.assertRaises(Fault) as missing_archive:
            self.resolve(request)
        self.assertEqual(missing_archive.exception.status, 503)
        self.archive(event)
        self.assertIsNotNone(self.resolve(request)["sources"][0]["observation"])
        with self.platform.store.connect(write=True) as db:
            db.execute("DELETE FROM admission_history")
        with self.assertRaises(Fault) as missing_confirm:
            self.resolve(request)
        self.assertEqual(missing_confirm.exception.status, 503)

    async def test_mapping_rejects_another_actor_scope_receipt_and_channel(self):
        _, _, _, request = await self.accepted()
        for field in ("actor", "scope", "receipt", "channel", "duplicate"):
            changed = copy.deepcopy(request)
            if field == "actor":
                changed["actor_id"] = "actor:b"
            elif field == "scope":
                changed["sources"][0]["scope"]["person_id"] = "person:other"
            elif field == "receipt":
                changed["sources"][0]["source"]["receipt_id"] = "receipt:other"
            elif field == "channel":
                changed["sources"][0]["source"]["message_key"]["channel"]["binding_id"] = (
                    "binding:other"
                )
            else:
                changed["sources"] *= 2
            with self.subTest(field=field), self.assertRaises(Fault):
                self.resolve(changed)
        with self.assertRaises(Fault):
            self.manager.verify(bearer("COMPANION"), request)

    async def test_unmapped_source_keeps_original_and_old_mapping_still_requires_fresh_proof(self):
        _, proof, _, request = await self.accepted()
        no_event = copy.deepcopy(request)
        no_event["sources"][0]["source"]["message_key"]["message_id"] = "message:manual"
        self.assertIsNone(self.resolve(no_event)["sources"][0]["observation"])
        # A known manual adapter is not inferred to own an observation frame.
        with closing(self.manager._db()) as db, db:
            reply = dict(db.execute("SELECT * FROM reply_connections").fetchone())
            db.execute("DELETE FROM reply_connections")
        self.assertIsNone(self.resolve(request)["sources"][0]["observation"])
        with closing(self.manager._db()) as db, db:
            db.execute(
                "INSERT INTO reply_connections VALUES(?,?,?,?)",
                tuple(
                    reply[key]
                    for key in ("connection_id", "observation_id", "conversation", "actor_id")
                ),
            )
        self.manager.catalog.put(dict(self.row, revision=2, host_revision=2, archive_epoch=2))
        mapped = self.resolve(request)["sources"][0]["observation"]
        self.assertEqual(mapped["archive_epoch"], 1)
        with self.assertRaises(Fault) as revoked:
            self.manager.verify(
                bearer("MEMORY"),
                dict(
                    operation="segment",
                    actor_id="actor:a",
                    instance_id="sdk:segment",
                    self_id="42",
                    conversation_id="group:999",
                    sources=[proof],
                ),
            )
        self.assertEqual(revoked.exception.status, 409)

    def context_request(self, admission):
        return dict(
            schema_version=2,
            request_id="context:segment-candidates",
            origin=dict(
                kind="response",
                actor_id=admission["scope"]["actor_id"],
                turn_id="turn:segment-candidates",
                origin=copy.deepcopy(admission["accepted_origin"]),
                sources=[],
            ),
            scope=copy.deepcopy(admission["scope"]),
            channel=copy.deepcopy(admission["source"]["message_key"]["channel"]),
        )

    async def test_delivery_context_skips_unrelated_channel_managed_reads(self):
        _, _, admission, _ = await self.accepted()
        unrelated, _ = await self.manager._ensure_reply_scope(
            self.row, "group:1000", "8", "actor:a"
        )
        self.assertNotEqual(unrelated, self.connection)
        with (
            patch.object(
                self.platform.bots, "_managed_active", wraps=self.platform.bots._managed_active
            ) as managed,
            patch.object(self.manager, "get", wraps=self.manager.get) as catalog,
        ):
            result = self.platform.bots.delivery.context(
                bearer("COMPANION"), self.context_request(admission)
            )
        self.assertEqual(result["scope"], admission["scope"])
        self.assertEqual(
            [call.args[0]["id"] for call in managed.call_args_list],
            [self.connection, self.connection],
        )
        self.assertEqual(catalog.call_count, 2)

    async def test_delivery_context_still_rejects_matching_inactive_connection(self):
        _, _, admission, _ = await self.accepted()
        self.manager.catalog.put(dict(self.row, enabled=False))
        with (
            patch.object(
                self.platform.bots, "_managed_active", wraps=self.platform.bots._managed_active
            ) as managed,
            patch.object(self.manager, "get", wraps=self.manager.get) as catalog,
            self.assertRaises(Fault) as rejected,
        ):
            self.platform.bots.delivery.context(
                bearer("COMPANION"), self.context_request(admission)
            )
        self.assertEqual((rejected.exception.status, rejected.exception.code), (403, "forbidden"))
        self.assertEqual([call.args[0]["id"] for call in managed.call_args_list], [self.connection])
        self.assertEqual(catalog.call_count, 1)

    async def test_delivery_context_rechecks_catalog_after_candidate_selection(self):
        _, _, admission, _ = await self.accepted()
        original = self.platform.bots._managed_active
        calls = []

        def revoke_after_first_guard(row):
            active = original(row)
            calls.append(row["id"])
            if len(calls) == 1:
                # Model a real control change between the two live reads.
                self.manager.catalog.put(dict(self.row, enabled=False))
            return active

        with (
            patch.object(self.platform.bots, "_managed_active", revoke_after_first_guard),
            patch.object(self.manager, "get", wraps=self.manager.get) as catalog,
            self.assertRaises(Fault) as rejected,
        ):
            self.platform.bots.delivery.context(
                bearer("COMPANION"), self.context_request(admission)
            )
        self.assertEqual((rejected.exception.status, rejected.exception.code), (403, "forbidden"))
        self.assertEqual(calls, [self.connection, self.connection])
        self.assertEqual(catalog.call_count, 2)

    async def test_delivery_context_keeps_duplicate_physical_owner_rejection(self):
        _, _, admission, _ = await self.accepted()
        bots = self.platform.bots
        with closing(bots._db()) as db:
            saved = dict(
                db.execute("SELECT * FROM connections WHERE id=?", (self.connection,)).fetchone()
            )
        duplicate_slot = "fixture:duplicate-observation-target"
        bots.slots[duplicate_slot] = copy.deepcopy(bots.slots[saved["slot_id"]])
        duplicate = bots.create(duplicate_slot, ["actor:a"])["connection_id"]
        # Normal enable rejects this state. Retain the existing read-side guard
        # for an inconsistent/legacy enabled row as well.
        with closing(bots._db()) as db, db:
            db.execute("UPDATE connections SET enabled=1 WHERE id=?", (duplicate,))
        with self.assertRaises(Fault) as rejected:
            bots.delivery.context(bearer("COMPANION"), self.context_request(admission))
        self.assertEqual(
            (rejected.exception.status, rejected.exception.code), (409, "idempotency_conflict")
        )

"""Real Platform transactions; explicit recorded Core/host peers, never SDK/CAS evidence."""

import copy
import json
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import aiohttp

from fixtures import bearer, start_http
from source_fixtures import core_fixture, input_request
import test_bot_adapters as adapter_fixtures
from services.platform.candidate_protocol import CandidateProtocol
from services.platform.contracts import Fault, canonical, digest, utc
from services.platform.participation.protocol import MANIFEST, PACKAGE, candidate_ref
from services.platform.server import create_app


class ParticipationTests(unittest.IsolatedAsyncioTestCase):
    _close = adapter_fixtures.AdapterTests._close
    _install_core_peer = adapter_fixtures.AdapterTests._install_core_peer

    async def asyncSetUp(self):
        await adapter_fixtures.AdapterTests.asyncSetUp(self)
        self.now = time.time()
        self.platform.origins.clock = lambda: self.now
        self.platform.sources.clock = lambda: self.now
        self.part = self.platform.participation
        self.manager = self.platform.bot_observation
        self.row = dict(
            kind="observation",
            id="obs:participation",
            name="synthetic",
            adapter="nonebot",
            address=self.address,
            access_key="synthetic-plugin-key-123456789",
            allow_private_http=True,
            ca_pem=None,
            pins=["127.0.0.1"],
            instance_id="sdk:one",
            account_id="42000",
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
            group_policy=dict(observe=True, mode="blacklist", list=[], actor_id="actor:a"),
            private_policy=dict(observe=True, mode="observe_only", list=[], actor_id=None),
        )
        self.manager.catalog.put(self.row)
        with closing(self.manager._db()) as db, db:
            db.execute(
                "INSERT INTO observation_versions VALUES(?,?,?,?,?)", (self.row["id"], 1, 1, 1, 1)
            )
        self.host_candidate = self.host_decision = self.host_registration = None
        self.host_claim = None
        self.host_active = True
        self.host_state = "free"
        self.host_bindings = {}
        self.core_calls = []
        self.claim_calls = 0
        self.original_plugin = self.platform.bot_adapters._plugin
        self.platform.bot_adapters._plugin = self.plugin
        self.core_patch = patch("services.platform.transport.core_post", side_effect=self.core)
        self.core_patch.start()
        self.addCleanup(self.core_patch.stop)

    def event(self, identity="native:one", *, mentioned=False):
        return dict(
            schema_version=2,
            platform_id="sdk:one",
            self_id="42000",
            namespace="qq",
            conversation_id="group:99900",
            account_id="70000",
            event_id=identity,
            revision=1,
            sent_at=utc(self.now),
            text="首次旁听作者的真实文本",
            content_state="text",
            mentioned=mentioned,
            scope_revision=1,
        )

    def owner(self, native):
        completion = dict(
            event_record_id="event:actual",
            handler_set_digest="a" * 64,
            capture_receipt_id="capture:actual",
            explicit_reply_receipt_id="explicit:actual",
            scope_receipt_id="scope:actual",
            closure_receipt_id="closure:actual",
            completed_at=utc(self.now),
        )
        state = "claimed" if self.host_claim else self.host_state
        return dict(
            native=native,
            host_epoch="host:one",
            host_generation="observation-policy:1",
            owner_revision=2 if self.host_claim else 1,
            state=state,
            basis={
                "free": "all_handlers_completed",
                "claimed": "claim_cas",
                "handled_elsewhere": "other_handler_entered",
                "processing": "event_processing",
            }[state],
            observed_at=utc(self.now),
            claim=self.host_claim,
            completion_evidence=completion if state in {"free", "claimed"} else None,
        )

    async def plugin(self, row, path, body):
        if path == "/observation/status":
            return {"found": True, "account": {"revision": 1, "pending": 0, "dropped": 0}}
        if path == "/observation/ack":
            self.acked.extend(body["event_ids"])
            return {"acknowledged": body["event_ids"]}
        if not path.startswith("/participation/"):
            return await self.original_plugin(row, path, body)
        operation = path.rsplit("/", 1)[1]
        if operation == "capabilities":
            return dict(
                available=True,
                framework=dict(name="nonebot2", version="2.5.0"),
                host_epoch="host:one",
                host_generation="observation-policy:1",
                manifest_sha256=MANIFEST,
                claim_supported=True,
            )
        if operation in {"candidate", "decision", "register"}:
            setattr(
                self,
                "host_" + ("registration" if operation == "register" else operation),
                copy.deepcopy(body),
            )
            return {"registration" if operation == "register" else operation: body}
        if operation == "ownership":
            return {
                "found": True,
                "ownership": dict(
                    schema_version=1, kind="host_ownership", ownership=self.owner(body["native"])
                ),
            }
        if operation == "claim":
            self.claim_calls += 1
            replay = self.host_claim is not None
            if not replay:
                self.host_claim = dict(
                    claim_id="claim:recorded",
                    candidate=body["candidate"],
                    decision=body["decision"],
                    host_epoch=body["host_epoch"],
                    host_generation=body["host_generation"],
                    owner_revision=2,
                    expires_at=body["expires_at"],
                )
                self.claim_recorded = utc(self.now)
            return dict(
                schema_version=1,
                kind="participation_claim_result",
                request_id=body["request_id"],
                request_digest=digest(body),
                candidate=body["candidate"],
                decision=body["decision"],
                receipt_status="replayed" if replay else "recorded",
                result="granted",
                reason="claim_granted",
                claim=self.host_claim,
                recorded_at=self.claim_recorded,
                ownership=self.owner(body["native"]),
                current_claim_state="active" if self.host_active else "inactive",
                checked_at=utc(self.now),
            )
        if operation == "bind":
            self.host_bindings[body["delivery"]["reply_id"]] = copy.deepcopy(body)
            return {"binding": body}
        raise AssertionError(operation)

    async def core(self, config, ingest, contracts):
        self.core_calls.append(copy.deepcopy(ingest))
        authority = self.platform.sources.read(bearer("COMPANION"), input_request(ingest))
        response = core_fixture(ingest, authority, self.now)
        for outcome in response["outcomes"]:
            if outcome["receipt"]:
                outcome["receipt"]["collection_key"] = dict(
                    channel=ingest["input"]["message_key"]["channel"],
                    author=ingest["input"]["author"],
                )
        self.accepted = response
        return response

    async def capture(self, event=None):
        event = event or self.event()
        source = self.manager.record(self.row, event)
        await self.part.observe(self.row, event, source)
        self.assertIsNone(self.part.last_error)
        listed = await self.part.route(
            bearer("COMPANION"), "candidates", dict(actor_id="actor:a", after="", limit=20)
        )
        selected = [
            item
            for item in listed["candidates"]
            if item["native"]["native_message_id"] == event["event_id"]
        ]
        self.assertEqual(len(selected), 1, listed)
        return selected[0]

    def decision(self, candidate):
        return dict(
            schema_version=1,
            kind="semantic_participation_decision",
            candidate=candidate_ref(candidate),
            decision_id="decision:one",
            revision=1,
            outcome="engage",
            reason="useful_contribution",
            contribution="有一条相关的观察想分享",
            decided_at=utc(self.now),
            expires_at=candidate["expires_at"],
        )

    async def engage(self):
        candidate = await self.capture()
        answer = await self.part.route(bearer("COMPANION"), "decision", self.decision(candidate))
        self.assertEqual(answer["state"], "accepted", answer)
        return candidate

    def send_request(self, registration, number=1):
        admission = self.accepted["outcomes"][0]["admission"]
        return dict(
            schema_version=2,
            request_id=f"send:{number}",
            expression_id="expression:participation",
            origin=dict(
                kind="response",
                actor_id="actor:a",
                turn_id="turn:actual",
                origin={"assertion_ref": registration["admission"]["actor_assertion_ref"]},
                sources=[],
            ),
            scope=admission["scope"],
            channel=self.core_calls[-1]["input"]["message_key"]["channel"],
            final=False,
            segments=[
                dict(
                    segment_id=f"segment:{number}",
                    reply_id=f"reply:{number}",
                    segment_sequence=number,
                    text="来自原队列的实际回复",
                    content_refs=[],
                )
            ],
        )

    async def claim(self, registration):
        conn = registration["admission"]["connection_id"]
        token = "Bearer " + self.manager._reply_token(conn)
        body = dict(connection_id=conn, instance_id="synthetic:host", limit=20)
        checked = await self.part.delivery.prepare(token, body)
        return self.platform.bots.claim(token, body, participation_checked=checked)

    async def test_observed_first_author_without_consumer_is_silent(self):
        candidate = await self.capture()
        self.assertEqual(candidate["author_anchor"]["stage"], "observed")
        self.assertIsNone(candidate["author_anchor"]["person_id"])
        self.assertEqual(self.core_calls, [])
        with closing(self.platform.bots._db()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM replies").fetchone()[0], 0)
        with self.assertRaises(Fault):
            await self.part.route(
                bearer("MEMORY"), "candidates", dict(actor_id="actor:a", after="", limit=20)
            )
        bad = self.decision(candidate)
        bad["candidate"]["actor_id"] = "actor:other"
        with self.assertRaises(Fault):
            await self.part.route(bearer("COMPANION"), "decision", bad)
        self.assertEqual(self.claim_calls, 0)

    async def test_first_author_engage_resolves_and_atomic_delivery_uses_original_ledger(self):
        candidate = await self.engage()
        registration = self.part.repository.registration(candidate["candidate_id"])["document"]
        self.assertEqual(registration["author_anchor"]["stage"], "registered")
        self.assertEqual(self.core_calls[0]["target_actor_ids"], ["actor:a"])
        self.assertEqual(self.core_calls[0]["input"]["author"]["immutable_account_id"], "70000")
        request = self.send_request(registration)
        await self.part.delivery.prepare_creation(
            bearer("COMPANION"), registration["admission"]["actor_assertion_ref"]
        )
        self.platform.bots.delivery.send(bearer("COMPANION"), request)
        with closing(self.platform.bots._db()) as db:
            row = db.execute("SELECT document FROM participation_bindings").fetchone()
            binding = json.loads(row[0])
        self.assertEqual(binding["author_anchor"]["stage"], "resolved")
        self.assertEqual(binding["source_confirmation"], "confirmed")
        self.assertEqual(len((await self.claim(registration))["deliveries"]), 1)
        repeated = await self.part.route(bearer("COMPANION"), "decision", self.decision(candidate))
        self.assertEqual(repeated["state"], "accepted")
        self.assertEqual(len(self.core_calls), 1)

    async def test_failed_atomic_binding_rolls_back_reply_and_expression(self):
        candidate = await self.engage()
        registration = self.part.repository.registration(candidate["candidate_id"])["document"]
        original = self.part.delivery.bind

        def interrupted(*args):
            original(*args)
            raise Fault("dependency_unavailable", 503)

        with patch.object(self.part.delivery, "bind", side_effect=interrupted):
            with self.assertRaises(Fault):
                self.platform.bots.delivery.send(
                    bearer("COMPANION"), self.send_request(registration)
                )
        with closing(self.platform.bots._db()) as db:
            for table in (
                "replies",
                "expression_segments",
                "expressions",
                "participation_bindings",
            ):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    async def test_old_identity_early_reply_waits_for_this_fanout_confirmation(self):
        candidate = await self.capture()
        original_core = self.core

        async def early(config, ingest, contracts):
            response = await original_core(config, ingest, contracts)
            registration = self.part.repository.registration(candidate["candidate_id"])["document"]
            actual = response["outcomes"][0]["admission"]
            # Existing owner mappings from an earlier dialogue; this fanout remains pending.
            with self.platform.store.connect(write=True) as db:
                db.execute(
                    "INSERT OR REPLACE INTO identities VALUES(?,?,?)",
                    (
                        canonical(ingest["input"]["author"]),
                        actual["scope"]["person_id"],
                        actual["binding_version"],
                    ),
                )
                db.execute(
                    "INSERT OR REPLACE INTO channels VALUES(?,?)",
                    (
                        canonical(ingest["input"]["message_key"]["channel"]),
                        actual["scope"]["conversation_id"],
                    ),
                )
            self.platform.bots.delivery.send(bearer("COMPANION"), self.send_request(registration))
            with closing(self.platform.bots._db()) as db:
                before = db.execute("SELECT document FROM participation_bindings").fetchone()[0]
            self.pending_binding = json.loads(before)
            self.assertEqual(self.pending_binding["source_confirmation"], "pending")
            self.assertEqual((await self.claim(registration))["deliveries"], [])
            self.assertEqual(self.host_bindings["reply:1"], self.pending_binding)
            return response

        with patch("services.platform.transport.core_post", side_effect=early):
            result = await self.part.route(
                bearer("COMPANION"), "decision", self.decision(candidate)
            )
        self.assertEqual(result["state"], "accepted", result)
        registration = self.part.repository.registration(candidate["candidate_id"])["document"]
        self.assertEqual(len((await self.claim(registration))["deliveries"]), 1)
        self.assertEqual(
            self.host_bindings["reply:1"],
            {**self.pending_binding, "source_confirmation": "confirmed"},
        )

    async def test_unknown_core_reuses_preparation_and_never_changes_event_identity(self):
        candidate = await self.capture()
        original = self.core

        async def lost(config, ingest, contracts):
            await original(config, ingest, contracts)
            raise Fault("dependency_unavailable", 503)

        with patch("services.platform.transport.core_post", side_effect=lost):
            first = await self.part.route(bearer("COMPANION"), "decision", self.decision(candidate))
        self.assertEqual(first["state"], "unknown")
        registered = self.part.repository.registration(candidate["candidate_id"])["document"]
        second = await self.part.route(bearer("COMPANION"), "decision", self.decision(candidate))
        self.assertEqual(second["state"], "accepted", second)
        self.assertEqual(self.core_calls[0], self.core_calls[1])
        self.assertEqual(
            self.part.repository.registration(candidate["candidate_id"])["document"], registered
        )
        with self.platform.store.connect() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM participation_sources").fetchone()[0], 1
            )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fanout_exchanges").fetchone()[0], 1)

    async def test_other_handler_or_revoked_claim_never_dispatches(self):
        candidate = await self.capture()
        self.host_state = "handled_elsewhere"
        result = await self.part.route(bearer("COMPANION"), "decision", self.decision(candidate))
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(self.core_calls, [])
        self.host_state = "free"
        self.host_active = False
        result = await self.part.route(bearer("COMPANION"), "decision", self.decision(candidate))
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(self.core_calls, [])

    async def test_missing_optional_package_does_not_break_archive_explicit_reply_or_ack(self):
        self.part.protocol = CandidateProtocol(Path(self.temp.name) / "absent", PACKAGE, MANIFEST)

        async def archive(body):
            return dict(source_ref=body["source_ref"], state="accepted", archive_state="archived")

        self.manager._companion = archive
        await self.manager._process_batch(
            self.row,
            [
                dict(id="ordinary", event=self.event(), reply_claimed=False),
                dict(
                    id="explicit", event=self.event("native:at", mentioned=True), reply_claimed=True
                ),
            ],
        )
        self.assertEqual(self.acked, ["ordinary", "explicit"])
        self.assertEqual(len(self.core_calls), 1)
        with closing(self.platform.bots._db()) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM participation_candidates").fetchone()[0], 0
            )

    async def test_configuration_change_invalidates_candidate_without_archiving_again(self):
        candidate = await self.capture()
        self.row["revision"] += 1
        self.manager.catalog.put(self.row)
        with self.assertRaises(Fault):
            await self.part.route(bearer("COMPANION"), "decision", self.decision(candidate))
        self.assertEqual(self.core_calls, [])

    async def test_legacy_send_has_the_same_atomic_registration_binding(self):
        candidate = await self.engage()
        registration = self.part.repository.registration(candidate["candidate_id"])["document"]
        scope = self.accepted["outcomes"][0]["admission"]["scope"]
        request = dict(
            command=dict(
                schema_version=1,
                request_id="legacy:request",
                idempotency_key="legacy:command",
                origin={"assertion_ref": registration["admission"]["actor_assertion_ref"]},
                deadline_at=utc(self.now + 20),
            ),
            conversation_id=scope["conversation_id"],
            turn_id="turn:legacy",
            turn_sequence=1,
            reply_id="reply:legacy",
            actor_id=scope["actor_id"],
            destination=self.core_calls[0]["input"]["message_key"]["channel"],
            segment_sequence=1,
            segment_count=1,
            text="旧send也沿同一领取",
        )
        original = self.part.delivery.bind

        def interrupted(*args):
            original(*args)
            raise Fault("dependency_unavailable", 503)

        with patch.object(self.part.delivery, "bind", side_effect=interrupted):
            with self.assertRaises(Fault):
                self.platform.bots.send(bearer("COMPANION"), request)
        with closing(self.platform.bots._db()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM replies").fetchone()[0], 0)
        first = self.platform.bots.send(bearer("COMPANION"), request)
        self.assertEqual(self.platform.bots.send(bearer("COMPANION"), request), first)
        deliveries = (await self.claim(registration))["deliveries"]
        self.assertEqual([item["reply_id"] for item in deliveries], ["reply:legacy"])

    async def test_invalid_claimed_participation_does_not_occupy_or_limit_normal_replies(self):
        candidate = await self.engage()
        registration = self.part.repository.registration(candidate["candidate_id"])["document"]
        self.platform.bots.delivery.send(bearer("COMPANION"), self.send_request(registration))
        self.assertEqual(len((await self.claim(registration))["deliveries"]), 1)
        self.host_active = False
        entry_id = registration["author_anchor"]["entry_id"]
        ordinary = self.platform.origins.issue(bearer("CONNECTOR"), entry_id)
        request = self.send_request(registration)
        request.update(request_id="normal:request", expression_id="normal:expression")
        request["origin"].update(
            origin={"assertion_ref": ordinary["assertion_ref"]}, turn_id="turn:normal"
        )
        request["segments"][0].update(reply_id="reply:normal", segment_id="segment:normal")
        self.platform.bots.delivery.send(bearer("COMPANION"), request)
        conn = registration["admission"]["connection_id"]
        token = "Bearer " + self.manager._reply_token(conn)
        body = dict(connection_id=conn, instance_id="normal:host", limit=1)
        checked = await self.part.delivery.prepare(token, body)
        claimed = self.platform.bots.claim(token, body, participation_checked=checked)
        self.assertEqual([item["reply_id"] for item in claimed["deliveries"]], ["reply:normal"])
        with closing(self.platform.bots._db()) as db:
            self.assertEqual(
                db.execute("SELECT state FROM replies WHERE reply_id='reply:1'").fetchone()[0],
                "claimed",
            )

    async def test_lost_claim_reply_and_interrupted_registration_recover_original_facts(self):
        candidate = await self.capture()
        plugin = self.plugin
        lost = True

        async def lose_claim(row, path, body):
            nonlocal lost
            result = await plugin(row, path, body)
            if path == "/participation/claim" and lost:
                lost = False
                raise Fault("adapter_unreachable", 503)
            return result

        self.platform.bot_adapters._plugin = lose_claim
        with self.assertRaises(Fault):
            await self.part.route(bearer("COMPANION"), "decision", self.decision(candidate))
        original_claim = copy.deepcopy(self.host_claim)
        with patch.object(
            self.part.repository, "register", side_effect=Fault("dependency_unavailable", 503)
        ):
            result = await self.part.route(
                bearer("COMPANION"), "decision", self.decision(candidate)
            )
        self.assertNotEqual(result["state"], "accepted")
        self.assertEqual(self.core_calls, [])
        with self.platform.store.connect() as db:
            saved = db.execute("SELECT registration FROM participation_sources").fetchone()[0]
        result = await self.part.route(bearer("COMPANION"), "decision", self.decision(candidate))
        self.assertEqual(result["state"], "accepted", result)
        self.assertEqual(self.host_claim, original_claim)
        self.assertEqual(
            self.part.repository.registration(candidate["candidate_id"])["document"],
            json.loads(saved),
        )
        self.assertEqual(len(self.core_calls), 1)

    async def test_http_candidates_decision_and_original_claim_authentication(self):
        candidate = await self.capture()
        runner, url = await start_http(create_app(self.platform))
        self.addAsyncCleanup(runner.cleanup)
        async with aiohttp.ClientSession() as client:

            async def post(path, body, token):
                async with client.post(
                    url + path, json=body, headers={"Authorization": token}
                ) as response:
                    return response.status, await response.json()

            prefix = "/internal/v1/observation-participation/"
            status, _ = await post(
                prefix + "candidates",
                dict(actor_id="actor:a", after="", limit=20),
                bearer("MEMORY"),
            )
            self.assertEqual(status, 403)
            status, result = await post(
                prefix + "decision", self.decision(candidate), bearer("COMPANION")
            )
            self.assertEqual(status, 200, result)
            registration = self.part.repository.registration(candidate["candidate_id"])["document"]
            status, queued = await post(
                "/internal/v2/bot-delivery/send",
                self.send_request(registration),
                bearer("COMPANION"),
            )
            self.assertEqual(status, 200, queued)
            conn = registration["admission"]["connection_id"]
            status, deliveries = await post(
                "/internal/v1/bot/replies/claim",
                dict(connection_id=conn, instance_id="http:host", limit=20),
                "Bearer " + self.manager._reply_token(conn),
            )
            self.assertEqual(status, 200, deliveries)
            self.assertEqual(len(deliveries["deliveries"]), 1)

    async def test_invalid_optional_pin_preserves_both_original_paths(self):
        # A present but unverified candidate package is independently isolated.
        self.part.protocol = CandidateProtocol(self.platform.contracts.root, PACKAGE, "0" * 64)

        async def archive(body):
            return dict(source_ref=body["source_ref"], state="accepted", archive_state="archived")

        self.manager._companion = archive
        await self.manager._process_batch(
            self.row,
            [
                dict(id="unverified", event=self.event("native:unverified"), reply_claimed=False),
                dict(
                    id="explicit", event=self.event("native:at", mentioned=True), reply_claimed=True
                ),
            ],
        )
        self.assertEqual(self.acked, ["unverified", "explicit"])
        self.assertEqual(len(self.core_calls), 1)

    async def test_observe_only_and_present_package_without_consumer_keep_ack_independent(self):
        async def archive(body):
            return dict(source_ref=body["source_ref"], state="accepted", archive_state="archived")

        self.manager._companion = archive
        await self.manager._process_batch(
            self.row, [dict(id="ordinary", event=self.event(), reply_claimed=False)]
        )
        self.assertEqual(self.acked, ["ordinary"])
        self.assertEqual(self.core_calls, [])
        self.row["group_policy"]["mode"] = "observe_only"
        self.manager.catalog.put(self.row)
        await self.manager._process_batch(
            self.row,
            [dict(id="observe-only", event=self.event("native:observe"), reply_claimed=False)],
        )
        self.assertEqual(self.acked, ["ordinary", "observe-only"])
        self.assertEqual(self.core_calls, [])
        with closing(self.platform.bots._db()) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM participation_candidates").fetchone()[0], 1
            )

    async def test_bounded_binding_scan_rotates_past_unavailable_first_page(self):
        candidate = await self.engage()
        registration = self.part.repository.registration(candidate["candidate_id"])["document"]
        for number in range(1, 22):
            request = self.send_request(registration, number)
            if number == 21:
                request["segments"][0]["reply_id"] = "zz:last"
            self.platform.bots.delivery.send(bearer("COMPANION"), request)
        plugin = self.plugin

        async def unavailable_first_page(row, path, body):
            if path == "/participation/bind" and body["delivery"]["reply_id"] != "zz:last":
                raise Fault("adapter_unreachable", 503)
            return await plugin(row, path, body)

        self.platform.bot_adapters._plugin = unavailable_first_page
        self.assertEqual((await self.claim(registration))["deliveries"], [])
        self.assertEqual(
            [item["reply_id"] for item in (await self.claim(registration))["deliveries"]],
            ["zz:last"],
        )

    async def test_unchecked_active_claimed_batch_after_twenty_invalid_rows_still_blocks(self):
        first = await self.engage()
        registration = self.part.repository.registration(first["candidate_id"])["document"]
        self.now += 40
        await self.part.delivery.prepare_creation(
            bearer("COMPANION"), registration["admission"]["actor_assertion_ref"]
        )
        for number in range(1, 21):
            self.platform.bots.delivery.send(
                bearer("COMPANION"), self.send_request(registration, number)
            )
        self.assertEqual(len((await self.claim(registration))["deliveries"]), 20)
        # Candidate/origin expires before the existing 60-second delivery claim lease.
        self.now += 21
        self.host_claim = None
        second = await self.capture(self.event("native:second"))
        core = self.core

        async def another_source(config, ingest, contracts):
            response = await core(config, ingest, contracts)
            response["physical_receipt_id"] = "physical:second"
            for outcome in response["outcomes"]:
                outcome["receipt"]["receipt_id"] = "receipt:second"
                outcome["admission"]["source"]["receipt_id"] = "receipt:second"
                outcome["admission"]["physical_receipt_id"] = "physical:second"
            return response

        with patch("services.platform.transport.core_post", side_effect=another_source):
            answer = await self.part.route(bearer("COMPANION"), "decision", self.decision(second))
        self.assertEqual(answer["state"], "accepted", answer)
        second_registration = self.part.repository.registration(second["candidate_id"])["document"]
        request = self.send_request(second_registration)
        request.update(request_id="second:request", expression_id="second:expression")
        request["origin"]["turn_id"] = "turn:second"
        request["segments"][0].update(reply_id="zz:active", segment_id="second:segment")
        self.platform.bots.delivery.send(bearer("COMPANION"), request)
        active = (await self.claim(second_registration))["deliveries"]
        self.assertEqual([item["reply_id"] for item in active], ["zz:active"])
        ordinary = self.platform.origins.issue(
            bearer("CONNECTOR"), second_registration["author_anchor"]["entry_id"]
        )
        normal = self.send_request(second_registration)
        normal.update(request_id="normal:request", expression_id="normal:expression")
        normal["origin"].update(
            origin={"assertion_ref": ordinary["assertion_ref"]}, turn_id="turn:normal"
        )
        normal["segments"][0].update(reply_id="zz:normal", segment_id="normal:segment")
        self.platform.bots.delivery.send(bearer("COMPANION"), normal)
        with closing(self.platform.bots._db()) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM replies WHERE state='claimed'").fetchone()[0], 21
            )
        self.assertEqual((await self.claim(second_registration))["deliveries"], [])
        connection = second_registration["admission"]["connection_id"]
        self.platform.bots.ack(
            "Bearer " + self.manager._reply_token(connection),
            dict(
                connection_id=connection,
                reply_id="zz:active",
                attempt_id=active[0]["attempt_id"],
                state="sent",
                channel_message_ids=["sdk:second"],
            ),
        )
        self.assertEqual(
            [item["reply_id"] for item in (await self.claim(second_registration))["deliveries"]],
            ["zz:normal"],
        )

    async def test_confirmed_source_outlives_dispatch_deadline_but_never_source_ttl(self):
        first = await self.capture()

        async def lost(config, ingest, contracts):
            await self.core(config, ingest, contracts)
            raise Fault("dependency_unavailable", 503)

        with patch("services.platform.transport.core_post", side_effect=lost):
            result = await self.part.route(bearer("COMPANION"), "decision", self.decision(first))
        self.assertEqual(result["state"], "unknown")
        pending = self.part.repository.registration(first["candidate_id"])["document"]

        def key(registration):
            return canonical(
                [
                    registration["admission"][field]
                    for field in ("connection_id", "event_id", "account_id")
                ]
            )

        self.now += 31
        with self.assertRaises(Fault) as unresolved:
            self.platform.sources.participation_prepared(
                bearer("CONNECTOR"), key(pending), pending["candidate"]
            )
        self.assertEqual(unresolved.exception.code, "timeout")
        self.host_claim = None
        second = await self.capture(self.event("native:later"))
        result = await self.part.route(bearer("COMPANION"), "decision", self.decision(second))
        self.assertEqual(result["state"], "accepted", result)
        registered = self.part.repository.registration(second["candidate_id"])["document"]
        self.now += 31
        prepared = self.platform.sources.participation_prepared(
            bearer("CONNECTOR"), key(registered), registered["candidate"]
        )
        self.assertIsNotNone(prepared["response"])
        with self.assertRaises(Fault) as re_dispatch:
            await self.platform.sources.dispatch_participation(
                bearer("CONNECTOR"), key(registered), registered
            )
        self.assertEqual(re_dispatch.exception.code, "timeout")
        ref = registered["admission"]["actor_assertion_ref"]
        await self.part.delivery.prepare_creation(bearer("COMPANION"), ref)
        request = self.send_request(registered)
        self.platform.bots.delivery.send(bearer("COMPANION"), request)
        self.platform.bots.delivery.context(
            bearer("COMPANION"),
            dict(
                schema_version=2,
                request_id="context:renew",
                origin=request["origin"],
                scope=request["scope"],
                channel=request["channel"],
            ),
        )
        self.now += 30
        with self.platform.store.connect() as db:
            self.assertGreater(
                db.execute("SELECT expires_at FROM origins WHERE ref=?", (ref,)).fetchone()[0],
                self.now,
            )
        with self.assertRaises(Fault) as expired:
            self.platform.sources.participation_prepared(
                bearer("CONNECTOR"), key(registered), registered["candidate"]
            )
        self.assertEqual(expired.exception.status, 403)

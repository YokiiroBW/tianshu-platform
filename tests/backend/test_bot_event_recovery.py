import asyncio
import copy
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import AsyncMock, patch

import test_bots as fixtures
from fixtures import bearer
from source_fixtures import core_fixture, input_request

from services.platform.contracts import Fault
from services.platform.transport import CoreFault


class BotRecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.BotTests.asyncSetUp
    _close = fixtures.BotTests._close
    event = fixtures.BotTests.event

    def core_response(self, ingest, previous=None):
        """Use the existing Core stand-in with real Platform authority/mapping.

        This is not Core execution: its idempotent ledger is controlled by each
        test. Platform still signs, freezes, reauthorizes and confirms the input.
        """
        authority = self.platform.sources.read(bearer("COMPANION"), input_request(ingest))
        result = core_fixture(ingest, authority, self.now, previous=previous)
        for outcome in result["outcomes"]:
            if outcome["receipt"]:
                outcome["receipt"]["collection_key"] = {
                    "channel": copy.deepcopy(ingest["input"]["message_key"]["channel"]),
                    "author": copy.deepcopy(ingest["input"]["author"]),
                }
        return result

    def assert_same_replay(self, calls):
        self.assertEqual(2, len(calls))
        self.assertEqual(calls[0]["input"], calls[1]["input"])
        self.assertEqual(calls[0]["target_actor_ids"], calls[1]["target_actor_ids"])
        self.assertEqual(
            calls[0]["command"]["idempotency_key"], calls[1]["command"]["idempotency_key"]
        )
        self.assertNotEqual(calls[0]["command"]["request_id"], calls[1]["command"]["request_id"])
        self.assertNotEqual(calls[0]["command"]["origin"], calls[1]["command"]["origin"])

    async def test_explicit_nonexecution_can_retry_same_message_and_reauthenticates(self):
        self.platform.sources.dispatch = AsyncMock(
            side_effect=[
                CoreFault(
                    dict(code="queue_full", execution_state="not_started", retryable=True), 429
                ),
                {"outcomes": [{"actor_id": "actor:a", "state": "accepted"}]},
            ]
        )
        body = self.event()
        first = await self.platform.bots.event(self.token, body)
        self.assertEqual("not_started", first["state"])
        self.assertIs(first["retryable"], True)
        status = self.platform.bots.event_status(
            self.token, {k: body[k] for k in ("connection_id", "event_id", "account_id")}
        )
        self.assertIs(status["retryable"], True)
        self.platform.bots.change(self.connection_id, "disable")
        with self.assertRaises(Fault):
            await self.platform.bots.event(self.token, body)
        self.platform.bots.change(self.connection_id, "enable")
        second = await self.platform.bots.event(self.token, body)
        self.assertEqual("accepted", second["state"])
        self.assertEqual(first["message_id"], second["message_id"])
        calls = self.platform.sources.dispatch.call_args_list
        self.assertEqual(calls[0].args[1]["input"], calls[1].args[1]["input"])
        self.assertEqual(
            calls[0].args[1]["command"]["idempotency_key"],
            calls[1].args[1]["command"]["idempotency_key"],
        )
        await self.platform.bots.event(self.token, body)
        self.assertEqual(2, self.platform.sources.dispatch.await_count)

    async def test_replayed_unknown_does_not_invent_success_or_nonexecution(self):
        for state, code, retry in [
            ("unknown", "queue_full", True),
            ("not_started", "forbidden", False),
        ]:
            self.platform.sources.dispatch = AsyncMock(
                side_effect=CoreFault(dict(code=code, execution_state=state, retryable=retry), 503)
            )
            body = {**self.event(), "event_id": state + code}
            result = await self.platform.bots.event(self.token, body)
            self.assertNotIn("retryable", result)
            second = await self.platform.bots.event(self.token, body)
            self.assertEqual("unknown", second["state"])
            self.assertNotIn("retryable", second)
            self.assertEqual(2, self.platform.sources.dispatch.await_count)

    async def test_timeout_before_admission_replays_same_input_for_first_acceptance(self):
        calls = []
        accepted = []

        async def core(_settings, ingest, _contracts):
            calls.append(copy.deepcopy(ingest))
            if len(calls) == 1:
                # The transport's projection of a timeout has no execution proof.
                # This stand-in has not admitted the first request.
                raise Fault("dependency_unavailable", 503)
            result = self.core_response(ingest)
            accepted.append(result)
            return result

        body = self.event()
        with patch("services.platform.transport.core_post", side_effect=core):
            first = await self.platform.bots.event(self.token, body)
            self.assertEqual(first["state"], "unknown")
            self.assertEqual(accepted, [])
            self.now += 1
            second = await self.platform.bots.event(self.token, body)
        self.assertEqual(second["state"], "accepted")
        self.assertEqual(first["message_id"], second["message_id"])
        self.assertEqual(1, len(accepted))
        self.assert_same_replay(calls)
        with self.platform.store.connect() as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM fanout_routes").fetchone()[0])
            self.assertEqual(
                1,
                db.execute(
                    "SELECT COUNT(*) FROM fanout_exchanges WHERE response IS NOT NULL"
                ).fetchone()[0],
            )

    async def test_lost_admission_ack_replays_without_another_admission(self):
        calls = []
        accepted = []

        async def core(_settings, ingest, _contracts):
            calls.append(copy.deepcopy(ingest))
            if not accepted:
                accepted.append(self.core_response(ingest))
                raise Fault("dependency_unavailable", 503)
            return self.core_response(ingest, previous=accepted[0])

        body = self.event()
        with patch("services.platform.transport.core_post", side_effect=core):
            first = await self.platform.bots.event(self.token, body)
            self.assertEqual(first["state"], "unknown")
            self.now += 1
            second = await self.platform.bots.event(self.token, body)
            repeated = await self.platform.bots.event(self.token, body)
        self.assertEqual(second["state"], "accepted")
        self.assertEqual(second["outcomes"], [{"actor_id": "actor:a", "state": "duplicate"}])
        self.assertEqual(repeated, second)
        self.assertEqual(first["message_id"], second["message_id"])
        self.assertEqual(1, len(accepted))
        self.assert_same_replay(calls)

    async def test_late_unknown_cannot_overwrite_concurrent_accepted_result(self):
        calls = []
        accepted = []
        entered = asyncio.Event()
        release = asyncio.Event()

        async def core(_settings, ingest, _contracts):
            calls.append(copy.deepcopy(ingest))
            if not accepted:
                accepted.append(self.core_response(ingest))
                entered.set()
                await release.wait()
                raise Fault("dependency_unavailable", 503)
            return self.core_response(ingest, previous=accepted[0])

        body = self.event()
        with patch("services.platform.transport.core_post", side_effect=core):
            first = asyncio.create_task(self.platform.bots.event(self.token, body))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                second = await self.platform.bots.event(self.token, body)
                self.assertEqual(second["state"], "accepted")
                with closing(self.platform.bots._db()) as db:
                    confirmed = tuple(db.execute("SELECT state,result FROM events").fetchone())
                with self.platform.store.connect() as db:
                    mapping = db.execute(
                        "SELECT response FROM fanout_exchanges WHERE response IS NOT NULL"
                    ).fetchone()[0]
            finally:
                release.set()
                late = await first
            self.assertEqual(late["state"], "unknown")
            status = self.platform.bots.event_status(
                self.token, {k: body[k] for k in ("connection_id", "event_id", "account_id")}
            )
            self.assertTrue(status["found"])
            self.assertEqual(status["state"], second["state"])
            self.assertEqual(status["message_id"], second["message_id"])
            self.assertEqual(status["outcomes"], second["outcomes"])
            self.assertEqual(await self.platform.bots.event(self.token, body), second)
        self.assertEqual(1, len(accepted))
        self.assert_same_replay(calls)
        with closing(self.platform.bots._db()) as db:
            self.assertEqual(
                confirmed, tuple(db.execute("SELECT state,result FROM events").fetchone())
            )
            self.assertIsNone(
                db.execute(
                    "SELECT last_error FROM connections WHERE id=?", (self.connection_id,)
                ).fetchone()[0]
            )
        with self.platform.store.connect() as db:
            self.assertEqual(
                mapping,
                db.execute(
                    "SELECT response FROM fanout_exchanges WHERE response IS NOT NULL"
                ).fetchone()[0],
            )

    async def test_unknown_replay_keeps_body_author_and_prepared_route_guards(self):
        calls = []

        async def core(_settings, ingest, _contracts):
            calls.append(copy.deepcopy(ingest))
            raise Fault("dependency_unavailable", 503)

        body = self.event()
        with patch("services.platform.transport.core_post", side_effect=core):
            self.assertEqual("unknown", (await self.platform.bots.event(self.token, body))["state"])
            for field, value in (
                ("text", "changed"),
                ("conversation_id", "group:124"),
                ("account_id", "9999"),
            ):
                with self.subTest(field=field), self.assertRaises(Fault):
                    await self.platform.bots.event(self.token, {**body, field: value})
            changed = copy.deepcopy(calls[0])
            changed["target_actor_ids"] = ["actor:b"]
            with self.assertRaises(Fault) as error:
                await self.platform.sources.dispatch(self.platform.bots._platform_header(), changed)
            self.assertEqual("idempotency_conflict", error.exception.code)
            for field, value, code in (
                ("parts", [{"kind": "text", "text": "changed"}], "idempotency_conflict"),
                ("author", {"namespace": "qq", "immutable_account_id": "1002"}, "forbidden"),
            ):
                with self.subTest(input_field=field), self.assertRaises(Fault) as error:
                    self.platform.sources.register_input(
                        self.platform.bots._platform_header(),
                        "bot-input-1",
                        {**calls[0]["input"], field: value},
                    )
                self.assertEqual(code, error.exception.code)
        self.assertEqual(1, len(calls))

    async def test_unknown_replay_rechecks_current_connection_and_actor_permission(self):
        send = AsyncMock(side_effect=Fault("dependency_unavailable", 503))
        body = self.event()
        with patch("services.platform.transport.core_post", new=send):
            self.assertEqual("unknown", (await self.platform.bots.event(self.token, body))["state"])
            self.platform.bots.change(self.connection_id, "disable")
            with self.assertRaises(Fault):
                await self.platform.bots.event(self.token, body)
            self.platform.bots.change(self.connection_id, "enable")
            self.platform.origins.revoke(bearer("ADMIN"), "entry", "bot-actor-1")
            with self.assertRaises(Fault):
                await self.platform.bots.event(self.token, body)
        self.assertEqual(1, send.await_count)

    async def test_local_registration_failure_is_known_nonexecution_and_retries(self):
        self.platform.sources.dispatch = AsyncMock(
            return_value={"outcomes": [{"actor_id": "actor:a", "state": "accepted"}]}
        )
        original = self.platform.sources.register_input
        with patch.object(
            self.platform.sources,
            "register_input",
            side_effect=sqlite3.OperationalError("synthetic locked"),
        ):
            first = await self.platform.bots.event(self.token, self.event())
        self.assertEqual(first["state"], "not_started")
        self.assertTrue(first["retryable"])
        self.assertEqual(self.platform.sources.dispatch.await_count, 0)
        second = await self.platform.bots.event(self.token, self.event())
        self.assertEqual(second["state"], "accepted")
        self.assertEqual(first["message_id"], second["message_id"])
        self.assertIs(self.platform.sources.register_input.__func__, original.__func__)

    async def test_pre_dispatch_mapping_failure_does_not_claim_unknown_execution(self):
        from services.platform.bot_event_recovery import retryable

        with (
            patch.object(
                self.platform.sources,
                "_prepare_dispatch",
                side_effect=Fault("dependency_unavailable", 503),
            ),
            patch("services.platform.transport.core_post", new=AsyncMock()) as send,
        ):
            with self.assertRaises(Fault) as error:
                await self.platform.sources.dispatch("synthetic", {})
            self.assertTrue(retryable(error.exception))
            self.assertEqual(send.await_count, 0)

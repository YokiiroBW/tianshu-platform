import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

import test_bots as fixtures

from services.platform.contracts import Fault
from services.platform.transport import CoreFault


class BotRecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.BotTests.asyncSetUp
    _close = fixtures.BotTests._close
    event = fixtures.BotTests.event

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

    async def test_unknown_execution_and_permanent_denial_never_get_retry_permission(self):
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
            with patch("services.platform.bots.confirmed", new=AsyncMock(return_value=False)):
                await self.platform.bots.event(self.token, body)
            self.assertEqual(1, self.platform.sources.dispatch.await_count)

    async def test_positive_receipt_repairs_unknown_mapping_with_same_key_and_input(self):
        self.platform.sources.dispatch = AsyncMock(
            side_effect=[
                Fault("dependency_unavailable", 503),
                {"outcomes": [{"actor_id": "actor:a", "state": "duplicate"}]},
            ]
        )
        body = self.event()
        first = await self.platform.bots.event(self.token, body)
        self.assertEqual(first["state"], "unknown")
        with patch("services.platform.bots.confirmed", new=AsyncMock(return_value=True)) as receipt:
            second = await self.platform.bots.event(self.token, body)
            self.assertEqual(receipt.await_count, 1)
        self.assertEqual(second["state"], "accepted")
        calls = self.platform.sources.dispatch.call_args_list
        self.assertEqual(calls[0].args[1]["input"], calls[1].args[1]["input"])
        self.assertEqual(
            calls[0].args[1]["command"]["idempotency_key"],
            calls[1].args[1]["command"]["idempotency_key"],
        )

    async def test_busy_receipt_query_does_not_authorize_original_replay(self):
        self.platform.sources.dispatch = AsyncMock(side_effect=Fault("dependency_unavailable", 503))
        body = self.event()
        await self.platform.bots.event(self.token, body)
        error = CoreFault(
            dict(code="queue_full", execution_state="not_started", retryable=True), 429
        )
        with patch("services.platform.bots.confirmed", new=AsyncMock(side_effect=error)):
            result = await self.platform.bots.event(self.token, body)
        self.assertEqual(result["state"], "unknown")
        self.assertNotIn("retryable", result)
        self.assertEqual(self.platform.sources.dispatch.await_count, 1)

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

import unittest
from unittest.mock import AsyncMock

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
            await self.platform.bots.event(self.token, body)
            self.assertEqual(1, self.platform.sources.dispatch.await_count)

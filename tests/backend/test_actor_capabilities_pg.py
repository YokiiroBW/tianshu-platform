"""Optional native PostgreSQL execution ownership; never default to a production URL."""

import os
import unittest

from services.platform.actor_capabilities import ActorCapabilities
from services.platform.plugins import PluginManager
from test_actor_capabilities import ActorFixture


@unittest.skipUnless(os.environ.get("TS237_TEST_POSTGRES"), "isolated PostgreSQL URL not provided")
class ActorCapabilityPostgresTests(ActorFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self.prepare(postgres=os.environ["TS237_TEST_POSTGRES"])

    async def test_native_ledger_keeps_idempotency_actor_boundary_and_restart_unknown(self):
        self.assertEqual(self.host.repository.backend.backend, "postgresql")
        self.assertEqual(self.platform.role_runtime.database.backend, "postgresql")
        first = await self.invoke()
        self.host.repository.update_call(
            first["invocation_id"], execution="running", dispatch="unknown"
        )
        await self.host.close()
        self.host = PluginManager(
            self.settings, local=self.platform.local_work.run, runtime=self.platform.storage_runtime
        )
        self.platform.plugins = self.host
        self.service = self.platform.actor_capabilities = ActorCapabilities(self.platform)
        await self.host.start()
        replay = await self.operate(self.invoke_request())
        self.assertEqual(replay["invocation_id"], first["invocation_id"])
        self.assertEqual(replay["effect"], "unknown")
        other = await self.invoke(actor="actor:b")
        self.assertNotEqual(other["invocation_id"], first["invocation_id"])
        self.assertEqual(
            self.host.repository.state(self.instance["id"], "get", {"key": "count"})["value"], 2
        )

"""Targeted PostgreSQL coverage for the managed OneBot server catalog."""

import os
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from services.platform import backends
from services.platform.bot_adapter_catalog import BotAdapterCatalog
from services.platform.onebot_servers import OneBotServers


@unittest.skipUnless(
    os.environ.get("TS231_TEST_POSTGRES"), "requires a disposable PostgreSQL database"
)
class OneBotServerPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_catalog_survives_restart_and_preserves_adapter_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings = {
                "database_path": str(root / "platform.sqlite"),
                "storage_backends": {
                    "postgresql_url": os.environ["TS231_TEST_POSTGRES"],
                    "namespace": "onebot_test_" + uuid.uuid4().hex,
                },
            }
            runtime = backends.start(settings)
            try:
                catalog = BotAdapterCatalog(root / "catalog", runtime=runtime)
                catalog.put({"id": "legacy", "name": "unchanged"})
                manager = OneBotServers(SimpleNamespace(catalog=catalog))
                row = await manager.create(
                    {"name": "fixture", "token": "fixture-ws-token", "client_id": "create"}
                )
                reservation = await manager.reserve(row["id"], row["token"], "10001")
                self.assertTrue(await manager.claim(row["id"], row["token"], "10001", reservation))
                runtime.finalize()
                self.assertEqual(runtime.probe(), "ok")
            finally:
                runtime.close()
            runtime = backends.start(settings)
            try:
                reopened = BotAdapterCatalog(root / "catalog", runtime=runtime)
                runtime.finalize()
                self.assertEqual(reopened.all(), [{"id": "legacy", "name": "unchanged"}])
                self.assertEqual(reopened.server(row["id"])["account_id"], "10001")
                self.assertEqual(reopened.server(row["id"])["token"], "fixture-ws-token")
                self.assertEqual(runtime.probe(), "ok")
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()

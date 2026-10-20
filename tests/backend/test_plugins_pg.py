"""Optional real PostgreSQL verification, with a unique synthetic namespace."""

import asyncio
import os
from pathlib import Path
import tempfile
import unittest
import uuid

from services.platform import backends
from services.platform.plugins import PluginManager
from test_plugins import package


@unittest.skipUnless(os.environ.get("TS237_TEST_POSTGRES"), "isolated PostgreSQL URL not provided")
class PluginPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_plugin_data_migrates_and_remains_native_after_restart(self):
        with tempfile.TemporaryDirectory(prefix="ts237-pg-") as directory:
            settings = {"database_path": str(Path(directory) / "platform.db")}
            host = PluginManager(settings)
            await host.start()
            source = package(Path(directory) / "source")
            operation = await host.install({"type": "directory", "path": str(source)}, "install")
            while True:
                operation = host.repository.get("operations", operation["id"])
                if operation["execution"] not in {"accepted", "running"}:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(operation["execution"], "succeeded")
            instance = await host.create(operation["package_id"], "Migrated", "pg-instance")
            instance = await host.configure(
                instance["id"],
                {"label": "migrated"},
                {"api_key": "isolated-pg-secret"},
                instance["revision"],
            )
            instance = await host.state(instance["id"], True, instance["revision"])
            call = await host.executions.invoke(instance["id"], "increment", {}, "first", "admin")
            while host.repository.call(call["id"])["execution"] in {"accepted", "running"}:
                await asyncio.sleep(0.02)
            await host.close()
            settings["storage_backends"] = {
                "postgresql_url": os.environ["TS237_TEST_POSTGRES"],
                "namespace": "plugin_" + uuid.uuid4().hex,
            }
            runtime = backends.start(settings)
            self.assertEqual(runtime.backend, "postgresql")
            host = PluginManager(settings, runtime=runtime)
            runtime.finalize()
            await host.start()
            try:
                self.assertEqual(host.repository.backend.backend, "postgresql")
                self.assertEqual(
                    host.vault.resolve(instance["id"], "api_key"), "isolated-pg-secret"
                )
                self.assertEqual(
                    host.repository.state(instance["id"], "get", {"key": "count"})["value"], 1
                )
                self.assertEqual(host.task_source.count(), 1)
                rows, more = host.task_source.history(after=None, limit=10, states={"succeeded"})
                self.assertEqual(rows[0]["document"]["id"], call["id"])
                self.assertFalse(more)
            finally:
                await host.close()
                runtime.close()
            runtime = backends.start(settings)
            host = PluginManager(settings, runtime=runtime)
            runtime.finalize()
            await host.start()
            try:
                call = await host.executions.invoke(
                    instance["id"], "increment", {}, "second", "admin"
                )
                while host.repository.call(call["id"])["execution"] in {"accepted", "running"}:
                    await asyncio.sleep(0.02)
                self.assertEqual(host.repository.call(call["id"])["result"], 2)
            finally:
                await host.close()
                runtime.close()

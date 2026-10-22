"""Optional real PostgreSQL verification, with a unique synthetic namespace."""

import asyncio
from contextlib import closing
import os
from pathlib import Path
import tempfile
import unittest
import uuid

from services.platform import backends
from services.platform.plugins import PluginManager
from services.platform.plugins.bootstrap import initialize
from test_plugins import package
from tianshu_storage import StorageError


@unittest.skipUnless(os.environ.get("TS237_TEST_POSTGRES"), "isolated PostgreSQL URL not provided")
class PluginPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_binding_adds_plugins_and_legacy_registration_preserves_state(self):
        with tempfile.TemporaryDirectory(prefix="ts294-pg-") as directory:
            anchor = str(Path(directory) / "platform.db")
            settings = {
                "database_path": anchor,
                "storage_backends": {
                    "postgresql_url": os.environ["TS237_TEST_POSTGRES"],
                    "namespace": "plugin_" + uuid.uuid4().hex,
                },
            }
            names = ["authority", *["existing_" + str(index) for index in range(15)]]

            def originals(runtime):
                return {
                    name: backends.database(
                        runtime, name, anchor if name == "authority" else anchor + "." + name
                    )
                    for name in names
                }

            runtime = backends.start(settings)
            for name, database in originals(runtime).items():
                with closing(database.connect()) as db, db:
                    db.execute("CREATE TABLE retained(id TEXT PRIMARY KEY,value TEXT NOT NULL)")
                    db.execute("INSERT INTO retained VALUES('original',?)", (name,))
            runtime.finalize()
            runtime.close()

            # Maintenance adds the new logical source without starting the app or
            # registering/reimporting its already active original source.
            initialized = initialize(settings)
            self.assertEqual(initialized["databases"], sorted([*names, "plugins"]))
            runtime = backends.start(settings)
            databases = originals(runtime)
            host = PluginManager(settings, runtime=runtime)
            runtime.finalize()
            self.assertEqual(runtime.status()["databases"], sorted([*names, "plugins"]))
            self.assertEqual(host.repository.backend.backend, "postgresql")
            host.repository.state(
                "retained-instance",
                "compare_and_set",
                {"key": "retained", "value": 7, "expected_revision": None},
            )
            await host.close()
            runtime.close()

            # The pre-plugin application cannot finalize its old subset after activation.
            # Its compatibility image must register the existing source without a host.
            runtime = backends.start(settings)
            try:
                databases = originals(runtime)
                with self.assertRaisesRegex(StorageError, "postgresql_activation_incomplete"):
                    runtime.finalize()
                plugins = backends.database(runtime, "plugins", anchor + ".plugins.sqlite")
                runtime.finalize()
                for name, database in databases.items():
                    with closing(database.connect(read_only=True)) as db:
                        self.assertEqual(
                            db.execute("SELECT value FROM retained").fetchone()[0], name
                        )
                with closing(plugins.connect(read_only=True)) as db:
                    self.assertEqual(
                        db.execute("SELECT value FROM plugin_state").fetchone()[0], "7"
                    )
            finally:
                runtime.close()

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

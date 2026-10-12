"""Shared connection migration, isolation and revision behavior with real SQLite."""

import json
import sqlite3
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from services.platform.contracts import Fault, canonical
from services.platform.provider_catalog import ProviderCatalog


class ProviderSourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.catalog = ProviderCatalog(Path(self.temp.name) / "providers", create=True)
        self.source = self.catalog.sources.save(
            client_id="source",
            name="中转站",
            base_url="https://example.invalid/v1",
            api_key="synthetic-shared-key",
        )

    def add(self, models=None, kind="text", source=None):
        source = source or self.source
        return self.catalog.sources.add_models(
            client_id=str(uuid.uuid4()),
            source_id=source["source_id"],
            expected_revision=source["revision"],
            models=models or ["chat-small", "chat-large"],
            kind=kind,
        )["provider_ids"]

    def test_shared_secret_persists_once_and_same_named_models_stay_separate(self):
        first, second = self.add()
        other = self.catalog.sources.save(
            client_id="other",
            name="另一中转站",
            base_url="https://second.invalid/v1",
            api_key="synthetic-other-key",
        )
        third = self.add(["chat-small"], source=other)[0]
        reopened = ProviderCatalog(self.catalog.directory)
        contexts = [reopened.execution_context(identity, 1) for identity in (first, second, third)]
        self.assertEqual(["chat-small", "chat-large", "chat-small"], [x.model_id for x in contexts])
        self.assertEqual(
            ["synthetic-shared-key", "synthetic-shared-key", "synthetic-other-key"],
            [x.api_key for x in contexts],
        )
        with closing(sqlite3.connect(reopened.database)) as db:
            self.assertEqual(0, db.execute("SELECT count(secret) FROM providers").fetchone()[0])
            self.assertEqual(
                2, db.execute("SELECT count(secret) FROM provider_sources").fetchone()[0]
            )
        self.assertNotIn("synthetic-shared-key", json.dumps(reopened.view()))
        self.assertNotIn(b"synthetic-shared-key", reopened.database.read_bytes())
        ProviderCatalog.verify_existing(reopened.directory)

    def test_batch_duplicate_and_concurrent_add_are_idempotent(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.add(["same", "same"]), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(1, len(self.catalog.view()["providers"]))
        with self.assertRaises(Fault):
            self.catalog.sources.add_models(
                client_id="invalid",
                source_id=self.source["source_id"],
                expected_revision=None,
                models=["bad"],
            )
        with self.assertRaises(Fault):
            self.add(["valid", "\n"])
        self.assertEqual(1, len(self.catalog.view()["providers"]))

    def test_model_edits_and_tests_do_not_invalidate_siblings(self):
        first, second = self.add()
        for identity in (first, second):
            self.catalog.record_test(
                client_id="test-" + identity,
                provider_id=identity,
                expected_revision=1,
                outcome="succeeded",
            )
        self.catalog.set_default(
            client_id="default", provider_id=first, expected_revision=1, expected_default_revision=0
        )
        self.catalog.sources.save_model(
            client_id="edit",
            provider_id=second,
            expected_revision=1,
            name="Large",
            model_id="chat-new",
            kind="text",
            enabled=True,
        )
        view = self.catalog.view()
        self.assertTrue(view["default"]["configured"])
        self.assertEqual(1, self.catalog.execution_context(first, 1).revision)
        self.assertEqual("chat-new", self.catalog.execution_context(second, 2).model_id)
        self.catalog.delete(client_id="delete", provider_id=second, expected_revision=2)
        self.assertTrue(self.catalog.view()["default"]["configured"])

    def test_connection_rename_keeps_bindings_and_key_rotation_invalidates_all_models(self):
        identities = self.add()
        for identity in identities:
            self.catalog.record_test(
                client_id="test-" + identity,
                provider_id=identity,
                expected_revision=1,
                outcome="succeeded",
            )
        self.catalog.set_default(
            client_id="default",
            provider_id=identities[0],
            expected_revision=1,
            expected_default_revision=0,
        )
        self.catalog.functions.set_binding(
            client_id="writing",
            function_id="writing",
            provider_id=identities[1],
            expected_revision=1,
            expected_binding_revision=0,
        )
        renamed = self.catalog.sources.save(
            client_id="rename",
            source_id=self.source["source_id"],
            expected_revision=1,
            name="新名称",
            base_url=self.source["base_url"],
        )
        self.assertTrue(self.catalog.view()["default"]["configured"])
        self.catalog.sources.save(
            client_id="rotate",
            source_id=renamed["source_id"],
            expected_revision=2,
            name=renamed["name"],
            base_url=renamed["base_url"],
            api_key="synthetic-new-key",
        )
        view = self.catalog.view()
        self.assertFalse(view["default"]["configured"])
        self.assertFalse(
            next(item for item in view["functions"] if item["function_id"] == "writing")[
                "configured"
            ]
        )
        for identity in identities:
            with self.assertRaises(Fault):
                self.catalog.execution_context(identity, 1)
            self.assertEqual(
                "synthetic-new-key", self.catalog.execution_context(identity, 2).api_key
            )
        with self.assertRaises(Fault):
            self.catalog.sources.delete(
                client_id="stale", source_id=renamed["source_id"], expected_revision=2
            )
        self.assertEqual(2, len(self.catalog.view()["providers"]))

    def test_source_disable_clear_delete_and_typed_models(self):
        identities = self.add()
        embed = self.add(["embedding"], "embed")[0]
        self.catalog.record_test(
            client_id="embed-test", provider_id=embed, expected_revision=1, outcome="succeeded"
        )
        with self.assertRaises(Fault):
            self.catalog.set_default(
                client_id="wrong-kind",
                provider_id=embed,
                expected_revision=1,
                expected_default_revision=0,
            )
        with self.assertRaises(Fault):
            self.catalog.functions.set_binding(
                client_id="wrong-function",
                function_id="writing",
                provider_id=embed,
                expected_revision=1,
                expected_binding_revision=0,
            )
        self.catalog.sources.save(
            client_id="disable",
            source_id=self.source["source_id"],
            expected_revision=1,
            name=self.source["name"],
            base_url=self.source["base_url"],
            enabled=False,
        )
        for identity in identities + [embed]:
            with self.assertRaises(Fault):
                self.catalog.execution_context(identity, 2)
        self.catalog.sources.clear_key(
            client_id="clear", source_id=self.source["source_id"], expected_revision=2
        )
        self.assertTrue(all(not model["has_key"] for model in self.catalog.view()["providers"]))
        self.catalog.sources.delete(
            client_id="delete", source_id=self.source["source_id"], expected_revision=3
        )
        self.assertEqual([], self.catalog.view()["providers"])
        self.assertEqual([], self.catalog.view()["sources"])

    def test_upgrade_keeps_original_ids_tests_default_functions_and_backup(self):
        model = self.catalog.save(
            client_id="old-create",
            name="旧供应商",
            base_url="https://old.invalid/v1",
            model_id="old-model",
            api_key="synthetic-old-key",
        )
        identity = model["provider_id"]
        self.catalog.record_test(
            client_id="old-test", provider_id=identity, expected_revision=1, outcome="succeeded"
        )
        self.catalog.set_default(
            client_id="old-default",
            provider_id=identity,
            expected_revision=1,
            expected_default_revision=0,
        )
        self.catalog.functions.set_binding(
            client_id="old-function",
            function_id="writing",
            provider_id=identity,
            expected_revision=1,
            expected_binding_revision=0,
        )
        # Exact prior format: encrypted secret and connection data on the model row.
        model = next(x for x in self.catalog.view()["providers"] if x["provider_id"] == identity)
        original = {
            key: model[key]
            for key in (
                "provider_id",
                "name",
                "base_url",
                "protocol",
                "model_id",
                "enabled",
                "has_key",
                "revision",
                "test",
            )
        }
        with closing(sqlite3.connect(self.catalog.database)) as db, db:
            db.execute("DROP TABLE provider_sources")
            db.execute(
                "UPDATE providers SET document=?,secret=? WHERE id=?",
                (
                    canonical(original),
                    self.catalog._encrypt("synthetic-old-key", identity),
                    identity,
                ),
            )
        ProviderCatalog.verify_existing(self.catalog.directory)
        upgraded = ProviderCatalog(self.catalog.directory)
        self.assertEqual(identity, upgraded.view()["default"]["provider_id"])
        self.assertTrue(upgraded.view()["default"]["configured"])
        self.assertEqual("synthetic-old-key", upgraded.execution_context(identity, 1).api_key)
        writing = next(x for x in upgraded.view()["functions"] if x["function_id"] == "writing")
        self.assertTrue(writing["configured"])
        self.assertEqual(identity, writing["provider_id"])
        self.assertEqual(1, len(list(upgraded.directory.glob("providers.pre-sources-*.sqlite"))))
        ProviderCatalog(upgraded.directory)
        self.assertEqual(1, len(list(upgraded.directory.glob("providers.pre-sources-*.sqlite"))))

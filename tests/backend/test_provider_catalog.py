"""Synthetic secrets only; exercise on-disk state, independent instances and failure paths."""

import json
import shutil
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from services.platform.contracts import Fault
from services.platform.provider_catalog import ProviderCatalog, normalize_base_url


class ProviderCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "private"
        self.catalog = ProviderCatalog(self.directory, create=True, clock=lambda: 1234567)
        self.sequence = 0

    def create(self, **changes):
        self.sequence += 1
        args = dict(
            client_id=f"create-{self.sequence}",
            name="Synthetic provider",
            base_url="https://example.invalid/v1/",
            model_id="synthetic-model",
            api_key="synthetic-api-key-NEVER-REAL",
        )
        args.update(changes)
        return self.catalog.save(**args)

    def edit(self, provider, **changes):
        self.sequence += 1
        args = {key: provider[key] for key in ("name", "base_url", "model_id", "enabled")}
        args.update(
            provider_id=provider["provider_id"],
            expected_revision=provider["revision"],
            client_id=f"edit-{self.sequence}",
        )
        args.update(changes)
        return self.catalog.save(**args)

    def default(self, provider):
        identity = provider["provider_id"]
        self.catalog.record_test(
            client_id="test-" + identity,
            provider_id=identity,
            expected_revision=provider["revision"],
            outcome="succeeded",
        )
        return self.catalog.set_default(
            client_id="default-" + identity,
            provider_id=identity,
            expected_revision=provider["revision"],
            expected_default_revision=self.catalog.view()["default"]["revision"],
        )

    def assert_fault(self, code, callback):
        with self.assertRaises(Fault) as caught:
            callback()
        self.assertEqual(code, caught.exception.code)
        self.assertEqual(code, str(caught.exception))

    def test_restart_and_complete_restore_preserve_secret_default_and_receipts(self):
        provider = self.create(client_id="stable-id")
        self.default(provider)
        restored = self.root / "restored"
        shutil.copytree(self.directory, restored)  # all writers stopped, no open DB connection
        for directory in (self.directory, restored):
            catalog = ProviderCatalog(directory)
            self.assertTrue(catalog.view()["default"]["configured"])
            context = catalog.execution_context(provider["provider_id"], 1)
            self.assertEqual("synthetic-api-key-NEVER-REAL", context.api_key)
            replay = catalog.save(
                client_id="stable-id",
                name="Synthetic provider",
                base_url="https://example.invalid/v1/",
                model_id="synthetic-model",
                api_key="synthetic-api-key-NEVER-REAL",
            )
            self.assertEqual(provider, replay)
            self.assertEqual(1, len(catalog.view()["providers"]))

    def test_raw_files_public_projection_and_repr_do_not_contain_secret(self):
        provider = self.create()
        self.default(provider)
        context = self.catalog.execution_context(provider["provider_id"], 1)
        self.assertNotIn(context.api_key, repr(context))
        self.assertNotIn(context.api_key, json.dumps(self.catalog.view()))
        for file in self.directory.iterdir():
            self.assertNotIn(context.api_key.encode(), file.read_bytes())

    def test_blank_edit_keeps_key_and_invalidates_test_and_default(self):
        provider = self.create()
        self.default(provider)
        updated = self.edit(provider, api_key="   ")
        self.assertTrue(updated["has_key"])
        self.assertIsNone(updated["test"])
        self.assertEqual(
            "synthetic-api-key-NEVER-REAL",
            self.catalog.execution_context(provider["provider_id"], 2).api_key,
        )
        self.assertFalse(self.catalog.view()["default"]["configured"])

    def test_key_replacement_clear_and_context_revision(self):
        provider = self.create()
        updated = self.edit(provider, api_key="synthetic-replacement")
        self.assertEqual(
            "synthetic-replacement",
            self.catalog.execution_context(provider["provider_id"], 2).api_key,
        )
        self.assert_fault(
            "revision_conflict", lambda: self.catalog.execution_context(provider["provider_id"], 1)
        )
        cleared = self.catalog.clear_key(
            client_id="clear",
            provider_id=provider["provider_id"],
            expected_revision=updated["revision"],
        )
        self.assertFalse(cleared["has_key"])
        self.assertEqual(3, cleared["revision"])
        self.assert_fault(
            "provider_unavailable",
            lambda: self.catalog.execution_context(provider["provider_id"], 3),
        )

    def test_save_is_not_testing_and_untested_cannot_be_default(self):
        provider = self.create()
        self.assertIsNone(provider["test"])
        self.assert_fault(
            "provider_not_tested",
            lambda: self.catalog.set_default(
                client_id="default",
                provider_id=provider["provider_id"],
                expected_revision=1,
                expected_default_revision=0,
            ),
        )

    def test_provider_can_be_saved_before_selecting_model_for_enumeration(self):
        provider = self.create(model_id="")
        context = self.catalog.execution_context(provider["provider_id"], 1)
        self.assertEqual("", context.model_id)
        self.assert_fault(
            "provider_unavailable",
            lambda: self.catalog.record_test(
                client_id="no-model",
                provider_id=provider["provider_id"],
                expected_revision=1,
                outcome="succeeded",
            ),
        )

    def test_missing_revision_never_bypasses_cas(self):
        provider = self.create()
        for method in (self.catalog.clear_key, self.catalog.delete):
            self.assert_fault(
                "invalid_input",
                lambda: method(
                    client_id="no-cas", provider_id=provider["provider_id"], expected_revision=None
                ),
            )
        self.assertEqual(1, self.catalog.view()["providers"][0]["revision"])

    def test_edit_race_two_instances_exactly_one_winner(self):
        provider = self.create()
        other = ProviderCatalog(self.directory)
        barrier = threading.Barrier(2)

        def run(index):
            catalog = self.catalog if index == 0 else other
            barrier.wait()
            try:
                return catalog.save(
                    client_id=f"racer-{index}",
                    provider_id=provider["provider_id"],
                    expected_revision=1,
                    name=f"name-{index}",
                    model_id="synthetic-model",
                    base_url=provider["base_url"],
                )
            except Fault as exc:
                return exc.code

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(run, range(2)))
        self.assertEqual(1, results.count("revision_conflict"))
        self.assertEqual(2, self.catalog.view()["providers"][0]["revision"])

    def test_idempotent_race_and_different_payload_conflict(self):
        barrier = threading.Barrier(2)

        def run(_):
            catalog = ProviderCatalog(self.directory)
            barrier.wait()
            return catalog.save(
                client_id="same",
                name="name",
                model_id="model",
                base_url="https://example.invalid",
                api_key="synthetic-key",
            )

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(run, range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(1, len(self.catalog.view()["providers"]))
        self.assert_fault("idempotency_conflict", lambda: self.create(client_id="same"))

    def test_delete_disable_and_failed_test_never_leave_default_configured(self):
        for action in ("delete", "disable", "failed"):
            provider = self.create()
            self.default(provider)
            identity = provider["provider_id"]
            if action == "delete":
                result = self.catalog.delete(
                    client_id="delete", provider_id=identity, expected_revision=1
                )
                self.assertEqual(
                    result,
                    self.catalog.delete(
                        client_id="delete", provider_id=identity, expected_revision=1
                    ),
                )
            elif action == "disable":
                self.edit(provider, enabled=False)
                self.assert_fault(
                    "provider_unavailable", lambda: self.catalog.execution_context(identity, 2)
                )
            else:
                self.catalog.record_test(
                    client_id="failed",
                    provider_id=identity,
                    expected_revision=1,
                    outcome="authentication_failed",
                )
            self.assertFalse(self.catalog.view()["default"]["configured"])
            self.assertIsNone(self.catalog.view()["default"]["provider_id"])

    def test_late_test_cannot_promote_modified_configuration(self):
        provider = self.create()
        self.edit(provider, model_id="different-model")
        self.assert_fault(
            "revision_conflict",
            lambda: self.catalog.record_test(
                client_id="late",
                provider_id=provider["provider_id"],
                expected_revision=1,
                outcome="succeeded",
            ),
        )
        self.assertIsNone(self.catalog.view()["providers"][0]["test"])

    def test_default_cas_does_not_overwrite_another_selection(self):
        first, second = self.create(), self.create()
        self.default(first)
        self.catalog.record_test(
            client_id="second-test",
            provider_id=second["provider_id"],
            expected_revision=1,
            outcome="succeeded",
        )
        self.assert_fault(
            "default_revision_conflict",
            lambda: self.catalog.set_default(
                client_id="second-default",
                provider_id=second["provider_id"],
                expected_revision=1,
                expected_default_revision=0,
            ),
        )
        self.assertEqual(first["provider_id"], self.catalog.view()["default"]["provider_id"])

    def test_test_body_or_unknown_status_cannot_enter_public_projection(self):
        provider = self.create()
        self.assert_fault(
            "invalid_input",
            lambda: self.catalog.record_test(
                client_id="bad",
                provider_id=provider["provider_id"],
                expected_revision=1,
                outcome="synthetic-api-key-NEVER-REAL",
            ),
        )

    def test_missing_files_and_partial_restore_fail_without_reinitialization(self):
        provider = self.create()
        self.catalog.key_file.unlink()
        self.assert_fault("provider_store_unavailable", lambda: ProviderCatalog(self.directory))
        self.assert_fault("provider_store_unavailable", lambda: self.catalog.view())
        self.assert_fault(
            "provider_store_unavailable", lambda: ProviderCatalog(self.directory, create=True)
        )
        self.assertFalse(self.catalog.key_file.exists())
        with closing(sqlite3.connect(self.catalog.database)) as db, db:
            self.assertEqual(
                provider["provider_id"], db.execute("SELECT id FROM providers").fetchone()[0]
            )

    def test_missing_database_is_never_created_by_existing_instance(self):
        self.catalog.database.unlink()
        self.assert_fault("provider_store_unavailable", lambda: self.catalog.view())
        self.assertFalse(self.catalog.database.exists())

    def test_wrong_key_and_modified_ciphertext_fail_closed(self):
        provider = self.create()
        other = ProviderCatalog(self.root / "other", create=True)
        original_key = self.catalog.key_file.read_bytes()
        self.catalog.key_file.write_bytes(other.key_file.read_bytes())
        self.assert_fault("provider_store_unavailable", lambda: ProviderCatalog(self.directory))
        self.catalog.key_file.write_bytes(original_key)
        with closing(sqlite3.connect(self.catalog.database)) as db, db:
            db.execute("UPDATE providers SET secret=?", (b"broken-ciphertext",))
        self.assert_fault("provider_store_unavailable", lambda: ProviderCatalog(self.directory))
        self.assert_fault(
            "provider_store_unavailable",
            lambda: self.catalog.execution_context(provider["provider_id"], 1),
        )

    def test_atomic_rollback_when_receipt_cannot_be_written(self):
        provider = self.create()
        self.default(provider)
        with closing(sqlite3.connect(self.catalog.database)) as db, db:
            db.execute(
                "CREATE TRIGGER fail_receipt BEFORE INSERT ON receipts BEGIN "
                "SELECT RAISE(ABORT, 'synthetic failure'); END"
            )
        self.assert_fault(
            "provider_store_unavailable", lambda: self.edit(provider, api_key="replacement")
        )
        self.assertEqual(1, self.catalog.view()["providers"][0]["revision"])
        self.assertTrue(self.catalog.view()["default"]["configured"])
        self.assertEqual(
            "synthetic-api-key-NEVER-REAL",
            self.catalog.execution_context(provider["provider_id"], 1).api_key,
        )

    def test_paid_test_verdict_and_replay_receipt_commit_atomically(self):
        provider = self.create()
        identity = provider["provider_id"]
        self.assertEqual(
            "new",
            self.catalog.claim_test(client_id="paid-1", provider_id=identity, expected_revision=1)[
                "state"
            ],
        )
        with closing(sqlite3.connect(self.catalog.database)) as db, db:
            db.execute(
                "CREATE TRIGGER fail_settle BEFORE UPDATE ON test_attempts BEGIN "
                "SELECT RAISE(ABORT, 'synthetic crash window'); END"
            )
        self.assert_fault(
            "provider_store_unavailable",
            lambda: self.catalog.finish_test(
                client_id="paid-1", provider_id=identity, expected_revision=1, outcome="succeeded"
            ),
        )
        self.assertIsNone(self.catalog.view()["providers"][0]["test"])
        self.assertEqual(
            "pending",
            self.catalog.claim_test(client_id="paid-1", provider_id=identity, expected_revision=1)[
                "state"
            ],
        )
        with closing(sqlite3.connect(self.catalog.database)) as db, db:
            db.execute("DROP TRIGGER fail_settle")
        result = self.catalog.finish_test(
            client_id="paid-1", provider_id=identity, expected_revision=1, outcome="succeeded"
        )
        restarted = ProviderCatalog(self.directory, clock=lambda: 1234567)
        self.assertEqual(
            result,
            restarted.claim_test(client_id="paid-1", provider_id=identity, expected_revision=1)[
                "result"
            ],
        )
        self.assertEqual("succeeded", restarted.view()["providers"][0]["test"]["outcome"])

    def test_encryption_failure_has_no_partial_public_write(self):
        with patch.object(self.catalog, "_encrypt", side_effect=OSError("synthetic disk failure")):
            self.assert_fault("provider_store_unavailable", lambda: self.create())
        self.assertEqual([], self.catalog.view()["providers"])

    def test_url_syntax_normalization_is_not_a_network_authorization(self):
        self.assertEqual(
            "https://example.invalid/v1", normalize_base_url("https://EXAMPLE.invalid:443/v1/")
        )
        for bad in (
            "http://example.invalid",
            "https://user:key@example.invalid",
            "https://example.invalid/?api_key=secret",
            "https://example.invalid/#secret",
            "https://example.invalid/%2f",
            "https://example.invalid/../v1",
        ):
            self.assert_fault("invalid_input", lambda: normalize_base_url(bad))
        # Syntax acceptance of HTTPS loopback is NOT permission for any executor to dial it.
        self.assertEqual("https://127.0.0.1/v1", normalize_base_url("https://127.0.0.1/v1"))


if __name__ == "__main__":
    unittest.main()

"""Complete source-current reads; SQLite adapters do not stand in for PG proof."""

import copy
import os
import unittest
import uuid
from contextlib import contextmanager
from unittest.mock import patch

import test_sources as source_tests
from fixtures import bearer
from services.platform import sources_current
from services.platform.contracts import utc
from source_fixtures import current_request

PG = os.environ.get("TS231_TEST_POSTGRES")


class ExecuteSpy:
    def __init__(self, db, reads):
        self.db, self.reads = db, reads

    def execute(self, sql, parameters=()):
        self.reads.append(sql)
        return self.db.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self.db, name)


class PipelineSpy(ExecuteSpy):
    def __init__(self, db, reads, batches, after_batch=None, failure=None):
        super().__init__(db, reads)
        self.batches, self.after_batch, self.failure = batches, after_batch, failure

    def read_batch(self, requests):
        requests = list(requests)
        self.batches.append(requests)
        if len(self.batches) == 2 and self.failure:
            if self.failure == "short":
                return []
            raise RuntimeError("synthetic pipeline failure")
        native = getattr(self.db, "read_batch", None)
        cursors = (
            native(requests)
            if callable(native)
            else [self.db.execute(sql, parameters) for sql, parameters in requests]
        )
        if self.after_batch is not None:
            self.after_batch(len(self.batches))
        return cursors


class CurrentReadTests(unittest.TestCase):
    prepare = source_tests.SourceTests.prepare
    confirmed = source_tests.SourceTests.confirmed
    reject = source_tests.SourceTests.reject

    def setUp(self):
        source_tests.SourceTests.setUp(self)
        self.addCleanup(self.p.close)

    @contextmanager
    def observe(self, *, pipeline=True, after_batch=None, failure=None):
        original = self.p.store.connect
        reads, batches = [], []

        @contextmanager
        def connect(*, write=False, timeout=5):
            self.assertTrue(write)
            with original(write=write, timeout=timeout) as db:
                if pipeline:
                    yield PipelineSpy(db, reads, batches, after_batch, failure)
                else:
                    yield ExecuteSpy(db, reads)

        with patch.object(self.p.store, "connect", connect):
            yield reads, batches

    def viewer_request(self, authority, response):
        return current_request(
            response,
            {
                "origin": {"assertion_ref": authority["actor_contexts"][0]["assertion_ref"]},
                "scope": response["outcomes"][0]["admission"]["scope"],
            },
        )

    def test_complete_pipeline_preserves_grants_viewer_and_terminal_head(self):
        _, authority, response = self.confirmed()
        request = self.viewer_request(authority, response)
        expected = self.p.sources.read(bearer("MEMORY"), request)
        for limit in (256, 1):
            with self.subTest(limit=limit), patch.object(sources_current, "_BATCH_SIZE", limit):
                with self.observe() as (reads, batches):
                    result = self.p.sources.read(bearer("MEMORY"), request)
                self.assertEqual(result, expected)
                self.assertEqual(len(batches), 2)
                self.assertEqual(
                    [len(batch) for batch in batches], [3, 4] if limit == 256 else [5, 6]
                )
                self.assertTrue(all(len(keys) <= limit for batch in batches for _, keys in batch))
                # Only the existing header policy/authentication and final head
                # remain outside both complete batches, including later chunks.
                self.assertEqual(len(reads), 3)
                self.assertIn("authority_policy", reads[0])
                self.assertIn("revoked_principals", reads[1])
                self.assertIn("authority_head", reads[-1])
                self.assertEqual([g["state"] for g in result["grants"]], ["allowed", "allowed"])
                self.assertEqual(
                    [g["selector"] for g in result["grants"]],
                    [a["selector"] for a in request["admissions"]],
                )
                self.assertEqual(
                    [g["scope"] for g in result["grants"]],
                    [a["scope"] for a in request["admissions"]],
                )
                self.assertEqual(
                    result["viewer_context"]["allowed_scope"], request["viewer"]["scope"]
                )

    def test_sqlite_keeps_lazy_reads_and_reuses_only_this_request(self):
        if self.p.storage_runtime.backend != "sqlite":
            self.skipTest("SQLite lazy path; other methods exercise the actual PG pipeline")
        _, _, response = self.confirmed()
        with self.observe(pipeline=False) as (reads, batches):
            result = self.p.sources.read(bearer("MEMORY"), current_request(response))
        self.assertFalse(batches)
        self.assertEqual(sum("FROM identities" in sql for sql in reads), 1)
        self.assertEqual(sum("FROM channels" in sql for sql in reads), 1)
        self.p.origins.revoke(bearer("ADMIN"), "entry", "actor-a")
        with self.observe(pipeline=False):
            fresh = self.p.sources.read(bearer("MEMORY"), current_request(response))
        self.assertEqual([g["state"] for g in fresh["grants"]], ["denied", "allowed"])
        self.assertGreater(fresh["head"]["sequence"], result["head"]["sequence"])

    def test_next_pipeline_observes_revocation_and_does_not_reuse_viewer(self):
        _, authority, response = self.confirmed()
        request = self.viewer_request(authority, response)
        with self.observe():
            before = self.p.sources.read(bearer("MEMORY"), request)
        self.p.origins.revoke(bearer("ADMIN"), "entry", "actor-a")
        with self.observe():
            after = self.p.sources.read(bearer("MEMORY"), current_request(response))
        self.assertEqual([g["state"] for g in after["grants"]], ["denied", "allowed"])
        self.assertGreater(after["head"]["sequence"], before["head"]["sequence"])
        with self.observe():
            self.reject("forbidden", self.p.sources.read, bearer("MEMORY"), request)

    def test_binding_channel_and_input_revocation_remain_real_relations(self):
        _, _, response = self.confirmed()
        request = current_request(response)
        with self.p.store.connect(write=True) as db:
            db.execute("UPDATE identities SET version=version+1")
        with self.observe():
            result = self.p.sources.read(bearer("MEMORY"), request)
        self.assertTrue(all(g["state"] == "denied" for g in result["grants"]))
        with self.p.store.connect(write=True) as db:
            db.execute("UPDATE identities SET version=version-1")
            db.execute("UPDATE channels SET conversation_id='conv:changed'")
        with self.observe():
            result = self.p.sources.read(bearer("MEMORY"), request)
        self.assertTrue(all(g["state"] == "denied" for g in result["grants"]))
        with self.p.store.connect(write=True) as db:
            db.execute(
                "UPDATE channels SET conversation_id=?",
                (request["admissions"][0]["scope"]["conversation_id"],),
            )
        self.p.origins.revoke(bearer("ADMIN"), "entry", "input-entry")
        with self.observe():
            result = self.p.sources.read(bearer("MEMORY"), request)
        self.assertTrue(all(g["state"] == "denied" for g in result["grants"]))

    def test_unknown_receipt_and_changed_admission_are_not_healthy(self):
        _, _, response = self.confirmed()
        for change in ("missing", "changed"):
            with self.subTest(change=change):
                request = current_request(copy.deepcopy(response))
                if change == "missing":
                    request["admissions"][0]["source"]["receipt_id"] = "receipt:unknown"
                    code = "dependency_unavailable"
                else:
                    request["admissions"][0]["accepted_at"] = utc(self.now - 1)
                    code = "forbidden"
                with self.observe():
                    self.reject(code, self.p.sources.read, bearer("MEMORY"), request)

    def test_pipeline_does_not_cache_secrets_or_viewer_clock_and_digest(self):
        _, authority, response = self.confirmed()
        with patch.dict(os.environ):

            def remove_secret(batch):
                if batch == 2:
                    os.environ.pop("TS012_CONNECTOR")

            with self.observe(after_batch=remove_secret):
                result = self.p.sources.read(bearer("MEMORY"), current_request(response))
            self.assertTrue(all(g["state"] == "denied" for g in result["grants"]))

        request = self.viewer_request(authority, response)
        original_now = self.now
        original_entry = copy.deepcopy(self.p.auth.entries["actor-a"])
        for change in ("expiry", "digest"):
            with self.subTest(change=change):

                def change_live_state(batch):
                    if batch == 2:
                        if change == "expiry":
                            self.now += 61
                        else:
                            self.p.auth.entries["actor-a"]["ttl_seconds"] += 1

                try:
                    with self.observe(after_batch=change_live_state):
                        self.reject("forbidden", self.p.sources.read, bearer("MEMORY"), request)
                finally:
                    self.now = original_now
                    self.p.auth.entries["actor-a"] = copy.deepcopy(original_entry)

    def test_failed_or_incomplete_pipeline_rolls_back_policy_sync(self):
        _, _, response = self.confirmed()
        with self.p.store.connect() as db:
            row = db.execute("SELECT generation,sequence FROM authority_head").fetchone()
            before = row["generation"], row["sequence"]
        for failure in ("raise", "short"):
            with self.subTest(failure=failure), patch.dict(os.environ):
                os.environ.pop("TS012_CONNECTOR")
                with self.observe(failure=failure):
                    with self.assertRaises((RuntimeError, ValueError)):
                        self.p.sources.read(bearer("MEMORY"), current_request(response))
            with self.p.store.connect() as db:
                row = db.execute("SELECT generation,sequence FROM authority_head").fetchone()
                after = row["generation"], row["sequence"]
            self.assertEqual(after, before)


@unittest.skipUnless(PG, "TS231_TEST_POSTGRES requires a disposable PostgreSQL database")
class PostgreSQLCurrentReadTests(CurrentReadTests):
    def setUp(self):
        environment = patch.dict(
            os.environ,
            {
                "TIANSHU_POSTGRES_URL": PG,
                "TIANSHU_STORAGE_NAMESPACE": "ts311_current_" + uuid.uuid4().hex,
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        super().setUp()
        self.assertEqual(self.p.storage_runtime.backend, "postgresql")

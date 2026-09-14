import copy
import math
import os
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fixtures import ENV, bearer, resolve
from services.platform.contracts import Fault, canonical, digest, epoch, utc
from services.platform.service import Platform
from source_fixtures import core_fixture, current_request, input_request, register, source_settings


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)
        self.now = time.time()
        self.settings = source_settings(self.temp.name)
        self.p = Platform(self.settings, clock=lambda: self.now)

    def reject(self, code, function, *args):
        with self.assertRaises(Fault) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)

    def prepare(self, targets=None, audience="group"):
        ingest = register(self.p, self.now, audience, targets)
        ticket = self.p.sources.prepare_mapping(bearer("CONNECTOR"), ingest)
        authority = self.p.sources.read(bearer("COMPANION"), input_request(ingest))
        response = core_fixture(ingest, authority, self.now, audience)
        return ingest, ticket, authority, response

    def confirmed(self):
        ingest, ticket, authority, response = self.prepare()
        self.p.sources.confirm_mapping(bearer("COMPANION"), ticket, response)
        return ingest, authority, response

    def test_exact_input_proof_cannot_be_actor_origin_or_payload_auth(self):
        ingest, _, authority, _ = self.prepare()
        self.reject(
            "forbidden",
            self.p.origins.resolve,
            bearer("MEMORY_RESOLVER"),
            resolve(ingest["command"]["origin"]["assertion_ref"]),
        )
        for field, value in (
            ("author", {"namespace": "web", "immutable_account_id": "forged"}),
            ("parts", [{"kind": "text", "text": "forged"}]),
            ("kind", "edit"),
        ):
            changed = copy.deepcopy(ingest)
            changed["input"][field] = value
            self.reject(
                "forbidden", self.p.sources.read, bearer("COMPANION"), input_request(changed)
            )
        forged = {**input_request(ingest), "authenticated_service": "companion"}
        self.reject("invalid_input", self.p.sources.read, bearer("COMPANION"), forged)
        self.reject("forbidden", self.p.sources.read, bearer("MEMORY"), input_request(ingest))
        self.reject("forbidden", self.p.sources.read, bearer("COMPANION"), current_request())
        self.reject(
            "forbidden",
            self.p.sources.register_input,
            bearer("ADMIN"),
            "input-entry",
            ingest["input"],
        )
        actor_ref = authority["actor_contexts"][0]["assertion_ref"]
        wrong = copy.deepcopy(ingest)
        wrong["command"]["origin"]["assertion_ref"] = actor_ref
        self.reject("forbidden", self.p.sources.read, bearer("COMPANION"), input_request(wrong))

    def test_inline_backfill_is_atomic_and_restart_preserves_current_history(self):
        _, authority, response = self.confirmed()
        current = self.p.sources.read(bearer("MEMORY"), current_request(response))
        self.assertEqual([g["state"] for g in current["grants"]], ["allowed", "allowed"])
        self.assertEqual(
            current["grants"][0]["entry_digest"], digest(self.settings["entries"]["actor-a"])
        )
        self.now += 70
        self.p = Platform(self.settings, clock=lambda: self.now)
        again = self.p.sources.read(bearer("MEMORY"), current_request(response))
        self.assertEqual(current, again)
        self.reject(
            "forbidden",
            self.p.origins.resolve,
            bearer("MEMORY_RESOLVER"),
            resolve(authority["actor_contexts"][0]["assertion_ref"]),
        )
        fresh = self.p.origins.issue(bearer("CONNECTOR"), "actor-a")["assertion_ref"]
        context = self.p.origins.resolve(bearer("MEMORY_RESOLVER"), resolve(fresh))["context"]
        self.assertEqual(context["allowed_scope"], response["outcomes"][0]["admission"]["scope"])

    def test_viewer_transport_identity_is_not_business_caller(self):
        _, authority, response = self.confirmed()
        viewer = {
            "origin": {"assertion_ref": authority["actor_contexts"][0]["assertion_ref"]},
            "scope": response["outcomes"][0]["admission"]["scope"],
        }
        current = self.p.sources.read(bearer("MEMORY"), current_request(response, viewer))
        context = current["viewer_context"]
        self.assertEqual(
            (context["issuer"], context["authenticated_service"], context["audience_service"]),
            ("platform", "companion", "memory"),
        )
        viewer["scope"] = response["outcomes"][1]["admission"]["scope"]
        self.reject(
            "forbidden", self.p.sources.read, bearer("MEMORY"), current_request(response, viewer)
        )
        self.now += 60
        self.reject(
            "forbidden", self.p.sources.read, bearer("MEMORY"), current_request(response, viewer)
        )

    def test_all_inline_correlations_and_history_must_match_before_any_write(self):
        _, ticket, _, response = self.prepare()
        self.reject("forbidden", self.p.sources.confirm_mapping, bearer("MEMORY"), ticket, response)
        changes = [
            lambda r: r["outcomes"].pop(),
            lambda r: r["outcomes"].append(copy.deepcopy(r["outcomes"][0])),
            lambda r: r.update(request_digest="0" * 64),
            lambda r: r.update(physical_receipt_id="wrong"),
            lambda r: r["outcomes"][1].update(receipt=copy.deepcopy(r["outcomes"][0]["receipt"])),
            lambda r: r["outcomes"][1].update(
                admission=copy.deepcopy(r["outcomes"][0]["admission"])
            ),
            lambda r: r["outcomes"][1]["admission"].update(binding_version=2),
            lambda r: r["outcomes"][1]["admission"]["scope"].update(person_id="wrong"),
            lambda r: r["outcomes"][1]["receipt"]["collection_key"]["author"].update(
                immutable_account_id="wrong"
            ),
            lambda r: r["outcomes"][1]["admission"]["source"]["message_key"].update(revision=2),
            lambda r: r["outcomes"][1]["admission"]["source"].update(
                archive_state="archived", locator="audit://fake"
            ),
            lambda r: r["outcomes"][1]["admission"]["accepted_origin"].update(
                assertion_ref="forged"
            ),
            lambda r: r["outcomes"][1]["admission"].update(accepted_at=utc(self.now + 1)),
        ]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                bad = copy.deepcopy(response)
                change(bad)
                with self.assertRaises(Fault):
                    self.p.sources.confirm_mapping(bearer("COMPANION"), ticket, bad)
                with self.p.store.connect() as db:
                    for table in ("identities", "channels", "admission_history"):
                        self.assertEqual(
                            db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0
                        )
        self.p.sources.confirm_mapping(bearer("COMPANION"), ticket, response)
        bad = current_request(response)
        bad["admissions"][0]["accepted_at"] = utc(self.now - 1)
        self.reject("forbidden", self.p.sources.read, bearer("MEMORY"), bad)
        bad["admissions"][0]["source"]["receipt_id"] = "unknown"
        self.reject("dependency_unavailable", self.p.sources.read, bearer("MEMORY"), bad)

    def test_current_denial_and_watermarks_for_entry_subject_route_and_binding(self):
        _, _, response = self.confirmed()
        before = self.p.sources.read(bearer("MEMORY"), current_request(response))
        self.p.origins.revoke(bearer("ADMIN"), "entry", "actor-a")
        after = self.p.sources.read(bearer("MEMORY"), current_request(response))
        self.assertEqual([g["state"] for g in after["grants"]], ["denied", "allowed"])
        self.assertGreater(after["head"]["sequence"], before["head"]["sequence"])
        self.settings["entries"]["actor-b"]["routes"] = []
        self.p = Platform(self.settings, clock=lambda: self.now)
        changed = self.p.sources.read(bearer("MEMORY"), current_request(response))
        self.assertEqual([g["state"] for g in changed["grants"]], ["denied", "denied"])
        self.assertGreater(changed["head"]["sequence"], after["head"]["sequence"])

    def test_revoked_input_entry_and_missing_credentials_deny_background(self):
        _, _, response = self.confirmed()
        before = self.p.sources.read(bearer("MEMORY"), current_request(response))
        os.environ.pop("TS012_CONNECTOR")
        denied = self.p.sources.read(bearer("MEMORY"), current_request(response))
        self.assertTrue(all(g["state"] == "denied" for g in denied["grants"]))
        self.assertGreater(denied["head"]["sequence"], before["head"]["sequence"])
        os.environ["TS012_CONNECTOR"] = ENV["TS012_CONNECTOR"]
        self.p.origins.revoke(bearer("ADMIN"), "entry", "input-entry")
        denied = self.p.sources.read(bearer("MEMORY"), current_request(response))
        self.assertTrue(all(g["state"] == "denied" for g in denied["grants"]))

    def test_frozen_default_retry_after_response_loss_and_expired_origins(self):
        ingest, _, _, original = self.prepare(targets=[])
        self.reject(
            "dependency_unavailable",
            self.p.sources.read,
            bearer("MEMORY"),
            current_request(original),
        )
        self.now += 70
        self.settings["input_entries"]["input-entry"].update(
            default_actor_ids=["actor:a", "actor:b"], routing_version=2
        )
        self.p = Platform(self.settings, clock=lambda: self.now)
        retry = register(self.p, self.now, targets=[])
        retry["command"]["request_id"] = "retry"
        ticket = self.p.sources.prepare_mapping(bearer("CONNECTOR"), retry)
        authority = self.p.sources.read(bearer("COMPANION"), input_request(retry))
        self.assertEqual(authority["default_actor_ids"], ["actor:a"])
        self.assertEqual(authority["routing_version"], 1)
        response = core_fixture(retry, authority, self.now, previous=original)
        self.p.sources.confirm_mapping(bearer("COMPANION"), ticket, response)
        self.assertEqual(
            self.p.sources.read(bearer("MEMORY"), current_request(response))["grants"][0]["state"],
            "allowed",
        )
        changed = copy.deepcopy(retry)
        changed["target_actor_ids"] = ["actor:a", "actor:b"]
        self.reject(
            "idempotency_conflict", self.p.sources.prepare_mapping, bearer("CONNECTOR"), changed
        )
        changed["command"]["idempotency_key"] = "new-route"
        current = self.p.sources.read(bearer("COMPANION"), input_request(changed))
        self.assertEqual(len(current["actor_contexts"]), 2)
        self.assertEqual(current["routing_version"], 2)
        self.assertEqual(ingest["input"], retry["input"])

    def test_partial_forbidden_empty_defaults_and_retract_do_not_invent_person(self):
        self.p.origins.revoke(bearer("ADMIN"), "entry", "actor-b")
        _, ticket, authority, response = self.prepare()
        self.assertEqual(len(authority["actor_contexts"]), 1)
        self.assertEqual(
            response["outcomes"][1],
            {"actor_id": "actor:b", "state": "forbidden", "receipt": None, "admission": None},
        )
        self.p.sources.confirm_mapping(bearer("COMPANION"), ticket, response)
        self.p.origins.revoke(bearer("ADMIN"), "entry", "actor-a")
        ingest = register(self.p, self.now)
        ingest["command"]["idempotency_key"] = "all-forbidden"
        authority = self.p.sources.read(bearer("COMPANION"), input_request(ingest))
        response = core_fixture(ingest, authority, self.now)
        ticket = self.p.sources.prepare_mapping(bearer("CONNECTOR"), ingest)
        self.p.sources.confirm_mapping(bearer("COMPANION"), ticket, response)
        self.assertIsNone(response["person_id"])
        retract = copy.deepcopy(ingest["input"])
        retract.update(kind="retract", parts=[])
        retract["message_key"]["revision"] = 2
        command = register(self.p, self.now, targets=[], data=retract)
        command["command"]["idempotency_key"] = "retract"
        authority = self.p.sources.read(bearer("COMPANION"), input_request(command))
        result = core_fixture(command, authority, self.now)
        ticket = self.p.sources.prepare_mapping(bearer("CONNECTOR"), command)
        self.p.sources.confirm_mapping(bearer("COMPANION"), ticket, result)
        self.assertEqual(result["outcomes"], [])
        resurrection = copy.deepcopy(retract)
        resurrection.update(kind="edit", parts=ingest["input"]["parts"])
        resurrection["message_key"]["revision"] = 3
        self.reject(
            "scope_changed",
            self.p.sources.register_input,
            bearer("CONNECTOR"),
            "input-entry",
            resurrection,
        )

    def test_input_revision_author_thread_and_expiry_are_exact(self):
        ingest = register(self.p, self.now)
        changed = copy.deepcopy(ingest["input"])
        changed["parts"][0]["text"] = "changed"
        self.reject(
            "idempotency_conflict",
            self.p.sources.register_input,
            bearer("CONNECTOR"),
            "input-entry",
            changed,
        )
        changed["message_key"]["revision"] = 2
        changed["kind"] = "edit"
        self.p.sources.register_input(bearer("CONNECTOR"), "input-entry", changed)
        self.reject(
            "scope_changed", self.p.sources.read, bearer("COMPANION"), input_request(ingest)
        )
        changed["message_key"]["channel"]["thread_id"] = "other"
        self.reject(
            "forbidden", self.p.sources.register_input, bearer("CONNECTOR"), "input-entry", changed
        )
        self.now += 60
        self.reject("forbidden", self.p.sources.read, bearer("COMPANION"), input_request(ingest))

    def test_shared_channel_private_scope_and_mapping_version_conflict(self):
        self.settings = source_settings(self.temp.name, "self_private")
        self.p = Platform(self.settings, clock=lambda: self.now)
        _, ticket, _, response = self.prepare(audience="self_private")
        account = canonical(self.settings["input_entries"]["input-entry"]["account"])
        with self.p.store.connect(write=True) as db:
            db.execute("INSERT INTO identities VALUES(?,?,?)", (account, response["person_id"], 2))
        self.reject(
            "version_conflict",
            self.p.sources.confirm_mapping,
            bearer("COMPANION"),
            ticket,
            response,
        )
        with self.p.store.connect(write=True) as db:
            db.execute("UPDATE identities SET version=1")
        self.p.sources.confirm_mapping(bearer("COMPANION"), ticket, response)
        self.assertEqual(
            {o["admission"]["scope"]["conversation_id"] for o in response["outcomes"]},
            {response["conversation_id"]},
        )
        with self.p.store.connect(write=True) as db:
            db.execute("UPDATE identities SET version=2")
        current = self.p.sources.read(bearer("MEMORY"), current_request(response))
        self.assertTrue(all(g["state"] == "denied" for g in current["grants"]))

    def test_concurrent_confirm_and_fault_roll_back_every_mapping_and_head(self):
        _, ticket, _, response = self.prepare()
        with self.p.store.connect(write=True) as db:
            db.execute(
                "CREATE TRIGGER fail_history BEFORE INSERT ON admission_history BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
            )
        before = self.p.sources.read(bearer("MEMORY"), current_request())["head"]
        with self.assertRaises(sqlite3.IntegrityError):
            self.p.sources.confirm_mapping(bearer("COMPANION"), ticket, response)
        self.assertEqual(before, self.p.sources.read(bearer("MEMORY"), current_request())["head"])
        with self.p.store.connect(write=True) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM identities").fetchone()[0], 0)
            db.execute("DROP TRIGGER fail_history")
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(
                pool.map(
                    lambda _: self.p.sources.confirm_mapping(bearer("COMPANION"), ticket, response),
                    range(2),
                )
            )
        with self.p.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM admission_history").fetchone()[0], 2)

    def test_stale_process_expired_entry_and_migration_backup(self):
        # Exercise the upward microsecond rounding that made now += 5 flaky.
        self.now = math.nextafter(epoch("2027-01-15T08:00:00.000001Z"), -math.inf)
        _, _, response = self.confirmed()
        stale = self.p
        self.settings["entries"]["actor-a"]["expires_at"] = utc(self.now + 5)
        self.p = Platform(self.settings, clock=lambda: self.now)
        self.reject(
            "dependency_unavailable", stale.sources.read, bearer("MEMORY"), current_request()
        )
        expires = epoch(self.settings["entries"]["actor-a"]["expires_at"])
        self.now = math.nextafter(expires, -math.inf)
        before = self.p.sources.read(bearer("MEMORY"), current_request(response))
        self.assertEqual([g["state"] for g in before["grants"]], ["allowed", "allowed"])
        self.now = expires
        exact = self.p.sources.read(bearer("MEMORY"), current_request(response))
        self.assertEqual([g["state"] for g in exact["grants"]], ["denied", "allowed"])
        self.assertGreater(exact["head"]["sequence"], before["head"]["sequence"])
        self.now = math.nextafter(expires, math.inf)
        after = self.p.sources.read(bearer("MEMORY"), current_request(response))
        self.assertEqual([g["state"] for g in after["grants"]], ["denied", "allowed"])
        self.assertEqual(after["head"], exact["head"])
        with self.p.store.connect(write=True) as db:
            db.execute("PRAGMA user_version=0")
        self.p = Platform(self.settings, clock=lambda: self.now)
        backups = list(Path(self.temp.name).glob("*.pre-source-*.sqlite"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM admission_history").fetchone()[0], 2)
        again = self.p.sources.read(bearer("MEMORY"), current_request(response))
        self.assertEqual(again["head"], after["head"])

    def test_actual_legacy_store_migration_keeps_refs_but_invents_no_admission(self):
        settings = source_settings(self.temp.name)
        settings["database_path"] = str(Path(self.temp.name) / "legacy.sqlite")
        with closing(sqlite3.connect(settings["database_path"])) as db:
            db.executescript(
                "CREATE TABLE origins(ref TEXT PRIMARY KEY, entry_id TEXT NOT NULL, entry_digest TEXT NOT NULL, expires_at REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0); CREATE TABLE revoked_entries(id TEXT PRIMARY KEY);"
            )
            db.execute(
                "INSERT INTO origins VALUES(?,?,?,?,0)",
                ("legacy:origin", "actor-a", digest(settings["entries"]["actor-a"]), self.now + 60),
            )
            db.commit()
        migrated = Platform(settings, clock=lambda: self.now)
        context = migrated.origins.resolve(bearer("MEMORY_RESOLVER"), resolve("legacy:origin"))[
            "context"
        ]
        self.assertEqual(context["allowed_scope"]["actor_id"], "actor:a")
        self.assertIsNone(context["allowed_scope"]["person_id"])
        with migrated.store.connect() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM admission_history").fetchone()[0], 0)
        self.assertEqual(
            len(list(Path(self.temp.name).glob("legacy.sqlite.pre-source-*.sqlite"))), 1
        )

    def test_principal_revocation_and_commit_race_do_not_publish_mapping(self):
        _, ticket, _, response = self.prepare()
        self.p.origins.revoke(bearer("ADMIN"), "principal", "connector")
        self.reject(
            "forbidden", self.p.sources.confirm_mapping, bearer("COMPANION"), ticket, response
        )
        self.reject(
            "dependency_unavailable",
            self.p.sources.read,
            bearer("MEMORY"),
            current_request(response),
        )
        with self.p.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM identities").fetchone()[0], 0)

    def test_unrouted_input_freezes_empty_defaults_without_inventing_identity(self):
        self.settings["input_entries"]["input-entry"]["default_actor_ids"] = []
        self.p = Platform(self.settings, clock=lambda: self.now)
        _, ticket, authority, response = self.prepare(targets=[])
        self.assertEqual(authority["actor_contexts"], [])
        self.assertIsNone(response["person_id"])
        self.assertEqual(response["routing_state"], "unrouted")
        self.p.sources.confirm_mapping(bearer("COMPANION"), ticket, response)
        with self.p.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM identities").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM channels").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()

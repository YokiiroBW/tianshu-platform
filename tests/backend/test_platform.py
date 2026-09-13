import copy
from concurrent.futures import ThreadPoolExecutor
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fixtures import (
    DOCUMENTS,
    ENV,
    TOKENS,
    bearer,
    complete_mapping,
    config,
    mapping_request,
    query,
    resolve,
    settings,
)
from services.platform.contracts import Contracts, Fault, loads, utc
from services.platform.service import Platform


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.now = 1_800_000_000.0
        self.settings = settings(self.temp.name)
        self.p = Platform(self.settings, clock=lambda: self.now)
        self.chat = self.p.origins.issue(bearer("CONNECTOR"), "chat-entry")["assertion_ref"]
        self.origin = self.p.origins.issue(bearer("ADMIN"), "config-entry")["assertion_ref"]

    def reject(self, code, method, *args, **kwargs):
        with self.assertRaises(Fault) as found:
            method(*args, **kwargs)
        self.assertEqual(found.exception.code, code)

    def test_contract_hash_and_strict_json(self):
        for raw in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":1e999}', b'"\xff"'):
            with self.subTest(raw=raw):
                self.reject("invalid_input", loads, raw)
        root = Path(self.temp.name) / "tampered"
        root.mkdir()
        (root / "manifest.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            Contracts(root)

    def test_no_unconfigured_credentials_or_payload_identity(self):
        for token in ("", "Bearer wrong", "Bearer 中文", "Basic abc"):
            self.reject("unauthorized", self.p.origins.issue, token, "chat-entry")
        self.reject("forbidden", self.p.origins.issue, bearer("ADMIN"), "chat-entry")
        self.reject("forbidden", self.p.models.publish, bearer("CONNECTOR"), config(self.now))
        os.environ.pop("TS012_CONNECTOR")
        self.reject("unauthorized", self.p.origins.issue, bearer("CONNECTOR"), "chat-entry")
        self.reject(
            "forbidden", self.p.origins.resolve, bearer("MEMORY_RESOLVER"), resolve(self.chat)
        )

    def test_duplicate_service_credentials_fail_closed(self):
        os.environ["TS012_READER"] = TOKENS["ADMIN"]
        self.reject("unauthorized", self.p.origins.issue, bearer("ADMIN"), "config-entry")

    def test_distinct_proxy_caller_receiver_and_purpose(self):
        context = self.p.origins.resolve(bearer("MEMORY_RESOLVER"), resolve(self.chat))["context"]
        self.assertEqual(
            (context["issuer"], context["authenticated_service"], context["audience_service"]),
            ("platform", "companion", "memory"),
        )
        self.assertIsNone(context["allowed_scope"]["person_id"])
        self.assertIsNone(context["allowed_scope"]["conversation_id"])
        self.reject(
            "forbidden", self.p.origins.resolve, bearer("WRONG_RESOLVER"), resolve(self.chat)
        )
        self.reject(
            "forbidden", self.p.origins.resolve, bearer("MEMORY_RESOLVER"), resolve(self.origin)
        )
        self.p.models.publish(bearer("ADMIN"), config(self.now))
        self.reject("forbidden", self.p.models.snapshot, bearer("GATEWAY"), query(self.chat))

    def test_reference_expiry_revocation_entry_and_principal(self):
        self.now += 60
        self.reject(
            "forbidden", self.p.origins.resolve, bearer("MEMORY_RESOLVER"), resolve(self.chat)
        )
        for kind, object_id in (
            ("origin", self.origin),
            ("entry", "chat-entry"),
            ("principal", "connector"),
        ):
            self.p.origins.revoke(bearer("ADMIN"), kind, object_id)
        self.reject("unauthorized", self.p.origins.issue, bearer("CONNECTOR"), "chat-entry")
        restarted = Platform(self.settings, clock=lambda: self.now - 60)
        self.reject("forbidden", restarted.models.snapshot, bearer("GATEWAY"), query(self.origin))

    def test_restart_preserves_issuance_and_authority_mappings(self):
        complete_mapping(self.p, self.chat, self.now)
        restarted = Platform(self.settings, clock=lambda: self.now)
        result = restarted.origins.resolve(bearer("MEMORY_RESOLVER"), resolve(self.chat))["context"]
        self.assertEqual(result["allowed_scope"], DOCUMENTS["turn_committed"]["scope"])
        new_ref = restarted.origins.issue(bearer("CONNECTOR"), "chat-entry")["assertion_ref"]
        self.assertNotEqual(new_ref, self.chat)
        self.assertEqual(
            restarted.origins.resolve(bearer("MEMORY_RESOLVER"), resolve(new_ref))["context"][
                "allowed_scope"
            ],
            result["allowed_scope"],
        )
        with restarted.store.connect() as db:
            self.assertNotIn(
                DOCUMENTS["ingest"]["parts"][0]["text"],
                str([dict(r) for r in db.execute("SELECT * FROM exchanges")]),
            )

    def test_entry_changes_invalidate_old_origins(self):
        changed = copy.deepcopy(self.settings)
        changed["entries"]["chat-entry"]["actor_id"] = "actor-other"
        restarted = Platform(changed, clock=lambda: self.now)
        self.reject(
            "forbidden", restarted.origins.resolve, bearer("MEMORY_RESOLVER"), resolve(self.chat)
        )

    def test_mapping_account_actor_channel_mismatch_and_response_auth(self):
        for field in ("account", "actor", "channel"):
            request = mapping_request(
                "identity" if field == "account" else "ingest", self.chat, self.now
            )
            if field == "account":
                request["account"]["immutable_account_id"] = "other"
            elif field == "actor":
                request["target_actor_ids"] = ["other"]
            else:
                request["message_key"]["channel"]["channel_conversation_id"] = "other"
            self.reject(
                "forbidden",
                self.p.origins.prepare_mapping,
                bearer("COMPANION" if field == "account" else "CONNECTOR"),
                "identity" if field == "account" else "ingest",
                request,
            )
        ticket = self.p.origins.prepare_mapping(
            bearer("CONNECTOR"), "ingest", mapping_request("ingest", self.chat, self.now)
        )
        response = copy.deepcopy(DOCUMENTS["ingest_receipt"])
        self.reject("forbidden", self.p.origins.confirm_mapping, bearer("MEMORY"), ticket, response)
        for field in ("request_id", "channel", "account"):
            bad = copy.deepcopy(response)
            if field == "request_id":
                bad[field] = "wrong"
            elif field == "channel":
                bad["collection_key"][field]["binding_id"] = "wrong"
            else:
                bad["collection_key"]["author"]["immutable_account_id"] = "wrong"
            self.reject(
                "forbidden", self.p.origins.confirm_mapping, bearer("COMPANION"), ticket, bad
            )
        self.p.origins.confirm_mapping(bearer("COMPANION"), ticket, response)
        context = self.p.origins.resolve(bearer("MEMORY_RESOLVER"), resolve(self.chat))["context"]
        self.assertIsNone(context["allowed_scope"]["person_id"])
        self.assertEqual(context["allowed_scope"]["conversation_id"], response["conversation_id"])

    def test_mapping_idempotency_version_and_deadline(self):
        request = mapping_request("identity", self.chat, self.now)
        ticket = self.p.origins.prepare_mapping(bearer("COMPANION"), "identity", request)
        response = copy.deepcopy(DOCUMENTS["registered"])
        self.p.origins.confirm_mapping(bearer("MEMORY"), ticket, response)
        self.p.origins.confirm_mapping(bearer("MEMORY"), ticket, response)
        changed = {**response, "person_id": "other-person"}
        self.reject(
            "idempotency_conflict",
            self.p.origins.confirm_mapping,
            bearer("MEMORY"),
            ticket,
            changed,
        )
        ticket2 = self.p.origins.prepare_mapping(bearer("COMPANION"), "identity", request)
        self.reject(
            "version_conflict", self.p.origins.confirm_mapping, bearer("MEMORY"), ticket2, changed
        )
        self.now += 30
        self.reject("timeout", self.p.origins.confirm_mapping, bearer("MEMORY"), ticket2, response)

    def test_mapping_rejects_conversation_replacement(self):
        complete_mapping(self.p, self.chat, self.now)
        ticket = self.p.origins.prepare_mapping(
            bearer("CONNECTOR"), "ingest", mapping_request("ingest", self.chat, self.now)
        )
        bad = {**DOCUMENTS["ingest_receipt"], "conversation_id": "other"}
        self.reject(
            "version_conflict", self.p.origins.confirm_mapping, bearer("COMPANION"), ticket, bad
        )

    def test_ingest_first_cannot_override_later_memory_subject(self):
        ticket = self.p.origins.prepare_mapping(
            bearer("CONNECTOR"), "ingest", mapping_request("ingest", self.chat, self.now)
        )
        self.p.origins.confirm_mapping(bearer("COMPANION"), ticket, DOCUMENTS["ingest_receipt"])
        ticket = self.p.origins.prepare_mapping(
            bearer("COMPANION"), "identity", mapping_request("identity", self.chat, self.now)
        )
        self.reject(
            "forbidden",
            self.p.origins.confirm_mapping,
            bearer("MEMORY"),
            ticket,
            {**DOCUMENTS["registered"], "person_id": "different-person"},
        )
        context = self.p.origins.resolve(bearer("MEMORY_RESOLVER"), resolve(self.chat))["context"]
        self.assertIsNone(context["allowed_scope"]["person_id"])

    def test_existing_memory_identity_resolve_and_unregistered_do_not_invent_mapping(self):
        request = {"query": query(self.chat)["query"], "account": DOCUMENTS["ingest"]["author"]}
        response = {
            "schema_version": 1,
            "request_id": "snapshot-test",
            "state": "unregistered",
            "person_id": None,
            "binding_version": 0,
        }
        ticket = self.p.origins.prepare_mapping(bearer("COMPANION"), "identity_resolve", request)
        self.p.origins.confirm_mapping(bearer("MEMORY"), ticket, response)
        context = self.p.origins.resolve(bearer("MEMORY_RESOLVER"), resolve(self.chat))["context"]
        self.assertIsNone(context["allowed_scope"]["person_id"])
        ticket = self.p.origins.prepare_mapping(bearer("COMPANION"), "identity_resolve", request)
        found = {
            **response,
            "state": "found",
            "person_id": "person-fixture-a",
            "binding_version": 1,
        }
        self.p.origins.confirm_mapping(bearer("MEMORY"), ticket, found)
        context = self.p.origins.resolve(bearer("MEMORY_RESOLVER"), resolve(self.chat))["context"]
        self.assertEqual(context["allowed_scope"]["person_id"], "person-fixture-a")
        ticket = self.p.origins.prepare_mapping(bearer("COMPANION"), "identity_resolve", request)
        self.reject(
            "version_conflict", self.p.origins.confirm_mapping, bearer("MEMORY"), ticket, response
        )

    def test_config_unconfigured_unknown_version_redacted_view(self):
        self.assertEqual(self.p.models.view(bearer("ADMIN"))["availability"], "unconfigured")
        self.reject(
            "dependency_unavailable",
            self.p.models.snapshot,
            bearer("GATEWAY"),
            query(self.origin, None),
        )
        self.reject("not_found", self.p.models.snapshot, bearer("GATEWAY"), query(self.origin, 7))
        self.p.models.publish(bearer("ADMIN"), config(self.now))
        view = self.p.models.view(bearer("ADMIN"))
        self.assertEqual(view["availability"], "available")
        self.assertNotIn("secret-ref", str(view))
        self.assertNotIn("base_url", str(view))
        self.reject("forbidden", self.p.models.snapshot, bearer("ADMIN"), query(self.origin))

    def test_config_unique_providers_workloads_and_bindings(self):
        for change in (
            "provider_duplicate",
            "workload_duplicate",
            "unknown_binding",
            "model_mismatch",
            "unknown_provider",
            "capability_claim",
            "url_credentials",
            "raw_key",
        ):
            with self.subTest(change=change):
                value = config(self.now)
                expected = "invalid_input"
                if change == "provider_duplicate":
                    value["providers"].append(copy.deepcopy(value["providers"][0]))
                elif change == "workload_duplicate":
                    value["bindings"].append(copy.deepcopy(value["bindings"][0]))
                elif change == "unknown_binding":
                    value["bindings"][0]["provider_id"] = "missing"
                elif change == "model_mismatch":
                    value["bindings"][0]["model_id"] = "missing"
                elif change == "unknown_provider":
                    value["providers"][0]["provider_id"] = "missing"
                    expected = "dependency_unavailable"
                elif change == "capability_claim":
                    value["providers"][0]["capability_verification"] = "verified_test_account"
                    expected = "forbidden"
                elif change == "url_credentials":
                    value["providers"][0]["base_url"] = "https://key@upstream.example.invalid/v1"
                    expected = "forbidden"
                else:
                    value["providers"][0]["credential_ref"] = "raw-secret"
                self.reject(expected, self.p.models.publish, bearer("ADMIN"), value)

    def test_config_immutable_monotonic_and_old_pinned_reads(self):
        original = config(self.now)
        self.p.models.publish(bearer("ADMIN"), original)
        self.assertTrue(
            self.p.models.publish(bearer("ADMIN"), {**original, "request_id": "retry"})[
                "deduplicated"
            ]
        )
        changed = copy.deepcopy(original)
        changed["bindings"][0]["timeout_ms"] += 1
        self.reject("version_conflict", self.p.models.publish, bearer("ADMIN"), changed)
        self.p.models.publish(bearer("ADMIN"), config(self.now, version=9))
        self.reject(
            "version_conflict", self.p.models.publish, bearer("ADMIN"), config(self.now, version=8)
        )
        self.assertEqual(
            self.p.models.snapshot(bearer("GATEWAY"), query(self.origin, 7))["config_version"], 7
        )
        self.assertEqual(
            self.p.models.snapshot(bearer("GATEWAY"), query(self.origin, None))["config_version"], 9
        )
        self.reject("forbidden", self.p.models.snapshot, bearer("GATEWAY"), query(self.origin, 99))

    def test_config_invalid_time_expiry_revocation_and_restart(self):
        for before, after in ((10, 20), (-10, -1), (-3600, 100), (0, 0)):
            value = config(self.now)
            value.update(published_at=utc(self.now + before), usable_until=utc(self.now + after))
            self.reject("invalid_input", self.p.models.publish, bearer("ADMIN"), value)
        value = config(self.now)
        value["usable_until"] = utc(self.now + 5)
        self.p.models.publish(bearer("ADMIN"), value)
        self.now += 5
        self.reject("forbidden", self.p.models.snapshot, bearer("GATEWAY"), query(self.origin))
        self.p.models.publish(bearer("ADMIN"), config(self.now, version=8))
        self.p.models.revoke(bearer("ADMIN"), 8)
        restarted = Platform(self.settings, clock=lambda: self.now)
        self.reject(
            "forbidden", restarted.models.snapshot, bearer("GATEWAY"), query(self.origin, 8)
        )
        self.reject(
            "forbidden", restarted.models.snapshot, bearer("GATEWAY"), query(self.origin, None)
        )

    def test_concurrent_publication_cannot_change_same_version(self):
        first, second = config(self.now), config(self.now)
        second["bindings"][0]["timeout_ms"] += 1

        def publish(value):
            try:
                return self.p.models.publish(bearer("ADMIN"), value)["published"]
            except Fault as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(publish, (first, second)))
        self.assertCountEqual(results, [True, "version_conflict"])

    def test_async_current_sources_do_not_reuse_expired_reference(self):
        complete_mapping(self.p, self.chat, self.now)
        event = copy.deepcopy(DOCUMENTS["turn_committed"])
        message = event["sources"][0]["message_key"]
        self.reject(
            "dependency_unavailable",
            self.p.origins.verify_current_sources,
            bearer("COMPANION"),
            event,
        )
        self.p.origins.observe_source(bearer("CONNECTOR"), "chat-entry", message)
        self.now += 70
        self.reject(
            "forbidden", self.p.origins.resolve, bearer("MEMORY_RESOLVER"), resolve(self.chat)
        )
        result = self.p.origins.verify_current_sources(bearer("COMPANION"), event)
        self.assertTrue(result["current_sources"])
        self.assertFalse(result["scope_version_verified"])
        self.reject("forbidden", self.p.origins.verify_current_sources, bearer("MEMORY"), event)
        event["scope"]["person_id"] = "other"
        self.reject("forbidden", self.p.origins.verify_current_sources, bearer("COMPANION"), event)

    def test_async_revisions_tombstones_current_scope_and_archive_unavailable(self):
        complete_mapping(self.p, self.chat, self.now)
        event = copy.deepcopy(DOCUMENTS["turn_committed"])
        key = event["sources"][0]["message_key"]
        self.p.origins.observe_source(bearer("CONNECTOR"), "chat-entry", key)
        self.p.origins.observe_source(
            bearer("CONNECTOR"), "chat-entry", {**key, "revision": 2}, tombstone=True
        )
        self.reject(
            "scope_changed", self.p.origins.verify_current_sources, bearer("COMPANION"), event
        )
        self.reject(
            "scope_changed",
            self.p.origins.observe_source,
            bearer("CONNECTOR"),
            "chat-entry",
            {**key, "revision": 3},
        )
        self.p.origins.revoke(bearer("ADMIN"), "entry", "chat-entry")
        self.reject("forbidden", self.p.origins.verify_current_sources, bearer("COMPANION"), event)
        event["sources"][0].update(archive_state="archived", locator="audit://unverified")
        self.reject(
            "dependency_unavailable",
            self.p.origins.verify_current_sources,
            bearer("COMPANION"),
            event,
        )

    def test_real_capability_catalog_and_no_fake_tasks(self):
        catalog = self.p.projections.capabilities(bearer("READER"))
        self.assertEqual(len(catalog["capabilities"]), 3)
        self.assertNotIn("device", str(catalog))
        self.assertEqual(self.p.projections.tasks(bearer("ADMIN"))["tasks"], [])
        self.assertEqual(self.p.projections.tasks(bearer("READER"))["availability"], "unconfigured")
        projection = {
            "owner": "companion",
            "job_id": "job1",
            "version": 1,
            "summary": "合成任务",
            "phase": "queued",
            "event_cursor": "event1",
            "link": None,
            "observed_at": utc(self.now),
        }
        self.p.projections.project(bearer("COMPANION"), projection)
        self.p.projections.project(bearer("COMPANION"), projection)
        self.reject("forbidden", self.p.projections.project, bearer("ADMIN"), projection)
        self.reject(
            "version_conflict",
            self.p.projections.project,
            bearer("COMPANION"),
            {**projection, "phase": "sent"},
        )
        self.p.projections.project(
            bearer("COMPANION"), {**projection, "version": 2, "phase": "sent"}
        )
        self.reject("version_conflict", self.p.projections.project, bearer("COMPANION"), projection)
        self.now += 61
        task = self.p.projections.tasks(bearer("ADMIN"))["tasks"][0]
        self.assertEqual((task["phase"], task["freshness"]), ("sent", "stale"))
        self.assertEqual(self.p.projections.tasks(bearer("READER"))["tasks"], [])


if __name__ == "__main__":
    unittest.main()

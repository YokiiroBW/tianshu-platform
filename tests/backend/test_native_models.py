"""Native model-protocol/v1 configuration: separate storage, grants, versions and revocation.

Every native document starts from the published model-protocol/v1 examples; only the synthetic
provider id is relabelled so that the two contract families keep separate registrations.
"""

import copy
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import aiohttp

from fixtures import (
    ENV,
    NATIVE_PROVIDER,
    ROOT,
    TOKENS,
    bearer,
    config,
    native_config,
    native_query,
    query,
    settings,
    start_http,
)
from services.platform.contracts import Fault, digest, epoch, utc
from services.platform.server import create_app
from services.platform.service import Platform


class NativePlatformTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.now = 1_800_000_000.0
        self.settings = settings(self.temp.name)
        self.p = Platform(self.settings, clock=lambda: self.now)
        self.origin = self.p.origins.issue(bearer("ADMIN"), "config-entry")["assertion_ref"]

    def reject(self, code, method, *args, **kwargs):
        with self.assertRaises(Fault) as found:
            method(*args, **kwargs)
        self.assertEqual(found.exception.code, code)
        return found.exception

    def restart(self):
        self.p = Platform(self.settings, clock=lambda: self.now)
        return self.p

    def test_native_publication_requires_protocol_pinned_registration(self):
        self.p.models.native_publish(bearer("ADMIN"), native_config(self.now))
        for change, expected in (
            ("protocol", "invalid_input"),
            ("base_url", "forbidden"),
            ("credential_namespace", "forbidden"),
            ("credential_ref", "forbidden"),
            ("model_id", "forbidden"),
            ("capability_claim", "forbidden"),
            ("capabilities", "invalid_input"),
            ("unregistered", "dependency_unavailable"),
            ("workload", "invalid_input"),
            ("binding_model", "invalid_input"),
            ("state_references", "invalid_input"),
            ("model_policy", "invalid_input"),
            ("raw_key", "invalid_input"),
        ):
            with self.subTest(change=change):
                value = copy.deepcopy(native_config(self.now, version=8))
                provider = value["providers"][0]
                if change == "protocol":
                    provider["protocol"] = "openai-chat-completions"
                elif change == "base_url":
                    provider["base_url"] = "https://other.example.invalid/v1"
                elif change == "credential_namespace":
                    provider["credential_namespace"] = "other-namespace"
                elif change == "credential_ref":
                    provider["credential_ref"] = "secret-ref:fixture/other"
                elif change == "model_id":
                    provider["model_id"] = "other-model"
                elif change == "capability_claim":
                    provider["capability_verification"] = "verified_test_account"
                elif change == "capabilities":
                    provider["verified_capabilities"] = ["text", "vision"]
                elif change == "unregistered":
                    provider["provider_id"] = "missing"
                    value["bindings"][0]["provider_id"] = "missing"
                elif change == "workload":
                    value["bindings"][0]["workload"] = "companion.text"
                elif change == "binding_model":
                    value["bindings"][0]["model_id"] = "other-model"
                elif change == "state_references":
                    provider["state_references"] = "allow"
                elif change == "model_policy":
                    provider["model_policy"] = {"mode": "platform_default", "fields": {}}
                else:
                    provider["credential_ref"] = "raw-secret-value"
                self.reject(expected, self.p.models.native_publish, bearer("ADMIN"), value)

    def test_native_registration_is_separate_from_legacy_chat_registration(self):
        # A legacy (unpinned) registration cannot carry a native publication.
        self.settings["providers"][NATIVE_PROVIDER].pop("protocol")
        with self.assertRaises(Fault) as found:
            Platform(self.settings, clock=lambda: self.now).models.native_publish(
                bearer("ADMIN"), native_config(self.now)
            )
        self.assertEqual(found.exception.code, "invalid_input")
        # A native-pinned registration cannot carry a legacy Chat publication either.
        self.settings["providers"]["provider-fixture"]["protocol"] = "openai-responses"
        platform = Platform(self.settings, clock=lambda: self.now)
        self.reject("invalid_input", platform.models.publish, bearer("ADMIN"), config(self.now))

    def test_native_capability_claims_stay_within_the_reviewed_registration(self):
        self.settings["providers"][NATIVE_PROVIDER]["verified_capabilities"] = ["text", "stream"]
        platform = Platform(self.settings, clock=lambda: self.now)
        # A schema-valid capability that was never reviewed for this provider is refused.
        self.reject(
            "forbidden", platform.models.native_publish, bearer("ADMIN"), native_config(self.now)
        )
        reduced = native_config(self.now)
        reduced["providers"][0]["verified_capabilities"] = ["text", "stream"]
        self.assertTrue(platform.models.native_publish(bearer("ADMIN"), reduced)["published"])

    def test_native_versions_are_an_independent_sequence(self):
        self.p.models.publish(bearer("ADMIN"), config(self.now, version=9))
        self.p.models.native_publish(bearer("ADMIN"), native_config(self.now, version=7))
        # The same integer means two different configurations and never collides.
        self.assertEqual(
            self.p.models.native_snapshot(bearer("NATIVE"), native_query(self.origin, 7))[
                "native_config_version"
            ],
            7,
        )
        self.reject(
            "forbidden",
            self.p.models.native_snapshot,
            bearer("NATIVE"),
            native_query(self.origin, 9),
        )
        self.reject(
            "forbidden",
            self.p.models.native_snapshot,
            bearer("NATIVE"),
            native_query(self.origin, 6),
        )
        view = self.p.models.view_native(bearer("ADMIN"))
        self.assertEqual((view["availability"], view["native_config_version"]), ("available", 7))
        self.assertNotIn("secret-ref", str(view))
        self.assertNotIn("base_url", str(view))
        self.assertNotIn("credential_namespace", str(view))
        self.assertNotIn(TOKENS["ADMIN"], str(view))
        # Legacy latest still reads only the Chat table, and native latest only the native one.
        self.assertEqual(
            self.p.models.snapshot(bearer("GATEWAY"), query(self.origin, None))["config_version"], 9
        )
        self.p.models.native_publish(bearer("ADMIN"), native_config(self.now, version=8))
        self.assertEqual(
            self.p.models.snapshot(bearer("GATEWAY"), query(self.origin, None))["config_version"], 9
        )
        self.assertEqual(
            self.p.models.native_snapshot(bearer("NATIVE"), native_query(self.origin, None))[
                "native_config_version"
            ],
            8,
        )

    def test_native_revocation_key_space_is_independent(self):
        self.p.models.publish(bearer("ADMIN"), config(self.now, version=7))
        self.p.models.native_publish(bearer("ADMIN"), native_config(self.now, version=7))
        self.p.models.native_revoke(bearer("ADMIN"), 7)
        # Revoking native 7 must not touch Chat 7 and vice versa.
        self.assertEqual(
            self.p.models.snapshot(bearer("GATEWAY"), query(self.origin, 7))["config_version"], 7
        )
        self.reject(
            "forbidden",
            self.p.models.native_snapshot,
            bearer("NATIVE"),
            native_query(self.origin, 7),
        )
        self.p.models.publish(bearer("ADMIN"), config(self.now, version=8))
        self.p.models.revoke(bearer("ADMIN"), 8)
        self.p.models.native_publish(bearer("ADMIN"), native_config(self.now, version=8))
        self.assertEqual(
            self.p.models.native_snapshot(bearer("NATIVE"), native_query(self.origin, 8))[
                "native_config_version"
            ],
            8,
        )
        self.reject("forbidden", self.p.models.snapshot, bearer("GATEWAY"), query(self.origin, 8))
        self.reject("not_found", self.p.models.native_revoke, bearer("ADMIN"), 9)

    def test_native_grant_list_is_explicit_and_never_inherited(self):
        self.p.models.publish(bearer("ADMIN"), config(self.now, version=9))
        self.p.models.native_publish(bearer("ADMIN"), native_config(self.now, version=8))
        self.p.models.native_publish(bearer("ADMIN"), native_config(self.now, version=9))
        # GATEWAY holds legacy config_versions only; that never authorizes native reads.
        self.reject(
            "forbidden",
            self.p.models.native_snapshot,
            bearer("GATEWAY"),
            native_query(self.origin, 8),
        )
        self.reject(
            "forbidden",
            self.p.models.native_snapshot,
            bearer("GATEWAY"),
            native_query(self.origin, None),
        )
        # NATIVE is authorized for 7 and 8; version 7 was never published, so it is not found.
        self.reject(
            "not_found",
            self.p.models.native_snapshot,
            bearer("NATIVE"),
            native_query(self.origin, 7),
        )
        self.assertEqual(
            self.p.models.native_snapshot(bearer("NATIVE"), native_query(self.origin, 8))[
                "native_config_version"
            ],
            8,
        )
        self.reject(
            "forbidden",
            self.p.models.native_snapshot,
            bearer("NATIVE"),
            native_query(self.origin, 9),
        )
        # Latest selects the highest authorized live version, not the global highest.
        self.assertEqual(
            self.p.models.native_snapshot(bearer("NATIVE"), native_query(self.origin, None))[
                "native_config_version"
            ],
            8,
        )
        self.reject("forbidden", self.p.models.snapshot, bearer("NATIVE"), query(self.origin, None))
        self.reject("forbidden", self.p.models.snapshot, bearer("NATIVE"), query(self.origin, 9))

    def test_native_expiry_and_revocation_follow_contract_statuses(self):
        value = native_config(self.now, version=7)
        value["usable_until"] = utc(self.now + 5)
        self.p.models.native_publish(bearer("ADMIN"), value)
        self.p.models.native_publish(bearer("ADMIN"), native_config(self.now, version=8))
        self.now = epoch(value["usable_until"])
        # The expired version is an unavailable dependency, so latest falls back to 8.
        self.reject(
            "dependency_unavailable",
            self.p.models.native_snapshot,
            bearer("NATIVE"),
            native_query(self.origin, 7),
        )
        self.assertEqual(
            self.p.models.native_snapshot(bearer("NATIVE"), native_query(self.origin, None))[
                "native_config_version"
            ],
            8,
        )
        self.p.models.native_revoke(bearer("ADMIN"), 8)
        # Latest never selects a revoked configuration; with nothing live it stays unavailable.
        self.reject(
            "forbidden",
            self.p.models.native_snapshot,
            bearer("NATIVE"),
            native_query(self.origin, 8),
        )
        self.reject(
            "dependency_unavailable",
            self.p.models.native_snapshot,
            bearer("NATIVE"),
            native_query(self.origin, None),
        )
        restarted = self.restart()
        self.reject(
            "forbidden",
            restarted.models.native_snapshot,
            bearer("NATIVE"),
            native_query(self.origin, 8),
        )

    def test_native_versions_are_immutable_and_monotonic(self):
        original = native_config(self.now)
        self.p.models.native_publish(bearer("ADMIN"), original)
        self.assertTrue(
            self.p.models.native_publish(
                bearer("ADMIN"), {**original, "request_id": "native-retry"}
            )["deduplicated"]
        )
        changed = native_config(self.now)
        changed["bindings"][0]["timeout_ms"] += 1
        self.reject("version_conflict", self.p.models.native_publish, bearer("ADMIN"), changed)
        self.p.models.native_publish(bearer("ADMIN"), native_config(self.now, version=9))
        self.reject(
            "version_conflict",
            self.p.models.native_publish,
            bearer("ADMIN"),
            native_config(self.now, version=8),
        )
        self.p.models.native_revoke(bearer("ADMIN"), 7)
        # A revoked native version is forbidden by the contract, never 410.
        self.reject("forbidden", self.p.models.native_publish, bearer("ADMIN"), original)

    def test_native_publication_rejects_local_credentials_and_bad_windows(self):
        for before, after in ((10, 20), (-10, -1), (-3600, 100), (0, 0)):
            value = native_config(self.now, version=8)
            value.update(published_at=utc(self.now + before), usable_until=utc(self.now + after))
            self.reject("invalid_input", self.p.models.native_publish, bearer("ADMIN"), value)
        # A deployment that wrongly registers a live credential still cannot publish it.
        for field in ("credential_namespace", "credential_ref"):
            with self.subTest(field=field):
                local = settings(self.temp.name)
                local["database_path"] = str(Path(self.temp.name) / (field + ".sqlite"))
                value = native_config(self.now, version=8)
                value["providers"][0][field] = (
                    TOKENS["ADMIN"]
                    if field == "credential_namespace"
                    else "secret-ref:fixture/" + TOKENS["READER"]
                )
                local["providers"][NATIVE_PROVIDER][field] = value["providers"][0][field]
                platform = Platform(local, clock=lambda: self.now)
                self.reject("invalid_input", platform.models.native_publish, bearer("ADMIN"), value)

    def test_native_publication_is_operator_only_and_stores_no_plaintext_key(self):
        self.assertEqual(self.p.models.view_native(bearer("ADMIN"))["availability"], "unconfigured")
        self.reject(
            "forbidden", self.p.models.native_publish, bearer("GATEWAY"), native_config(self.now)
        )
        self.reject("forbidden", self.p.models.view_native, bearer("GATEWAY"))
        self.reject("forbidden", self.p.models.native_revoke, bearer("GATEWAY"), 7)
        self.p.models.native_publish(bearer("ADMIN"), native_config(self.now))
        with self.p.store.connect() as db:
            row = db.execute("SELECT document FROM native_configs WHERE version=7").fetchone()
        stored = row[0]
        self.assertNotIn(TOKENS["ADMIN"], stored)
        self.assertNotIn(TOKENS["UPSTREAM"], stored)
        self.assertIn("secret-ref:", stored)
        self.assertEqual(self.p.models.view_native(bearer("ADMIN"))["availability"], "available")

    def test_native_restart_keeps_the_same_immutable_snapshot(self):
        self.p.models.native_publish(bearer("ADMIN"), native_config(self.now))
        first = self.p.models.native_snapshot(bearer("NATIVE"), native_query(self.origin, 7))
        second = self.restart().models.native_snapshot(
            bearer("NATIVE"), native_query(self.origin, 7)
        )
        self.assertEqual(first, second)
        self.p.contracts.check("model-protocol#config_response", second)
        self.assertEqual(second["request_id"], "native-snapshot-test")
        self.assertEqual(second["contract"], "model-protocol/v1")
        self.assertNotIn("config_version", second)
        # A tampered native row is reported as an unavailable dependency, not served.
        with self.p.store.connect(write=True) as db:
            db.execute("UPDATE native_configs SET digest=? WHERE version=7", (digest({"x": 1}),))
        restarted = self.restart()
        self.reject(
            "dependency_unavailable",
            restarted.models.native_snapshot,
            bearer("NATIVE"),
            native_query(self.origin, 7),
        )

    def test_native_store_migration_backs_up_a_version_one_database(self):
        legacy = Path(self.temp.name) / "legacy.sqlite"
        with closing(sqlite3.connect(legacy)) as db:
            db.executescript(
                "CREATE TABLE origins(ref TEXT PRIMARY KEY, entry_id TEXT NOT NULL,"
                " entry_digest TEXT NOT NULL, expires_at REAL NOT NULL,"
                " revoked INTEGER NOT NULL DEFAULT 0);"
                " CREATE TABLE configs(version INTEGER PRIMARY KEY, document TEXT NOT NULL,"
                " digest TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);"
                " PRAGMA user_version=1;"
            )
            db.execute(
                "INSERT INTO configs VALUES(?,?,?,0)",
                (7, json.dumps(config(self.now), sort_keys=True), digest(config(self.now))),
            )
            db.commit()
        self.settings["database_path"] = str(legacy)
        migrated = Platform(self.settings, clock=lambda: self.now)
        backups = list(Path(self.temp.name).glob("legacy.sqlite.pre-native-*.sqlite"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
        with migrated.store.connect() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM configs").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM native_configs").fetchone()[0], 0)
        migrated.models.native_publish(bearer("ADMIN"), native_config(self.now))
        origin = migrated.origins.issue(bearer("ADMIN"), "config-entry")["assertion_ref"]
        self.assertEqual(
            migrated.models.native_snapshot(bearer("NATIVE"), native_query(origin, 7))[
                "native_config_version"
            ],
            7,
        )


class NativeHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)
        self.now = time.time()
        self.settings = settings(self.temp.name)
        self.settings["native_config_http"] = True
        self.p = Platform(self.settings, clock=lambda: self.now)
        self.origin = self.p.origins.issue(bearer("ADMIN"), "config-entry")["assertion_ref"]
        self.runner, self.url = await start_http(create_app(self.p))
        self.addAsyncCleanup(self.runner.cleanup)
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8), trust_env=False)
        self.addAsyncCleanup(self.client.close)

    async def post(
        self, body, identity="NATIVE", expected=200, headers=None, url=None, raw=None, echo=True
    ):
        async with self.client.post(
            (url or self.url) + "/internal/v1/model-config/native/snapshot",
            json=body if raw is None else None,
            data=raw,
            headers={
                "Authorization": bearer(identity),
                "Content-Type": "application/json",
                **(headers or {}),
            },
        ) as response:
            self.assertEqual(response.status, expected, await response.text())
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            result = await response.json()
            if expected == 200:
                self.p.contracts.check("model-protocol#config_response", result)
            else:
                self.p.contracts.check("model-protocol#error", result)
                if echo:
                    # Correlation is adopted only for a body that matches the native schema.
                    self.assertEqual(result["request_id"], body["query"]["request_id"])
                else:
                    self.assertTrue(result["request_id"].startswith("request:"))
                self.assertEqual(result["retryable"], False)
            return result

    async def test_real_http_native_snapshot_and_contract_errors(self):
        self.p.models.native_publish(bearer("ADMIN"), native_config(self.now))
        response = await self.post(native_query(self.origin))
        self.assertEqual(response["native_config_version"], 7)
        self.assertEqual(response["contract"], "model-protocol/v1")
        self.assertNotIn("config_version", response)
        # An unpinned native read resolves the highest authorized live version.
        self.assertEqual(
            (await self.post(native_query(self.origin, None)))["native_config_version"], 7
        )
        # Legacy Chat reads on the native port and native reads on the legacy port both fail.
        await self.post(query(self.origin), expected=400, echo=False)
        async with self.client.post(
            self.url + "/internal/v1/model-config/snapshot",
            json=native_query(self.origin),
            headers={"Authorization": bearer("GATEWAY")},
        ) as response:
            self.assertEqual(response.status, 400)
            self.p.contracts.check("common#error", await response.json())
        # Forged authorization fields are rejected by the schema, not honoured.
        await self.post(
            {**native_query(self.origin), "principal_id": "admin"}, expected=400, echo=False
        )
        await self.post(
            {**native_query(self.origin), "contract": "text-dialogue/v1"},
            expected=400,
            echo=False,
        )
        await self.post(native_query(self.origin, 9), expected=403)
        await self.post(native_query(self.origin, 6), expected=403)
        await self.post(native_query(self.origin, 8), expected=404)
        await self.post(native_query(self.origin), "GATEWAY", 403)
        self.p.models.native_revoke(bearer("ADMIN"), 7)
        await self.post(native_query(self.origin, 7), expected=403)
        # With nothing live left for this caller, latest is an unavailable dependency.
        await self.post(native_query(self.origin, None), expected=503)

    async def test_http_boundary_stays_closed_to_browsers_and_bad_input(self):
        self.p.models.native_publish(bearer("ADMIN"), native_config(self.now))
        # Browser sessions are refused before the body is ever correlated.
        await self.post(
            native_query(self.origin),
            expected=403,
            headers={"Origin": "http://localhost"},
            echo=False,
        )
        await self.post(
            native_query(self.origin),
            expected=403,
            headers={"Cookie": "session=fake"},
            echo=False,
        )
        async with self.client.post(
            self.url + "/internal/v1/model-config/native/snapshot",
            json=native_query(self.origin),
        ) as response:
            self.assertEqual(response.status, 401)
        async with self.client.post(
            self.url + "/internal/v1/model-config/native/snapshot",
            json=native_query(self.origin),
            headers=[
                ("Authorization", bearer("NATIVE")),
                ("Authorization", bearer("ADMIN")),
            ],
        ) as response:
            self.assertEqual(response.status, 401)
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'"\xff"', b"[]"):
            async with self.client.post(
                self.url + "/internal/v1/model-config/native/snapshot",
                data=raw,
                headers={
                    "Authorization": bearer("NATIVE"),
                    "Content-Type": "application/json",
                },
            ) as response:
                self.assertEqual(response.status, 400)
                self.p.contracts.check("model-protocol#error", await response.json())
        await self.post(
            native_query(self.origin),
            raw=io.BytesIO(b" " * 1_048_577),
            expected=413,
            echo=False,
        )

    async def test_closed_native_port_is_absent_and_never_serves_configuration(self):
        self.p.models.publish(bearer("ADMIN"), config(self.now, version=7))
        self.p.models.native_publish(bearer("ADMIN"), native_config(self.now))
        closed = Platform({**self.settings, "native_config_http": False}, clock=lambda: self.now)
        runner, url = await start_http(create_app(closed))
        self.addAsyncCleanup(runner.cleanup)
        async with self.client.post(
            url + "/internal/v1/model-config/native/snapshot",
            json=native_query(self.origin),
            headers={"Authorization": bearer("NATIVE")},
        ) as response:
            self.assertEqual(response.status, 404)
            body = await response.json()
            self.p.contracts.check("model-protocol#error", body)
            self.assertEqual(body["code"], "not_found")
        # The legacy Chat port keeps serving on the very same closed deployment.
        async with self.client.post(
            url + "/internal/v1/model-config/snapshot",
            json=query(self.origin),
            headers={"Authorization": bearer("GATEWAY")},
        ) as response:
            self.assertEqual(response.status, 200)
            self.p.contracts.check("model#config_response", await response.json())

    async def test_http_restart_serves_the_same_native_snapshot(self):
        self.p.models.native_publish(bearer("ADMIN"), native_config(self.now))
        first = await self.post(native_query(self.origin))
        await self.runner.cleanup()
        self.runner, self.url = await start_http(
            create_app(Platform(self.settings, clock=lambda: self.now))
        )
        self.addAsyncCleanup(self.runner.cleanup)
        self.assertEqual(first, await self.post(native_query(self.origin)))


class NativeCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = time.time()
        self.settings_path = Path(self.temp.name) / "settings.json"
        self.settings_path.write_text(json.dumps(settings(self.temp.name)), encoding="utf-8")
        self.input_path = Path(self.temp.name) / "input.json"

    def cli(self, action, body=None, identity="ADMIN"):
        command = [
            sys.executable,
            "-m",
            "services.platform",
            "--settings",
            str(self.settings_path),
            "local",
            "--credential-env",
            "TS012_" + identity,
            action,
        ]
        if body is not None:
            self.input_path.write_text(json.dumps(body), encoding="utf-8")
            command += ["--input", str(self.input_path)]
        return subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, **ENV},
        )

    def test_actual_cli_publish_revoke_and_redacted_view(self):
        denied = self.cli(
            "publish-native", native_config(self.now, request_id="native-cli"), "NATIVE"
        )
        self.assertEqual(denied.returncode, 1)
        self.assertEqual(json.loads(denied.stdout)["code"], "forbidden")
        published = self.cli("publish-native", native_config(self.now, request_id="native-cli"))
        self.assertEqual(published.returncode, 0, published.stderr)
        self.assertEqual(
            json.loads(published.stdout),
            {"native_config_version": 7, "published": True, "deduplicated": False},
        )
        retried = self.cli("publish-native", native_config(self.now, request_id="native-cli"))
        self.assertEqual(json.loads(retried.stdout)["deduplicated"], True)
        view = self.cli("view-native")
        self.assertEqual(view.returncode, 0, view.stderr)
        self.assertEqual(json.loads(view.stdout)["native_config_version"], 7)
        self.assertNotIn("secret-ref:", view.stdout)
        self.assertNotIn("base_url", view.stdout)
        self.assertNotIn(TOKENS["ADMIN"], view.stdout)
        self.assertNotIn(TOKENS["UPSTREAM"], view.stdout)
        revoked = self.cli("revoke-native", {"id": 7})
        self.assertEqual(revoked.returncode, 0, revoked.stderr)
        self.assertEqual(json.loads(revoked.stdout), {"revoked": True})
        self.assertEqual(json.loads(self.cli("view-native").stdout)["availability"], "revoked")
        missing = self.cli("revoke-native", {"id": 8})
        self.assertEqual(json.loads(missing.stdout)["code"], "not_found")
        # The CLI still refuses a payload that claims its own identity.
        forged = self.cli(
            "publish-native",
            {**native_config(self.now, request_id="native-cli"), "principal_id": "admin"},
        )
        self.assertEqual(json.loads(forged.stdout)["code"], "invalid_input")


if __name__ == "__main__":
    unittest.main()

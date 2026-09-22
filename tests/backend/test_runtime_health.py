"""The read-only probes: real HTTP and real TLS, a closed document, and nothing written.

Every readiness answer here is produced by the real server over a real socket. Certificates are
really parsed, the authority store is really opened read-only, and the log is really inspected for
the absence of anything a probe might have written.
"""

import json
import os
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import aiohttp

from runtime_fixture import (
    READY_TOKEN,
    READY_TOKEN_ENV,
    built_static,
    certificates,
    environment,
    log_files,
    read_events,
    require_contract,
    runtime_settings,
    start_server,
)

from services.platform import diagnostics, diagnostics_config, runtime_health
from services.platform.server import create_app
from services.platform.service import Platform, registered_credentials

LIVE = "/health/live"
READY = "/health/ready"


def runtime_available():
    return os.environ.get("TS013_TLS_PYTHON") is not None


def authorization(token=READY_TOKEN):
    return {"Authorization": "Bearer " + token}


class ProbeTestCase(unittest.IsolatedAsyncioTestCase):
    """A real deployment on a real socket, with the pieces a test needs to break on purpose."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.env = patch.dict(os.environ, environment(), clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(diagnostics.reset)
        self.settings = runtime_settings(
            self.temp.name, static=built_static(self.directory / "static")
        )
        self.platform = None
        self.runner = None
        self.client = None
        self.stream = None

    async def boot(self, settings=None):
        """Assemble the real composition root and serve it; TLS when the settings say so."""
        settings = self.settings if settings is None else settings
        self.settings = settings
        self.platform = Platform(settings)
        self.sink = diagnostics.Diagnostics(settings["diagnostics"].get("log_directory"))
        diagnostics.activate(self.sink)
        self.addCleanup(self.sink.close, 2.0)
        context = None
        if settings.get("mode") == "service_https":
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(
                settings["tls"]["certificate_file"], settings["tls"]["private_key_file"]
            )
            self.trusted = ssl.create_default_context(cafile=settings["tls"]["certificate_file"])
        self.runner, self.url = await start_server(create_app(self.platform), tls=context)
        self.addAsyncCleanup(self.runner.cleanup)
        self.client = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            trust_env=False,
            connector=aiohttp.TCPConnector(ssl=self.trusted) if context else None,
        )
        self.addAsyncCleanup(self.client.close)
        return self.url

    async def probe(self, path, token=READY_TOKEN, method="GET"):
        headers = {} if token is None else authorization(token)
        async with self.client.request(method, self.url + path, headers=headers) as response:
            body = await response.text()
            parsed = json.loads(body) if body else None
            return response, parsed

    def events(self):
        return read_events(self.settings["diagnostics"].get("log_directory") or self.directory)


class LivenessTests(ProbeTestCase):
    async def test_liveness_is_public_and_says_only_that_the_loop_answered(self):
        await self.boot()
        response, body = await self.probe(LIVE, token=None)
        self.assertEqual(response.status, 200)
        self.assertEqual(body, {"status": "alive"})
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_liveness_stays_alive_while_readiness_is_red(self):
        # No durable log and no store yet: liveness must not be a proxy for business capability.
        settings = runtime_settings(
            self.temp.name, diagnostics={"contract_directory": str(require_contract())}
        )
        await self.boot(settings)
        live, body = await self.probe(LIVE, token=None)
        ready, document = await self.probe(READY)
        self.assertEqual(live.status, 200)
        self.assertEqual(body, {"status": "alive"})
        self.assertEqual(ready.status, 503)
        self.assertEqual(document["status"], "not_ready")

    async def test_liveness_never_reports_a_dependency_or_a_version(self):
        await self.boot()
        _, body = await self.probe(LIVE, token=None)
        self.assertEqual(set(body), {"status"})

    async def test_a_probe_path_is_not_a_place_to_send_anything_else(self):
        await self.boot()
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            response, body = await self.probe(LIVE, token=None, method=method)
            self.assertEqual(response.status, 404, method)
            self.assertNotIn("alive", json.dumps(body))

    async def test_head_is_answered_without_a_body(self):
        await self.boot()
        async with self.client.head(self.url + LIVE) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.text(), "")


@unittest.skipUnless(runtime_available(), "TS013_TLS_PYTHON is not configured")
class ReadinessTests(ProbeTestCase):
    async def test_readiness_requires_its_own_credential(self):
        await self.boot()
        missing, body = await self.probe(READY, token=None)
        self.assertEqual(missing.status, 401)
        self.assertEqual(body, {"code": "unauthorized"})
        for wrong in ("", "wrong", READY_TOKEN + "x", READY_TOKEN.upper()):
            response, _ = await self.probe(READY, token=wrong)
            self.assertEqual(response.status, 401, wrong)
        allowed, document = await self.probe(READY)
        self.assertEqual(allowed.status, 200)
        self.assertEqual(document["status"], "ready")

    async def test_a_server_without_a_configured_token_says_so_instead_of_passing(self):
        with patch.dict(os.environ, {READY_TOKEN_ENV: ""}, clear=False):
            os.environ.pop(READY_TOKEN_ENV, None)
            await self.boot()
            response, body = await self.probe(READY)
        self.assertEqual(response.status, 503)
        self.assertEqual(body, {"code": "dependency_unavailable"})
        live, _ = await self.probe(LIVE, token=None)
        self.assertEqual(live.status, 200)

    async def test_another_identitys_credential_is_not_a_readiness_credential(self):
        await self.boot()
        for name, value in environment().items():
            if name.startswith("TS012_"):
                response, _ = await self.probe(READY, token=value)
                self.assertEqual(response.status, 401, name)

    async def test_a_green_readiness_answer_means_every_check_passed(self):
        await self.boot()
        _, document = await self.probe(READY)
        self.assertEqual(set(document), {"status", "service", "checks"})
        self.assertEqual(document["service"], "platform")
        self.assertEqual(tuple(document["checks"]), runtime_health.CHECK_KEYS)
        self.assertEqual(set(document["checks"].values()), {"ok"})
        self.assertEqual(document["status"], "ready")

    async def test_the_document_is_closed_and_carries_no_configured_value(self):
        await self.boot()
        async with self.client.get(self.url + READY, headers=authorization()) as response:
            raw = await response.text()
        document = json.loads(raw)
        self.assertEqual(set(document), {"status", "service", "checks"})
        self.assertEqual(tuple(document["checks"]), runtime_health.CHECK_KEYS)
        for value in document["checks"].values():
            self.assertIn(value, runtime_health.CHECK_VALUES)
        for secret in (
            READY_TOKEN,
            self.temp.name,
            self.settings["database_path"],
            self.settings["tls"]["private_key_file"],
            "TS012_",
        ):
            self.assertNotIn(secret, raw)

    async def test_the_readiness_answer_is_bounded_and_does_not_wait_without_limit(self):
        await self.boot()
        started = time.monotonic()
        await self.probe(READY)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, runtime_health.BUDGET_SECONDS + 2.0)

    async def test_a_probe_that_cannot_get_in_reports_unverified_rather_than_waiting(self):
        await self.boot()
        probe = runtime_health.Probe(self.platform.health, budget=0.05, cache_seconds=0.0)
        await probe._lock.acquire()
        try:
            document = await probe.ready()
        finally:
            probe._lock.release()
        self.assertEqual(document["status"], "not_ready")
        self.assertEqual(set(document["checks"].values()), {"not_verified"})

    async def test_a_stale_green_is_not_reused_as_a_current_fact(self):
        await self.boot()
        now = {"value": 1000.0}
        probe = runtime_health.Probe(
            self.platform.health, clock=lambda: now["value"], cache_seconds=1.0
        )
        first = await probe.ready()
        self.assertEqual(first["status"], "ready")
        now["value"] += 0.5
        self.assertIs(await probe.ready(), first)
        now["value"] += 2.0
        second = await probe.ready()
        self.assertIsNot(second, first)
        self.assertEqual(second["checks"], first["checks"])


@unittest.skipUnless(runtime_available(), "TS013_TLS_PYTHON is not configured")
class ReadinessFailureTests(ProbeTestCase):
    """Each way a deployment can be wrong must be red, and must say which check is red."""

    def standalone(self, settings):
        """Readiness for a deployment that cannot even be served, judged from its settings alone.

        Some faults are exactly the ones that stop a server from coming up - an expired
        certificate, a store that is not a database - so asserting them over a socket would test
        the handshake rather than the check. The composition root is still never constructed here:
        constructing it would create or migrate the very store under test.
        """
        inputs = runtime_health.health_inputs(
            settings,
            credential_names=registered_credentials(settings),
            credential_present=lambda name: os.environ.get(name) is not None,
        )
        return runtime_health.Probe(inputs, cache_seconds=0.0).evaluate()

    def assert_red(self, document, check, value):
        self.assertEqual(document["checks"][check], value)
        self.assertEqual(document["status"], "not_ready")

    async def red(self, settings, check, value):
        """A deployment that can boot: judge it through the very inputs its server was given."""
        await self.boot(settings)
        document = await runtime_health.Probe(self.platform.health, cache_seconds=0.0).ready()
        self.assert_red(document, check, value)
        response, served = await self.probe(READY)
        self.assertEqual(response.status, 503)
        self.assertEqual(served["status"], "not_ready")
        self.assertEqual(served["checks"][check], value)
        return document

    async def test_an_expired_certificate_is_never_ready(self):
        self.assert_red(
            self.standalone(runtime_settings(self.temp.name, tls_name="expired")), "tls", "failed"
        )

    async def test_a_certificate_that_is_not_yet_valid_is_never_ready(self):
        self.assert_red(
            self.standalone(runtime_settings(self.temp.name, tls_name="future")), "tls", "failed"
        )

    async def test_a_missing_certificate_file_is_never_ready(self):
        settings = runtime_settings(self.temp.name)
        settings["tls"] = {
            "certificate_file": str(self.directory / "nowhere.pem"),
            "private_key_file": str(self.directory / "nowhere-key.pem"),
        }
        self.assert_red(self.standalone(settings), "tls", "failed")

    async def test_a_mismatched_key_pair_is_never_ready(self):
        keys = certificates(self.directory / "tls")
        settings = runtime_settings(self.temp.name)
        settings["tls"] = {
            "certificate_file": keys["valid"]["certificate_file"],
            "private_key_file": keys["expired"]["private_key_file"],
        }
        self.assert_red(self.standalone(settings), "tls", "failed")

    async def test_a_store_that_was_never_created_is_never_ready(self):
        settings = runtime_settings(self.temp.name)
        self.assertFalse(Path(settings["database_path"]).exists())
        self.assert_red(self.standalone(settings), "store", "failed")

    async def test_a_store_with_a_schema_this_build_cannot_read_is_never_ready(self):
        settings = runtime_settings(self.temp.name)
        database = sqlite3.connect(settings["database_path"])
        database.execute("PRAGMA user_version=99")
        database.execute("CREATE TABLE origins (id TEXT)")
        database.commit()
        database.close()
        self.assert_red(self.standalone(settings), "store", "failed")

    async def test_a_store_without_its_authority_tables_is_never_ready(self):
        settings = runtime_settings(self.temp.name)
        database = sqlite3.connect(settings["database_path"])
        database.execute("CREATE TABLE unrelated (id TEXT)")
        database.commit()
        database.close()
        self.assert_red(self.standalone(settings), "store", "failed")

    async def test_a_file_that_is_not_a_database_is_never_ready(self):
        settings = runtime_settings(self.temp.name)
        Path(settings["database_path"]).write_bytes(b"this is not a sqlite database")
        self.assert_red(self.standalone(settings), "store", "failed")

    async def test_a_sidecar_that_cannot_be_read_is_never_ready(self):
        await self.boot()
        sidecar = self.settings["database_path"] + ".web-inputs.sqlite"
        Path(sidecar).write_bytes(b"not a database either")
        probe = runtime_health.Probe(self.platform.health, cache_seconds=0.0)
        document = await probe.ready()
        self.assertEqual(document["checks"]["sidecars"], "failed")
        self.assertEqual(document["status"], "not_ready")

    async def test_an_incomplete_build_is_never_ready(self):
        static = self.directory / "partial"
        static.mkdir()
        (static / "index.html").write_text(
            '<!doctype html><script src="/assets/missing.js"></script>', encoding="utf-8"
        )
        settings = runtime_settings(self.temp.name, static=str(static))
        await self.red(settings, "web_static", "failed")

    async def test_an_entry_point_with_no_assets_at_all_is_never_ready(self):
        static = self.directory / "placeholder"
        static.mkdir()
        (static / "index.html").write_text("<!doctype html><title>placeholder</title>", "utf-8")
        settings = runtime_settings(self.temp.name, static=str(static))
        await self.red(settings, "web_static", "failed")

    async def test_an_unverifiable_contract_is_never_ready(self):
        settings = runtime_settings(self.temp.name)
        settings["diagnostics"]["contract_directory"] = str(self.directory / "absent-contract")
        await self.red(settings, "contract", "failed")

    async def test_a_contract_that_is_not_the_reviewed_bytes_is_never_ready(self):
        tampered = self.directory / "tampered"
        tampered.mkdir()
        source = require_contract()
        for name in diagnostics_config.CONTRACT_FILES + ("manifest.json",):
            (tampered / name).write_bytes((source / name).read_bytes())
        (tampered / "event.schema.json").write_bytes(b'{"type":"object"}\r\n')
        settings = runtime_settings(self.temp.name)
        settings["diagnostics"]["contract_directory"] = str(tampered)
        await self.red(settings, "contract", "failed")

    async def test_a_production_entry_point_without_a_contract_is_never_ready(self):
        settings = runtime_settings(self.temp.name)
        del settings["diagnostics"]["contract_directory"]
        with patch.dict(os.environ, {"TIANSHU_DIAGNOSTICS_CONTRACT_DIR": ""}, clear=False):
            os.environ.pop("TIANSHU_DIAGNOSTICS_CONTRACT_DIR", None)
            await self.red(settings, "contract", "failed")

    async def test_a_credential_that_is_not_in_the_environment_is_never_ready(self):
        await self.boot()
        with patch.dict(os.environ, {}, clear=False):
            for name in self.platform.other_credentials:
                os.environ.pop(name, None)
            probe = runtime_health.Probe(self.platform.health, cache_seconds=0.0)
            document = await probe.ready()
        self.assertEqual(document["checks"]["credentials"], "failed")
        self.assertEqual(document["status"], "not_ready")

    async def test_development_logging_is_reported_as_non_durable_and_never_green(self):
        settings = runtime_settings(
            self.temp.name,
            diagnostics={"contract_directory": str(require_contract())},
            static=built_static(self.directory / "static"),
        )
        await self.boot(settings)
        response, document = await self.probe(READY)
        self.assertEqual(document["checks"]["logging"], "non_durable")
        self.assertEqual(document["status"], "not_ready")
        self.assertEqual(response.status, 503)

    async def test_a_log_sink_that_failed_is_never_ready(self):
        await self.boot()
        self.sink._fail("log_write_failed")
        probe = runtime_health.Probe(self.platform.health, cache_seconds=0.0)
        document = await probe.ready()
        self.assertEqual(document["checks"]["logging"], "failed")
        self.assertEqual(document["status"], "not_ready")

    async def test_a_runtime_that_is_no_longer_running_is_never_ready(self):
        await self.boot()
        self.platform.close()
        probe = runtime_health.Probe(self.platform.health, cache_seconds=0.0)
        document = await probe.ready()
        self.assertEqual(document["checks"]["runtime"], "failed")
        self.assertEqual(document["status"], "not_ready")


@unittest.skipUnless(runtime_available(), "TS013_TLS_PYTHON is not configured")
class ProbePurityTests(ProbeTestCase):
    async def test_a_probe_writes_no_event_of_its_own(self):
        await self.boot()
        await self.probe(LIVE, token=None)
        await self.probe(READY)
        await self.probe(READY, token=None)
        self.sink.flush()
        # The sink opens its first segment when it is assembled, so an empty file is expected; a
        # single record in it would mean a probe had written to the log.
        self.assertEqual(read_events(self.settings["diagnostics"]["log_directory"]), [])
        for path in log_files(self.settings["diagnostics"]["log_directory"]):
            self.assertEqual(path.stat().st_size, 0)

    async def test_business_traffic_is_logged_while_probes_stay_silent(self):
        from fixtures import bearer

        await self.boot()
        async with self.client.post(
            self.url + "/internal/v1/origins/resolve",
            json={},
            headers={"Authorization": bearer("ADMIN")},
        ) as response:
            self.assertEqual(response.status, 400)
        await self.probe(READY)
        self.sink.flush()
        names = [
            record["event"] for record in read_events(self.settings["diagnostics"]["log_directory"])
        ]
        self.assertIn("http.request.started", names)
        self.assertIn("http.request.finished", names)
        # The probe that ran after the business request added nothing to that stream.
        self.assertEqual(names.count("http.request.started"), 1)

    async def test_a_probe_creates_no_database_and_no_sidecar(self):
        settings = runtime_settings(self.temp.name)
        await self.boot(settings)
        database = Path(settings["database_path"])
        self.assertTrue(database.is_file())
        # These exist because the composition root booted, not because a probe touched them.
        for suffix in (".web-inputs.sqlite", ".web-replies.sqlite", ".web-models.sqlite"):
            self.assertTrue(Path(settings["database_path"] + suffix).is_file(), suffix)
        # No household is registered here, so no control ledger exists and none is demanded.
        self.assertFalse(Path(settings["database_path"] + ".home-controls.sqlite").exists())

    def test_only_the_ledgers_this_deployment_owns_are_asked_about(self):
        settings = runtime_settings(self.temp.name)
        inputs = runtime_health.health_inputs(settings)
        self.assertEqual(
            [kind for _, kind in inputs.sidecars], ["web-inputs", "web-replies", "web-models"]
        )
        settings["home"] = {"enabled": True}
        self.assertIn(
            "home-controls", [kind for _, kind in runtime_health.health_inputs(settings).sidecars]
        )
        settings["home"] = {"enabled": False}
        self.assertNotIn(
            "home-controls", [kind for _, kind in runtime_health.health_inputs(settings).sidecars]
        )
        without_web = dict(settings)
        without_web.pop("web")
        self.assertEqual(runtime_health.health_inputs(without_web).sidecars, ())

    async def test_the_store_is_opened_read_only_and_never_as_an_immutable_snapshot(self):
        await self.boot()
        database = Path(self.settings["database_path"])
        uri = runtime_health._readonly_uri(database)
        self.assertIn("mode=ro", uri)
        self.assertNotIn("immutable", uri)
        before = (database.stat().st_size, database.stat().st_mtime_ns)
        names = self.tables(database)
        version = self.version(database)
        await runtime_health.Probe(self.platform.health, cache_seconds=0.0).ready()
        self.assertEqual((database.stat().st_size, database.stat().st_mtime_ns), before)
        self.assertEqual(self.tables(database), names)
        self.assertEqual(self.version(database), version)

    async def test_a_probe_never_migrates_the_authority_store(self):
        settings = runtime_settings(self.temp.name)
        database = sqlite3.connect(settings["database_path"])
        database.execute("CREATE TABLE origins (id TEXT)")
        database.execute("PRAGMA user_version=0")
        database.commit()
        database.close()
        before = self.tables(Path(settings["database_path"]))
        report = runtime_health.preflight(settings)
        self.assertEqual(report["status"], "not_ready")
        self.assertEqual(self.tables(Path(settings["database_path"])), before)

    async def test_preflight_never_creates_the_database_it_is_asked_about(self):
        settings = runtime_settings(self.temp.name)
        database = Path(settings["database_path"])
        self.assertFalse(database.exists())
        report = runtime_health.preflight(settings)
        self.assertFalse(database.exists())
        self.assertTrue(report["requires_initialization"])
        self.assertEqual(report["checks"]["store"], "not_configured")

    def tables(self, database):
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            return sorted(
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            )
        finally:
            connection.close()

    def version(self, database):
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            return connection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            connection.close()


@unittest.skipUnless(runtime_available(), "TS013_TLS_PYTHON is not configured")
class RealTlsTests(ProbeTestCase):
    async def test_a_real_tls_handshake_carries_the_readiness_document(self):
        await self.boot()
        self.assertTrue(self.url.startswith("https://"))
        response, document = await self.probe(READY)
        self.assertEqual(response.status, 200)
        self.assertEqual(document["status"], "ready")
        self.assertEqual(set(document), {"status", "service", "checks"})

    async def test_a_plain_http_client_cannot_read_the_production_probe(self):
        await self.boot()
        async with aiohttp.ClientSession(trust_env=False) as plain:
            with self.assertRaises(aiohttp.ClientError):
                async with plain.get(self.url + READY, headers=authorization()):
                    pass

    async def test_an_untrusted_client_is_refused_before_any_document_is_sent(self):
        await self.boot()
        untrusted = aiohttp.ClientSession(
            trust_env=False, connector=aiohttp.TCPConnector(ssl=ssl.create_default_context())
        )
        self.addAsyncCleanup(untrusted.close)
        with self.assertRaises(aiohttp.ClientError):
            async with untrusted.get(self.url + READY, headers=authorization()):
                pass

    async def test_liveness_over_tls_is_public_and_bounded(self):
        await self.boot()
        started = time.monotonic()
        response, body = await self.probe(LIVE, token=None)
        self.assertEqual(response.status, 200)
        self.assertEqual(body, {"status": "alive"})
        self.assertLess(time.monotonic() - started, 5.0)


class PreflightTests(ProbeTestCase):
    def settings_file(self, settings):
        path = self.directory / "settings.json"
        path.write_text(json.dumps(settings), encoding="utf-8")
        return path

    def test_a_first_boot_is_reported_as_needing_initialization_not_as_broken(self):
        settings = runtime_settings(self.temp.name, static=built_static(self.directory / "static"))
        report = runtime_health.preflight(
            settings,
            credential_names=registered_credentials(settings),
            credential_present=lambda name: os.environ.get(name) is not None,
        )
        self.assertEqual(report["status"], "not_ready")
        self.assertTrue(report["requires_initialization"])
        self.assertEqual(report["checks"]["store"], "not_configured")
        self.assertEqual(report["checks"]["sidecars"], "not_configured")
        self.assertEqual(report["reasons"], ["requires_initialization"])

    def test_a_deployment_that_already_booted_reports_ready(self):
        settings = runtime_settings(self.temp.name)
        # A real boot is what creates the authority store and the ledgers the console owns.
        create_app(Platform(settings))
        report = runtime_health.preflight(
            settings,
            credential_names=registered_credentials(settings),
            credential_present=lambda name: os.environ.get(name) is not None,
        )
        self.assertEqual(report["status"], "ready", report["reasons"])
        self.assertFalse(report["requires_initialization"])
        self.assertEqual(report["reasons"], [])

    def test_the_report_is_closed_and_its_reasons_are_sorted_and_deduplicated(self):
        settings = runtime_settings(self.temp.name, tls_name="expired")
        report = runtime_health.preflight(settings)
        self.assertEqual(
            set(report), {"status", "service", "checks", "reasons", "requires_initialization"}
        )
        self.assertEqual(report["reasons"], sorted(set(report["reasons"])))
        self.assertIn("tls_not_usable", report["reasons"])
        for value in report["checks"].values():
            self.assertIn(value, runtime_health.CHECK_VALUES)

    def test_the_preflight_command_exits_zero_only_for_a_ready_deployment(self):
        settings = runtime_settings(self.temp.name)
        path = self.settings_file(settings)
        first = self.run_cli(path)
        self.assertEqual(first.returncode, 1, first.stdout)
        self.assertTrue(json.loads(first.stdout)["requires_initialization"])
        # A real boot is what creates the authority store and the ledgers the console owns; until
        # then the honest answer is "this deployment has not started yet", not "ready".
        create_app(Platform(settings))
        second = self.run_cli(path)
        self.assertEqual(second.returncode, 0, second.stdout)
        self.assertEqual(json.loads(second.stdout)["status"], "ready")

    def test_the_preflight_command_starts_no_server_and_writes_no_log(self):
        settings = runtime_settings(self.temp.name)
        path = self.settings_file(settings)
        self.run_cli(path)
        logs = Path(settings["diagnostics"]["log_directory"])
        self.assertFalse(logs.exists() and log_files(logs))

    def run_cli(self, path):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "services.platform",
                "--settings",
                str(path),
                "preflight",
            ],
            cwd=str(Path(__file__).resolve().parents[2]),
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, **environment()},
        )


class ProbeUnitTests(unittest.TestCase):
    """Checks that need no server: the closed vocabulary and the honest unknowns."""

    def test_the_check_vocabulary_is_closed(self):
        self.assertEqual(len(runtime_health.CHECK_KEYS), 9)
        self.assertEqual(len(set(runtime_health.CHECK_KEYS)), 9)
        self.assertEqual(
            set(runtime_health.CHECK_VALUES),
            {"ok", "failed", "not_configured", "not_verified", "non_durable"},
        )
        self.assertLessEqual(set(runtime_health.BLOCKING), set(runtime_health.CHECK_VALUES))

    def test_an_unknown_is_reported_as_unverified_and_blocks_readiness(self):
        inputs = runtime_health.HealthInputs(mode="local_rehearsal")
        document = runtime_health.Probe(inputs).evaluate()
        self.assertEqual(set(document["checks"]), set(runtime_health.CHECK_KEYS))
        self.assertEqual(document["checks"]["logging"], "not_verified")
        self.assertEqual(document["checks"]["runtime"], "not_verified")
        self.assertEqual(document["checks"]["credentials"], "not_configured")
        self.assertEqual(document["status"], "not_ready")

    def test_the_probe_module_cannot_write_anything(self):
        source = Path(runtime_health.__file__).read_text(encoding="utf-8")
        # Every statement that could create, migrate, mutate or delete, in the exact spelling the
        # module would have to use. The read-only SQLite URI is asserted separately and positively.
        for forbidden in (
            "INSERT INTO",
            "UPDATE ",
            "DELETE FROM",
            "CREATE TABLE",
            "DROP TABLE",
            "ALTER TABLE",
            "commit()",
            "executescript",
            "mode=rw",
            "immutable=1",
            "makedirs",
            "write_text",
            "write_bytes",
            "os.remove",
            "os.unlink",
            "os.rename",
            "shutil.",
            "open(",
            "diagnostics.event",
        ):
            self.assertNotIn(forbidden, source, forbidden)
        self.assertIn("?mode=ro", source)
        self.assertIn("PRAGMA query_only=ON", source)

    def test_the_probe_never_imports_the_diagnostic_sink(self):
        source = Path(runtime_health.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import diagnostics", source.replace("diagnostics_config", ""))

    def test_the_store_schema_this_build_accepts_is_the_one_it_was_reviewed_with(self):
        self.assertEqual(runtime_health.STORE_VERSIONS, (0, 1, 2))
        self.assertIn("authority_head", runtime_health.STORE_TABLES)
        self.assertEqual(
            set(runtime_health.SIDECAR_TABLES),
            {"web-inputs", "web-replies", "web-models", "home-controls"},
        )

    def test_the_probe_budget_is_bounded(self):
        self.assertLessEqual(runtime_health.BUDGET_SECONDS, 5.0)
        self.assertLess(runtime_health.CACHE_SECONDS, runtime_health.BUDGET_SECONDS)


class IsolationTests(unittest.TestCase):
    def test_the_readiness_credential_is_not_a_registered_business_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, environment(), clear=False):
                settings = runtime_settings(directory)
                names = registered_credentials(settings)
        self.assertNotIn(READY_TOKEN_ENV, names)

    def test_a_deployment_that_reuses_a_business_variable_for_readiness_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, environment(), clear=False):
                settings = runtime_settings(directory)
                settings["diagnostics"]["ready_token_env"] = "TS012_ADMIN"
                with self.assertRaises(Exception) as caught:
                    Platform(settings)
        self.assertEqual(getattr(caught.exception, "code", None), "invalid_input")

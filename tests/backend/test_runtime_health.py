"""The read-only probes: real HTTP and real TLS, a closed document, and nothing written.

Every readiness answer here is produced by the real server over a real socket. Certificates are
really parsed, the authority store is really opened read-only, and the log is really inspected for
the absence of anything a probe might have written.
"""

import asyncio
import copy
import json
import os
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
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

from web_fixtures import PASSWORD

from services.platform import diagnostics, diagnostics_config, models, runtime_health
from services.platform.contracts import Fault
from services.platform.server import create_app
from services.platform.service import Platform, registered_credentials, validate_settings

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


class ServingBoundaryTests(ProbeTestCase):
    """What the serving boundary really records, read back from the bytes it wrote.

    The same real HTTPS deployment as the probes, plus a console session. Every claim the boundary
    makes about a request - admitted, refused, cancelled, authenticated - is checked against the
    records that are already on disk when the answer came back: the terminal events are confirmed
    before the response leaves, so this needs no polling and no sleeping.
    """

    async def console(self):
        """Boot, and hand this test a client that can hold the console's session cookie."""
        await self.boot()
        # The console pins every session to the deployment's own origin; the harness binds an
        # ephemeral port, so the deployment is told where it is actually reachable instead of the
        # guard being weakened.
        self.platform.settings["web"]["origin"] = self.url
        self.cookie_client = aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=True),
            timeout=aiohttp.ClientTimeout(total=10),
            trust_env=False,
            connector=aiohttp.TCPConnector(ssl=self.trusted),
        )
        self.addAsyncCleanup(self.cookie_client.close)
        return self.cookie_client

    def records(self):
        return read_events(self.settings["diagnostics"]["log_directory"])

    def named(self, name):
        return [record for record in self.records() if record["event"] == name]

    async def login(self, client):
        async with client.get(self.url + "/api/web/session") as response:
            session = await response.json()
        async with client.post(
            self.url + "/api/web/login",
            json={"username": "synthetic-admin", "password": PASSWORD},
            headers={"Origin": self.url, "X-CSRF-Token": session["csrf"]},
        ) as response:
            self.assertEqual(response.status, 200)

    def resolve_body(self):
        return {
            "schema_version": 1,
            "request_id": "request:synthetic-canary",
            "assertion_ref": "assertion:synthetic-canary",
        }

    async def test_a_public_page_is_not_recorded_as_a_successful_authentication(self):
        client = await self.console()
        async with client.get(self.url + "/") as response:
            self.assertEqual(response.status, 200)
        names = [record["event"] for record in self.records()]
        self.assertIn("http.request.started", names)
        self.assertIn("http.request.finished", names)
        # Nothing was presented, so nothing was authenticated. A public page load is not evidence
        # that anybody logged in, and it must never be written as if it were.
        self.assertNotIn("http.auth.succeeded", names)
        self.assertNotIn("http.auth.rejected", names)

    async def test_a_pre_authentication_refusal_claims_nothing_about_identity(self):
        client = await self.console()
        async with client.post(
            self.url + "/internal/v1/origins/resolve",
            data=b"{}",
            headers={"Authorization": "Bearer synthetic-canary", "Content-Type": "text/plain"},
        ) as response:
            # Refused on its content type, before any credential is ever looked at.
            self.assertEqual(response.status, 400)
        names = [record["event"] for record in self.records()]
        self.assertNotIn("http.auth.succeeded", names)
        self.assertNotIn("http.auth.rejected", names)
        finished = self.named("http.request.finished")[-1]
        self.assertEqual(finished["outcome"], "rejected")
        self.assertEqual(finished["error_code"], "invalid_input")

    async def test_an_unknown_path_makes_no_claim_about_identity(self):
        client = await self.console()
        async with client.post(
            self.url + "/internal/v1/not-a-route",
            json={},
            headers={"Authorization": "Bearer synthetic-canary"},
        ) as response:
            self.assertEqual(response.status, 404)
        names = [record["event"] for record in self.records()]
        self.assertIn("http.request.unknown_path", names)
        self.assertNotIn("http.auth.succeeded", names)
        self.assertNotIn("http.auth.rejected", names)

    async def test_a_refused_credential_is_recorded_as_rejected(self):
        client = await self.console()
        async with client.get(self.url + "/api/web/session") as response:
            session = await response.json()
        async with client.post(
            self.url + "/api/web/login",
            json={"username": "synthetic-admin", "password": "wrong-synthetic-password"},
            headers={"Origin": self.url, "X-CSRF-Token": session["csrf"]},
        ) as response:
            self.assertEqual(response.status, 401)
        rejected = self.named("http.auth.rejected")
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["error_code"], "unauthorized")
        self.assertNotIn("http.auth.succeeded", [r["event"] for r in self.records()])

    async def test_a_real_session_request_is_recorded_as_successful_authentication(self):
        client = await self.console()
        # A request that presents no credential at all claims nothing about identity.
        async with client.get(self.url + "/api/web/session") as response:
            self.assertEqual(response.status, 200)
        self.assertNotIn("http.auth.succeeded", [r["event"] for r in self.records()])
        # The login verifies the operator's password against the deployment's own hash, which is a
        # real authentication; the session request after it presents that session and is admitted
        # by it. Both are claims the boundary may make.
        await self.login(client)
        async with client.get(self.url + "/api/web/session") as response:
            self.assertEqual(response.status, 200)
        succeeded = self.named("http.auth.succeeded")
        self.assertGreaterEqual(len(succeeded), 1)
        self.assertTrue(all(record["outcome"] == "succeeded" for record in succeeded))

    async def test_a_refused_request_is_not_admitted_and_never_reaches_business(self):
        client = await self.console()

        def broken(line):
            raise OSError("device not configured")

        self.sink._sink.write = broken
        self.sink.emit("cli.action.started", "INFO", "started")
        for _ in range(300):
            if self.sink.state == diagnostics.UNAVAILABLE:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.sink.state, diagnostics.UNAVAILABLE)
        before = len(self.records())
        async with client.get(self.url + "/api/web/snapshot") as response:
            self.assertEqual(response.status, 503)
            self.assertEqual((await response.json())["code"], "dependency_unavailable")
        # The refusal happens in front of the work, so there is nothing to undo and nothing new in
        # the log either: a refused request is never admitted.
        self.assertEqual(len(self.records()), before)

    async def test_a_public_page_with_an_unrelated_cookie_claims_nothing(self):
        """The measured false positive: a correct Host and a cookie that authenticates nothing.

        The console serves its own page to anybody; the cookie here was never issued by this
        deployment and authenticates nothing at all. The mere presence of a `Cookie` header is
        something a stranger can produce, so it is not evidence of authentication.
        """
        client = await self.console()
        async with client.get(
            self.url + "/", headers={"Cookie": "unregistered=synthetic-not-a-session"}
        ) as response:
            self.assertEqual(response.status, 200)
        names = [record["event"] for record in self.records()]
        self.assertIn("http.request.finished", names)
        self.assertNotIn("http.auth.succeeded", names)
        self.assertNotIn("http.auth.rejected", names)

    async def test_a_forged_authorization_header_on_a_console_route_claims_nothing(self):
        """A header nobody verified is a header, not an identity."""
        client = await self.console()
        async with client.get(
            self.url + "/api/web/snapshot",
            headers={"Authorization": "Bearer synthetic-forged-credential"},
        ) as response:
            self.assertEqual(response.status, 403)
        names = [record["event"] for record in self.records()]
        self.assertNotIn("http.auth.succeeded", names)

    async def test_the_console_page_after_a_real_login_still_claims_only_what_it_verified(self):
        """The contrast that makes the rule meaningful: a real session, on a route that checks it.

        The session request really presents a session this deployment issued after verifying the
        operator's password, so it is recorded. The public page load that follows carries the same
        cookie but verifies nothing, so it claims nothing - which is exactly the difference the
        measured false positive had erased.
        """
        client = await self.console()
        await self.login(client)
        before = len(self.named("http.auth.succeeded"))
        async with client.get(self.url + "/api/web/session") as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(len(self.named("http.auth.succeeded")), before + 1)
        after = len(self.named("http.auth.succeeded"))
        async with client.get(self.url + "/") as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(len(self.named("http.auth.succeeded")), after)

    async def test_a_cancelled_request_settles_its_own_terminal_record(self):
        """The inbound half of the same rule, checked without the test finishing the work.

        The disk is held, so the terminal bound really expires. Nothing here flushes or closes on the
        implementation's behalf: the sink reports for itself what became of the record, and the
        record is still written by the owner afterwards.
        """
        client = await self.console()
        correlation = diagnostics.new_correlation()
        release = threading.Event()
        holding = threading.Event()
        real = self.sink._sink.write

        def held(line):
            # Only the terminal record is held. The request's own admission is confirmed first, so
            # the request really reaches the handler and really is cancelled: the disk is held at the
            # one moment that matters, which is when the terminal record asks to be made durable.
            if b"http.request.finished" in line:
                holding.set()
                release.wait(10)
            return real(line)

        def cancelled(header, request):
            raise asyncio.CancelledError()

        self.platform.origins.resolve = cancelled
        self.sink._sink.write = held
        try:
            try:
                async with client.post(
                    self.url + "/internal/v1/origins/resolve",
                    json=self.resolve_body(),
                    headers={
                        "Authorization": "Bearer synthetic-canary",
                        diagnostics.HEADER: correlation,
                    },
                ) as response:
                    await response.read()
            except aiohttp.ClientError:
                pass
            for _ in range(500):
                if holding.is_set():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(holding.is_set())
            # No flush, no close: the confirmation expired inside the terminal bound and the sink
            # says so about itself rather than the test making it true.
            for _ in range(500):
                if self.sink.state == diagnostics.UNAVAILABLE:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(self.sink.state, diagnostics.UNAVAILABLE)
            self.assertEqual(self.sink.error, "log_flush_timeout")
            self.assertGreaterEqual(self.sink.unconfirmed, 1)
        finally:
            release.set()
        for _ in range(500):
            if self.sink._synced >= self.sink._sequence:
                break
            await asyncio.sleep(0.01)
        # The obligation stayed with the sink, which wrote and fsynced the record once the disk
        # moved: a cancelled request does not take its terminal record with it.
        finished = [
            record
            for record in self.records()
            if record["event"] == "http.request.finished"
            and record["correlation_id"] == correlation
        ]
        self.assertEqual([record["outcome"] for record in finished], ["cancelled"])
        self.assertEqual(self.sink._owed, set())

    async def test_a_cancelled_request_still_gets_its_terminal_state(self):
        client = await self.console()
        correlation = diagnostics.new_correlation()

        def cancelled(header, request):
            raise asyncio.CancelledError()

        self.platform.origins.resolve = cancelled
        try:
            async with client.post(
                self.url + "/internal/v1/origins/resolve",
                json=self.resolve_body(),
                headers={
                    "Authorization": "Bearer synthetic-canary",
                    diagnostics.HEADER: correlation,
                },
            ) as response:
                await response.read()
        except aiohttp.ClientError:
            # A cancelled request never produces an answer, which is the point. Whether the server
            # library surfaces that as a disconnect or the connection simply ends is its own
            # detail; what this product promises is the record, asserted below.
            pass
        # A cancelled request has a terminal state like any other. It is recorded even though the
        # task that owned it was torn down before it could await anything.
        for _ in range(500):
            finished = [
                record
                for record in self.named("http.request.finished")
                if record["correlation_id"] == correlation
            ]
            if finished:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["outcome"], "cancelled")
        # And nothing was re-sent or re-run because of it.
        self.assertEqual(self.named("http.auth.succeeded"), [])


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
    async def test_current_bot_delivery_queue_is_ready(self):
        self.settings["principals"]["health-bot"] = {
            "kind": "service",
            "service": "platform",
            "token_env": "TS100_HEALTH_BOT",
            "actions": ["source.register", "source.dispatch", "mapping.prepare"],
        }
        self.settings["bot_connections"] = {"principal": "health-bot", "slots": {}}
        with patch.dict(os.environ, {"TS100_HEALTH_BOT": "synthetic-health-bot-token"}):
            await self.boot()
            database = sqlite3.connect(self.platform.bots.path)
            try:
                self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertIsNotNone(
                    database.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='expression_segments'"
                    ).fetchone()
                )
            finally:
                database.close()
            response, document = await self.probe(READY)
        self.assertEqual(response.status, 200)
        self.assertEqual(document["checks"]["sidecars"], "ok")
        self.assertEqual(document["status"], "ready")

    async def test_other_sidecars_do_not_accept_bot_queue_version(self):
        await self.boot()
        database = sqlite3.connect(self.settings["database_path"] + ".web-inputs.sqlite")
        try:
            database.execute("PRAGMA user_version=2")
        finally:
            database.close()
        response, document = await self.probe(READY)
        self.assertEqual(response.status, 503)
        self.assertEqual(document["checks"]["sidecars"], "failed")
        self.assertEqual(document["status"], "not_ready")

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
        # One real check is already in flight and has not finished.
        probe._inflight = True
        try:
            document = await probe.ready()
        finally:
            probe._inflight = False
        self.assertEqual(document["status"], "not_ready")
        self.assertEqual(set(document["checks"].values()), {"not_verified"})

    async def test_a_second_caller_never_starts_a_second_check(self):
        """Concurrent probes share one pass, and none of them queues behind it."""
        await self.boot()
        probe = runtime_health.Probe(self.platform.health, budget=2.0, cache_seconds=0.0)
        started = threading.Event()
        release = threading.Event()
        running = {"now": 0, "most": 0}
        real = probe.evaluate
        lock = threading.Lock()

        def slow():
            with lock:
                running["now"] += 1
                running["most"] = max(running["most"], running["now"])
            started.set()
            release.wait(10)
            try:
                return real()
            finally:
                with lock:
                    running["now"] -= 1

        probe.evaluate = slow
        owner = asyncio.ensure_future(probe.ready())
        self.assertTrue(await asyncio.to_thread(started.wait, 5))
        # Two more callers arrive while the one real check is still running. Neither starts work of
        # its own and neither waits for the owner: both answer from the closed document.
        others = [await probe.ready(), await probe.ready()]
        for document in others:
            self.assertEqual(document["status"], "not_ready")
            self.assertEqual(set(document["checks"].values()), {"not_verified"})
        self.assertEqual(running["most"], 1)
        release.set()
        self.assertEqual((await owner)["status"], "ready")
        self.assertEqual(running["most"], 1)

    async def test_a_timeout_does_not_overwrite_a_fresh_green_with_an_unknown(self):
        """A cached answer is never replaced by the not_verified document a timeout produced."""
        await self.boot()
        now = {"value": 1000.0}
        probe = runtime_health.Probe(
            self.platform.health, clock=lambda: now["value"], budget=2.0, cache_seconds=1.0
        )
        green = await probe.ready()
        self.assertEqual(green["status"], "ready")
        # The cached answer goes stale, so the next pass really checks - and that check is held past
        # its budget.
        now["value"] += 5.0
        release = threading.Event()
        real = probe.evaluate
        probe.budget = 0.05

        def slow():
            release.wait(10)
            return real()

        probe.evaluate = slow
        try:
            timed_out = await probe.ready()
            self.assertEqual(timed_out["status"], "not_ready")
            # The owner is still running, so nothing has replaced the good answer with the bad one.
            self.assertTrue(probe._inflight)
            self.assertIs(probe._cached, green)
        finally:
            release.set()
        # And when it really finishes, what is cached is its own real answer.
        for _ in range(200):
            if not probe._inflight:
                break
            await asyncio.sleep(0.01)
        self.assertFalse(probe._inflight)
        self.assertEqual(probe._cached["status"], "ready")

    async def test_repeated_cancellation_never_piles_up_checks(self):
        """Cancelling the wait does not cancel the work, so ownership must outlive the waiter."""
        await self.boot()
        probe = runtime_health.Probe(self.platform.health, budget=2.0, cache_seconds=0.0)
        release = threading.Event()
        real = probe.evaluate
        running = {"now": 0, "most": 0}
        lock = threading.Lock()

        def slow():
            with lock:
                running["now"] += 1
                running["most"] = max(running["most"], running["now"])
            release.wait(10)
            try:
                return real()
            finally:
                with lock:
                    running["now"] -= 1

        probe.evaluate = slow
        try:
            for _ in range(4):
                owner = asyncio.ensure_future(probe.ready())
                for _ in range(500):
                    if running["now"] > 0:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(running["now"], 1)
                # The caller that owns the check goes away, and the check does not: a cancelled wait
                # cannot cancel a thread, which is exactly why ownership is released by the work.
                owner.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await owner
                self.assertTrue(probe._inflight)
                # Every later caller answers from the closed document and starts nothing, however
                # many times this is repeated.
                document = await probe.ready()
                self.assertEqual(document["status"], "not_ready")
                self.assertEqual(set(document["checks"].values()), {"not_verified"})
                self.assertEqual(running["most"], 1)
        finally:
            release.set()
        for _ in range(500):
            if not probe._inflight:
                break
            await asyncio.sleep(0.01)
        self.assertFalse(probe._inflight)
        self.assertEqual(running["most"], 1)
        self.assertEqual(probe._cached["status"], "ready")

    async def test_a_late_result_never_becomes_a_fresh_green(self):
        """A check that outlived its own budget reports the moment it started, not the moment it
        happened to arrive: otherwise a slow, stale snapshot would be served as a current fact."""
        await self.boot()
        now = {"value": 1000.0}
        probe = runtime_health.Probe(
            self.platform.health, clock=lambda: now["value"], budget=0.05, cache_seconds=1.0
        )
        release = threading.Event()
        real = probe.evaluate
        calls = {"count": 0}

        def slow():
            calls["count"] += 1
            release.wait(10)
            return real()

        probe.evaluate = slow
        try:
            timed_out = await probe.ready()
            self.assertEqual(timed_out["status"], "not_ready")
            # The clock moves well past the cache TTL while the check is still running, so the answer
            # it eventually produces is a fact about a moment that has already gone.
            now["value"] += 10.0
        finally:
            release.set()
        for _ in range(500):
            if not probe._inflight:
                break
            await asyncio.sleep(0.01)
        self.assertFalse(probe._inflight)
        late = probe._cached
        self.assertEqual(late["status"], "ready")
        # The owner is free, and the late answer is not handed back as a current one: the next
        # caller really checks again.
        served = await probe.ready()
        self.assertEqual(calls["count"], 2)
        self.assertIsNot(served, late)
        self.assertEqual(served["status"], "ready")

    async def test_a_result_that_arrived_late_but_still_inside_its_ttl_is_reused(self):
        """The control: the age is the check's own, so a slow-but-recent answer is still reused."""
        await self.boot()
        now = {"value": 1000.0}
        probe = runtime_health.Probe(
            self.platform.health, clock=lambda: now["value"], budget=0.05, cache_seconds=30.0
        )
        release = threading.Event()
        real = probe.evaluate
        calls = {"count": 0}

        def slow():
            calls["count"] += 1
            release.wait(10)
            return real()

        probe.evaluate = slow
        try:
            self.assertEqual((await probe.ready())["status"], "not_ready")
            now["value"] += 1.0
        finally:
            release.set()
        for _ in range(500):
            if not probe._inflight:
                break
            await asyncio.sleep(0.01)
        late = probe._cached
        self.assertIs(await probe.ready(), late)
        self.assertEqual(calls["count"], 1)

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

    def test_every_configuration_the_real_startup_refuses_is_not_ready(self):
        """The rolling check asks the same question the entry point asks, not a second one.

        A preflight that calls a deployment ready and a service that then refuses to start is worse
        than no preflight at all, so each of these is checked twice: the pure validator refuses it,
        and the report says not ready for the same reason.
        """
        base = runtime_settings(self.temp.name, static=built_static(self.directory / "static"))
        cases = {
            "an unknown mode": {**base, "mode": "bogus"},
            "a storage this build does not implement": {**base, "storage": "postgres"},
            "no registered identities at all": {**base, "principals": {}},
            "a top-level key this build does not know": {**base, "mystery": 1},
            "no database path": {k: v for k, v in base.items() if k != "database_path"},
            "a flag that is not a boolean": {**base, "native_config_http": "yes"},
            # The publication lifetime is validated by the model owner while it is assembled, which
            # is exactly the kind of rule a preflight is tempted to re-implement - and to get wrong.
            "a lifetime the model owner refuses": {**base, "config_max_lifetime_seconds": 0},
            "a lifetime past the published maximum": {
                **base,
                "config_max_lifetime_seconds": 86401,
            },
            "a lifetime that is not an integer": {
                **base,
                "config_max_lifetime_seconds": "3600",
            },
        }
        for description, settings in cases.items():
            with self.subTest(description):
                with self.assertRaises(Fault):
                    validate_settings(copy.deepcopy(settings))
                # The real composition root refuses it too, so the preflight is not the only thing
                # that knows about the rule.
                with self.assertRaises(Fault):
                    Platform(copy.deepcopy(settings))
                report = runtime_health.preflight(
                    settings,
                    credential_names=registered_credentials(settings),
                    credential_present=lambda name: os.environ.get(name) is not None,
                    validate=validate_settings,
                )
                self.assertEqual(report["status"], "not_ready", report)
                self.assertEqual(report["checks"]["config"], "failed")
                # The refusal has a name of its own: a configuration this build cannot assemble is
                # a fault, and it is reported as one rather than as a first boot.
                self.assertTrue(
                    {"invalid_input", "dependency_unavailable"} & set(report["reasons"]),
                    report["reasons"],
                )

    def test_a_deployment_that_already_booted_is_not_ready_when_only_the_lifetime_is_wrong(self):
        """The measured divergence: a booted deployment whose only fault is one setting.

        Everything else here is a real, already-initialised deployment - store, sidecars and static
        build all exist - so the *only* thing that can make this red is the configuration check
        itself. A preflight that answered `ready` here would be calling a deployment ready that the
        service then refuses to start.
        """
        settings = runtime_settings(self.temp.name, static=built_static(self.directory / "static"))
        create_app(Platform(settings))
        report = runtime_health.preflight(
            settings,
            credential_names=registered_credentials(settings),
            credential_present=lambda name: os.environ.get(name) is not None,
            validate=validate_settings,
        )
        self.assertEqual(report["status"], "ready", report["reasons"])
        broken = {**settings, "config_max_lifetime_seconds": 0}
        report = runtime_health.preflight(
            broken,
            credential_names=registered_credentials(broken),
            credential_present=lambda name: os.environ.get(name) is not None,
            validate=validate_settings,
        )
        self.assertEqual(report["status"], "not_ready", report)
        self.assertEqual(report["checks"]["config"], "failed")
        self.assertIn("invalid_input", report["reasons"])
        self.assertNotIn("requires_initialization", report["reasons"])
        with self.assertRaises(Fault) as refused:
            Platform(broken)
        self.assertEqual(refused.exception.code, "invalid_input")

    def test_the_lifetime_rule_is_the_model_owners_own(self):
        """One rule, one place: the validator and the model owner agree by construction."""
        settings = runtime_settings(self.temp.name)
        self.assertEqual(models.validate_max_lifetime({}), models.DEFAULT_MAX_LIFETIME)
        self.assertEqual(models.validate_max_lifetime(settings), models.DEFAULT_MAX_LIFETIME)
        self.assertEqual(
            models.validate_max_lifetime({**settings, "config_max_lifetime_seconds": 86400}), 86400
        )
        for bad in (0, -1, 86401, True, 1.0, "3600", None):
            with self.subTest(repr(bad)):
                with self.assertRaises(Fault):
                    models.validate_max_lifetime({"config_max_lifetime_seconds": bad})

    def test_a_first_boot_on_an_empty_directory_is_still_only_uninitialized(self):
        """Nothing about a good deployment may be reported as broken just because it is new."""
        settings = runtime_settings(self.temp.name, static=built_static(self.directory / "static"))
        report = runtime_health.preflight(
            settings,
            credential_names=registered_credentials(settings),
            credential_present=lambda name: os.environ.get(name) is not None,
            validate=validate_settings,
        )
        self.assertEqual(report["checks"]["config"], "ok")
        self.assertEqual(report["reasons"], ["requires_initialization"])
        self.assertFalse(Path(settings["database_path"]).exists())

    def test_the_validator_opens_no_store_and_creates_nothing(self):
        settings = runtime_settings(self.temp.name, static=built_static(self.directory / "static"))
        before = sorted(path.name for path in self.directory.iterdir())
        contracts, auth = validate_settings(settings)
        self.assertIsNotNone(contracts)
        self.assertEqual(auth.mode, "service_https")
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()), before)
        self.assertFalse(Path(settings["database_path"]).exists())

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
            {"web-inputs", "web-replies", "web-models", "home-controls", "bots", "media"},
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

    def test_two_variables_holding_one_value_are_one_identity(self):
        """A name is not the value it resolves to, and the value is what decides."""
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, environment(), clear=False):
                settings = runtime_settings(directory)
                # The readiness variable is its own name, and it now resolves to exactly the
                # secret a registered business identity already reads.
                os.environ[READY_TOKEN_ENV] = os.environ["TS012_ADMIN"]
                self.addCleanup(os.environ.pop, READY_TOKEN_ENV, None)
                with self.assertRaises(Fault) as caught:
                    validate_settings(settings)
                self.assertEqual(caught.exception.code, "invalid_input")
                report = runtime_health.preflight(
                    settings,
                    credential_names=registered_credentials(settings),
                    credential_present=lambda name: os.environ.get(name) is not None,
                    validate=validate_settings,
                )
                self.assertEqual(report["status"], "not_ready")
                self.assertEqual(report["checks"]["config"], "failed")
                # The verdict travels; neither value does.
                self.assertNotIn(os.environ["TS012_ADMIN"], json.dumps(report))


class CommandLineTests(ProbeTestCase):
    """The entry point's own failure paths: fixed codes out, nothing else."""

    def settings_file(self, settings):
        path = self.directory / "settings.json"
        path.write_text(json.dumps(settings), encoding="utf-8")
        return path

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "services.platform", *args],
            cwd=str(Path(__file__).resolve().parents[2]),
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, **environment()},
        )

    def test_a_settings_file_that_cannot_be_read_exits_with_one_fixed_code(self):
        missing = self.directory / "not-here" / "settings.json"
        result = self.run_cli("--settings", str(missing), "preflight")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout), {"code": "config_load_failed", "status": 503})
        # The path and the exception text never reach the terminal: an operator gets a code, and
        # anything reading the output learns nothing about the deployment's filesystem.
        for leaked in (str(missing), "not-here", "FileNotFoundError", "Traceback"):
            self.assertNotIn(leaked, result.stdout + result.stderr)

    def test_a_settings_file_that_is_not_json_exits_with_one_fixed_code(self):
        path = self.directory / "settings.json"
        path.write_text("{not json at all", encoding="utf-8")
        result = self.run_cli("--settings", str(path), "preflight")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout), {"code": "config_load_failed", "status": 503})
        for leaked in (str(path), "JSONDecodeError", "Traceback", "not json at all"):
            self.assertNotIn(leaked, result.stdout + result.stderr)

    def test_a_local_action_is_refused_before_it_runs_when_the_log_cannot_be_written(self):
        settings = runtime_settings(self.temp.name, static=built_static(self.directory / "static"))
        # A file where the log directory should be: the sink cannot be assembled at all, which is
        # the same state an operator sees when the volume is gone.
        blocked = self.directory / "logs-as-a-file"
        blocked.write_text("not a directory", encoding="utf-8")
        settings["diagnostics"] = {
            "contract_directory": str(require_contract()),
            "log_directory": str(blocked),
        }
        path = self.settings_file(settings)
        result = self.run_cli(
            "--settings", str(path), "local", "--credential-env", "TS012_ADMIN", "view-config"
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(
            json.loads(result.stdout), {"code": "dependency_unavailable", "status": 503}
        )
        # The action never ran: a refused action has not started, so there is nothing to undo and
        # the log carries no start for it either.
        self.assertEqual(log_files(blocked) if blocked.is_dir() else [], [])

    def test_a_startup_that_cannot_be_assembled_is_recorded_with_fixed_codes(self):
        settings = runtime_settings(self.temp.name, static=built_static(self.directory / "static"))
        # Refused by the same validator the preflight uses, so this is the deployment that a
        # rolling check would have stopped - the question here is what the service does when it is
        # started anyway.
        settings["mode"] = "bogus"
        path = self.settings_file(settings)
        result = self.run_cli("--settings", str(path), "serve", "--port", "0")
        self.assertNotEqual(result.returncode, 0)
        records = read_events(settings["diagnostics"]["log_directory"])
        by_name = {record["event"]: record for record in records}
        self.assertIn("runtime.starting", by_name)
        self.assertIn("config.load_failed", by_name)
        self.assertIn("runtime.startup_failed", by_name)
        # Both are recorded with registered codes, and the failure carries no message: the log is
        # not a place a configuration value or a path can be parked.
        for name in ("config.load_failed", "runtime.startup_failed"):
            self.assertIn(by_name[name]["error_code"], diagnostics.ERROR_CODES)
            self.assertEqual(set(by_name[name]), set(diagnostics.FIELDS))
        self.assertNotIn("bogus", json.dumps(records))
        # The sink sealed what it wrote: a failure recorded on the way out is still durable.
        self.assertTrue(log_files(settings["diagnostics"]["log_directory"]))


class ReadinessCredentialTests(ProbeTestCase):
    """The readiness credential is read on every request, and is nobody else's identity."""

    async def status_for(self, token):
        response, _ = await self.probe(READY, token=token)
        return response.status

    async def test_a_rotation_takes_effect_on_the_next_request(self):
        await self.boot()
        self.assertNotEqual(await self.status_for(READY_TOKEN), 401)
        os.environ[READY_TOKEN_ENV] = "synthetic-rotated-readiness-token"
        self.addCleanup(os.environ.__setitem__, READY_TOKEN_ENV, READY_TOKEN)
        # The old credential is refused immediately and the new one is accepted: nothing about
        # readiness depends on a value that was cached when the server started.
        self.assertEqual(await self.status_for(READY_TOKEN), 401)
        self.assertNotEqual(await self.status_for("synthetic-rotated-readiness-token"), 401)

    async def test_an_empty_or_blank_credential_is_a_server_fault_not_a_pass(self):
        await self.boot()
        for empty in ("", "   ", "\t"):
            with self.subTest(repr(empty)):
                os.environ[READY_TOKEN_ENV] = empty
                self.addCleanup(os.environ.__setitem__, READY_TOKEN_ENV, READY_TOKEN)
                # Even a caller presenting exactly the configured value gets nothing: a server
                # with no usable credential says so instead of letting anyone read it.
                self.assertEqual(await self.status_for(empty), 503)
                self.assertEqual(await self.status_for("Bearer "), 503)

    async def test_a_credential_reused_from_a_business_identity_is_refused_at_request_time(self):
        await self.boot()
        business = os.environ["TS012_ADMIN"]
        os.environ[READY_TOKEN_ENV] = business
        self.addCleanup(os.environ.__setitem__, READY_TOKEN_ENV, READY_TOKEN)
        # The value that opens a business identity must not also open readiness, even when it is
        # presented exactly as configured - and the answer carries no value and no path.
        response, body = await self.probe(READY, token=business)
        self.assertEqual(response.status, 503)
        self.assertEqual(body, {"code": "dependency_unavailable"})
        self.assertNotIn(business, json.dumps(body))

    async def test_a_missing_or_wrong_credential_gets_one_opaque_refusal(self):
        await self.boot()
        for token in (None, "", "wrong", "Bearer ", READY_TOKEN.upper()):
            with self.subTest(repr(token)):
                response, body = await self.probe(READY, token=token)
                self.assertEqual(response.status, 401)
                self.assertEqual(body, {"code": "unauthorized"})

    async def test_a_unicode_credential_is_refused_without_an_exception(self):
        await self.boot()
        # `hmac.compare_digest` refuses non-ASCII text, so a comparison that passed raw strings
        # would turn a wrong probe into an unexpected 400. The answer must stay the opaque 401.
        response, body = await self.probe(READY, token="就绪令牌-不是凭据")
        self.assertEqual(response.status, 401)
        self.assertEqual(body, {"code": "unauthorized"})
        self.assertNotIn("Traceback", json.dumps(body))
        self.assertNotIn("Unicode", json.dumps(body))

    async def test_a_server_with_no_configured_credential_never_lets_a_caller_in(self):
        await self.boot()
        os.environ.pop(READY_TOKEN_ENV, None)
        self.addCleanup(os.environ.__setitem__, READY_TOKEN_ENV, READY_TOKEN)
        response, body = await self.probe(READY, token=READY_TOKEN)
        self.assertEqual(response.status, 503)
        self.assertEqual(body, {"code": "dependency_unavailable"})

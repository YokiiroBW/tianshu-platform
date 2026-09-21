"""TS-025 shape, identity, budget and error tests for the read-only persona page.

The peer here is a **controlled double**: a real HTTPS server that speaks the candidate's four read
operations against its own synthetic persona state. It is deliberately not a request recorder - it
keeps real revisions, real version numbers and real opaque cursors, refuses a page request bound to
a version that has moved, and echoes the `request_id` on every error - so the shape, scope, budget,
cancellation and error rules of this product are exercised end to end over actual TLS. It never
stands in for the joint acceptance: `test_personas_joint.py` runs the fixed integrated Core itself,
and the two results are recorded and reported separately.

What is checked here, one rule at a time:

* the candidate is verified or absent - a corrupted hash, a wrong status, a missing consumer task,
  a wrong producer commit, a wrong operation list or a production mode all make the page
  unavailable at configuration time rather than half-working at read time;
* authority is layered - the console login, the explicit `persona.read` action, the deployment's
  closed subject allowlist and the connection's own credential are four separate gates;
* a browser body can widen nothing: no operation, limit, endpoint, reader or scope is accepted;
* an answer that does not answer the question (wrong operation, wrong subject, wrong kind, a broken
  `count`/`has_more` relationship, an over-budget body, a bare connection failure) is one refusal,
  never an empty page;
* the read gate is bounded: the fifth simultaneous business request is refused with 429, and the
  outbound ceiling to the peer is four.
"""

import asyncio
import json
import os
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import aiohttp
from aiohttp import web
from fixtures import ENV, ROOT
from personas_fixture import (
    CONNECTION,
    PERSONA_ENV,
    REVISION_FIELDS,
    Synthetic,
    UNIT_SUBJECTS as SUBJECTS,
    copy_candidate,
    free_port,
    persona_settings,
)
from services.platform.server import create_app
from services.platform.service import Platform
from web_fixtures import PASSWORD

CANDIDATE = Path(os.environ.get("TS025_CANDIDATE_DIR", ".")).resolve()


def candidate_directory():
    """The verified candidate package, or an explicit skip when the checkout has no contracts."""
    if not (CANDIDATE / "manifest.json").is_file():
        raise unittest.SkipTest("TS025_CANDIDATE_DIR does not point at the candidate package")
    return str(CANDIDATE)


class PersonaPageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = mock.patch.dict(os.environ, {**ENV, **PERSONA_ENV})
        env.start()
        self.addCleanup(env.stop)
        # One stated skip, before any settings object asks the fixture for the candidate package.
        candidate_directory()
        tool = os.environ.get("TS013_TLS_PYTHON")
        if not tool:
            raise unittest.SkipTest("TS013_TLS_PYTHON is not configured")
        subprocess.run(
            [tool, str(ROOT / "tests/backend/make_tls_fixture.py"), self.temp.name],
            check=True,
            capture_output=True,
        )
        self.ca = str(Path(self.temp.name) / "localhost.pem")
        self.key = str(Path(self.temp.name) / "localhost-key.pem")
        self.peer = Synthetic()
        self.port = free_port()
        self.peer_runner = web.AppRunner(self.persona_app(), access_log=None)
        await self.peer_runner.setup()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.ca, self.key)
        await web.TCPSite(self.peer_runner, "127.0.0.1", self.port, ssl_context=context).start()
        self.addAsyncCleanup(self.peer_runner.cleanup)
        self.base_url = f"https://127.0.0.1:{self.port}"
        self.web_port = free_port()
        self.url = f"http://127.0.0.1:{self.web_port}"
        self.settings = persona_settings(self.temp.name, self.base_url, self.ca)
        await self.boot(self.settings)
        self.client = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.addAsyncCleanup(self.client.close)

    def persona_app(self):
        app = web.Application()
        app.router.add_post("/internal/v1/persona/manage", self.peer.handle)
        return app

    async def boot(self, config):
        """Boot the console on a pre-chosen origin, so the origin pin is the deployed one."""
        previous = getattr(self, "runner", None)
        if previous is not None:
            await previous.cleanup()
        self.config = config
        config["web"]["origin"] = self.url
        self.platform = Platform(config)
        self.app = create_app(self.platform)
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.web_port).start()
        self.addAsyncCleanup(self.runner.cleanup)
        return self.url

    # ------------------------------------------------------------------ harness

    async def call(self, path, body, csrf, expected=200, session=None, **headers):
        status, data = await self.raw(path, body, csrf, session=session, **headers)
        self.assertEqual(status, expected, data)
        return data

    async def raw(self, path, body, csrf, session=None, **headers):
        client = session or self.client
        async with client.post(
            self.url + "/api/web/" + path,
            json=body,
            headers={"Origin": self.url, "X-CSRF-Token": csrf, **headers},
        ) as response:
            data = await response.json()
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            return response.status, data

    async def current(self, client=None):
        async with (client or self.client).get(self.url + "/api/web/session") as response:
            return response.status, await response.json()

    async def login(self, client=None):
        _, anonymous = await self.current(client)
        return await self.call(
            "login",
            {"username": "synthetic-admin", "password": PASSWORD},
            anonymous["csrf"],
            session=client,
        )

    async def page(self, logged, path, body, expected=200, session=None, **headers):
        return await self.call(
            f"personas/{path}", body, logged["csrf"], expected, session=session, **headers
        )

    async def catalog(self, logged, cursor=None, expected=200, **kwargs):
        return await self.page(logged, "catalog", {"cursor": cursor}, expected, **kwargs)

    async def history(self, logged, subject="actor:a", kind="revisions", cursor=None, **kwargs):
        return await self.page(
            logged, "history", {"subject": subject, "kind": kind, "cursor": cursor}, **kwargs
        )

    def adapter(self):
        """The very adapter the running app assembled, for its own bounded read gate."""
        from services.platform.web_console import WebConsole

        for route in self.app.router.routes():
            console = getattr(route.handler, "__self__", None)
            if isinstance(console, WebConsole):
                return console.personas
        raise AssertionError("the console route was not assembled")

    # ------------------------------------------------------------- configuration

    def test_corrupted_candidate_file_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = copy_candidate(directory)
            with mock.patch.dict(os.environ, {**ENV, **PERSONA_ENV}):
                settings = persona_settings(
                    directory,
                    "https://127.0.0.1:1",
                    str(Path(directory) / "ca.pem"),
                    candidate_directory=str(root),
                )
                Platform(settings)
                (root / "examples.json").write_text("{}", encoding="utf-8")
                with self.assertRaises(Exception) as caught:
                    Platform(settings)
            self.assertEqual(getattr(caught.exception, "code", None), "dependency_unavailable")

    def test_manifest_identity_mismatch_is_refused(self):
        for field, value in (
            ("status", "published"),
            ("allowed_consumer_task", "TS-999"),
            ("producer_commit", "0" * 40),
            ("production_publish_authorized", True),
            ("operations", ["get"]),
        ):
            with tempfile.TemporaryDirectory() as directory:
                root = copy_candidate(directory, **{field: value})
                with mock.patch.dict(os.environ, {**ENV, **PERSONA_ENV}):
                    settings = persona_settings(
                        directory,
                        "https://127.0.0.1:1",
                        str(Path(directory) / "ca.pem"),
                        candidate_directory=str(root),
                    )
                    with self.assertRaises(Exception) as caught:
                        Platform(settings)
                self.assertEqual(
                    getattr(caught.exception, "code", None), "dependency_unavailable", field
                )

    def test_missing_candidate_directory_is_refused(self):
        with mock.patch.dict(os.environ, {**ENV, **PERSONA_ENV}):
            settings = persona_settings(
                self.temp.name,
                self.base_url,
                self.ca,
                candidate_directory=str(Path(self.temp.name) / "absent"),
            )
            with self.assertRaises(Exception) as caught:
                Platform(settings)
            self.assertEqual(getattr(caught.exception, "code", None), "dependency_unavailable")

    def test_enabling_without_explicit_candidate_permission_is_refused(self):
        with mock.patch.dict(os.environ, {**ENV, **PERSONA_ENV}):
            settings = persona_settings(self.temp.name, self.base_url, self.ca)
            settings["web_personas"]["allow_candidate"] = False
            with self.assertRaises(Exception) as caught:
                Platform(settings)
            self.assertEqual(getattr(caught.exception, "code", None), "invalid_input")

    def test_production_mode_never_loads_the_candidate(self):
        with mock.patch.dict(os.environ, {**ENV, **PERSONA_ENV}):
            settings = persona_settings(self.temp.name, self.base_url, self.ca)
            settings["mode"] = "service_https"
            with self.assertRaises(Exception) as caught:
                Platform(settings)
            self.assertEqual(getattr(caught.exception, "code", None), "invalid_input")

    def test_disabled_section_is_unconfigured_not_ready(self):
        with mock.patch.dict(os.environ, {**ENV, **PERSONA_ENV}):
            settings = persona_settings(self.temp.name, self.base_url, self.ca)
            settings["web_personas"]["enabled"] = False
            platform = Platform(settings)
            self.assertFalse(platform.personas["enabled"])
            self.assertIsNone(platform.personas["candidate"])
            self.assertEqual(platform.personas["reason"], "personas_disabled")

    def test_connection_shape_is_https_only_and_bounded(self):
        cases = (
            {"base_url": "http://127.0.0.1:9"},
            {"base_url": "https://user:secret@127.0.0.1:9"},
            {"base_url": "https://127.0.0.1:9/internal/v1"},
            {"base_url": "https://127.0.0.1:9?x=1"},
            {"base_url": "https://127.0.0.1:9#f"},
            {"base_url": "https://127.0.0.1:9", "timeout_seconds": 0},
            {"base_url": "https://127.0.0.1:9", "timeout_seconds": 11},
            {"base_url": "https://127.0.0.1:9", "timeout_seconds": True},
            {"base_url": "https://127.0.0.1:9", "ca_file": "relative.pem"},
            {"base_url": "https://127.0.0.1:9", "token_env": "lower_case"},
            {"base_url": "https://127.0.0.1:9", "endpoint": "https://elsewhere.invalid"},
        )
        with mock.patch.dict(os.environ, {**ENV, **PERSONA_ENV}):
            for override in cases:
                settings = persona_settings(self.temp.name, self.base_url, self.ca)
                settings["persona_connections"][CONNECTION].update(override)
                with self.assertRaises(Exception) as caught:
                    Platform(settings)
                self.assertEqual(getattr(caught.exception, "code", None), "invalid_input", override)

    def test_persona_credential_is_never_another_identity_credential(self):
        with mock.patch.dict(os.environ, {**ENV, **PERSONA_ENV}):
            settings = persona_settings(self.temp.name, self.base_url, self.ca)
            settings["persona_connections"][CONNECTION]["token_env"] = "TS012_COMPANION"
            with self.assertRaises(Exception) as caught:
                Platform(settings)
            self.assertEqual(getattr(caught.exception, "code", None), "invalid_input")

    def test_subject_allowlist_shape_is_bounded(self):
        with mock.patch.dict(os.environ, {**ENV, **PERSONA_ENV}):
            for subjects in ([], ["actor:a"] * 2, ["actor:a"] * 65, ["not an id"], [7]):
                settings = persona_settings(self.temp.name, self.base_url, self.ca)
                settings["web_personas"]["allowed_subjects"] = subjects
                with self.assertRaises(Exception) as caught:
                    Platform(settings)
                self.assertEqual(getattr(caught.exception, "code", None), "invalid_input", subjects)

    def test_unknown_connection_and_section_keys_are_refused(self):
        with mock.patch.dict(os.environ, {**ENV, **PERSONA_ENV}):
            settings = persona_settings(self.temp.name, self.base_url, self.ca)
            settings["web_personas"]["connection_id"] = "elsewhere"
            with self.assertRaises(Exception) as caught:
                Platform(settings)
            self.assertEqual(getattr(caught.exception, "code", None), "invalid_input")
            settings = persona_settings(self.temp.name, self.base_url, self.ca)
            settings["web_personas"]["reader"] = "browser"
            with self.assertRaises(Exception) as caught:
                Platform(settings)
            self.assertEqual(getattr(caught.exception, "code", None), "invalid_input")

    # ------------------------------------------------------------------ identity

    async def test_login_without_persona_read_is_forbidden(self):
        settings = persona_settings(self.temp.name, self.base_url, self.ca)
        settings["principals"]["admin"]["actions"] = [
            action
            for action in settings["principals"]["admin"]["actions"]
            if action != "persona.read"
        ]
        await self.boot(settings)
        logged = await self.login()
        answer = await self.catalog(logged, expected=403)
        self.assertEqual(answer["code"], "persona_read_required")
        self.assertEqual(self.adapter().code(), "persona_read_required")
        self.assertEqual(self.peer.calls, [])

    async def test_anonymous_session_cannot_read(self):
        _, anonymous = await self.current()
        for body, path in (
            ({"cursor": None}, "catalog"),
            ({"subject": "actor:a", "kind": "revisions", "cursor": None}, "history"),
            ({"subject": "actor:a", "revision_id": "0" * 64}, "revision"),
            ({"subject": "actor:a", "left": "0" * 64, "right": "0" * 64}, "compare"),
        ):
            await self.call(f"personas/{path}", body, anonymous["csrf"], 401)
        self.assertEqual(self.peer.calls, [])

    async def test_cross_origin_and_missing_csrf_are_refused(self):
        logged = await self.login()
        async with self.client.post(
            self.url + "/api/web/personas/catalog",
            json={"cursor": None},
            headers={"Origin": "http://elsewhere.invalid", "X-CSRF-Token": logged["csrf"]},
        ) as response:
            self.assertEqual(response.status, 403)
        async with self.client.post(
            self.url + "/api/web/personas/catalog",
            json={"cursor": None},
            headers={"Origin": self.url},
        ) as response:
            self.assertEqual(response.status, 403)
        self.assertEqual(self.peer.calls, [])

    async def test_browser_cannot_widen_the_scope(self):
        logged = await self.login()
        await self.page(logged, "catalog", {"cursor": None, "limit": 100}, 400)
        await self.page(logged, "catalog", {"cursor": None, "operation": "catalog"}, 400)
        await self.page(logged, "catalog", {"cursor": None, "connection": "other"}, 400)
        await self.page(logged, "catalog", {}, 400)
        await self.page(logged, "database", {"cursor": None}, 404)
        await self.history(logged, kind="everything", expected=400)
        await self.history(logged, subject="actor:z", expected=403)
        await self.page(logged, "revision", {"subject": "actor:z", "revision_id": "0" * 64}, 403)
        await self.page(logged, "revision", {"subject": "actor:a", "revision_id": "short"}, 400)
        await self.page(logged, "compare", {"subject": "actor:a", "left": "0" * 64}, 400)
        await self.page(
            logged,
            "compare",
            {"subject": "actor:a", "left": "0" * 64, "right": "0" * 64, "scope": "admin"},
            400,
        )
        self.assertEqual(self.peer.calls, [])

    async def test_unavailable_page_never_touches_the_peer(self):
        settings = persona_settings(self.temp.name, self.base_url, self.ca)
        settings["web_personas"]["enabled"] = False
        await self.boot(settings)
        logged = await self.login()
        answer = await self.catalog(logged, expected=503)
        # The page repeats the deployment's own word, so "not enabled" is never read as "no
        # characters" and never as a broken peer.
        self.assertEqual(answer["code"], "personas_disabled")
        self.assertEqual(self.adapter().code(), "personas_disabled")
        self.assertEqual(self.peer.calls, [])

    async def test_an_unregistered_page_says_so_instead_of_showing_nothing(self):
        """No section at all is its own word: not disabled, and never an empty directory."""
        settings = persona_settings(self.temp.name, self.base_url, self.ca)
        del settings["web_personas"]
        await self.boot(settings)
        self.assertIsNone(self.platform.personas)
        logged = await self.login()
        answer = await self.catalog(logged, expected=503)
        self.assertEqual(answer["code"], "personas_not_configured")
        self.assertEqual(self.adapter().code(), "personas_not_configured")
        self.assertEqual(self.peer.calls, [])

    # ------------------------------------------------------------------ catalog

    async def test_catalog_projects_identity_only_and_pages_at_twenty(self):
        logged = await self.login()
        page = await self.catalog(logged)
        self.assertEqual(page["code"], "ready")
        self.assertEqual(page["count"], 2)
        self.assertFalse(page["has_more"])
        self.assertIsNone(page["next_cursor"])
        self.assertEqual([entry["subject"] for entry in page["entries"]], list(SUBJECTS))
        # The projection carries identity and pointers only: no registration name, endpoint,
        # credential, reader or scope reaches the browser.
        self.assertEqual(
            set(page),
            {
                "code",
                "page",
                "subjects",
                "count",
                "entries",
                "has_more",
                "next_cursor",
            },
        )
        for entry in page["entries"]:
            self.assertEqual(
                set(entry),
                {
                    "subject",
                    "state",
                    "version",
                    "published_revision",
                    "draft_revision",
                    "retired",
                    "updated_at",
                    "error",
                },
            )
        # The directory is enumerated through the single-character read, never the global one.
        self.assertEqual({call["operation"] for call in self.peer.calls}, {"get"})
        self.assertNotIn("content", json.dumps(page, ensure_ascii=False))

    async def test_catalog_states_a_missing_subject_without_hiding_the_others(self):
        logged = await self.login()
        self.peer.personas.subjects.pop("actor:b")
        page = await self.catalog(logged)
        self.assertEqual(page["count"], 2)
        self.assertIsNone(page["entries"][0]["error"])
        self.assertEqual(page["entries"][1]["error"], "not_found")
        self.assertEqual(page["entries"][1]["subject"], "actor:b")

    async def test_catalog_connection_failure_is_not_an_empty_directory(self):
        logged = await self.login()
        self.peer.mode = "disconnect"
        answer = await self.catalog(logged, expected=503)
        self.assertEqual(answer["code"], "dependency_unavailable")
        self.peer.mode = "normal"

    async def test_forged_catalog_cursor_is_a_conflict_not_page_one(self):
        logged = await self.login()
        forged = self.peer.cursor({"kind": "catalog", "position": 1})
        answer = await self.catalog(logged, cursor=forged, expected=409)
        self.assertEqual(answer["code"], "cursor_conflict")
        self.assertEqual(self.peer.calls, [])
        await self.catalog(logged, cursor="not-a-cursor", expected=400)
        await self.catalog(logged, cursor="x" * 2049, expected=400)

    # ------------------------------------------------------------------ history

    async def test_history_kinds_are_four_and_pages_carry_their_basis(self):
        logged = await self.login()
        for kind in ("revisions", "publications", "approvals", "rollbacks"):
            page = await self.history(logged, kind=kind)
            self.assertEqual(page["kind"], kind)
            self.assertEqual(page["consistency"], "version_bound")
            self.assertIn("persona_version", page)
            self.assertFalse(page["has_more"])
            self.assertIsNone(page["next_cursor"])

    async def test_history_paginates_then_expires_when_the_persona_moves(self):
        logged = await self.login()
        for index in range(25):
            self.peer.personas.draft("actor:a", {"persona": "修订 %d" % index})
        first = await self.history(logged)
        self.assertEqual(first["count"], 20)
        self.assertTrue(first["has_more"])
        self.assertIsNotNone(first["next_cursor"])
        second = await self.history(logged, cursor=first["next_cursor"])
        self.assertEqual(second["count"], 6)
        self.assertFalse(second["has_more"])
        # The peer moves the character on; the page states the conflict and never loops.
        self.peer.personas.draft("actor:a", {"persona": "又改了"})
        answer = await self.history(logged, cursor=first["next_cursor"], expected=409)
        self.assertEqual(answer["code"], "version_conflict")

    async def test_wrong_shaped_history_answer_is_refused(self):
        logged = await self.login()
        for mode in (
            "wrong-operation",
            "wrong-subject",
            "broken-count",
            "broken-cursor",
            "wrong-kind",
        ):
            self.peer.mode = mode
            answer = await self.history(logged, expected=502)
            self.assertEqual(answer["code"], "invalid_upstream", mode)
        self.peer.mode = "normal"

    async def test_oversized_and_unparsable_answers_are_refused(self):
        logged = await self.login()
        self.peer.mode = "oversized"
        answer = await self.history(logged, expected=502)
        self.assertEqual(answer["code"], "invalid_upstream")
        self.peer.mode = "garbage"
        answer = await self.history(logged, expected=502)
        self.assertEqual(answer["code"], "invalid_upstream")
        self.peer.mode = "normal"

    async def test_slow_peer_becomes_one_timeout_without_retrying(self):
        logged = await self.login()
        self.peer.delay = 12
        calls = len(self.peer.calls)
        answer = await self.history(logged, expected=503)
        self.assertEqual(answer["code"], "timeout")
        self.peer.delay = 0
        self.assertEqual(len(self.peer.calls) - calls, 1, "a timeout must never be retried")

    # ------------------------------------------------------- revision/compare

    async def test_revision_and_compare_are_bound_to_what_was_asked(self):
        logged = await self.login()
        revision = self.peer.personas.subjects["actor:a"]["published"]
        single = await self.page(
            logged, "revision", {"subject": "actor:a", "revision_id": revision["revision_id"]}
        )
        self.assertEqual(single["revision"]["revision_id"], revision["revision_id"])
        self.assertTrue(set(single["revision"]["content"]) <= set(REVISION_FIELDS))
        self.assertEqual(single["revision"]["additional_fields"], [])
        draft = self.peer.personas.draft("actor:a", {"persona": "新版", "extension_flag": True})
        answer = await self.page(
            logged,
            "compare",
            {
                "subject": "actor:a",
                "left": revision["revision_id"],
                "right": draft["revision_id"],
            },
        )
        self.assertFalse(answer["content_identical"])
        self.assertEqual(answer["additional_field_names"], ["extension_flag"])
        self.assertTrue(answer["additional_fields_changed"])
        self.assertEqual(answer["fields"]["persona"]["change"], "modified")
        self.assertEqual(answer["fields"]["tone"]["presence"], {"left": False, "right": False})
        # A revision object never repeats the body; the values live in the comparison itself.
        for side in ("left", "right"):
            self.assertNotIn("content", answer[side])
        same = await self.page(
            logged,
            "compare",
            {
                "subject": "actor:a",
                "left": revision["revision_id"],
                "right": revision["revision_id"],
            },
        )
        self.assertTrue(same["content_identical"])
        self.assertTrue(same["identical_revision"])

    async def test_revision_of_another_character_is_refused(self):
        """The producer refuses a cross-character revision outright; this page never reinterprets."""
        logged = await self.login()
        other = self.peer.personas.subjects["actor:b"]["published"]
        answer = await self.page(
            logged,
            "revision",
            {"subject": "actor:a", "revision_id": other["revision_id"]},
            expected=400,
        )
        self.assertEqual(answer["code"], "invalid_input")
        answer = await self.page(
            logged,
            "compare",
            {"subject": "actor:a", "left": other["revision_id"], "right": other["revision_id"]},
            expected=400,
        )
        self.assertEqual(answer["code"], "invalid_input")

    async def test_mismatched_revision_answer_is_refused(self):
        logged = await self.login()
        revision = self.peer.personas.subjects["actor:a"]["published"]
        self.peer.mode = "wrong-revision"
        answer = await self.page(
            logged,
            "revision",
            {"subject": "actor:a", "revision_id": revision["revision_id"]},
            expected=502,
        )
        self.assertEqual(answer["code"], "invalid_upstream")
        self.peer.mode = "normal"

    # ------------------------------------------------------------ read gate

    async def test_outbound_reads_never_exceed_four_at_once(self):
        logged = await self.login()
        self.peer.delay = 0.05
        await self.catalog(logged)
        self.assertLessEqual(self.peer.peak, 4)
        self.assertEqual(self.peer.peak, 2)

    async def test_fifth_simultaneous_request_is_refused_without_queueing(self):
        logged = await self.login()
        adapter = self.adapter()
        self.peer.delay = 0.3
        tasks = [
            asyncio.create_task(self.raw("personas/catalog", {"cursor": None}, logged["csrf"]))
            for _ in range(8)
        ]
        await asyncio.sleep(0.05)
        refused = await self.catalog(logged, expected=429)
        self.assertEqual(refused["code"], "too_many_requests")
        answers = await asyncio.gather(*tasks)
        refused = [status for status, _ in answers if status == 429]
        served = [status for status, _ in answers if status == 200]
        # Four are served at once and every other caller is told so immediately; nothing waits in
        # an unbounded queue and nothing is silently dropped.
        self.assertEqual(len(served), 4, answers)
        self.assertEqual(len(refused), 4, answers)
        self.peer.delay = 0.0
        self.assertEqual(adapter.active, 0)

    # ------------------------------------------------------------- isolation

    async def test_logout_during_a_read_returns_no_body(self):
        logged = await self.login()
        self.peer.delay = 0.5
        task = asyncio.create_task(self.history(logged, expected=401))
        await asyncio.sleep(0.15)
        await self.call("logout", {}, logged["csrf"])
        answer = await task
        self.assertEqual(answer["code"], "session_expired")
        self.peer.delay = 0.0

    async def test_revoked_read_action_stops_an_in_flight_read(self):
        """Losing `persona.read` while the peer is answering must not return the answer."""
        logged = await self.login()
        self.peer.delay = 0.5
        task = asyncio.create_task(self.catalog(logged, expected=403))
        await asyncio.sleep(0.15)
        self.platform.auth.principals["admin"]["actions"].remove("persona.read")
        answer = await task
        self.assertEqual(answer["code"], "persona_read_required")
        self.assertEqual(self.adapter().code(), "persona_read_required")
        self.peer.delay = 0.0
        # The next read is refused before it ever reaches the peer.
        calls = len(self.peer.calls)
        await self.catalog(logged, expected=403)
        self.assertEqual(len(self.peer.calls), calls)

    async def test_cancelled_read_releases_its_gate(self):
        """A cancelled read stops holding the read gate; the peer's own wait is not a leak."""
        logged = await self.login()
        adapter = self.adapter()
        self.peer.delay = 0.4
        tasks = [asyncio.create_task(self.catalog(logged)) for _ in range(4)]
        await asyncio.sleep(0.1)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with self.assertRaises(asyncio.CancelledError):
                await task
        # The peer finishes its own delayed answers, and the gate is empty afterwards: nothing is
        # left holding a slot for a caller that has gone away. A cancelled browser request is not
        # a slot leaked for the lifetime of the process.
        for _ in range(20):
            if adapter.active == 0:
                break
            await asyncio.sleep(0.1)
        self.peer.delay = 0.0
        self.assertEqual(adapter.active, 0)
        page = await self.catalog(logged)
        self.assertEqual(page["code"], "ready")


class PersonaPageShapeTests(unittest.TestCase):
    """Pure shape rules: no server, no clock, no peer."""

    def test_request_documents_match_the_candidate_schema(self):
        from services.platform.persona_page_config import load_candidate, request_document

        candidate = load_candidate(candidate_directory())
        for document in (
            request_document("get", "actor:a", "req:1"),
            request_document("history_page", "actor:a", "req:1", kind="revisions"),
            request_document("history_page", "actor:a", "req:1", kind="approvals", cursor="opaque"),
            request_document("revision", "actor:a", "req:1", revision_id="a" * 64),
            request_document("compare", "actor:a", "req:1", left="a" * 64, right="b" * 64),
        ):
            candidate.request_validator.validate(document)
        with self.assertRaises(Exception):
            candidate.request_validator.validate({"operation": "list", "request_id": "req:1"})

    def test_cursor_is_bound_and_bounded(self):
        from services.platform.persona_page_config import PageCursor

        cursor = PageCursor()
        subjects = ("actor:a", "actor:b")
        token = cursor.issue("catalog", "authority-a", "c1", subjects, 20, 20)
        self.assertLessEqual(len(token), 2048)
        self.assertEqual(cursor.read(token, "catalog", "authority-a", "c1", subjects, 20), 20)
        for wrong in (
            ("history", "authority-a", "c1", subjects, 20),
            ("catalog", "authority-b", "c1", subjects, 20),
            ("catalog", "authority-a", "c2", subjects, 20),
            ("catalog", "authority-a", "c1", ("actor:a",), 20),
            ("catalog", "authority-a", "c1", subjects, 40),
        ):
            with self.assertRaises(Exception) as caught:
                cursor.read(token, *wrong)
            self.assertEqual(getattr(caught.exception, "code", None), "cursor_conflict")
        with self.assertRaises(Exception) as caught:
            cursor.read(token + "x", "catalog", "authority-a", "c1", subjects, 20)
        # A token this page never issued - truncated, forged, or issued by an earlier process - is
        # one conflict, so the page reopens its first page instead of continuing from a guess.
        self.assertEqual(getattr(caught.exception, "code", None), "cursor_conflict")

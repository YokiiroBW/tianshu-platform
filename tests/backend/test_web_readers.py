"""The browser reader boundary against real local TLS sockets and synthetic peer answers."""

import os
import socket
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import aiohttp
from aiohttp import web

from fixtures import ENV, start_http
from services.platform.contracts import Contracts, Fault
from services.platform.server import create_app
from services.platform.service import Platform
from services.platform.web_readers import WebReader, validate_readers
from web_fixtures import PASSWORD, web_settings


def port():
    with socket.socket() as address:
        address.bind(("127.0.0.1", 0))
        return address.getsockname()[1]


class ReaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tool = os.environ.get("TS013_TLS_PYTHON")
        if not tool:
            raise unittest.SkipTest("TS013_TLS_PYTHON not configured")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        subprocess.run(
            [tool, str(Path(__file__).with_name("make_tls_fixture.py")), self.temp.name],
            check=True,
            capture_output=True,
        )
        self.cert = str(Path(self.temp.name) / "localhost.pem")
        self.key = str(Path(self.temp.name) / "localhost-key.pem")
        self.port = port()
        self.calls = []
        app = web.Application()
        app.router.add_post("/local/v1/project-knowledge/action", self.knowledge)
        app.router.add_post("/internal/v1/life-read/{name}", self.life)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, self.key)
        await web.TCPSite(self.runner, "127.0.0.1", self.port, ssl_context=context).start()
        self.addAsyncCleanup(self.runner.cleanup)
        env = mock.patch.dict(
            os.environ,
            {
                "TEST_KNOWLEDGE_READER": "synthetic-knowledge-reader-secret-0001",
                "TEST_LIFE_READER": "synthetic-life-reader-secret-0000001",
                "OTHER_READER": "synthetic-other-reader-secret-000000",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.config = {
            "contract_directory": os.environ["TS012_CONTRACT_DIR"],
            "web_knowledge": {
                "enabled": True,
                "base_url": f"https://127.0.0.1:{self.port}",
                "token_env": "TEST_KNOWLEDGE_READER",
                "ca_file": self.cert,
                "projects": [
                    {
                        "project_id": "alpha",
                        "label": "Alpha",
                        "checkouts": [{"id": "agent-a", "label": "Agent A"}],
                    }
                ],
            },
            "web_life": {
                "enabled": True,
                "base_url": f"https://127.0.0.1:{self.port}",
                "token_env": "TEST_LIFE_READER",
                "ca_file": self.cert,
            },
        }
        self.principal = {"actions": ["knowledge.read", "life.read"]}
        self.platform = SimpleNamespace(
            settings=self.config,
            contracts=Contracts(os.environ["TS012_CONTRACT_DIR"]),
            auth=SimpleNamespace(principals={"admin": self.principal}),
            other_credentials=("OTHER_READER", "TEST_KNOWLEDGE_READER", "TEST_LIFE_READER"),
        )
        self.now = 1000
        self.console = SimpleNamespace(
            config={"principal": "admin"}, session_valid=lambda _: True, clock=lambda: self.now
        )
        self.knowledge_reader = WebReader("knowledge", self.platform, self.console)
        self.life_reader = WebReader("life", self.platform, self.console)

    async def knowledge(self, request):
        body = await request.json()
        self.calls.append((request.path, body, request.headers.get("Authorization")))
        if body["operation"] == "document_list":
            return web.json_response(
                {
                    "project_id": "alpha",
                    "project_revision": 1,
                    "items": [],
                    "next_cursor": None,
                    "omissions": [],
                    "trust": "source_material_not_instructions",
                }
            )
        if body["operation"] == "lesson_query":
            return web.json_response(
                {
                    "project_id": "alpha",
                    "lessons": [{"lesson_id": "lesson:a"}],
                    "omissions": [],
                    "retrieval": "lexical",
                    "trust": "operator_statement_with_source_evidence",
                }
            )
        if body["operation"] == "experience_query":
            return web.json_response(
                {
                    "project_id": "alpha",
                    "entries": [{"entry_id": "experience:a"}],
                    "omissions": [],
                    "retrieval": "lexical",
                    "trust": "approved_operator_rule_with_protected_citations",
                }
            )
        if body["operation"] == "continuation_recover":
            return web.json_response(
                {
                    "project_id": "alpha",
                    "status": "recovered",
                    "seal": "a" * 64,
                    "worktree": {
                        "id": "agent-a",
                        "branch": "main",
                        "head": "b" * 40,
                        "dirty": False,
                        "collected_at": "2026-09-27T00:00:00Z",
                        "files": [{"locator": "private/path"}],
                    },
                    "index": {
                        "total": 1,
                        "listed": 1,
                        "truncated": False,
                        "documents": [{"locator": "private/path"}],
                    },
                    "state": {
                        "version": 1,
                        "current": True,
                        "stale_evidence": False,
                        "goal": "Test",
                        "constraints": [],
                        "unfinished": [],
                    },
                    "units": [],
                    "omissions": [],
                    "budget": {"limit_bytes": 16384, "used_bytes": 1200, "over_budget": False},
                    "revision": 1,
                }
            )
        if body["operation"] == "continuation_check":
            return web.json_response(
                {
                    "project_id": "alpha",
                    "valid": True,
                    "reason": "current",
                    "differences": [],
                    "observed": True,
                    "worktree": {"id": body["arguments"]["package"]["worktree"]["id"]},
                    "checked_at": "2026-09-27T00:00:01Z",
                }
            )
        return web.json_response(
            {
                "project_id": "alpha",
                "blocks": [],
                "omissions": [],
                "retrieval": "lexical",
                "trust": "source_material_not_instructions",
            }
        )

    async def life(self, request):
        body = await request.json()
        self.calls.append((request.path, body, request.headers.get("Authorization")))
        name = request.match_info["name"]
        if name in {"today", "timeline"}:
            common = {
                "schema_version": 1,
                "fictional": True,
                "actor_id": "actor:a",
                "state_basis": "last_persisted",
            }
            if name == "today":
                return web.json_response(
                    {
                        **common,
                        "day": "2026-10-03",
                        "timezone": "Asia/Shanghai",
                        "enabled": True,
                        "observed_at": 1790985600,
                        "plan": {
                            "plan_id": "plan:a",
                            "version": 1,
                            "state": "active",
                            "generation_state": "unavailable",
                            "generated_by": "baseline",
                            "entries": [
                                {
                                    "phase_id": "phase:a",
                                    "minute": 540,
                                    "activity": "reading",
                                    "detail": None,
                                    "state": "current",
                                    "generation_state": "unavailable",
                                }
                            ],
                            "current_phase_id": "phase:a",
                        },
                    }
                )
            return web.json_response(
                {**common, "day": body["day"], "items": [], "next_after": None}
            )
        return web.json_response(
            {"schema_version": 1, "fictional": True, "items": [], "next_after_actor_id": None}
        )

    async def test_read_only_wire_and_scopes(self):
        validate_readers(self.config, ("OTHER_READER",))
        state = self.knowledge_reader.state()
        self.assertTrue(state["available"])
        self.assertIsNone(state["peer"]["verified_at"])
        answer = await self.knowledge_reader.route(
            "/api/web/knowledge/documents",
            {"project_id": "alpha", "limit": 8, "cursor": None},
            {},
        )
        self.assertEqual(answer["result"]["items"], [])
        self.assertEqual(self.calls[0][1]["operation"], "document_list")
        self.assertEqual(self.calls[0][1]["arguments"]["budget_bytes"], 32768)
        self.assertEqual(self.calls[0][2], "Bearer synthetic-knowledge-reader-secret-0001")
        self.assertEqual(self.knowledge_reader.state()["peer"]["code"], "ok")
        with self.assertRaises(Fault) as error:
            await self.knowledge_reader.route(
                "/api/web/knowledge/query",
                {"project_id": "other", "text": "a", "budget_bytes": 8192},
                {},
            )
        self.assertEqual(error.exception.code, "forbidden")
        self.assertEqual(len(self.calls), 1)
        life = await self.life_reader.route(
            "/api/web/life/actors", {"limit": 20, "after_actor_id": None}, {}
        )
        self.assertEqual(life["items"], [])
        self.assertEqual(self.calls[-1][1], {"schema_version": 1, "limit": 20})

    async def test_life_today_and_timeline_use_bounded_reader_and_recheck_identity(self):
        today = await self.life_reader.route("/api/web/life/today", {"actor_id": "actor:a"}, {})
        import json
        from jsonschema import Draft202012Validator, FormatChecker

        schema = json.loads(
            (
                Path(os.environ["TS012_CONTRACT_DIR"]).parents[1] / "life-read/v1/schemas/life.json"
            ).read_text(encoding="utf-8")
        )

        def validate(name, value):
            Draft202012Validator(
                {**schema, "$ref": "#/$defs/" + name}, format_checker=FormatChecker()
            ).validate(value)

        validate("today_request", self.calls[-1][1])
        validate("today_response", today)
        self.assertEqual(today["plan"]["generation_state"], "unavailable")
        self.assertEqual(self.calls[-1][1], {"schema_version": 1, "actor_id": "actor:a"})
        after = {"position": 1790985600, "known_id": "known:a"}
        timeline = await self.life_reader.route(
            "/api/web/life/timeline",
            {"actor_id": "actor:a", "day": "2026-10-03", "limit": 20, "after": after},
            {},
        )
        validate("timeline_request", self.calls[-1][1])
        validate("timeline_response", timeline)
        self.assertEqual(self.calls[-1][1]["after"], after)
        self.assertEqual(self.calls[-1][2], "Bearer synthetic-life-reader-secret-0000001")
        before = len(self.calls)
        for invalid in (
            {"actor_id": "actor:a", "day": "2026-02-30", "limit": 20, "after": None},
            {"actor_id": "actor:a", "day": "2026-10-03", "limit": 51, "after": None},
            {
                "actor_id": "actor:a",
                "day": "2026-10-03",
                "limit": 20,
                "after": {"position": True, "known_id": "known:a"},
            },
        ):
            with self.assertRaises(Fault) as error:
                await self.life_reader.route("/api/web/life/timeline", invalid, {})
            self.assertEqual(error.exception.code, "invalid_input")
        self.assertEqual(len(self.calls), before)
        with self.assertRaises(Fault) as error:
            await self.life_reader.route("/api/web/life/today", {"actor_id": "actor:b"}, {})
        self.assertEqual(error.exception.code, "invalid_upstream")
        self.principal["actions"].remove("life.read")
        with self.assertRaises(Fault) as error:
            await self.life_reader.route("/api/web/life/today", {"actor_id": "actor:a"}, {})
        self.assertEqual(error.exception.code, "life_read_required")

    async def test_revocation_and_distinct_credentials(self):
        self.principal["actions"].remove("life.read")
        with self.assertRaises(Fault) as error:
            await self.life_reader.route(
                "/api/web/life/actors", {"limit": 20, "after_actor_id": None}, {}
            )
        self.assertEqual(error.exception.code, "life_read_required")
        self.assertEqual(self.calls, [])
        with mock.patch.dict(os.environ, {"TEST_KNOWLEDGE_READER": os.environ["OTHER_READER"]}):
            with self.assertRaises(Fault) as error:
                await self.knowledge_reader.route(
                    "/api/web/knowledge/query",
                    {"project_id": "alpha", "text": "a", "budget_bytes": 8192},
                    {},
                )
            self.assertEqual(error.exception.code, "forbidden")
        self.assertEqual(self.calls, [])

    async def test_generation_retry_proves_independent_read_scope_before_and_after(self):
        from services.platform.web_life_management import WebLifeManagement

        async def run(function, *args):
            return function(*args)

        self.config["core"] = {}
        self.principal["actions"].append("role.manage")
        self.console.life = self.life_reader
        self.platform.local_work = SimpleNamespace(run=run)
        management = WebLifeManagement(self.platform, self.console)
        body = {"actor_id": "actor:a", "plan_id": "plan:a", "phase_id": None, "expected_version": 1}
        receipt = {
            "schema_version": 1,
            "actor_id": "actor:a",
            "plan_id": "plan:a",
            "plan_version": 2,
            "state": "queued",
        }

        async def accepted(*args):
            self.assertEqual(len(self.calls), 1, "actor read must precede management")
            return receipt

        with mock.patch(
            "services.platform.web_life_management.management_call", side_effect=accepted
        ) as remote:
            self.assertEqual(await management.retry(body, {}), receipt)
            self.assertEqual(len(self.calls), 2)
            remote.assert_awaited_once()

        with mock.patch.object(self.life_reader, "route", side_effect=Fault("not_found", 404)):
            with mock.patch("services.platform.web_life_management.management_call") as remote:
                with self.assertRaises(Fault) as error:
                    await management.retry(body, {})
                self.assertEqual(error.exception.status, 404)
                remote.assert_not_called()

        async def revoked(*args):
            self.principal["actions"].remove("life.read")
            return receipt

        with mock.patch(
            "services.platform.web_life_management.management_call", side_effect=revoked
        ):
            with self.assertRaises(Fault) as error:
                await management.retry(body, {})
            self.assertEqual(error.exception.status, 403)

    async def test_lesson_experience_and_session_bound_continuation(self):
        session = {}
        self.assertEqual(
            self.knowledge_reader.state()["projects"][0]["checkouts"][0]["id"], "agent-a"
        )
        for name, operation, field in (
            ("lessons", "lesson_query", "lessons"),
            ("experiences", "experience_query", "entries"),
        ):
            answer = await self.knowledge_reader.route(
                "/api/web/knowledge/" + name,
                {"project_id": "alpha", "text": "receipt", "budget_bytes": 8192},
                session,
            )
            self.assertTrue(answer["result"][field])
            self.assertEqual(self.calls[-1][1]["operation"], operation)
        recovered = await self.knowledge_reader.route(
            "/api/web/knowledge/continuation",
            {
                "project_id": "alpha",
                "checkout_id": "agent-a",
                "text": "receipt",
                "budget_bytes": 16384,
            },
            session,
        )
        self.assertNotIn("seal", str(recovered))
        self.assertNotIn("private/path", str(recovered))
        handle = recovered["result"]["handle"]
        checked = await self.knowledge_reader.route(
            "/api/web/knowledge/continuation-check",
            {"project_id": "alpha", "handle": handle},
            session,
        )
        self.assertTrue(checked["result"]["valid"])
        self.assertEqual(self.calls[-1][1]["arguments"]["package"]["seal"], "a" * 64)
        with self.assertRaises(Fault) as error:
            await self.knowledge_reader.route(
                "/api/web/knowledge/continuation-check",
                {"project_id": "alpha", "handle": handle},
                {},
            )
        self.assertEqual(error.exception.code, "continuation_handle_expired")
        self.now += 901
        with self.assertRaises(Fault) as error:
            await self.knowledge_reader.route(
                "/api/web/knowledge/continuation-check",
                {"project_id": "alpha", "handle": handle},
                session,
            )
        self.assertEqual(error.exception.code, "continuation_handle_expired")

    async def test_configuration_rejects_widened_targets(self):
        bad = dict(self.config)
        bad["web_knowledge"] = dict(self.config["web_knowledge"], base_url="http://127.0.0.1:8135")
        with self.assertRaises(Fault):
            validate_readers(bad, ("OTHER_READER",))
        bad["web_knowledge"] = dict(self.config["web_knowledge"], token_env="OTHER_READER")
        with self.assertRaises(Fault):
            validate_readers(bad, ("OTHER_READER",))

    async def test_real_console_session_and_connection_summary(self):
        env = mock.patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)
        config = web_settings(self.temp.name)
        config.update(self.config)
        config["principals"]["admin"]["actions"] += ["knowledge.read", "life.read"]
        platform = Platform(config)
        runner, url = await start_http(create_app(platform))
        self.addAsyncCleanup(runner.cleanup)
        config["web"]["origin"] = url
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
            async with client.get(url + "/api/web/session") as response:
                csrf = (await response.json())["csrf"]
            headers = {"Origin": url, "X-CSRF-Token": csrf}
            async with client.post(
                url + "/api/web/login",
                json={"username": "synthetic-admin", "password": PASSWORD},
                headers=headers,
            ) as response:
                self.assertEqual(response.status, 200)
                csrf = (await response.json())["csrf"]
            headers["X-CSRF-Token"] = csrf
            async with client.post(
                url + "/api/web/connections/view", json={}, headers=headers
            ) as response:
                self.assertEqual(response.status, 200)
                entries = {row["id"]: row for row in (await response.json())["connections"]}
                self.assertEqual(entries["knowledge"]["state"], "unverified")
                self.assertEqual(entries["memory_profiles"]["state"], "not_configured")
            async with client.post(
                url + "/api/web/knowledge/documents",
                json={"project_id": "alpha", "limit": 8, "cursor": None},
                headers=headers,
            ) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())["result"]["items"], [])
            async with client.post(
                url + "/api/web/connections/view", json={}, headers=headers
            ) as response:
                entries = {row["id"]: row for row in (await response.json())["connections"]}
                self.assertEqual(entries["knowledge"]["state"], "connected")
            async with client.post(url + "/api/web/logout", json={}, headers=headers) as response:
                self.assertEqual(response.status, 200)
            async with client.post(
                url + "/api/web/knowledge/state", json={}, headers=headers
            ) as response:
                self.assertEqual(response.status, 401)

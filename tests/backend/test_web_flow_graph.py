"""Synthetic HTTP authority and frozen-event projection checks for TS-382."""

import os
import tempfile
import time
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import aiohttp

from fixtures import ENV, start_http
from services.platform import diagnostics
from services.platform.server import create_app
from services.platform.service import Platform
from services.platform.web_console import WebConsole
from web_fixtures import PASSWORD, web_settings

CORRELATION = "a" * 32


def event(*, sequence=1, service="companion", name="turn.prepared", outcome="started"):
    value = diagnostics.build(
        "http.request.started",
        "INFO",
        outcome,
        sequence=sequence,
        service=service,
        instance_id=str(uuid.UUID(int=1)),
        event_id=str(uuid.UUID(int=sequence)),
        correlation_id=CORRELATION,
    )
    value["event"] = name
    return value


class FlowGraphHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)
        self.config = web_settings(temp.name)
        self.config["principals"]["admin"]["actions"].append("bot.manage")
        self.platform = Platform(self.config)
        self.console = WebConsole(self.platform)
        app = create_app(self.platform, console=self.console)
        runner, self.url = await start_http(app)
        self.config["web"]["origin"] = self.url
        self.addAsyncCleanup(runner.cleanup)
        self.client = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.addAsyncCleanup(self.client.close)
        async with self.client.get(self.url + "/api/web/session") as response:
            session = await response.json()
        logged = await self.post(
            "login", {"username": "synthetic-admin", "password": PASSWORD}, session["csrf"]
        )
        self.csrf = logged["csrf"]

    async def post(self, operation, body, csrf=None, expected=200):
        async with self.client.post(
            self.url + "/api/web/" + operation,
            json=body,
            headers={"Origin": self.url, "X-CSRF-Token": csrf or self.csrf},
        ) as response:
            result = await response.json()
            self.assertEqual(response.status, expected, result)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            return result

    def window(self):
        now = time.time_ns()
        return {"start_ns": str(now - 60_000_000_000), "end_ns": str(now)}

    async def test_missing_reader_is_not_configured_and_does_not_claim_empty_success(self):
        answer = await self.post("flow-graph/traces", {"minutes": 15})
        self.assertEqual(answer["availability"], "not_configured")
        self.assertFalse(answer["complete"])
        self.assertEqual(answer["traces"], [])
        topology = await self.post("flow-graph/relationships", {})
        self.assertEqual(topology["accounts"], [])
        self.assertTrue(all(not source["available"] for source in topology["sources"]))

    async def test_fixed_reader_arguments_and_http_events_do_not_invent_business_phases(self):
        values = [event(service="gateway", name="http.request.finished", outcome="succeeded")]
        reader = AsyncMock(return_value={"events": values, "complete": True})
        self.console.flow_graph.event_reader = reader
        answer = await self.post(
            "flow-graph/trace", {"correlation_id": CORRELATION, **self.window()}
        )
        self.assertEqual(answer["events"], values)
        self.assertTrue(answer["complete"])
        self.assertTrue(all(not stage["event_ids"] for stage in answer["stages"]))
        kwargs = reader.call_args.kwargs
        self.assertEqual(set(kwargs), {"correlation_id", "start_ns", "end_ns", "limit"})
        self.assertEqual(kwargs["correlation_id"], CORRELATION)
        self.assertEqual(kwargs["limit"], 1000)

    async def test_incomplete_and_duplicate_records_keep_honest_counts(self):
        row = event()
        self.console.flow_graph.event_reader = AsyncMock(
            return_value={"events": [row, dict(row)], "complete": False}
        )
        answer = await self.post("flow-graph/traces", {"minutes": 5})
        self.assertFalse(answer["complete"])
        self.assertEqual(answer["event_count"], 1)
        self.assertEqual(answer["traces"][0]["event_count"], 1)
        self.assertFalse(answer["traces"][0]["has_failure"])
        self.assertTrue(answer["traces"][0]["business_observed"])

    async def test_explicit_gateway_attempt_and_background_events_map_only_their_evidence(self):
        values = [
            event(service="gateway", name="upstream.first_output"),
            event(sequence=2, name="turn.generation.finished", outcome="succeeded"),
            event(sequence=3, name="memory.phase.finished", outcome="degraded"),
            event(sequence=4, name="turn.memory.finished", outcome="succeeded"),
            event(sequence=5, name="turn.knowledge.finished", outcome="failed"),
        ]
        self.console.flow_graph.event_reader = AsyncMock(
            return_value={"events": values, "complete": True}
        )
        answer = await self.post(
            "flow-graph/trace", {"correlation_id": CORRELATION, **self.window()}
        )
        stages = {stage["id"]: stage["event_ids"] for stage in answer["stages"]}
        self.assertEqual(stages["model"], [values[0]["event_id"]])
        self.assertEqual(stages["companion"], [values[1]["event_id"]])
        self.assertEqual(stages["learning"], [values[2]["event_id"]])
        self.assertEqual(stages["receipt"], [])
        self.assertEqual(stages["memory"], [values[3]["event_id"]])
        self.assertEqual(stages["knowledge"], [values[4]["event_id"]])

    async def test_query_error_and_extra_payload_never_escape_the_projection(self):
        self.console.flow_graph.event_reader = AsyncMock(
            side_effect=RuntimeError("https://secret.invalid/token=private-value")
        )
        result = await self.post("flow-graph/traces", {"minutes": 5}, expected=503)
        self.assertEqual(result["code"], "flow_query_unavailable")
        self.assertNotIn("private-value", str(result))
        self.console.flow_graph.event_reader = AsyncMock(
            return_value={"events": [{**event(), "payload": "private-content"}], "complete": True}
        )
        result = await self.post("flow-graph/traces", {"minutes": 5}, expected=503)
        self.assertNotIn("private-content", str(result))

    async def test_selector_injection_bounds_and_csrf_rejected_before_reader(self):
        reader = AsyncMock()
        self.console.flow_graph.event_reader = reader
        await self.post("flow-graph/traces", {"minutes": 1}, expected=400)
        await self.post("flow-graph/traces", {"minutes": 5, "selector": "{}"}, expected=400)
        await self.post(
            "flow-graph/trace", {"correlation_id": '"} |= "secret', **self.window()}, expected=400
        )
        await self.post("flow-graph/traces", {"minutes": 5}, csrf="wrong", expected=403)
        reader.assert_not_called()

    async def test_original_operator_permission_and_post_read_revocation_are_enforced(self):
        reader = AsyncMock(return_value={"events": [event()], "complete": True})
        self.console.flow_graph.event_reader = reader
        self.platform.auth.principals["admin"]["actions"].remove("bot.manage")
        # The existing operator action is checked server-side before any query.
        await self.post("flow-graph/traces", {"minutes": 5}, expected=403)
        reader.assert_not_called()

    async def test_expired_session_cannot_receive_an_inflight_result(self):
        async def reader(**kwargs):
            self.console.sessions.clear()
            return {"events": [event()], "complete": True}

        self.console.flow_graph.event_reader = reader
        await self.post("flow-graph/traces", {"minutes": 5}, expected=401)

    async def test_topology_redacts_addresses_and_retains_explicit_inherited_unbound(self):
        self.platform.bot_adapters.route = AsyncMock(
            return_value={
                "available": True,
                "actors": [{"id": "actor:a", "label": "角色 A"}],
                "connections": [],
            }
        )
        self.platform.bots.web_route = AsyncMock(
            return_value={"available": False, "slots": [], "connections": []}
        )
        row = {
            "id": "synthetic-observation",
            "name": "合成账号",
            "adapter": "onebot",
            "account_id": "10001",
            "instance_id": "synthetic-sdk",
            "state": "ready",
            "revision": 3,
            "read_enabled": True,
            "address": "https://secret.invalid",
            "access_key": "private-token",
            "group_policy": {
                "version": 2,
                "default": {"mode": "observe", "actor_id": "actor:a", "keywords": []},
                "groups": {"20001": {"mode": "disabled", "actor_id": None, "keywords": []}},
            },
            "private_policy": {
                "observe": True,
                "mode": "observe_only",
                "list": [],
                "actor_id": None,
            },
        }

        async def observation(console, path, body, session):
            if path.endswith("/view"):
                return {"available": True, "unlocked": True, "connections": [row]}
            return {"items": [{"conversation": "group:20002"}], "next_cursor": None}

        self.platform.bot_observation.route = observation
        result = await self.post("flow-graph/relationships", {})
        self.assertEqual(len(result["accounts"]), 1)
        bindings = {item["conversation_id"]: item for item in result["conversations"]}
        self.assertEqual(bindings["group:20001"]["binding"], "unbound")
        self.assertEqual(bindings["group:20002"]["binding"], "inherited")
        self.assertEqual(bindings["group:20002"]["actor_ids"], ["actor:a"])
        self.assertNotIn("secret.invalid", str(result))
        self.assertNotIn("private-token", str(result))

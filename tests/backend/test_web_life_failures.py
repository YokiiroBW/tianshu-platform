"""Targeted owner failure and read-to-write authorization boundary regressions."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
from aiohttp import web

from fixtures import start_http
from services.platform.contracts import Fault, require
from services.platform.transport import CoreFault
from services.platform.web_life_management import WebLifeManagement
from services.platform.web_people_life import WebPeopleLife


class LifeFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_unknown_and_timeout_preserve_uncertainty_without_retry(self):
        calls = []
        mode = "unknown"

        async def peer(request):
            body = await request.json()
            calls.append(body)
            if mode == "timeout":
                await asyncio.sleep(0.3)
            return web.json_response({
                "schema_version": 1, "request_id": body["request_id"],
                "code": "result_unknown", "execution_state": mode,
                "retryable": False,
            }, status=409)

        app = web.Application()
        app.router.add_post("/internal/v2/life/{path}", peer)
        runner, url = await start_http(app)
        self.addAsyncCleanup(runner.cleanup)
        config = {"base_url": url, "token_env": "SYNTHETIC", "timeout_seconds": 0.1}
        p = SimpleNamespace(settings={"core": config})
        console = SimpleNamespace(life=SimpleNamespace(config=config, _credential=lambda: "synthetic"))
        client = WebLifeManagement(p, console, session_factory=aiohttp.ClientSession)
        payload = {"request_id": "same-semantic-id", "operation": "activity.pause"}
        with patch("services.platform.auth.secret", return_value="synthetic"):
            for mode in ("unknown", "not_started", "timeout"):
                with self.subTest(mode=mode), self.assertRaises(CoreFault) as caught:
                    await client._call("manage", payload, management=True)
                self.assertEqual(caught.exception.document["execution_state"],
                                 "unknown" if mode == "timeout" else mode)
            with self.assertRaises(Fault) as caught:
                await client._call("read", payload)
            self.assertNotIsInstance(caught.exception, CoreFault)
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(item["request_id"] == "same-semantic-id" for item in calls))


class PeopleLifeRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_control_read_revocation_blocks_write(self):
        for change in ("session", "scope", "none"):
            with self.subTest(change=change):
                revoked = False
                calls = []
                entry = {"channel": {"id": "original"}}
                scope = {"person_id": "person:1", "audience": {"kind": "private"},
                         "conversation_id": "conversation:1"}

                async def run(fn, *args):
                    return fn(*args)

                def check_access(*args):
                    require(not (revoked and change == "session"), "session_expired", 401)

                async def owner(path, payload, **kwargs):
                    nonlocal revoked
                    calls.append((path, payload))
                    revoked = True
                    return {"subscriptions": [{"id": "subscription:1"}]}

                p = SimpleNamespace(local_work=SimpleNamespace(run=run),
                    qq_admin=SimpleNamespace(check_access=check_access),
                    contracts=SimpleNamespace(check=lambda *args: None))
                console = SimpleNamespace(life_management=SimpleNamespace(_call=owner))
                client = WebPeopleLife(p, console)
                client.target = lambda *args: ("entry", entry,
                    {**scope, "person_id": "person:2"} if revoked and change == "scope" else scope)
                body = {"actor_id": "actor:1", "qq_id": "123", "conversation": "123",
                        "client_id": "original-key", "expected_version": 1,
                        "value": {"id": "subscription:1", "state": "paused"}}
                if change == "none":
                    await client.route("state", body, "session")
                    self.assertEqual(calls[1][1]["request_id"], "original-key")
                else:
                    with self.assertRaises(Fault) as caught:
                        await client.route("state", body, "session")
                    self.assertEqual(caught.exception.code,
                        "session_expired" if change == "session" else "scope_changed")
                self.assertEqual([path for path, _ in calls],
                    ["proactive/control", "manage"] if change == "none" else ["proactive/control"])

"""Real HTTP transport and closed candidate envelopes, using synthetic registered roles."""

import json
import unittest

import aiohttp

from fixtures import bearer, start_http
from services.platform.actor_capabilities.http import PATH
from services.platform.server import create_app
from test_actor_capabilities import ActorFixture


class ActorCapabilityHttpTests(ActorFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self.prepare()
        self.runner, self.url = await start_http(create_app(self.platform))
        self.addAsyncCleanup(self.runner.cleanup)
        self.client = aiohttp.ClientSession()
        self.addAsyncCleanup(self.client.close)

    async def post(self, body, *, expected=200, headers=None, data=None):
        values = {"Authorization": bearer("COMPANION"), **(headers or {})}
        arguments = {"json": body} if data is None else {"data": data}
        async with self.client.post(self.url + PATH, headers=values, **arguments) as response:
            result = await response.json()
            self.assertEqual(response.status, expected, result)
            if expected >= 400:
                self.service.protocol.check("error", result)
            return result

    async def test_real_discovery_invoke_poll_and_lost_response_retry(self):
        found = await self.post(self.request("discover"))
        self.assertEqual(found["role_version"], 7)
        body = self.invoke_request()
        first = await self.post(body)
        second = await self.post({**body, "request_id": "response-lost-retry"})
        self.assertEqual(first["invocation_id"], second["invocation_id"])
        await self.settled(first["invocation_id"])
        done = await self.post(self.request("get", invocation_id=first["invocation_id"]))
        self.assertEqual((done["execution"], done["result"]), ("succeeded", 1))
        self.assertNotIn("config", done)

    async def test_cookie_origin_wrong_service_and_missing_grant_are_rejected(self):
        body = self.request("discover")
        for headers in (
            {"Cookie": "tianshu_session=fake"},
            {"Origin": self.url},
            {"Authorization": bearer("MEMORY")},
        ):
            await self.post(body, headers=headers, expected=403)
        await self.post(body, headers={"Authorization": "Bearer wrong"}, expected=401)
        self.platform.auth.principals["companion"]["actions"].remove("actor.capabilities")
        await self.post(body, expected=403)
        self.assertEqual(self.host.repository.calls(), [])

    async def test_closed_shape_size_and_version_checks_happen_before_invocation(self):
        body = self.invoke_request()
        invalid = await self.post({**body, "principal": "admin"}, expected=400)
        self.assertEqual(invalid["execution"], "not_started")
        await self.post({**body, "role_version": 19}, expected=409)
        value = await self.post({**body, "arguments": {"huge": "x" * (256 * 1024)}}, expected=413)
        self.assertEqual(value["code"], "capacity_exceeded")
        await self.post(
            {}, data=b'{"operation":', headers={"Content-Type": "application/json"}, expected=400
        )
        self.assertEqual(self.host.repository.calls(), [])

    async def test_after_acceptance_projection_error_never_claims_not_started(self):
        original = self.service.bridge.receipt

        def lost(*args):
            raise OSError("synthetic storage loss with private details")

        self.service.bridge.receipt = lost
        try:
            result = await self.post(self.invoke_request(), expected=503)
        finally:
            self.service.bridge.receipt = original
        self.assertIn(result["execution"], {"unknown", "completed"})
        self.assertFalse(result["retryable"])
        self.assertNotIn("private details", json.dumps(result))
        retried = await self.post(self.invoke_request())
        await self.settled(retried["invocation_id"])
        self.assertEqual(len(self.host.repository.calls()), 1)

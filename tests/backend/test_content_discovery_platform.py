"""Actual Platform assembly and actor capability HTTP; no Memory/model/real account calls."""

import asyncio
import unittest

import aiohttp

from fixtures import bearer, start_http
from services.platform.actor_capabilities.http import PATH
from services.platform.server import create_app
from test_actor_capabilities import ActorFixture


class DiscoveryPlatformTests(ActorFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self.prepare()
        operation = await self.host.install(
            {"type": "builtin", "id": "content-discovery"}, "discovery-install"
        )
        for _ in range(400):
            operation = self.host.repository.get("operations", operation["id"])
            if operation["execution"] not in {"accepted", "running"}:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(operation["execution"], "succeeded", operation)
        self.instance = await self.host.create(
            operation["package_id"], "Actor discovery", "discovery-instance"
        )
        self.instance = await self.host.configure(
            self.instance["id"], {"include_books": True}, {}, self.instance["revision"]
        )
        self.instance = await self.host.state(self.instance["id"], True, self.instance["revision"])
        self.bindings = [
            self.binding(actor, actions=["discover"]) for actor in ("actor:a", "actor:b")
        ]
        await self.save_bindings()
        self.platform.media.repo.put(
            "book",
            "a" * 24,
            {
                "book_id": "a" * 24,
                "metadata": {
                    "title": "A recorded book",
                    "author": "Writer",
                    "description": "Library metadata",
                },
            },
        )
        self.runner, self.url = await start_http(create_app(self.platform))
        self.addAsyncCleanup(self.runner.cleanup)
        self.client = aiohttp.ClientSession()
        self.addAsyncCleanup(self.client.close)

    async def post(self, value, status=200):
        async with self.client.post(
            self.url + PATH, json=value, headers={"Authorization": bearer("COMPANION")}
        ) as response:
            body = await response.json()
            self.assertEqual(response.status, status, body)
            return body

    async def test_real_role_discovery_invocation_receipt_and_cross_actor_boundary(self):
        found = await self.post(self.request("discover"))
        capability = next(
            row
            for row in found["capabilities"]
            if row["binding"]["instance_id"] == self.instance["id"]
        )
        self.assertEqual(capability["availability"], "ready")
        request = self.invoke_request("discover", arguments={"sources": ["books"]})
        first = await self.post(request)
        again = await self.post({**request, "request_id": "same-intent-retry"})
        self.assertEqual(again["invocation_id"], first["invocation_id"])
        await self.settled(first["invocation_id"])
        final = await self.post(self.request("get", invocation_id=first["invocation_id"]))
        self.assertEqual(final["execution"], "succeeded")
        self.assertEqual(final["effect"], "none")
        self.assertEqual(final["result"]["candidates"][0]["title"], "A recorded book")
        self.assertEqual(final["result"]["seen_state"], "unknown")
        await self.post(self.request("get", "actor:b", invocation_id=first["invocation_id"]), 404)
        other = await self.post(
            self.invoke_request("discover", actor="actor:b", arguments={"sources": ["books"]})
        )
        self.assertNotEqual(other["invocation_id"], first["invocation_id"])

    async def test_removed_action_and_model_background_identity_do_not_get_actor_capability(self):
        self.bindings[0]["entries"] = []
        await self.save_bindings()
        found = await self.post(self.request("discover"))
        self.assertFalse(
            any(
                row["binding"]["instance_id"] == self.instance["id"]
                for row in found["capabilities"]
            )
        )
        await self.post(self.invoke_request("discover"), 403)
        principal = self.platform.auth.principals["companion"]
        original = principal["service"]
        principal["service"] = "companion_background"
        try:
            await self.post(self.request("discover"), 403)
        finally:
            principal["service"] = original

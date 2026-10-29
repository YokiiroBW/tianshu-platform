"""Native HTTP management boundary with an isolated synthetic Companion owner."""

import copy
import os
import tempfile
import unittest
from unittest.mock import patch

import aiohttp
from aiohttp import web
from fixtures import ENV, bearer, settings, start_http

from services.platform.memory_learning_management import PATH
from services.platform.role_runtime import RoleRuntime
from services.platform.server import create_app
from services.platform.service import Platform


class RecoveryManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ts413-")
        self.addCleanup(self.temp.cleanup)
        environment = patch.dict(os.environ, {**ENV, "TS413_CORE": "synthetic-core-management"})
        environment.start()
        self.addCleanup(environment.stop)
        config = settings(self.temp.name)
        config["principals"]["admin"]["actions"].append("role.manage")
        config["principals"]["companion"]["actions"].append("role.manage")
        config["core"] = {"base_url": "https://companion.invalid", "token_env": "TS413_CORE"}
        self.p = Platform(config)
        self.addCleanup(self.p.close)
        self.p.settings["role_runtime"] = {
            "enabled": True,
            "memory": {"base_url": "https://memory.invalid", "token_env": "TS413_CORE"},
        }
        catalog = self.p.provider_catalog
        self.p.provider_catalog = object()
        try:
            self.p.role_runtime = RoleRuntime(self.p)
        finally:
            self.p.provider_catalog = catalog
        self.role = {
            "actor_id": "actor:a",
            "operator": "admin",
            "enabled": True,
            "state": "active",
            "stage": "complete",
            "capabilities": ["memory.write"],
        }
        self.p.role_runtime._put(self.role)
        self.commands, self.authorizations, self.receipts = [], [], {}
        self.lose_response = False

        async def companion(request):
            body = await request.json()
            self.commands.append(body)
            self.authorizations.append(request.headers.get("Authorization"))
            result = self.receipts.setdefault(
                body["request_id"],
                {
                    "request_id": body["request_id"],
                    "job_id": body["job_id"],
                    "state": "resumed",
                    "attempts": body["expected_attempts"],
                    "failure_count": body["expected_failure_count"],
                    "model_request_id": body["model_request_id"],
                },
            )
            if self.lose_response:
                self.lose_response = False
                request.transport.close()
            return web.json_response(result)

        peer = web.Application()
        peer.router.add_post(PATH, companion)
        runner, peer_url = await start_http(peer)
        self.addAsyncCleanup(runner.cleanup)

        class RegisteredCompanionSession:
            def __init__(self, **options):
                self.session = aiohttp.ClientSession(**options)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                await self.session.close()

            def post(self, url, **options):
                assert url == "https://companion.invalid" + PATH
                return self.session.post(peer_url + PATH, **options)

        self.p.client_factories["companion"] = RegisteredCompanionSession
        runner, self.url = await start_http(create_app(self.p))
        self.addAsyncCleanup(runner.cleanup)
        self.client = aiohttp.ClientSession(trust_env=False)
        self.addAsyncCleanup(self.client.close)
        self.body = {
            "actor_id": "actor:a",
            "job_id": "job:original",
            "request_id": "recover:original",
            "expected_attempts": 6,
            "expected_failure_count": 6,
            "model_request_id": "model:" + "a" * 32,
        }

    async def post(self, *, body=None, header=None, data=None, expected=200):
        options = {"json": self.body if body is None else body} if data is None else {"data": data}
        async with self.client.post(
            self.url + PATH,
            headers={
                "Authorization": header or bearer("ADMIN"),
                "Content-Type": "application/json",
            },
            **options,
        ) as response:
            value = await response.json()
            self.assertEqual(expected, response.status, value)
            if expected != 200:
                self.p.contracts.check("common#error", value)
            return value

    async def test_operator_only_and_current_actor_write_authority(self):
        await self.post(header="Bearer wrong", expected=401)
        await self.post(header=bearer("COMPANION"), expected=403)
        self.p.auth.principals["admin"]["actions"].remove("role.manage")
        await self.post(expected=403)
        self.p.auth.principals["admin"]["actions"].append("role.manage")
        for change in (
            {"enabled": False},
            {"state": "pending"},
            {"capabilities": []},
            {"operator": "another"},
        ):
            self.p.role_runtime._put({**self.role, **change})
            await self.post(expected=403)
        self.p.role_runtime._put(self.role)
        await self.post(body={**self.body, "actor_id": "actor:missing"}, expected=403)
        self.assertEqual([], self.commands)

    async def test_original_command_forwarded_once_using_registered_platform_credential(self):
        original = copy.deepcopy(self.body)
        response = await self.post()
        self.assertEqual("resumed", response["state"])
        self.assertEqual(original["request_id"], response["request_id"])
        self.assertEqual([original], self.commands)
        self.assertEqual(["Bearer synthetic-core-management"], self.authorizations)
        self.assertEqual(original, self.body)

    async def test_lost_ack_is_unknown_and_manual_retry_preserves_original_key(self):
        self.lose_response = True
        unknown = await self.post(expected=503)
        self.assertEqual("unknown", unknown["execution_state"])
        self.assertFalse(unknown["retryable"])
        self.assertEqual(self.body["request_id"], unknown["request_id"])
        self.assertEqual([self.body], self.commands)
        recovered = await self.post()
        self.assertEqual("resumed", recovered["state"])
        self.assertEqual([self.body, self.body], self.commands)
        self.assertEqual(1, len(self.receipts))

    async def test_closed_json_and_4096_byte_boundary_prevent_forwarding(self):
        await self.post(body={**self.body, "extra": True}, expected=400)
        await self.post(body={**self.body, "expected_attempts": True}, expected=400)
        await self.post(data=b'{"actor_id":', expected=400)
        await self.post(data=b" " * 4097, expected=413)
        self.assertEqual([], self.commands)

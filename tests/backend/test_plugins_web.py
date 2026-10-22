"""Cookie/CSRF/login and plugin actions through the actual Platform HTTP server."""

import asyncio
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

import aiohttp

from fixtures import ENV, start_http
from services.platform.server import create_app
from services.platform.service import Platform
from services.platform.web_console import WebConsole
from web_fixtures import PASSWORD, web_settings


class PluginWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        environment = patch.dict(os.environ, ENV)
        environment.start()
        self.addCleanup(environment.stop)
        self.settings = web_settings(self.temp.name)
        self.platform = Platform(self.settings)
        self.console = WebConsole(self.platform)
        self.runner, self.url = await start_http(create_app(self.platform, console=self.console))
        self.addAsyncCleanup(self.runner.cleanup)
        self.settings["web"]["origin"] = self.url
        self.client = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.addAsyncCleanup(self.client.close)
        async with self.client.get(self.url + "/api/web/session") as response:
            session = await response.json()
        self.csrf = session["csrf"]

    async def post(self, operation, body=None, expected=200):
        async with self.client.post(
            self.url + "/api/web/" + operation,
            json=body or {},
            headers={"Origin": self.url, "X-CSRF-Token": self.csrf},
        ) as response:
            result = await response.json()
            self.assertEqual(response.status, expected, result)
            return result

    async def login(self):
        result = await self.post("login", {"username": "synthetic-admin", "password": PASSWORD})
        self.csrf = result["csrf"]

    async def install(self, source="hello"):
        operation = await self.post(
            "plugins/install",
            {"source": {"type": "builtin", "id": source}, "client_request_id": str(uuid.uuid4())},
        )
        for _ in range(100):
            result = await self.post("plugins/installation", {"id": operation["id"]})
            if result["execution"] not in {"accepted", "running"}:
                self.assertEqual(result["execution"], "succeeded", result)
                return result["package_id"]
            await asyncio.sleep(0.02)
        self.fail("Installation not finished")

    async def test_authenticated_install_enable_invoke_and_task_centre(self):
        await self.login()
        package = await self.install()
        instance = await self.post(
            "plugins/create",
            {"package_id": package, "name": "HTTP hello", "client_request_id": "http-instance"},
        )
        instance = await self.post(
            "plugins/enable",
            {"instance_id": instance["id"], "expected_revision": instance["revision"]},
        )
        result = await self.post(
            "plugins/invoke",
            {
                "instance_id": instance["id"],
                "action": "echo",
                "input": {"text": "actual HTTP"},
                "client_request_id": "http-action",
            },
        )
        for _ in range(100):
            result = await self.post("plugins/invocation", {"id": result["id"]})
            if result["execution"] not in {"accepted", "running"}:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(result["result"], {"text": "actual HTTP"})
        tasks = await self.post("tasks/view", {"source": "platform.plugins"})
        self.assertEqual(tasks["items"][0]["source"], "platform.plugins")
        self.assertEqual(tasks["items"][0]["status"], "observed")
        self.assertEqual(tasks["items"][0]["module"]["page"], "#/plugins")

    async def test_login_csrf_and_logout_are_required(self):
        await self.post("plugins/view", expected=401)
        await self.login()
        self.csrf, original = "wrong-csrf", self.csrf
        await self.post("plugins/view", expected=403)
        self.csrf = original
        await self.post("logout")
        await self.post("plugins/view", expected=401)

    async def test_real_schema_configuration_never_reads_back_secrets(self):
        await self.login()
        package = await self.install("http-query")
        instance = await self.post(
            "plugins/create",
            {"package_id": package, "name": "Query", "client_request_id": "query-instance"},
        )
        self.assertEqual(instance["state"], "pending_configuration")
        result = await self.post(
            "plugins/configure",
            {
                "instance_id": instance["id"],
                "config": {"base_url": "http://127.0.0.1:1"},
                "secrets": {"api_key": "synthetic-api-key"},
                "expected_revision": instance["revision"],
            },
        )
        self.assertTrue(result["secret_configured"]["api_key"])
        self.assertNotIn("synthetic-api-key", str(result))
        overview = await self.post("plugins/view")
        self.assertNotIn("synthetic-api-key", str(overview))

    async def test_missing_action_and_caller_identity_never_become_an_invocation(self):
        await self.login()
        package = await self.install()
        instance = await self.post(
            "plugins/create",
            {"package_id": package, "name": "Hello", "client_request_id": "identity-test"},
        )
        await self.post(
            "plugins/enable",
            {"instance_id": instance["id"], "expected_revision": instance["revision"]},
        )
        await self.post(
            "plugins/invoke",
            {
                "instance_id": instance["id"],
                "action": "nonexistent",
                "input": {},
                "client_request_id": "unknown",
            },
            expected=404,
        )
        self.assertEqual(self.platform.plugins.task_source.count(), 0)
        await self.post(
            "plugins/invoke",
            {
                "instance_id": instance["id"],
                "action": "echo",
                "input": {"text": "spoof"},
                "client_request_id": "spoof",
                "context_ref": "browser-chosen",
            },
            expected=400,
        )
        self.assertEqual(self.platform.plugins.task_source.count(), 0)

    async def test_view_ticket_has_its_own_csp_is_single_use_and_requires_the_session(self):
        await self.login()
        package = await self.install()
        instance = await self.post(
            "plugins/create", {"package_id": package, "name": "View", "client_request_id": "view"}
        )
        result = await self.post(
            "plugins/view-content",
            {"instance_id": instance["id"], "page_id": "panel", "nonce": str(uuid.uuid4())},
        )
        async with aiohttp.ClientSession() as anonymous:
            async with anonymous.get(self.url + result["url"]) as response:
                self.assertEqual(response.status, 401)
        async with self.client.get(self.url + result["url"]) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(
                "sandbox allow-scripts allow-forms", response.headers["Content-Security-Policy"]
            )
            self.assertNotIn("allow-same-origin", response.headers["Content-Security-Policy"])
            self.assertIn("window.tianshu", await response.text())
        async with self.client.get(self.url + result["url"]) as response:
            self.assertEqual(response.status, 404)
        async with self.client.get(self.url + "/") as response:
            policy = response.headers["Content-Security-Policy"]
            self.assertIn("script-src 'self';", policy)
            self.assertNotIn("script-src 'unsafe-inline'", policy)

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aiohttp

from fixtures import ENV, start_http
from services.platform.contracts import Fault
from services.platform.server import create_app
from services.platform.service import Platform
from services.platform.web_access import WebAccess
from services.platform.web_console import WebConsole
from web_fixtures import PASSWORD, web_settings


def config(directory):
    settings = web_settings(directory)
    settings["mode"] = "service_https"
    settings["web_access"] = {
        "host": "127.0.0.1",
        "port": 8080,
        "certificates": {},
        "initial": {"mode": "http", "origin": "http://192.168.31.210:19443", "certificate": None},
    }
    return settings


class PolicyTests(unittest.TestCase):
    def test_expired_certificate_is_refused_before_save(self):
        from datetime import datetime, timedelta, timezone
        from make_tls_fixture import generate

        with tempfile.TemporaryDirectory() as directory:
            with patch("make_tls_fixture.datetime") as clock:
                clock.now.return_value = datetime.now(timezone.utc) - timedelta(days=3)
                generate(directory)
            settings = config(directory)
            settings["web_access"]["certificates"]["expired"] = {
                "certificate_file": str(Path(directory) / "localhost.pem"),
                "private_key_file": str(Path(directory) / "localhost-key.pem"),
            }
            access = WebAccess(settings)
            with self.assertRaises(Fault):
                access.save(
                    {"mode": "https", "origin": "https://localhost:8080", "certificate": "expired"},
                    0,
                )
            self.assertFalse(access.path.exists())

    def test_save_is_pending_and_restart_loads_it(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = config(directory)
            access = WebAccess(settings)
            value = {"mode": "proxy", "origin": "https://bot.example.test", "certificate": None}
            result = access.save(value, 0)
            self.assertTrue(result["restart_required"])
            self.assertEqual(access.current["mode"], "http")
            reloaded = WebAccess(settings)
            self.assertEqual(reloaded.current, value)
            self.assertFalse(reloaded.view()["restart_required"])
            before = access.path.read_bytes()
            with self.assertRaises(Fault):
                access.save(value, 0)
            self.assertEqual(access.path.read_bytes(), before)

    def test_invalid_addresses_and_certificates_never_replace_file(self):
        with tempfile.TemporaryDirectory() as directory:
            access = WebAccess(config(directory))
            for address in (
                "https://evil.test/path",
                "https://user@evil.test",
                "http://bad host",
                "http://x:0",
                "http://x:99999",
                "http://x#frag",
                "javascript://x",
            ):
                with self.subTest(address=address), self.assertRaises((Fault, ValueError)):
                    access.save({"mode": "proxy", "origin": address, "certificate": None}, 0)
                self.assertFalse(access.path.exists())
            with self.assertRaises(Fault):
                access.save(
                    {
                        "mode": "https",
                        "origin": "https://bot.example.test",
                        "certificate": "missing",
                    },
                    0,
                )
            self.assertFalse(access.path.exists())

    def test_certificate_san_and_lifetime_checked_offline(self):
        from make_tls_fixture import generate

        with tempfile.TemporaryDirectory() as directory:
            generate(directory)
            settings = config(directory)
            settings["web_access"]["certificates"]["local"] = {
                "certificate_file": str(Path(directory) / "localhost.pem"),
                "private_key_file": str(Path(directory) / "localhost-key.pem"),
            }
            access = WebAccess(settings)
            access.save(
                {"mode": "https", "origin": "https://localhost:8080", "certificate": "local"}, 0
            )
            before = access.path.read_bytes()
            with self.assertRaises(Fault):
                access.save(
                    {
                        "mode": "https",
                        "origin": "https://wrong.example.test",
                        "certificate": "local",
                    },
                    1,
                )
            self.assertEqual(before, access.path.read_bytes())
            self.assertIsNotNone(WebAccess(settings).tls)


class PublicBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)
        self.settings = config(self.temp.name)
        self.platform = Platform(self.settings)
        self.addCleanup(self.platform.close)
        self.console = WebConsole(self.platform)
        self.runner, self.url = await start_http(
            create_app(self.platform, console=self.console, public=True)
        )
        self.addAsyncCleanup(self.runner.cleanup)
        self.client = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.addAsyncCleanup(self.client.close)
        self.origin = self.settings["web_access"]["initial"]["origin"]
        self.headers = {"Host": "192.168.31.210:19443", "Origin": self.origin}

    async def post(self, path, body, csrf, expected=200, **headers):
        async with self.client.post(
            self.url + "/api/web/" + path,
            json=body,
            headers={**self.headers, "X-CSRF-Token": csrf, **headers},
        ) as response:
            data = await response.json()
            self.assertEqual(response.status, expected, data)
            return data

    async def login(self):
        async with self.client.get(self.url + "/api/web/session", headers=self.headers) as response:
            self.assertEqual(response.status, 200)
            self.assertNotIn("Secure", response.headers["Set-Cookie"])
            csrf = (await response.json())["csrf"]
        return (
            await self.post("login", {"username": "synthetic-admin", "password": PASSWORD}, csrf)
        )["csrf"]

    async def test_public_has_no_rpc_or_diagnostics_and_wrong_host_fails(self):
        for path in (
            "/internal/v1/origins/resolve",
            "/internal/v1/model-config/native/snapshot",
            "/health/ready",
            "/health/live",
        ):
            async with self.client.get(self.url + path, headers=self.headers) as response:
                self.assertEqual(response.status, 404)
        async with self.client.get(
            self.url + "/", headers={"Host": "evil.test", "X-Forwarded-Host": self.headers["Host"]}
        ) as response:
            self.assertEqual(response.status, 403)
        async with self.client.get(self.url + "/", headers=self.headers) as response:
            self.assertEqual(response.status, 200)

    async def test_settings_need_login_csrf_password_and_current_revision(self):
        csrf = await self.login()
        view = await self.post("access/view", {}, csrf)
        value = {"mode": "proxy", "origin": "https://bot.example.test", "certificate": None}
        body = {"value": value, "revision": view["revision"], "password": PASSWORD}
        await self.post("access/save", body, "wrong", 403)
        await self.post("access/save", {**body, "password": "wrong-password-123"}, csrf, 401)
        result = await self.post("access/save", body, csrf)
        self.assertTrue(result["restart_required"])
        self.assertEqual(result["active"]["origin"], self.origin)
        await self.post("access/save", body, csrf, 409)
        await self.post("access/view", {}, csrf, 403, Origin="https://evil.test")

    async def test_internal_app_still_requires_tls(self):
        runner, url = await start_http(create_app(self.platform, console=self.console))
        self.addAsyncCleanup(runner.cleanup)
        async with self.client.get(url + "/api/web/session", headers=self.headers) as response:
            self.assertEqual(response.status, 403)

    async def test_proxy_cookie_uses_configured_scheme_not_forwarded_headers(self):
        self.console.config["origin"] = "https://bot.example.test"
        self.console.access.current = {
            "mode": "proxy",
            "origin": "https://bot.example.test",
            "certificate": None,
        }
        async with self.client.get(
            self.url + "/api/web/session",
            headers={"Host": "bot.example.test", "X-Forwarded-Proto": "http"},
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("Secure", response.headers["Set-Cookie"])

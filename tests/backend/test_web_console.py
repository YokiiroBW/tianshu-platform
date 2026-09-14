import os
import tempfile
import unittest
from unittest.mock import patch

import aiohttp

from fixtures import ENV, bearer, start_http
from services.platform.server import create_app
from services.platform.service import Platform
from services.platform.web_console import COOKIE
from web_fixtures import PASSWORD, web_settings


class WebConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)
        self.config = web_settings(self.temp.name)
        self.platform = Platform(self.config)
        self.app = create_app(self.platform)
        self.runner, self.url = await start_http(self.app)
        self.config["web"]["origin"] = self.url
        self.addAsyncCleanup(self.runner.cleanup)
        self.client = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.addAsyncCleanup(self.client.close)

    async def get(self):
        async with self.client.get(self.url + "/api/web/session") as r:
            return r.status, await r.json()

    async def post(self, path, body, csrf, expected=200, **headers):
        async with self.client.post(
            self.url + "/api/web/" + path,
            json=body,
            headers={"Origin": self.url, "X-CSRF-Token": csrf, **headers},
        ) as r:
            data = await r.json()
            self.assertEqual(r.status, expected, data)
            self.assertEqual(r.headers["Cache-Control"], "no-store")
            return data

    async def login(self):
        _, session = await self.get()
        return await self.post(
            "login", {"username": "synthetic-admin", "password": PASSWORD}, session["csrf"]
        )

    async def test_real_login_rotation_static_and_logout(self):
        async with self.client.get(self.url + "/") as r:
            self.assertEqual(r.status, 200)
            self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])
        _, anonymous = await self.get()
        old_cookie = next(c.value for c in self.client.cookie_jar if c.key == COOKIE)
        logged = await self.login()
        cookie = next(c for c in self.client.cookie_jar if c.key == COOKIE)
        self.assertNotEqual(cookie.value, old_cookie)
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Strict")
        self.assertNotEqual(logged["csrf"], anonymous["csrf"])
        status, session = await self.get()
        self.assertEqual(status, 200)
        self.assertTrue(session["authenticated"])
        self.assertEqual(session["conversations"][0]["actors"], ["actor:a", "actor:b"])
        self.assertFalse(session["dialogue"]["available"])
        self.assertNotIn("assertion_ref", str(session))
        for token in ENV.values():
            self.assertNotIn(token, str(session))
        await self.post("logout", {}, logged["csrf"])
        self.assertFalse((await self.get())[1]["authenticated"])
        async with self.client.get(
            self.url + "/api/web/session", headers={"Cookie": f"{COOKIE}={old_cookie}"}
        ) as r:
            self.assertFalse((await r.json())["authenticated"])

    async def test_cross_origin_csrf_host_and_internal_cookie_denied(self):
        _, session = await self.get()
        body = {"username": "synthetic-admin", "password": PASSWORD}
        await self.post("login", body, "wrong", 403)
        await self.post("login", body, session["csrf"], 403, Origin="http://evil.invalid")
        await self.post("login", body, session["csrf"], 403, Host="evil.invalid")
        await self.post("login", body, session["csrf"], 403, Authorization=bearer("ADMIN"))
        async with self.client.post(
            self.url + "/internal/v1/origins/resolve",
            json={},
            headers={"Authorization": bearer("ADMIN")},
        ) as r:
            self.assertEqual(r.status, 403)
        async with self.client.post(
            self.url + "/api/web/login", json=body, headers={"X-CSRF-Token": session["csrf"]}
        ) as r:
            self.assertEqual(r.status, 403)

    async def test_forged_identity_and_unavailable_mutations(self):
        logged = await self.login()
        await self.post(
            "messages",
            {"text": "合成测试", "verified": True, "scope": "other"},
            logged["csrf"],
            503,
        )
        await self.post("cancel", {"turn_id": "other"}, logged["csrf"], 503)
        await self.post(
            "login",
            {"username": "synthetic-admin", "password": PASSWORD, "person_id": "other"},
            logged["csrf"],
            400,
        )
        with self.platform.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM source_inputs").fetchone()[0], 0)

    async def test_wrong_password_rate_limit(self):
        _, session = await self.get()
        for _ in range(5):
            await self.post(
                "login",
                {"username": "synthetic-admin", "password": "incorrect-password-014"},
                session["csrf"],
                401,
            )
        await self.post(
            "login", {"username": "synthetic-admin", "password": PASSWORD}, session["csrf"], 429
        )

    async def test_entry_revocation_removes_only_allowed_choice(self):
        await self.login()
        self.platform.origins.revoke(bearer("ADMIN"), "entry", "actor-a")
        status, session = await self.get()
        self.assertEqual(status, 200)
        self.assertEqual(session["conversations"][0]["actors"], ["actor:b"])
        self.platform.origins.revoke(bearer("ADMIN"), "entry", "input-entry")
        self.assertEqual((await self.get())[1]["conversations"], [])

    async def test_principal_revoke_and_credential_rotation_invalidate(self):
        await self.login()
        with patch.dict(os.environ, {"TS012_ADMIN": "rotated-synthetic-credential-014"}):
            status, _ = await self.get()
            self.assertEqual(status, 401)
            self.assertFalse((await self.get())[1]["authenticated"])
        await self.login()
        self.platform.origins.revoke(bearer("ADMIN"), "principal", "admin")
        self.assertEqual((await self.get())[0], 401)

    async def test_absolute_expiry_and_restart(self):
        with patch("services.platform.web_console.SESSION_TTL", -1):
            await self.login()
        self.assertFalse((await self.get())[1]["authenticated"])
        await self.login()
        runner, url = await start_http(create_app(self.platform))
        self.addAsyncCleanup(runner.cleanup)
        self.config["web"]["origin"] = url
        async with self.client.get(url + "/api/web/session") as r:
            self.assertFalse((await r.json())["authenticated"])

    async def test_disabled_by_default_and_body_budget(self):
        logged = await self.login()
        await self.post("messages", {"text": "x" * 20000}, logged["csrf"], 413)
        del self.config["web"]
        runner, url = await start_http(create_app(self.platform))
        self.addAsyncCleanup(runner.cleanup)
        async with self.client.get(url + "/api/web/session") as r:
            self.assertEqual(r.status, 503)
            self.assertEqual((await r.json())["code"], "web_not_configured")

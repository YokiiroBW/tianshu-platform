"""Synthetic first-use accounts exercise real HTTP, persistent storage and process races."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import aiohttp

from fixtures import ENV, start_http
from services.platform.__main__ import _preflight
from services.platform.contracts import Fault
from services.platform.runtime_health import Probe, health_inputs
from services.platform.server import create_app
from services.platform.service import (
    Platform,
    credential_present,
    registered_credentials,
    validate_settings,
)
from services.platform.web_account import WebAccount, password_hash
from services.platform.web_console import WebConsole
from web_fixtures import PASSWORD, web_settings

TOKEN = "synthetic-first-install-token-20260926"
NEW_PASSWORD = "synthetic-user-chosen-password-20260926"


def configure(directory, mode="create"):
    settings = web_settings(directory)
    settings["web_account"] = {"mode": mode}
    if mode == "create":
        settings["web_account"]["setup_token_env"] = "TS_SETUP_TOKEN"
        settings["web"].pop("username")
        settings["web"].pop("password_hash")
    return settings


class AccountStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {**ENV, "TS_SETUP_TOKEN": TOKEN})
        env.start()
        self.addCleanup(env.stop)
        self.settings = configure(self.temp.name)
        self.account = WebAccount(self.settings)

    def test_validation_preflight_and_reads_do_not_initialize(self):
        before = sorted(str(p) for p in Path(self.temp.name).rglob("*"))
        validate_settings(self.settings)
        result = _preflight(self.settings)
        self.assertTrue(result["requires_initialization"])
        self.assertNotEqual(result["checks"]["sidecars"], "failed")
        self.assertIsNone(self.account.read())
        self.assertEqual(before, sorted(str(p) for p in Path(self.temp.name).rglob("*")))
        platform = Platform(self.settings)
        self.addCleanup(platform.close)
        WebConsole(platform)
        self.assertFalse(self.account.path.exists())
        self.assertFalse(self.account.seal.exists())
        self.assertEqual(Probe(health_inputs(self.settings))._sidecars(), "ok")

    def test_create_requires_absent_deployment_credentials_and_separate_token(self):
        self.settings["web"]["username"] = "admin"
        with self.assertRaises(Fault):
            validate_settings(self.settings)
        self.settings["web"].pop("username")
        with patch.dict(os.environ, {"TS_SETUP_TOKEN": ENV["TS012_ADMIN"]}):
            with self.assertRaises(Fault):
                validate_settings(self.settings)
        self.settings["web_account"]["setup_token_env"] = "TS012_ADMIN"
        with self.assertRaises(Fault):
            validate_settings(self.settings)

    def test_commit_persistence_closed_setup_and_removable_install_token(self):
        verifier = password_hash(NEW_PASSWORD)
        record = self.account.create("chosen-owner", verifier)
        self.assertEqual(record, WebAccount(self.settings).read())
        self.assertEqual(record["version"], 1)
        self.assertNotIn(NEW_PASSWORD.encode(), self.account.path.read_bytes())
        with self.assertRaises(Fault) as caught:
            self.account.create("replacement", verifier)
        self.assertEqual(caught.exception.code, "setup_closed")
        with patch.dict(os.environ, {"TS_SETUP_TOKEN": ""}):
            validate_settings(self.settings)
            self.assertTrue(credential_present(self.settings, "TS_SETUP_TOKEN"))
        self.assertIn("TS_SETUP_TOKEN", registered_credentials(self.settings))

    def test_removing_opt_in_never_restores_the_deployment_password(self):
        self.settings["web_account"] = {"mode": "claim"}
        self.settings["web"].update(
            username="synthetic-admin", password_hash=password_hash(PASSWORD)
        )
        legacy = WebAccount({k: v for k, v in self.settings.items() if k != "web_account"})
        account = WebAccount(self.settings)
        account.create("chosen-owner", password_hash(NEW_PASSWORD))
        with self.assertRaises(Fault):
            legacy.read()
        self.settings.pop("web_account")
        for read in (
            lambda: WebAccount(self.settings),
            lambda: validate_settings(self.settings),
            lambda: Platform(self.settings),
        ):
            with self.assertRaises(Fault) as caught:
                read()
            self.assertEqual(caught.exception.code, "account_unavailable")
        account.path.unlink()
        with self.assertRaises(Fault):
            WebAccount(self.settings)

    def test_missing_corrupt_and_empty_account_fail_closed(self):
        self.account.create("chosen-owner", password_hash(NEW_PASSWORD))
        original = self.account.path.read_bytes()
        self.account.path.unlink()
        with self.assertRaises(Fault) as caught:
            self.account.read()
        self.assertEqual(caught.exception.code, "account_unavailable")
        self.assertEqual(Probe(health_inputs(self.settings))._account_state(), "failed")
        self.account.path.write_bytes(b"not a database")
        with self.assertRaises(Fault):
            validate_settings(self.settings)
        self.account.path.write_bytes(original)
        with closing(sqlite3.connect(self.account.path)) as db:
            db.execute("DELETE FROM administrator")
            db.commit()
        with self.assertRaises(Fault):
            self.account.read()
        self.account.path.write_bytes(original)
        self.account.seal.unlink()
        with self.assertRaises(Fault):
            self.account.read()

    def test_write_failure_never_commits_half_account_or_reopens_setup(self):
        with patch.object(self.account, "_seal", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(Fault):
                self.account.create("chosen-owner", password_hash(NEW_PASSWORD))
        with closing(sqlite3.connect(self.account.path)) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master").fetchall(), [])
        with self.assertRaises(Fault):
            self.account.read()

    def test_two_processes_create_exactly_one_account(self):
        script = """import json,sys
from services.platform.web_account import WebAccount
from services.platform.contracts import Fault
try:
 WebAccount(json.loads(sys.argv[1])).create(sys.argv[2],sys.argv[3]); print('created')
except Fault as exc: print(exc.code)
"""
        verifier = password_hash(NEW_PASSWORD)
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, json.dumps(self.settings), name, verifier],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for name in ("first", "second")
        ]
        results = [p.communicate(timeout=15) for p in processes]
        self.assertTrue(all(p.returncode == 0 for p in processes), results)
        self.assertEqual(sum(out.strip() == "created" for out, _ in results), 1, results)
        self.assertTrue(
            all(
                out.strip() in {"created", "setup_closed", "account_unavailable"}
                for out, _ in results
            ),
            results,
        )
        self.assertIn(self.account.read()["username"], {"first", "second"})


class AccountHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {**ENV, "TS_SETUP_TOKEN": TOKEN})
        env.start()
        self.addCleanup(env.stop)
        self.settings = configure(self.temp.name)
        self.client = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.addAsyncCleanup(self.client.close)

    async def start(self, claim=False):
        if claim:
            self.settings["web_account"] = {"mode": "claim"}
            self.settings["web"].update(
                username="synthetic-admin", password_hash=password_hash(PASSWORD)
            )
        self.platform = Platform(self.settings)
        self.addCleanup(self.platform.close)
        self.console = WebConsole(self.platform)
        runner, self.url = await start_http(create_app(self.platform, console=self.console))
        self.settings["web"]["origin"] = self.url
        self.addAsyncCleanup(runner.cleanup)

    async def session(self, expected=200):
        async with self.client.get(self.url + "/api/web/session") as response:
            result = await response.json()
            self.assertEqual(response.status, expected, result)
            return result

    async def post(self, path, body, csrf, expected=200, **headers):
        async with self.client.post(
            self.url + "/api/web/" + path,
            json=body,
            headers={"Origin": self.url, "X-CSRF-Token": csrf, **headers},
        ) as response:
            result = await response.json()
            self.assertEqual(response.status, expected, result)
            return result

    async def setup(self):
        session = await self.session()
        return await self.post(
            "setup",
            {"username": "chosen-owner", "password": NEW_PASSWORD, "setup_token": TOKEN},
            session["csrf"],
        )

    async def test_create_signin_restart_and_real_capability_projection(self):
        await self.start()
        first = await self.session()
        self.assertEqual(
            first["onboarding"],
            {"state": "create_admin", "credential_source": "setup", "setup_token_required": True},
        )
        self.assertNotIn("username", first)
        await self.post(
            "login", {"username": "synthetic-admin", "password": PASSWORD}, first["csrf"], 409
        )
        created = await self.setup()
        ready = await self.session()
        self.assertEqual(ready["username"], "chosen-owner")
        self.assertEqual(ready["onboarding"]["state"], "ready")
        self.assertFalse(ready["dialogue"]["available"])
        self.assertEqual(ready["conversations"][0]["actors"], ["actor:a", "actor:b"])
        self.assertNotIn(TOKEN, json.dumps(ready))
        self.assertNotIn(NEW_PASSWORD, json.dumps(ready))
        self.assertTrue(self.console.verify_password(NEW_PASSWORD))
        self.assertFalse(self.console.verify_password(PASSWORD))
        await self.post(
            "setup",
            {"username": "other", "password": NEW_PASSWORD, "setup_token": TOKEN},
            created["csrf"],
            409,
        )
        await self.start()
        session = await self.session()
        self.assertEqual(session["onboarding"]["state"], "sign_in")
        self.assertEqual(session["onboarding"]["credential_source"], "account")
        await self.post(
            "login", {"username": "chosen-owner", "password": NEW_PASSWORD}, session["csrf"]
        )
        self.assertEqual((await self.session())["onboarding"]["state"], "ready")

    async def test_setup_csrf_origin_authorization_extra_fields_and_token_limits(self):
        await self.start()
        session = await self.session()
        body = {"username": "chosen-owner", "password": NEW_PASSWORD, "setup_token": TOKEN}
        await self.post("setup", body, "wrong", 403)
        await self.post("setup", body, session["csrf"], 403, Origin="https://evil.invalid")
        await self.post("setup", {**body, "principal": "admin"}, session["csrf"], 400)
        with patch.dict(os.environ, {"TS_SETUP_TOKEN": ENV["TS012_ADMIN"]}):
            await self.post(
                "setup", {**body, "setup_token": ENV["TS012_ADMIN"]}, session["csrf"], 503
            )
        for _ in range(5):
            await self.post("setup", {**body, "setup_token": "wrong"}, session["csrf"], 401)
        await self.post("setup", body, session["csrf"], 429)
        self.assertFalse(self.console.account.path.exists())

    async def test_claim_requires_old_authentication_and_replaces_all_password_consumers(self):
        await self.start(claim=True)
        first = await self.session()
        self.assertEqual(first["onboarding"]["state"], "sign_in")
        self.assertNotIn("username", first)
        claim = {"username": "chosen-owner", "password": NEW_PASSWORD, "current_password": PASSWORD}
        await self.post("account/claim", claim, first["csrf"], 401)
        await self.post("setup", {"username": "x", "password": NEW_PASSWORD}, first["csrf"], 409)
        logged = await self.post(
            "login", {"username": "synthetic-admin", "password": PASSWORD}, first["csrf"]
        )
        self.assertEqual((await self.session())["onboarding"]["state"], "claim_admin")
        old_session = next(s for s in self.console.sessions.values() if s["authenticated"])
        other_console = WebConsole(self.platform)
        old_fingerprint = other_console.authority()[0]
        await self.post(
            "account/claim", {**claim, "current_password": NEW_PASSWORD}, logged["csrf"], 401
        )
        claimed = await self.post("account/claim", claim, logged["csrf"])
        self.assertNotEqual(claimed["csrf"], logged["csrf"])
        self.assertFalse(self.console.session_live(old_session))
        self.assertNotEqual(old_fingerprint, other_console.authority()[0])
        self.assertFalse(other_console.verify_password(PASSWORD))
        self.assertTrue(other_console.verify_password(NEW_PASSWORD))
        self.assertEqual((await self.session())["username"], "chosen-owner")
        await self.post("models/unlock", {"password": PASSWORD}, claimed["csrf"], 401)
        await self.post("models/unlock", {"password": NEW_PASSWORD}, claimed["csrf"])
        await self.post("account/claim", claim, claimed["csrf"], 409)
        await self.post("logout", {}, claimed["csrf"])
        anonymous = await self.session()
        await self.post(
            "login", {"username": "synthetic-admin", "password": PASSWORD}, anonymous["csrf"], 401
        )
        await self.post(
            "login", {"username": "chosen-owner", "password": NEW_PASSWORD}, anonymous["csrf"]
        )

    async def test_loss_after_setup_fails_closed_while_running(self):
        await self.start()
        await self.setup()
        self.console.account.path.unlink()
        self.assertEqual((await self.session(503))["code"], "account_unavailable")

    async def test_committed_account_survives_a_failed_success_response(self):
        await self.start()
        first = await self.session()
        original = self.console.authority

        def interrupted_response():
            if self.console.account.read() is not None:
                raise Fault("dependency_unavailable", 503)
            return original()

        with patch.object(self.console, "authority", interrupted_response):
            await self.post(
                "setup",
                {"username": "chosen-owner", "password": NEW_PASSWORD, "setup_token": TOKEN},
                first["csrf"],
                503,
            )
        session = await self.session()
        self.assertEqual(session["onboarding"]["state"], "sign_in")
        await self.post(
            "login", {"username": "chosen-owner", "password": NEW_PASSWORD}, session["csrf"]
        )

    async def test_revoked_server_principal_cannot_initialize(self):
        await self.start()
        first = await self.session()
        with self.platform.store.connect(write=True) as db:
            db.execute("INSERT INTO revoked_principals VALUES ('admin')")
        await self.post(
            "setup",
            {"username": "chosen-owner", "password": NEW_PASSWORD, "setup_token": TOKEN},
            first["csrf"],
            401,
        )
        self.assertFalse(self.console.account.path.exists())

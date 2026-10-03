"""Administrator saved HA/AssetLink connections through real same-origin HTTP and TLS peers."""

import asyncio
import os
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp

from asset_fixtures import TOKEN_A, Synthetic, start
from fixtures import ENV, start_http
from home_fixtures import TOKEN as HOME_TOKEN
from home_fixtures import home_app, reserve, start_home
from services.platform.external_catalog import ExternalCatalog
from services.platform.contracts import Fault
from services.platform.server import create_app
from services.platform.service import Platform
from services.platform.web_console import WebConsole
from web_fixtures import PASSWORD, web_settings


class ExternalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {**ENV, "TS019_ASSET_READ": "synthetic-external-reader-token-0001"})
        env.start()
        self.addCleanup(env.stop)
        self.ca = Path(self.temp.name) / "localhost.pem"
        self.key = Path(self.temp.name) / "localhost-key.pem"
        import subprocess

        subprocess.run(
            [os.environ["TS013_TLS_PYTHON"], str(Path(__file__).with_name("make_tls_fixture.py")), self.temp.name],
            check=True,
            capture_output=True,
        )
        self.asset_port = reserve()
        self.control_port = reserve()
        self.peer = Synthetic()
        for runner in await start(self.peer, str(self.ca), str(self.key), self.asset_port, self.control_port):
            self.addAsyncCleanup(runner.cleanup)
        self.ha_runner, self.ha_url = await start_home(home_app())
        self.addAsyncCleanup(self.ha_runner.cleanup)
        self.directory = str(Path(self.temp.name) / "external")
        ExternalCatalog(self.directory, create=True)
        settings = web_settings(self.temp.name)
        settings["principals"]["admin"]["actions"].append("external.manage")
        settings["principals"]["assetreader"] = {
            "kind": "service", "service": "platform", "token_env": "TS019_ASSET_READ",
            "actions": ["asset.read"], "asset_connections": ["library-a"],
        }
        settings["asset_connections"] = {"library-a": {"managed_external": True}}
        settings["web_assets"] = {
            "enabled": True, "principal": "assetreader", "allowed_connections": ["library-a"],
        }
        settings["web_external"] = {
            "directory": self.directory,
            "allowed_cidrs": ["127.0.0.0/8"],
            "assets_connection_id": "library-a",
        }
        self.platform = Platform(settings)
        self.app = create_app(self.platform)
        self.console = next(
            route.handler.__self__
            for route in self.app.router.routes()
            if isinstance(getattr(route.handler, "__self__", None), WebConsole)
        )
        self.runner, self.url = await start_http(self.app)
        self.addAsyncCleanup(self.runner.cleanup)
        settings["web"]["origin"] = self.url
        self.client = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.addAsyncCleanup(self.client.close)
        async with self.client.get(self.url + "/api/web/session") as response:
            csrf = (await response.json())["csrf"]
        self.csrf = (await self.call("login", {"username": "synthetic-admin", "password": PASSWORD}, csrf))["csrf"]

    async def call(self, path, body, csrf=None, expected=200, client=None):
        async with (client or self.client).post(
            self.url + "/api/web/" + path,
            json=body,
            headers={"Origin": self.url, "X-CSRF-Token": csrf or self.csrf},
        ) as response:
            answer = await response.json()
            self.assertEqual(response.status, expected, answer)
            return answer

    async def unlock(self):
        await self.call("external/unlock", {"password": PASSWORD})

    async def test_weather_routes_keep_session_csrf_and_external_store_boundaries(self):
        from test_web_weather import CITY, CURRENT, KEY

        actor = {"actor_id": "actor:one"}
        with patch.object(self.console.life, "route", AsyncMock(return_value={})):
            await self.call("weather/state", actor, csrf="wrong-csrf", expected=403)
            state = await self.call("weather/state", actor)
            self.assertEqual(state["code"], "weather_not_configured")
            configured = await self.call("weather/configure", {
                **actor, "host": "example.qweatherapi.com",
                "credential": {"action": "replace", "value": KEY},
                "expected_revision": state["revision"], "client_id": str(uuid.uuid4()),
            })
            self.assertNotIn(KEY, str(configured))
            with patch.object(self.console.weather, "request", AsyncMock(return_value={
                "code": "200", "location": [CITY],
            })):
                await self.call("weather/location", {
                    **actor, "location_id": CITY["id"],
                    "expected_revision": configured["revision"], "client_id": str(uuid.uuid4()),
                })
            with patch.object(self.console.weather, "request", AsyncMock(return_value=CURRENT)):
                current = await self.call("weather/current", actor)
            self.assertEqual(current["weather"]["temp"], "22.3")
            self.assertEqual(current["location"]["tz"], "Asia/Shanghai")
            self.assertNotIn(KEY, str(current))
            # Existing connectors remain readable after reopening a catalog containing weather.
            ExternalCatalog.verify_existing(self.directory)
            view = await self.call("external/view", {})
            self.assertFalse(view["assets"]["configured"])

    def save_body(self, kind, revision, value, credential, ca=None):
        return {
            "kind": kind, "expected_revision": revision, "client_id": str(uuid.uuid4()),
            "value": value,
            "credential": {"action": "replace", "value": credential},
            "ca": ca or {"action": "keep"},
        }

    async def test_assets_save_test_rotate_and_scope(self):
        view = await self.call("external/view", {})
        self.assertFalse(view["assets"]["configured"])
        await self.call("external/save", {}, expected=403)
        await self.call("external/unlock", {"password": "wrong-password-000000"}, expected=401)
        await self.unlock()
        endpoint = f"https://127.0.0.1:{self.asset_port}/assetlink/v1/control"
        save = self.save_body(
            "assets", 0, {"enabled": True, "endpoint": endpoint}, TOKEN_A,
            {"action": "replace", "value": self.ca.read_text()},
        )
        bad = {**save, "client_id": str(uuid.uuid4()), "value": {"enabled": True, "endpoint": "https://127.0.0.1/admin"}}
        await self.call("external/save", bad, expected=400)
        saved = await self.call("external/save", save)
        self.assertEqual(saved["revision"], 1)
        self.assertTrue(saved["applied"])
        await self.call("external/save", {**save, "client_id": str(uuid.uuid4())}, expected=409)
        view = await self.call("external/view", {})
        self.assertTrue(view["assets"]["credential_configured"])
        self.assertTrue(view["assets"]["ca_configured"])
        self.assertNotIn(TOKEN_A, str(view))
        self.assertNotIn(TOKEN_A.encode(), (Path(self.directory) / "external.sqlite").read_bytes())
        self.assertIsNone(view["assets"]["last_test"])
        result = await self.call("external/test", {"kind": "assets"})
        self.assertEqual(result["state"], "connected")
        await self.call("assets/connection", {"connection_id": "library-a"})
        libraries = await self.call("assets/libraries", {})
        self.assertTrue(libraries["libraries"])
        changed = {**save, "client_id": str(uuid.uuid4()), "expected_revision": 1,
                   "credential": {"action": "clear"}, "ca": {"action": "keep"}}
        await self.call("external/save", changed)
        view = await self.call("external/view", {})
        self.assertFalse(view["assets"]["credential_configured"])
        self.assertIsNone(view["assets"]["last_test"])
        await self.call("external/test", {"kind": "assets"}, expected=503)
        self.assertIsNone(ExternalCatalog(self.directory).snapshot()[1]["assets"]["credential"])
        with closing(sqlite3.connect(Path(self.directory) / "external.sqlite")) as db:
            db.execute("UPDATE connections SET document='{}' WHERE kind='assets'")
            db.commit()
        with self.assertRaises(Fault) as error:
            ExternalCatalog.verify_existing(self.directory)
        self.assertEqual(error.exception.code, "external_store_unavailable")

    async def test_expired_unlock_and_live_permission_gate(self):
        await self.unlock()
        session = next(item for item in self.console.sessions.values() if item["authenticated"])
        session["external_unlock"] = self.console.clock() - 1
        locked = await self.call("external/test", {"kind": "assets"}, expected=403)
        self.assertEqual(locked["code"], "external_locked")
        self.platform.auth.principals["admin"]["actions"].remove("external.manage")
        refused = await self.call("external/view", {}, expected=403)
        self.assertEqual(refused["code"], "external_manage_required")
        self.assertNotIn("assets", refused)

    async def test_in_flight_test_cannot_confirm_new_revision(self):
        await self.unlock()
        endpoint = f"https://127.0.0.1:{self.asset_port}/assetlink/v1/control"
        first = self.save_body(
            "assets", 0, {"enabled": True, "endpoint": endpoint}, TOKEN_A,
            {"action": "replace", "value": self.ca.read_text()},
        )
        await self.call("external/save", first)
        other = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.addAsyncCleanup(other.close)
        async with other.get(self.url + "/api/web/session") as response:
            csrf = (await response.json())["csrf"]
        second_login = await self.call(
            "login", {"username": "synthetic-admin", "password": PASSWORD},
            csrf=csrf, client=other,
        )
        other_csrf = second_login["csrf"]
        await self.call("external/unlock", {"password": PASSWORD}, csrf=other_csrf, client=other)
        self.peer.scenario = "stall"
        pending = asyncio.create_task(self.call("external/test", {"kind": "assets"}, expected=409))
        await asyncio.wait_for(self.peer.entered.wait(), 5)
        replaced = {
            **first, "client_id": str(uuid.uuid4()), "expected_revision": 1,
            "credential": {"action": "replace", "value": "synthetic-rotated-asset-token-0001"},
            "ca": {"action": "keep"},
        }
        await self.call("external/save", replaced)
        self.peer.release.set()
        self.assertEqual((await pending)["code"], "revision_conflict")
        view = await self.call("external/view", {})
        self.assertIsNone(view["assets"]["last_test"])
        other_view = await self.call("external/view", {}, csrf=other_csrf, client=other)
        self.assertFalse(other_view["unlocked"])
        await self.call("external/test", {"kind": "assets"}, csrf=other_csrf, client=other, expected=403)

    async def test_home_saved_one_read_and_no_control_template(self):
        await self.unlock()
        value = {
            "enabled": True, "base_url": self.ha_url, "allow_private_http": True,
            "entities": [{"entity_id": "light.study", "label": "书房灯", "kind": "light"}],
        }
        await self.call("external/save", self.save_body("home", 0, value, HOME_TOKEN))
        result = await self.call("external/test", {"kind": "home"})
        self.assertEqual(result["state"], "connected")
        self.assertEqual(result["revision"], 1)
        home = await self.call("home/view", {})
        self.assertEqual(home["entities"][0]["availability"], "current")
        self.assertEqual(home["connector"]["templates"], 0)
        self.assertEqual((await self.call("external/view", {}))["home"]["last_test"], result)
        await self.call("external/test", {"kind": "home", "url": "http://127.0.0.1:1"}, expected=400)
        public = self.save_body(
            "home", 1, {**value, "base_url": "http://8.8.8.8:8123"}, HOME_TOKEN,
        )
        await self.call("external/save", public, expected=403)
        private_key = self.save_body(
            "home", 1, value, HOME_TOKEN,
            {"action": "replace", "value": self.key.read_text()},
        )
        await self.call("external/save", private_key, expected=400)

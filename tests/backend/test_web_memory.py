"""Platform login, real origin issuance and a synthetic Memory HTTPS browser peer."""

import asyncio
import os
import socket
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import aiohttp
from aiohttp import web

from fixtures import ENV, start_http
from services.platform.contracts import canonical
from services.platform.origins import channel_key
from services.platform.server import create_app
from services.platform.service import Platform
from web_fixtures import PASSWORD, web_settings


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class MemoryBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tool = os.environ.get("TS013_TLS_PYTHON")
        if not tool:
            raise unittest.SkipTest("TS013_TLS_PYTHON not configured")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        subprocess.run(
            [tool, str(Path(__file__).with_name("make_tls_fixture.py")), self.temp.name],
            check=True,
            capture_output=True,
        )
        cert = str(Path(self.temp.name) / "localhost.pem")
        key = str(Path(self.temp.name) / "localhost-key.pem")
        self.port = port()
        self.requests = []
        self.pause_actor = None
        self.peer_entered = asyncio.Event()
        self.peer_release = asyncio.Event()
        env = mock.patch.dict(
            os.environ,
            {**ENV, "TEST_MEMORY_BROWSER": "synthetic-browser-memory-reader-0001"},
        )
        env.start()
        self.addCleanup(env.stop)
        self.config = web_settings(self.temp.name)
        self.config["web_memory"] = {
            "enabled": True,
            "base_url": f"https://127.0.0.1:{self.port}",
            "token_env": "TEST_MEMORY_BROWSER",
            "ca_file": cert,
            "entry_id": "actor-a",
            "runtime_roles": True,
        }
        self.config["principals"]["admin"]["actions"].append("memory.read")
        self.config["principals"]["memory_resolver"]["resolver"]["caller"] = "platform"
        entry = self.config["entries"]["actor-a"]
        entry["routes"].append({"caller": "platform", "receiver": "memory", "purpose": "dialogue"})
        self.platform = Platform(self.config)
        with self.platform.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO identities VALUES(?,?,?)",
                (canonical(entry["account"]), "person:synthetic", 1),
            )
            db.execute(
                "INSERT INTO channels VALUES(?,?)",
                (channel_key(entry), "conversation:synthetic"),
            )
        app = web.Application()
        app.router.add_post("/internal/v1/memory/browser/{name}", self.peer)
        self.peer_runner = web.AppRunner(app, access_log=None)
        await self.peer_runner.setup()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        await web.TCPSite(self.peer_runner, "127.0.0.1", self.port, ssl_context=context).start()
        self.addAsyncCleanup(self.peer_runner.cleanup)
        self.runner, self.url = await start_http(create_app(self.platform))
        self.addAsyncCleanup(self.runner.cleanup)
        self.config["web"]["origin"] = self.url
        self.client = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.addAsyncCleanup(self.client.close)
        async with self.client.get(self.url + "/api/web/session") as response:
            csrf = (await response.json())["csrf"]
        async with self.client.post(
            self.url + "/api/web/login",
            json={"username": "synthetic-admin", "password": PASSWORD},
            headers={"Origin": self.url, "X-CSRF-Token": csrf},
        ) as response:
            self.assertEqual(response.status, 200)
            self.csrf = (await response.json())["csrf"]

    async def peer(self, request):
        self.assertEqual(
            request.headers["Authorization"], "Bearer synthetic-browser-memory-reader-0001"
        )
        body = await request.json()
        self.requests.append(body)
        resolved = self.platform.origins.resolve(
            "Bearer " + ENV["TS012_MEMORY_RESOLVER"],
            {
                "schema_version": 1,
                "request_id": body["request_id"],
                "assertion_ref": body["origin"]["assertion_ref"],
            },
        )
        self.assertEqual(resolved["context"]["allowed_scope"], body["scope"])
        if body["scope"]["actor_id"] == self.pause_actor:
            self.peer_entered.set()
            await self.peer_release.wait()
        self.assertEqual(
            resolved["context"]["verified_account"],
            self.config["web"]["account"]
            if "account" in self.config["web"]
            else self.config["principals"]["admin"]["account"],
        )
        common = {
            "schema_version": 1,
            "request_id": body["request_id"],
            "scope": body["scope"],
            "verified_at": "2026-09-27T00:00:00Z",
            "scope_version": 1,
        }
        name = request.match_info["name"]
        if name == "overview":
            common.update(memory_group_count=0, counts_truncated=False)
        else:
            common.update(items=[], next_cursor=None)
        return web.json_response(common)

    async def call(self, name, body, expected=200):
        async with self.client.post(
            self.url + "/api/web/memory/" + name,
            json=body,
            headers={"Origin": self.url, "X-CSRF-Token": self.csrf},
        ) as response:
            answer = await response.json()
            self.assertEqual(response.status, expected, answer)
            return answer

    async def test_origin_scope_and_pages(self):
        state = await self.call("state", {})
        self.assertEqual(state["actor_id"], "actor:a")
        overview = await self.call("overview", {})
        self.assertEqual(overview["memory_group_count"], 0)
        self.assertNotIn("scope", overview)
        self.assertNotIn("request_id", overview)
        subjects = await self.call("subjects", {"limit": 20, "cursor": None})
        self.assertEqual(subjects["items"], [])
        self.assertNotEqual(self.requests[0]["origin"], self.requests[1]["origin"])
        records = await self.call("records", {"subject": None, "limit": 20, "cursor": None})
        self.assertEqual(records["items"], [])
        status = await self.call("state", {})
        self.assertIsNotNone(status["peer"]["verified_at"])

    async def test_missing_binding_and_browser_scope_claim_refused(self):
        with self.platform.store.connect(write=True) as db:
            db.execute("DELETE FROM identities")
        answer = await self.call("overview", {}, 503)
        self.assertEqual(answer["code"], "memory_identity_not_ready")
        self.assertEqual(self.requests, [])
        answer = await self.call(
            "records",
            {"subject": None, "limit": 20, "cursor": None, "scope": {"actor_id": "other"}},
            400,
        )
        self.assertEqual(answer["code"], "invalid_input")

    async def test_current_operator_role_directory_and_derived_origin(self):
        role = {
            "actor_id": "actor:role-b",
            "operator": "admin",
            "name": "小岚",
            "version": 7,
            "state": "active",
            "enabled": True,
            "capabilities": ["dialogue", "memory.read"],
        }
        other = {**role, "actor_id": "actor:other", "operator": "someone-else"}
        self.platform.role_runtime.config = {"enabled": True}
        self.platform.role_runtime.directory = lambda: [role, other]
        state = await self.call("state", {})
        self.assertEqual([row["id"] for row in state["roles"]], ["actor:a", "actor:role-b"])
        self.assertEqual(state["roles"][1]["label"], "小岚")
        selected = {"role_id": "actor:role-b", "role_version": 7}
        await self.call("overview", selected)
        self.assertEqual(self.requests[-1]["scope"]["actor_id"], "actor:role-b")
        self.assertNotIn("role_id", self.requests[-1])
        self.assertFalse(any(key.startswith("memory-view-") for key in self.platform.auth.entries))
        self.assertEqual(
            (await self.call("overview", {"role_id": "actor:other", "role_version": 7}, 403))[
                "code"
            ],
            "forbidden",
        )
        self.assertEqual(
            (await self.call("overview", {"role_id": None, "role_version": None}, 400))["code"],
            "invalid_input",
        )
        self.assertEqual(
            (await self.call("overview", {"role_id": "actor:role-b", "role_version": 6}, 409))[
                "code"
            ],
            "scope_changed",
        )
        role["capabilities"] = ["dialogue"]
        self.assertEqual(
            (await self.call("overview", selected, 403))["code"], "memory_read_disabled"
        )
        role["capabilities"] = ["dialogue", "memory.read"]
        role["enabled"] = False
        self.assertEqual((await self.call("overview", selected, 403))["code"], "role_disabled")
        self.assertEqual(self.requests[-1]["scope"]["actor_id"], "actor:role-b")
        managed_default = {**role, "actor_id": "actor:a", "name": "默认角色", "version": 3}
        self.platform.role_runtime.directory = lambda: [managed_default]
        default_state = await self.call("state", {})
        self.assertFalse(default_state["roles"][0]["available"])
        self.assertEqual((await self.call("overview", {}, 403))["code"], "role_disabled")

    async def test_role_version_change_during_slow_read_discards_response(self):
        role = {
            "actor_id": "actor:role-b",
            "operator": "admin",
            "name": "小岚",
            "version": 1,
            "state": "active",
            "enabled": True,
            "capabilities": ["dialogue", "memory.read"],
        }
        self.platform.role_runtime.config = {"enabled": True}
        self.platform.role_runtime.directory = lambda: [role]
        self.pause_actor = "actor:role-b"
        pending = asyncio.create_task(
            self.call("overview", {"role_id": "actor:role-b", "role_version": 1}, 409)
        )
        await asyncio.wait_for(self.peer_entered.wait(), timeout=3)
        role["version"] = 2
        self.peer_release.set()
        self.assertEqual((await pending)["code"], "scope_changed")
        self.assertFalse(any(key.startswith("memory-view-") for key in self.platform.auth.entries))

    async def test_concurrent_same_role_origins_are_independent_after_cancel(self):
        role = {
            "actor_id": "actor:role-b",
            "operator": "admin",
            "name": "小岚",
            "version": 1,
            "state": "active",
            "enabled": True,
            "capabilities": ["dialogue", "memory.read"],
        }
        self.platform.role_runtime.config = {"enabled": True}
        self.platform.role_runtime.directory = lambda: [role]
        self.pause_actor = "actor:role-b"
        selected = {"role_id": "actor:role-b", "role_version": 1}
        abandoned = asyncio.create_task(self.call("overview", selected))
        retained = asyncio.create_task(
            self.call(
                "records",
                {
                    **selected,
                    "subject": None,
                    "limit": 20,
                    "cursor": None,
                },
            )
        )
        await asyncio.wait_for(self.peer_entered.wait(), timeout=3)
        for _ in range(100):
            if sum(key.startswith("memory-view-") for key in self.platform.auth.entries) == 2:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(
            sum(key.startswith("memory-view-") for key in self.platform.auth.entries), 2
        )
        abandoned.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await abandoned
        self.peer_release.set()
        self.assertEqual((await retained)["items"], [])
        self.assertFalse(any(key.startswith("memory-view-") for key in self.platform.auth.entries))

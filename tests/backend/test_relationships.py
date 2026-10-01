"""Real Platform login/origin boundary with a narrow synthetic Memory port."""

import asyncio
import copy
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import aiohttp

from fixtures import ENV, start_http
from web_fixtures import PASSWORD, web_settings
from services.platform.contracts import Fault, canonical
from services.platform.origins import channel_key
from services.platform.server import create_app
from services.platform.service import Platform, validate_settings

ROOT = Path(os.environ["TS012_CONTRACT_DIR"]).parents[2]
SCHEMA = ROOT / "contracts/role-relationship/v1/schema.json"
SELECTION = {
    "role_id": "actor:a",
    "role_version": 0,
    "person_id": "person:one",
    "people_after": None,
}


def settings(directory, static=None):
    config = web_settings(directory, static=static)
    config["principals"]["admin"]["actions"] += ["memory.read", "role.manage", "qq.admin.view"]
    config["principals"]["memory_resolver"]["resolver"]["caller"] = "platform"
    config["entries"]["actor-a"]["routes"].append(
        {"caller": "platform", "receiver": "memory", "purpose": "dialogue"}
    )
    config["web_memory"] = {
        "enabled": True,
        "base_url": "https://127.0.0.1:4998",
        "token_env": "TS116_MEMORY_READ",
        "entry_id": "actor-a",
        "runtime_roles": True,
    }
    config["web_qq_profiles"] = {
        "base_url": "https://127.0.0.1:4998",
        "token_env": "TS116_PROFILES",
    }
    config["web_relationships"] = {
        "enabled": True,
        "candidate_schema_path": str(SCHEMA),
        "memory": {
            "base_url": "https://127.0.0.1:4998",
            "token_env": "TS116_MANAGER",
            "timeout_seconds": 2,
        },
    }
    return config


SYNTHETIC_ENV = {
    **ENV,
    "TS116_MEMORY_READ": "synthetic-ts116-read-credential-0001",
    "TS116_PROFILES": "synthetic-ts116-profiles-credential-0001",
    "TS116_MANAGER": "synthetic-ts116-manager-credential-0001",
}


class RelationshipTests(unittest.IsolatedAsyncioTestCase):
    async def test_serve_entrypoint_exposes_relationship_routes_on_public_http(self):
        from make_tls_fixture import generate
        from services.platform.__main__ import serve_forever
        from services.platform.transport import server_tls

        def port():
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                return listener.getsockname()[1]

        directory = Path(self.directory.name) / "entrypoint"
        directory.mkdir()
        config = settings(directory)
        generate(str(directory))
        config["mode"] = "service_https"
        config["tls"] = {
            "certificate_file": str(directory / "localhost.pem"),
            "private_key_file": str(directory / "localhost-key.pem"),
        }
        internal_port, public_port = port(), port()
        origin = f"http://127.0.0.1:{public_port}"
        config["web"]["origin"] = origin
        config["web_access"] = {
            "host": "127.0.0.1",
            "port": public_port,
            "certificates": {},
            "initial": {"mode": "http", "origin": origin, "certificate": None},
        }
        platform = Platform(config)
        called = []

        async def relationship_route(console, name, body, session):
            called.append((name, body, session["authenticated"]))
            return {"catalog_reached": True}

        platform.relationships.route = relationship_route
        ready, stopping = asyncio.Event(), []

        def install(stop):
            stopping.append(stop)
            ready.set()

        task = asyncio.create_task(
            serve_forever(
                platform,
                Mock(),
                host="127.0.0.1",
                port=internal_port,
                tls=server_tls(config["tls"]),
                install_signals=install,
            )
        )
        try:
            await asyncio.wait_for(ready.wait(), 5)
            async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
                async with client.get(origin + "/api/web/session") as response:
                    self.assertEqual(response.status, 200)
                    csrf = (await response.json())["csrf"]
                async with client.post(
                    origin + "/api/web/login",
                    json={"username": "synthetic-admin", "password": PASSWORD},
                    headers={"Origin": origin, "X-CSRF-Token": csrf},
                ) as response:
                    self.assertEqual(response.status, 200)
                    csrf = (await response.json())["csrf"]
                async with client.post(
                    origin + "/api/web/relationships/catalog",
                    json={},
                    headers={"Origin": origin, "X-CSRF-Token": csrf},
                ) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(await response.json(), {"catalog_reached": True})
            self.assertEqual(called, [("catalog", {}, True)])
        finally:
            if stopping:
                stopping[0].set()
            elif not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=False)

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        env = patch.dict(os.environ, SYNTHETIC_ENV)
        env.start()
        self.addCleanup(env.stop)
        self.config = settings(self.directory.name)
        self.platform = Platform(self.config)
        self.addCleanup(self.platform.close)
        entry = self.config["entries"]["actor-a"]
        with self.platform.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO identities VALUES(?,?,?)",
                (canonical(entry["account"]), "person:operator", 1),
            )
            db.execute(
                "INSERT INTO channels VALUES(?,?)", (channel_key(entry), "conversation:operator")
            )
        self.people = [
            {
                "qq_id": "10001",
                "person_id": "person:one",
                "display_name": "一号人物",
                "aliases": [],
            },
            {
                "qq_id": "10002",
                "person_id": "person:two",
                "display_name": "二号人物",
                "aliases": [],
            },
        ]

        async def profiles(body):
            assert set(body) == {"limit", "after"}
            return {"items": copy.deepcopy(self.people), "next_cursor": None}

        self.platform.qq_admin._profiles = profiles
        self.calls = []
        self.fail = None
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.pause = False
        self.projection = {
            "view": "private",
            "pair": {"actor_id": "actor:a", "person_id": "person:one"},
            "version": 1,
            "policy_version": "synthetic-policy",
            "relationship_type": "unspecified",
            "display_label": "",
            "score": 0,
            "stage": "acquaintance",
            "frozen": False,
            "frozen_since": None,
            "decay_cursor": "2026-10-01T00:00:00Z",
            "checked_at": "2026-10-01T00:00:00Z",
        }

        async def memory(operation, payload):
            self.calls.append((operation, copy.deepcopy(payload)))
            if self.pause:
                self.entered.set()
                await self.release.wait()
            if self.fail:
                raise Fault(*self.fail)
            projection = dict(
                self.projection, pair=payload.get("pair", payload.get("command", {}).get("pair"))
            )
            if operation == "history":
                return {"projection": projection, "items": [], "has_more": False}
            return dict(projection, version=2)

        self.platform.relationships.client.call = memory
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
            assert response.status == 200
            self.csrf = (await response.json())["csrf"]

    async def call(self, name, body, status=200, headers=None):
        async with self.client.post(
            self.url + "/api/web/relationships/" + name,
            json=body,
            headers=headers or {"Origin": self.url, "X-CSRF-Token": self.csrf},
        ) as response:
            result = await response.json()
            self.assertEqual(response.status, status, result)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            return result

    async def test_catalog_reuses_confirmed_people_and_role_directory(self):
        result = await self.call("catalog", {})
        assert result["roles"][0]["id"] == "actor:a"
        assert [person["label"] for person in result["items"]] == ["一号人物", "二号人物"]
        assert "token" not in str(result) and "origin" not in str(result)

    async def test_selected_pair_gets_real_current_operator_origin(self):
        answer = await self.call("view", SELECTION)
        assert answer["projection"]["pair"]["person_id"] == "person:one"
        reference = self.calls[-1][1]["origin"]["assertion_ref"]
        with self.platform.store.connect() as db:
            _, _, context = self.platform.origins.context(
                db, reference, "platform", "memory", "dialogue"
            )
        assert context["principal_id"] == "admin"
        assert context["allowed_scope"]["actor_id"] == "actor:a"

    async def test_browser_cannot_claim_person_origin_operator_or_pair(self):
        await self.call("view", dict(SELECTION, person_id="person:unknown"), 403)
        await self.call("view", dict(SELECTION, origin={"assertion_ref": "fake"}), 400)
        await self.call(
            "manage",
            dict(
                SELECTION,
                client_id="one",
                command={
                    "operation": "set_freeze",
                    "frozen": True,
                    "expected_version": 1,
                    "pair": {},
                },
            ),
            400,
        )
        assert self.calls == []

    async def test_cookie_origin_and_csrf_are_required(self):
        await self.call(
            "view",
            SELECTION,
            403,
            {"Origin": "https://untrusted.example", "X-CSRF-Token": self.csrf},
        )
        await self.call("view", SELECTION, 403, {"Origin": self.url, "X-CSRF-Token": "wrong"})
        async with aiohttp.ClientSession() as anonymous:
            async with anonymous.post(
                self.url + "/api/web/relationships/view",
                json=SELECTION,
                headers={"Origin": self.url},
            ) as response:
                assert response.status == 401
        assert self.calls == []

    async def test_login_does_not_grant_management(self):
        self.platform.auth.principals["admin"]["actions"].remove("role.manage")
        await self.call("catalog", {}, 403)
        assert self.calls == []

    async def test_same_client_id_and_payload_reuse_memory_command_identity(self):
        body = dict(
            SELECTION,
            client_id="same-intent",
            command={
                "operation": "adjust_affinity",
                "delta": 4,
                "reason": "合成原因",
                "expected_version": 1,
            },
        )
        first = await self.call("manage", body)
        second = await self.call("manage", body)
        assert first == second
        assert self.calls[0][1]["command"] == self.calls[1][1]["command"]

    async def test_conflict_and_unknown_are_not_success_and_not_retried(self):
        body = dict(
            SELECTION,
            client_id="intent",
            command={"operation": "set_freeze", "frozen": True, "expected_version": 1},
        )
        self.fail = ("version_conflict", 409)
        assert (await self.call("manage", body, 409))["code"] == "version_conflict"
        self.fail = ("result_unknown", 503)
        assert (await self.call("manage", body, 503))["code"] == "result_unknown"
        assert len(self.calls) == 2

    async def test_permission_withdrawn_during_read_hides_response(self):
        self.pause = True
        pending = asyncio.create_task(self.call("view", SELECTION, 403))
        await self.entered.wait()
        self.platform.auth.principals["admin"]["actions"].remove("role.manage")
        self.release.set()
        assert (await pending)["code"] == "forbidden"

    async def test_configuration_refuses_other_memory_authority(self):
        config = copy.deepcopy(self.config)
        config["web_relationships"]["memory"]["base_url"] = "https://other.example.invalid"
        with self.assertRaises(Fault):
            validate_settings(config)

    async def test_cancelled_selection_discards_entry_created_after_cancellation(self):
        from services.platform.relationships.routes import RelationshipConsole

        console = RelationshipConsole(self.platform)
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        temporary_ids = []

        def late_selection(actor, version, temporary):
            temporary_ids.append(temporary)
            entered.set()
            assert release.wait(3)
            self.platform.auth.entries[temporary] = {"actor_id": actor}
            finished.set()
            return "synthetic-ref", {}

        async def people(*args):
            return {"items": [{"id": "person:one"}]}

        with (
            patch.object(self.platform.relationships, "gate", return_value="pin"),
            patch.object(self.platform.relationships, "people", side_effect=people),
            patch.object(console.memory, "_role", return_value={"available": True}),
            patch.object(console.memory, "_selection", side_effect=late_selection),
        ):
            task = asyncio.create_task(
                self.platform.relationships.route(
                    console, "view", dict(SELECTION, role_id="actor:b"), {}
                )
            )
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
            assert await asyncio.to_thread(finished.wait, 3)
            for _ in range(50):
                if self.platform.local_work.active == 0:
                    await asyncio.sleep(0.01)
                    break
                await asyncio.sleep(0.01)
        assert temporary_ids and all(key not in self.platform.auth.entries for key in temporary_ids)
        assert self.calls == []

"""Actual Platform/Memory/Companion HTTPS; only identities, model and channel are synthetic.

The fixture exports immutable product Git commits, uses fresh temporary databases,
and connects the real source-input, source-current, origin and source-facts ports.
It never reads another product's mutable checkout as application code.
"""

import asyncio
import copy
import importlib.util
import io
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
from aiohttp import web
import uvicorn

from fixtures import ENV, TOKENS, bearer
from make_tls_fixture import generate
from test_relationships import settings, SYNTHETIC_ENV, SCHEMA
from services.platform.contracts import canonical, utc
from services.platform.provider_catalog import ProviderCatalog
from services.platform.relationships.routes import RelationshipConsole
from services.platform.server import create_app as platform_app
from services.platform.service import Platform
from services.platform.transport import server_tls
from web_fixtures import PASSWORD

MEMORY_SHA = "1f3121c9758faeb31fc9d0fe2a54974c72505d55"
COMPANION_SHA = "21e4ff37f1d4f4e3e9db94f8c965031a8dcb9cd6"
PLATFORM_BASE = "dffedb231ae884c97661056e2c5bcb90d9ea950b"
ROOT = Path(os.environ["TS012_CONTRACT_DIR"]).parents[2]


def export(repo, revision, destination, paths):
    archive = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={repo}",
            "-C",
            str(repo),
            "archive",
            "--format=zip",
            revision,
            *paths,
        ],
        capture_output=True,
        check=True,
    ).stdout
    with zipfile.ZipFile(io.BytesIO(archive)) as files:
        if any(
            not (destination / name).resolve().is_relative_to(destination)
            for name in files.namelist()
        ):
            raise ValueError("Archive path outside temporary fixture")
        files.extractall(destination)


class Joint:
    async def start(self, *, port=0, static=None, browser_http=False):
        self.temp = tempfile.TemporaryDirectory(
            prefix="ts116-three-products-", ignore_cleanup_errors=True
        )
        self.root = Path(self.temp.name).resolve()
        self.clients, self.runners, self.tasks = [], [], []
        # Full-suite collection can already have imported older integration peers.
        # Borrow their module slots only while these immutable snapshots are active.
        self.prior_modules = {
            name: module
            for name, module in list(sys.modules.items())
            if any(
                name == package or name.startswith(package + ".")
                for package in ("tianshu_companion", "tianshu_memory")
            )
        }
        for name in self.prior_modules:
            del sys.modules[name]
        for name, sha, paths in [
            ("tianshu-memory", MEMORY_SHA, ["src"]),
            ("tianshu-companion", COMPANION_SHA, ["src", "tests/support.py"]),
        ]:
            path = self.root / name
            export(ROOT / "projects" / name, sha, path, paths)
            sys.path.insert(0, str(path / "src"))
        from tianshu_companion.app import create_app as core_app
        from tianshu_companion.clients import JsonService, Memory, Origins, command
        from tianshu_companion.relationships import assemble
        from tianshu_memory.app import create_app as memory_app
        from tianshu_memory.auth import Authenticator
        from tianshu_memory.contracts import Contracts
        from tianshu_memory.relationships import Relationships
        from tianshu_memory.service import MemoryService
        from tianshu_memory.source_authority import SourceAuthority
        from tianshu_memory.source_transport import SourceTransport
        from tianshu_memory.store import Store

        for package in ["tianshu_companion.app", "tianshu_memory.app"]:
            assert Path(sys.modules[package].__file__).resolve().is_relative_to(self.root)
        spec = importlib.util.spec_from_file_location(
            "ts116_pinned_support", self.root / "tianshu-companion/tests/support.py"
        )
        support = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(support)
        self.h = support.Harness(path=self.root / "companion.sqlite", silence_ms=0)
        # Exact integer-second synthetic clock: the protocol serializes milliseconds.
        self.h.clock.now = int(self.h.clock.now)
        # The external web/QQ delivery channel is the same explicit synthetic receipt owner.
        self.h.core.web_sender = self.h.sender
        self.command = command
        for binding in self.h.core.bindings.values():
            binding["service"] = "platform"
        generate(self.root)
        self.ca = str(self.root / "localhost.pem")
        self.tls = {
            "certificate_file": self.ca,
            "private_key_file": str(self.root / "localhost-key.pem"),
        }
        self.verify = ssl.create_default_context(cafile=self.ca)
        self.core_token = "synthetic-ts116-platform-to-core"
        self.facts_token = "synthetic-ts116-memory-to-core-facts"
        self.alias_token = "synthetic-ts116-unused-alias"
        self.environ = {
            **SYNTHETIC_ENV,
            "TS116_CORE": self.core_token,
            "TS116_MANAGER_RESOLVER": "synthetic-ts116-platform-manager-resolver",
            "TS116_GATEWAY": "synthetic-ts116-unused-gateway",
        }
        self.prior_env = {name: os.environ.get(name) for name in self.environ}
        os.environ.update(self.environ)
        self.core_server, self.core_url = await self.start_asgi(
            core_app(self.h.core, tokens={"platform": self.core_token, "memory": self.facts_token})
        )
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0 if browser_http else port))
        self.platform_url = "https://127.0.0.1:" + str(listener.getsockname()[1])
        self.web_url = "http://127.0.0.1:" + str(port) if browser_http else self.platform_url
        contracts = Contracts(os.environ["TS012_CONTRACT_DIR"])
        contracts.load_profiles()
        contracts.load_sources()
        self.memory_store = Store(self.root / "memory.sqlite")
        self.memory_store.migrate_profiles(self.root / "before-profiles.sqlite")
        self.memory_store.migrate_sources(self.root / "before-sources.sqlite", contracts)
        self.memory_config_path = self.root / "memory-config.json"
        issuer = self.platform_url + "/internal/v1/origins/resolve"
        common = {
            "issuer": "platform",
            "allowed_actors": ["actor:a", "actor:b"],
            "issuer_url": issuer,
            "issuer_ca_file": self.ca,
        }
        self.memory_config = {
            "mode": "source_sync",
            "callers": {
                "companion": {
                    **common,
                    "token": "synthetic-ts116-core-memory",
                    "issuer_token": TOKENS["MEMORY_RESOLVER"],
                    "event_scopes": [],
                    "operations": [
                        "resolve",
                        "register",
                        "select",
                        "select_profiles",
                        "consume",
                        "check_sources",
                        "relationships.read",
                        "relationships.check",
                        "relationships.settle",
                    ],
                },
                "platform": {
                    **common,
                    "token": self.environ["TS116_MANAGER"],
                    "role_admin": True,
                    "issuer_token": self.environ["TS116_MANAGER_RESOLVER"],
                    "operations": ["relationships.read", "relationships.manage"],
                },
                "platform_reader": {
                    **common,
                    "token": self.environ["TS116_MEMORY_READ"],
                    "issuer_token": self.environ["TS116_MANAGER_RESOLVER"],
                    "operations": ["relationships.read"],
                },
                "platform_qq_profiles": {
                    "token": self.environ["TS116_PROFILES"],
                    "operations": ["qq_profiles"],
                },
                "platform_qq_alias": {"token": self.alias_token, "operations": ["qq_alias"]},
            },
            "source_sync": {
                "core": {
                    "url": self.core_url + "/internal/v1/source-facts/read",
                    "token": self.facts_token,
                    "ca_file": self.ca,
                },
                "platform": {
                    "url": self.platform_url + "/internal/v1/source-access/read",
                    "token": TOKENS["MEMORY"],
                    "ca_file": self.ca,
                },
            },
        }
        self.save_memory_config()
        self.memory = MemoryService(
            self.memory_store,
            contracts,
            source_authority=SourceAuthority(
                SourceTransport(self.memory_config_path, contracts), contracts
            ),
            clock=lambda: datetime.fromtimestamp(self.h.clock(), UTC),
        )
        self.memory_store.migrate_relationships(
            self.root / "before-relationships.sqlite", clock=self.memory.clock
        )
        auth = Authenticator(self.memory_config_path, contracts, self.memory.clock)
        self.relations = Relationships(self.memory, auth=auth)
        self.delay, self.next_error, self.relationship_calls = 0, None, []
        self.response_gate = None
        self.response_hold = False
        self.response_release = threading.Event()
        self.response_release.set()
        self.manage_committed = threading.Event()

        fixture = self

        class TestFaultBoundary:
            async def __call__(self, scope, receive, send):
                if scope["type"] == "http" and scope["path"].startswith(
                    "/internal/v1/relationships/"
                ):
                    fixture.relationship_calls.append(scope["path"])
                    if fixture.next_error:
                        code = fixture.next_error
                        fixture.next_error = None
                        chunks = bytearray()
                        while True:
                            event = await receive()
                            chunks.extend(event.get("body", b""))
                            if not event.get("more_body"):
                                break
                        payload = json.loads(chunks)
                        await send(
                            {
                                "type": "http.response.start",
                                "status": 503,
                                "headers": [(b"content-type", b"application/json")],
                            }
                        )
                        await send(
                            {
                                "type": "http.response.body",
                                "body": canonical(
                                    {"request_id": payload["request_id"], "code": code}
                                ).encode(),
                            }
                        )
                        return
                    if fixture.delay:
                        await asyncio.sleep(fixture.delay)
                    if scope["path"].endswith("/manage") and fixture.response_hold:

                        async def delayed_send(message):
                            if (
                                message["type"] == "http.response.start"
                                and message["status"] == 200
                            ):
                                fixture.manage_committed.set()
                                await asyncio.to_thread(fixture.response_release.wait, 10)
                            await send(message)

                        await actual_memory(scope, receive, delayed_send)
                        return
                await actual_memory(scope, receive, send)

        actual_memory = memory_app(service=self.memory, auth=auth, relationships=self.relations)
        self.memory_server, self.memory_url = await self.start_asgi(
            TestFaultBoundary(), threaded=True
        )
        http = JsonService(
            self.memory_url, self.memory_config["callers"]["companion"]["token"], ca_file=self.ca
        )
        self.clients.append(http)
        self.h.core.memory = Memory(self.h.contracts, http)
        self.h.core.relationships = assemble(
            {"enabled": True, "candidate_schema_path": str(SCHEMA)}, http
        )
        config = settings(str(self.root), static)
        config["mode"] = "service_https"
        config["tls"] = self.tls
        config["web"]["origin"] = self.web_url
        if browser_http:
            config["web_access"] = {
                "host": "127.0.0.1",
                "port": port,
                "certificates": {},
                "initial": {"mode": "http", "origin": self.web_url, "certificate": None},
            }
        config["principals"]["memory_resolver"]["resolver"]["caller"] = "companion"
        config["principals"]["manager_resolver"] = {
            "kind": "service",
            "service": "memory",
            "token_env": "TS116_MANAGER_RESOLVER",
            "actions": ["origin.resolve"],
            "resolver": {"caller": "platform", "purpose": "dialogue"},
        }
        config["principals"]["companion"]["actions"].append("origin.resolve")
        config["principals"]["companion"]["resolver"] = {
            "caller": "platform",
            "purpose": "dialogue",
        }
        for actor_entry in ["actor-a", "actor-b"]:
            config["entries"][actor_entry]["ttl_seconds"] = 3600
        config["input_entries"]["input-entry"]["ttl_seconds"] = 3600
        web_channel = config["input_entries"]["input-entry"]["channel"]
        self.h.core.bindings[web_channel["binding_id"]] = dict(
            copy.deepcopy(self.h.core.bindings["qq-private"]), namespace="web"
        )
        for identifier in ["10001", "10002"]:
            for audience, binding in [("self_private", "qq-private"), ("group", "qq-group")]:
                prefix = f"chat-{identifier}-{audience}"
                author = {"namespace": "qq", "immutable_account_id": identifier}
                channel = {
                    "namespace": "qq",
                    "binding_id": binding,
                    "channel_conversation_id": "private:" + identifier
                    if audience == "self_private"
                    else "group:20001",
                    "thread_id": None,
                }
                actors = []
                for actor in ["a", "b"]:
                    key = prefix + ":" + actor
                    actors.append(key)
                    config["entries"][key] = {
                        "kind": "trusted_application",
                        "owner": "connector",
                        "account": author,
                        "channel": channel,
                        "actor_id": "actor:" + actor,
                        "audience": audience,
                        "ttl_seconds": 3600,
                        "routes": [
                            {"caller": "platform", "receiver": "companion", "purpose": "dialogue"},
                            {"caller": "companion", "receiver": "memory", "purpose": "dialogue"},
                        ],
                    }
                config["input_entries"][prefix] = {
                    "owner": "connector",
                    "account": author,
                    "channel": channel,
                    "audience": audience,
                    "ttl_seconds": 3600,
                    "actor_entries": actors,
                    "default_actor_ids": ["actor:a"],
                    "routing_version": 1,
                }
        for name in ["web_memory", "web_qq_profiles"]:
            config[name].update(base_url=self.memory_url, ca_file=self.ca)
        config["web_relationships"]["memory"].update(
            base_url=self.memory_url, ca_file=self.ca, timeout_seconds=5
        )
        config["core"] = {
            "base_url": self.core_url,
            "token_env": "TS116_CORE",
            "ca_file": self.ca,
            "timeout_seconds": 5,
        }
        provider_path = self.root / "providers"
        ProviderCatalog(provider_path, create=True)
        config["provider_self_service"] = {
            "directory": str(provider_path),
            "gateway_url": "https://127.0.0.1:1",
            "gateway_token_env": "TS116_GATEWAY",
        }
        config["role_runtime"] = {
            "enabled": True,
            "memory": {
                "base_url": self.memory_url,
                "token_env": "TS116_MANAGER",
                "ca_file": self.ca,
            },
        }
        self.p = Platform(config, clock=self.h.clock)
        for actor in ["a", "b"]:
            self.p.role_runtime._put(
                {
                    "actor_id": "actor:" + actor,
                    "operator": "admin",
                    "provider_id": None,
                    "name": "合成角色" + actor.upper(),
                    "version": 1,
                    "state": "active",
                    "enabled": True,
                    "capabilities": ["dialogue", "memory.read", "memory.write"],
                }
            )
        self.console = RelationshipConsole(self.p)
        app = platform_app(self.p, console=self.console)
        self.control_enabled = False

        @web.middleware
        async def control(request, handler):
            if request.path == "/__fixture/control":
                if not self.control_enabled or request.headers.get("X-Fixture") != "synthetic-only":
                    return web.Response(status=404)
                body = await request.json()
                action = body["action"]
                if action == "delay":
                    self.delay = body["seconds"]
                elif action == "hold_response":
                    self.manage_committed.clear()
                    self.response_release.clear()
                    self.response_hold = True
                elif action == "release_response":
                    self.response_hold = False
                    self.response_release.set()
                elif action == "error":
                    self.next_error = "dependency_unavailable"
                elif action == "advance":
                    self.h.clock.advance(body["seconds"])
                elif action == "expire":
                    for session in self.console.sessions.values():
                        session["expires"] = 0
                elif action == "permission":
                    actions = self.p.auth.principals["admin"]["actions"]
                    if body["enabled"] and "role.manage" not in actions:
                        actions.append("role.manage")
                    elif not body["enabled"] and "role.manage" in actions:
                        actions.remove("role.manage")
                return web.json_response(
                    {
                        "calls": len(self.relationship_calls),
                        "committed": self.manage_committed.is_set(),
                    }
                )
            return await handler(request)

        app.middlewares.insert(0, control)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        self.runners.append(runner)
        await web.SockSite(runner, listener, ssl_context=server_tls(self.tls)).start()
        if browser_http:
            public_app = platform_app(self.p, console=self.console, public=True)
            public_app.middlewares.insert(0, control)
            public_runner = web.AppRunner(public_app, access_log=None)
            self.runners.append(public_runner)
            await public_runner.setup()
            await web.TCPSite(public_runner, "127.0.0.1", port).start()
        origins_http = JsonService(self.platform_url, TOKENS["COMPANION"], ca_file=self.ca)
        self.clients.append(origins_http)
        self.h.core.origins = Origins(self.h.contracts, {"platform": ("platform", origins_http)})
        self.web = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True), trust_env=False)
        await self.login()
        # Register identities via real source-input → Core HTTP → Memory HTTP; no test rows are inserted.
        await self.complete(account="operator")
        self.person_ids = {}
        for identifier in ["10001", "10002"]:
            registered = await self.complete(account=identifier)
            self.person_ids[identifier] = registered["scope"]["person_id"]
        assert len(set(self.person_ids.values())) == len(self.person_ids)
        self.control_enabled = True
        return self

    async def start_asgi(self, app, *, threaded=False):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_level="error",
                lifespan="off",
                timeout_graceful_shutdown=2,
                ssl_certfile=self.ca,
                ssl_keyfile=self.tls["private_key_file"],
            )
        )
        if threaded:
            self.memory_thread = threading.Thread(
                target=server.run, kwargs={"sockets": [listener]}, daemon=True
            )
            self.memory_thread.start()
        else:
            self.tasks.append(asyncio.create_task(server.serve(sockets=[listener])))
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert server.started, "ephemeral HTTPS service did not start"
        return server, "https://127.0.0.1:" + str(port)

    def save_memory_config(self):
        self.memory_config_path.write_text(canonical(self.memory_config), encoding="utf-8")

    async def login(self):
        async with self.web.get(self.web_url + "/api/web/session", ssl=self.verify) as response:
            answer = await response.json()
            assert response.status == 200, answer
            csrf = answer["csrf"]
        async with self.web.post(
            self.web_url + "/api/web/login",
            ssl=self.verify,
            json={"username": "synthetic-admin", "password": PASSWORD},
            headers={"Origin": self.web_url, "X-CSRF-Token": csrf},
        ) as response:
            answer = await response.json()
            assert response.status == 200, answer
            self.csrf = answer["csrf"]

    async def post(self, operation, body, status=200):
        async with self.web.post(
            self.web_url + "/api/web/relationships/" + operation,
            ssl=self.verify,
            json=body,
            headers={"Origin": self.web_url, "X-CSRF-Token": self.csrf},
        ) as response:
            answer = await response.json()
            assert response.status == status, answer
            return answer

    async def selection(self, actor="actor:a", account="10001"):
        catalog = await self.post("catalog", {})
        person = next(p for p in catalog["items"] if p["id"] == self.person_ids[account])
        role = next(r for r in catalog["roles"] if r["id"] == actor)
        return {
            "role_id": actor,
            "role_version": role["version"],
            "person_id": person["id"],
            "people_after": person["after"],
        }

    async def view(self, selection=None):
        return await self.post("view", selection or await self.selection())

    async def manage(self, operation, selection=None, **fields):
        selection = selection or await self.selection()
        view = await self.view(selection)
        return await self.post(
            "manage",
            {
                **selection,
                "client_id": str(uuid.uuid4()),
                "command": {
                    "operation": operation,
                    "expected_version": view["projection"]["version"],
                    **fields,
                },
            },
        )

    async def accept(self, *, actor="actor:a", account="10001", group=False, text="合成本地消息"):
        audience = "group" if group else "self_private"
        entry_id = "input-entry" if account == "operator" else f"chat-{account}-{audience}"
        entry = self.p.sources.entries[entry_id]
        physical = {
            "message_key": {
                "channel": entry["channel"],
                "message_id": "synthetic:" + uuid.uuid4().hex,
                "revision": 1,
            },
            "author": entry["account"],
            "sent_at": utc(self.h.clock()),
            "kind": "message",
            "parts": [{"kind": "text", "text": text}],
            "reply_refs": [],
            "mentioned_accounts": [],
        }
        header = bearer("ADMIN" if account == "operator" else "CONNECTOR")
        origin = self.p.sources.register_input(header, entry_id, physical)
        request = {
            "schema_version": 1,
            "command": self.command(
                {"assertion_ref": origin["assertion_ref"]},
                "synthetic:" + uuid.uuid4().hex,
                self.h.clock(),
                300,
            ),
            "input": physical,
            "target_actor_ids": [actor],
        }
        response = await self.p.sources.dispatch(header, request)
        outcome = response["outcomes"][0]
        assert outcome["state"] == "accepted", response
        scope = outcome["admission"]["scope"]
        if scope not in self.memory_config["callers"]["companion"]["event_scopes"]:
            self.memory_config["callers"]["companion"]["event_scopes"].append(scope)
            self.save_memory_config()
        return outcome["receipt"]["collection_id"]

    async def drain(self, collection, phase="sent"):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            await self.h.cycles(1)
            await asyncio.sleep(0.003)
            turn = next(
                (t for t in self.h.turns() if t["bundle"]["collection_id"] == collection), None
            )
            if turn and turn["phase"] == phase:
                await self.h.core.flush_outbox()
                return turn
        raise AssertionError([(t["phase"], t.get("failure")) for t in self.h.turns()])

    async def complete(self, **fields):
        return await self.drain(await self.accept(**fields))

    async def generating(self, *, actor="actor:a", account="10001"):
        prior = [
            turn["sequence"]
            for turn in self.h.turns()
            if turn["scope"]["actor_id"] == actor
            and turn["scope"]["person_id"] == self.person_ids[account]
            and turn["scope"]["audience"] == "self_private"
        ]
        sequence = max(prior, default=0) + 1
        gate = asyncio.Event()
        self.h.gateway.gates[sequence] = gate
        calls = len(self.h.gateway.calls)
        collection = await self.accept(actor=actor, account=account)
        deadline = time.monotonic() + 5
        while len(self.h.gateway.calls) == calls and time.monotonic() < deadline:
            await self.h.cycles(1)
            await asyncio.sleep(0.003)
        assert len(self.h.gateway.calls) == calls + 1, "model did not enter the blocked generation"
        turn = next(t for t in self.h.turns() if t["bundle"]["collection_id"] == collection)
        assert turn["sequence"] == sequence and turn["phase"] == "generating"
        assert self.h.gateway.calls[-1][0] == sequence and not gate.is_set()
        return collection, gate, turn

    async def close(self):
        if hasattr(self, "memory_server"):
            self.memory_server.should_exit = True
        if hasattr(self, "memory_thread"):
            await asyncio.to_thread(self.memory_thread.join, 10)
            assert not self.memory_thread.is_alive()
        if hasattr(self, "core_server"):
            self.core_server.should_exit = True
        if hasattr(self, "web"):
            await self.web.close()
        await asyncio.gather(*(runner.cleanup() for runner in self.runners))
        await asyncio.gather(*self.tasks)
        await asyncio.gather(*(client.close() for client in self.clients))
        if hasattr(self, "h"):
            await self.h.core.close()
        if hasattr(self, "p"):
            self.p.close()
        for package in ["tianshu_companion", "tianshu_memory"]:
            module_path = str(self.root / package.replace("_", "-") / "src")
            if module_path in sys.path:
                sys.path.remove(module_path)
            for name in list(sys.modules):
                if name == package or name.startswith(package + "."):
                    del sys.modules[name]
        sys.modules.update(self.prior_modules)
        for name, value in getattr(self, "prior_env", {}).items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.temp.cleanup()


class RelationshipThreeProductTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.j = Joint()
        self.addAsyncCleanup(self.j.close)
        await self.j.start()

    async def test_binding_private_expression_public_crop_and_actual_reply_settlement(self):
        j = self.j
        selected = await j.selection()
        await j.manage(
            "set_binding", selected, relationship_type="partner", display_label="合成私密称呼"
        )
        before = (await j.view(selected))["projection"]
        await j.complete()
        model_messages = j.h.gateway.calls[-1][1]
        messages = canonical(model_messages)
        assert "合成私密称呼" in messages and "partner" in messages
        expression = json.loads(model_messages[1]["content"])["relationship_expression"][0]
        assert expression["pair"] == {
            "actor_id": selected["role_id"],
            "person_id": selected["person_id"],
        }
        assert "score" not in expression and "frozen" not in expression
        after = (await j.view(selected))["projection"]
        assert after["score"] == before["score"] + 1
        await j.complete(group=True)
        model_messages = j.h.gateway.calls[-1][1]
        messages = canonical(model_messages)
        assert "合成私密称呼" not in messages and '"relationship_type"' not in messages
        assert "自然回应，不谈私聊关系细节。" in messages
        public = json.loads(model_messages[1]["content"])["relationship_expression"][0]
        assert set(public) == {"view", "pair", "expression_hint"}
        assert public["view"] == "public" and public["pair"] == expression["pair"]
        assert (await j.view(selected))["projection"]["score"] == after["score"]
        # Read the test ledger only to independently compare real HTTP projection with its owner.
        with j.memory_store.transaction() as db:
            row = db.execute(
                "SELECT score,version FROM relationship_pairs WHERE actor_id=? AND person_id=?",
                (selected["role_id"], selected["person_id"]),
            ).fetchone()
        assert row["score"] == after["score"] and row["version"] == after["version"]

    async def test_freeze_long_time_unfreeze_no_catchup_and_new_event(self):
        j = self.j
        selected = await j.selection()
        await j.manage("adjust_affinity", selected, delta=30, reason="合成冻前调整")
        frozen = (await j.manage("set_freeze", selected, frozen=True))["projection"]
        j.h.clock.advance(20 * 86400)
        await j.complete()
        assert (await j.view(selected))["projection"]["score"] == frozen["score"]
        unfrozen = (await j.manage("set_freeze", selected, frozen=False))["projection"]
        assert unfrozen["score"] == frozen["score"]
        j.h.clock.advance(1)
        await j.complete()
        assert (await j.view(selected))["projection"]["score"] == frozen["score"] + 1

    async def test_version_change_and_permission_revocation_stop_inflight_send(self):
        j = self.j
        selected = await j.selection()
        collection, gate, blocked = await j.generating()
        sends = len(j.h.sender.calls)
        before = (await j.view(selected))["projection"]
        await j.manage(
            "set_binding", selected, relationship_type="friend", display_label="合成新称呼"
        )
        gate.set()
        failed = await j.drain(collection, "failed")
        assert failed["id"] == blocked["id"]
        assert len(j.h.sender.calls) == sends
        changed = (await j.view(selected))["projection"]
        assert changed["version"] > before["version"] and changed["score"] == before["score"]
        await j.complete()
        assert "合成新称呼" in canonical(j.h.gateway.calls[-1][1])
        assert (await j.view(selected))["projection"]["score"] == before["score"] + 1
        j.p.auth.principals["admin"]["actions"].remove("role.manage")
        await j.post("view", selected, 403)

    async def test_source_origin_revocation_stops_blocked_private_reply(self):
        j = self.j
        selected = await j.selection()
        before = (await j.view(selected))["projection"]
        collection, gate, blocked = await j.generating()
        sends = len(j.h.sender.calls)
        j.p.origins.revoke(bearer("ADMIN"), "origin", blocked["origin"]["assertion_ref"])
        gate.set()
        await j.drain(collection, "failed")
        assert len(j.h.sender.calls) == sends
        assert (await j.view(selected))["projection"]["score"] == before["score"]

    async def test_memory_relationship_capability_revocation_stops_blocked_reply(self):
        j = self.j
        selected = await j.selection()
        before = (await j.view(selected))["projection"]
        collection, gate, _ = await j.generating()
        sends = len(j.h.sender.calls)
        j.memory_config["callers"]["companion"]["operations"].remove("relationships.check")
        j.save_memory_config()
        gate.set()
        await j.drain(collection, "failed")
        assert len(j.h.sender.calls) == sends
        assert (await j.view(selected))["projection"]["score"] == before["score"]


async def serve():
    os.environ.update(ENV)
    os.environ.setdefault("TIANSHU_CONTRACTS", os.environ["TS012_CONTRACT_DIR"])
    fixture = Joint()
    try:
        await fixture.start(port=4844, static=Path(__file__).resolve().parents[2] / "apps/web/dist")
        print("TS116 synthetic three-product HTTPS fixture ready", flush=True)
        await asyncio.Event().wait()
    finally:
        await fixture.close()


if __name__ == "__main__" and "--serve" in sys.argv:
    asyncio.run(serve())

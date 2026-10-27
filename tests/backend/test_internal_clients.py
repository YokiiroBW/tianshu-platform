"""Actual local exchange and Platform middleware; peer business answers are synthetic."""

import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
from aiohttp import web

from fixtures import ENV, bearer, config, query
from services.platform.contracts import Fault
from services.platform.qq_admin import observe_alias
from services.platform.server import create_app, quiesce
from services.platform.service import Platform
from services.platform.web_console import WebConsole
from web_fixtures import web_settings

EXCHANGE_ROOT = os.environ.get("TS335_EXCHANGE_ROOT")
LocalExchange = None
if EXCHANGE_ROOT:
    sys.path.insert(0, str(Path(EXCHANGE_ROOT).resolve()))
    from deploy.single_container.exchange import LocalExchange


@unittest.skipUnless(LocalExchange, "TS335_EXCHANGE_ROOT names the host's exchange checkout")
class InternalClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tokens = {
            name: "synthetic-internal-" + name
            for name in ("companion", "memory", "knowledge", "gateway")
        }
        environment = {"TS335_" + name.upper(): value for name, value in self.tokens.items()}
        env = patch.dict(os.environ, {**ENV, **environment})
        env.start()
        self.addCleanup(env.stop)
        self.exchange = LocalExchange(buffer_bytes=4096)
        self.calls = []
        self.received = []
        self.binary = b"private-original\x00\xff" * 4096
        self.peers = {
            name: {
                "base_url": "https://" + name + ".internal",
                "token_env": "TS335_" + name.upper(),
            }
            for name in self.tokens
        }
        settings = web_settings(self.temp.name, origin="https://platform.internal")
        settings.update(mode="service_https", core=self.peers["companion"])
        # Bind factories before registration, as required by real host construction.
        self.p = Platform(settings, internal_clients=self.exchange)
        self.console = WebConsole(self.p)
        self.app = create_app(self.p, console=self.console, trusted_local=self.exchange.owns)

        @web.middleware
        async def record(request, handler):
            self.received.append((request.secure, self.exchange.owns(request)))
            return await handler(request)

        self.app.middlewares.insert(0, record)
        for name, peer in self.peers.items():
            app = web.Application()

            async def handle(request, name=name):
                body = await request.read()
                self.calls.append(
                    (
                        name,
                        request.path,
                        body,
                        request.headers["Authorization"],
                        request.secure,
                        self.exchange.owns(request),
                    )
                )
                if request.path.endswith("/original"):
                    response = web.StreamResponse(
                        headers={
                            "Content-Type": "application/octet-stream",
                            "X-Content-SHA256": hashlib.sha256(self.binary).hexdigest(),
                        }
                    )
                    await response.prepare(request)
                    for position in range(0, len(self.binary), 3072):
                        await response.write(self.binary[position : position + 3072])
                    await response.write_eof()
                    return response
                if request.path.endswith("/provider-self-service/test"):
                    return web.json_response({"outcome": "succeeded"})
                if "/role-runtime/" in request.path:
                    result = {"owner": name}
                    if name == "companion":
                        async with self.exchange.session_factory("platform")() as client:
                            async with client.post(
                                "https://platform.internal/internal/v1/model-config/snapshot",
                                json=query(self.origin),
                                headers={"Authorization": bearer("GATEWAY")},
                            ) as callback:
                                self.assertEqual(callback.status, 200, await callback.text())
                                result["callback"] = (await callback.json())["request_id"]
                    return web.json_response(result)
                return web.json_response({"code": "not_found"}, status=404)

            app.router.add_route("*", "/{path:.*}", handle)
            self.exchange.register(name, app, "aiohttp", peer["base_url"])
        self.exchange.register("platform", self.app, "aiohttp", "https://platform.internal")
        public_console = WebConsole(self.p)
        public_console.access = SimpleNamespace(current={"mode": "https"})
        public_app = create_app(
            self.p, console=public_console, public=True, trusted_local=self.exchange.owns
        )
        self.exchange.register("public", public_app, "aiohttp", "https://public.internal")
        self.origin = self.p.origins.issue(bearer("ADMIN"), "config-entry")["assertion_ref"]
        self.p.models.publish(bearer("ADMIN"), config(self.p.origins.clock()))
        await self.exchange.open()
        self.addAsyncCleanup(self.exchange.close)

    async def test_composition_binds_clients_and_preserves_authenticated_reentrant_callbacks(self):
        companion = self.p.client_factories["companion"]
        memory = self.p.client_factories["memory"]
        knowledge = self.p.client_factories["knowledge"]
        self.assertIs(self.console.internal_clients, self.exchange)
        for client in (
            self.p.sources,
            self.console.dialogue,
            self.console.life,
            self.console.life_management,
        ):
            self.assertIs(client.session_factory, companion)
        for client in (self.console.memory, self.console.memory_links, self.p.qq_admin):
            self.assertIs(client.session_factory, memory)
        self.assertIs(self.console.content.session_factory, knowledge)
        self.assertIs(self.console.knowledge.session_factory, knowledge)
        self.assertIs(self.p.bot_adapters.core_session_factory, companion)
        self.p.role_runtime.config = {
            "memory": self.peers["memory"],
            "knowledge": self.peers["knowledge"],
        }
        for name in ("companion", "memory", "knowledge"):
            answer = await self.p.role_runtime._remote(
                self.peers[name],
                "/internal/v1/role-runtime/manage"
                if name == "companion"
                else "/internal/v1/role-runtime/authorize",
                {"operation": "list" if name == "companion" else "status"},
            )
            self.assertEqual(answer["owner"], name)
        self.assertEqual(self.calls[0][3], "Bearer " + self.tokens["companion"])
        self.assertTrue(all(not row[4] and row[5] for row in self.calls))
        self.assertTrue(all(not secure and owned for secure, owned in self.received))
        await quiesce(self.app)
        self.assertFalse(self.p.local_work.closed)
        self.assertEqual(await self.p.local_work.run(lambda: "callback"), "callback")
        async with self.exchange.session_factory("platform")() as client:
            async with client.post(
                "https://platform.internal/internal/v1/model-config/snapshot",
                json=query(self.origin),
                headers={"Authorization": bearer("GATEWAY")},
            ) as response:
                self.assertEqual(response.status, 200)
            async with client.post(
                "https://platform.internal/internal/v1/model-config/snapshot",
                json=query(self.origin),
                headers={"Authorization": "Bearer wrong"},
            ) as response:
                self.assertEqual(response.status, 401)

    async def test_binary_original_stream_and_raw_upload_keep_bytes_and_owner(self):
        self.p.settings["web_knowledge"] = {"enabled": True, **self.peers["knowledge"]}
        body, headers = await self.console.content.call("original", {"request_id": "binary"})
        self.assertEqual(body, self.binary)
        self.assertEqual(headers["X-Content-SHA256"], hashlib.sha256(self.binary).hexdigest())

        async def upload():
            yield b"one\x00"
            yield b"two\xff"

        with self.assertRaises(Fault) as refused:
            await self.console.content.call(
                "upload",
                raw=upload(),
                upload_id="synthetic",
                headers={"Content-Type": "application/octet-stream"},
            )
        self.assertEqual(refused.exception.code, "not_found")
        self.assertEqual(
            self.calls[-1][:3],
            ("knowledge", "/internal/v1/knowledge/content/uploads/synthetic", b"one\x00two\xff"),
        )

    async def test_gateway_bot_and_qq_clients_use_their_explicit_owner(self):
        self.console.providers.config = {
            "gateway_url": self.peers["gateway"]["base_url"],
            "gateway_token_env": "TS335_GATEWAY",
        }
        context = SimpleNamespace(
            provider_id="synthetic",
            revision=1,
            protocol="openai-chat-completions",
            base_url="https://external.invalid/v1",
            model_id="synthetic",
            api_key="synthetic-only",
        )
        self.assertEqual(
            await self.console.providers.gateway_request("test", context), {"outcome": "succeeded"}
        )
        with self.assertRaises(Fault):
            await self.p.bot_adapters._core("roles", {})
        self.p.qq_admin.profiles_config = self.peers["memory"]
        with self.assertRaises(Fault):
            await self.p.qq_admin._profiles({"limit": 1, "after": None})
        self.p.settings["qq_alias_memory"] = self.peers["memory"]
        with self.assertRaises(Fault):
            await observe_alias(
                self.p,
                {
                    "schema_version": 2,
                    "nickname": "synthetic",
                    "group_card": None,
                    "account_id": "100",
                    "self_id": "200",
                    "conversation_id": "group",
                    "connection_id": "binding",
                    "event_id": "one",
                    "sent_at": "2026-10-27T00:00:00Z",
                },
            )
        self.assertEqual(
            [row[0] for row in self.calls], ["gateway", "companion", "memory", "memory"]
        )

    async def test_network_requests_cannot_forge_local_ownership(self):
        # Bind a real HTTP listener to this already started application solely
        # for the negative admission check; the module exchange has no listener.
        runner = next(
            adapter.runner
            for name, adapter in self.exchange._adapters.items()
            if name == "platform"
        )
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.addAsyncCleanup(site.stop)
        address = "http://127.0.0.1:" + str(next(iter(site._server.sockets)).getsockname()[1])
        async with aiohttp.ClientSession() as client:
            async with client.post(
                address + "/internal/v1/model-config/snapshot",
                json=query(self.origin),
                headers={"Authorization": bearer("GATEWAY"), "X-Tianshu-Internal": "true"},
            ) as response:
                self.assertEqual(response.status, 403)
        self.assertEqual(self.received[-1], (False, False))

    async def test_public_browser_boundary_never_accepts_the_internal_predicate(self):
        async with self.exchange.session_factory("public")() as client:
            async with client.get("https://public.internal/api/web/session") as response:
                self.assertEqual(response.status, 403)

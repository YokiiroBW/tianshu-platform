import asyncio
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import aiohttp
from aiohttp import web

from fixtures import (
    CONTRACT,
    ENV,
    ROOT,
    TOKENS,
    bearer,
    config,
    query,
    resolve,
    settings,
    start_http,
)
from services.platform.contracts import epoch, utc
from services.platform.server import create_app
from services.platform.service import Platform


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)
        self.settings = settings(self.temp.name)
        self.now = time.time()
        self.p = Platform(self.settings, clock=lambda: self.now)
        self.origin = self.p.origins.issue(bearer("ADMIN"), "config-entry")["assertion_ref"]
        self.chat = self.p.origins.issue(bearer("CONNECTOR"), "chat-entry")["assertion_ref"]
        self.runner, self.url = await start_http(create_app(self.p))
        self.addAsyncCleanup(self.runner.cleanup)
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8), trust_env=False)
        self.addAsyncCleanup(self.client.close)

    async def post(self, path, body, identity="GATEWAY", expected=200, headers=None):
        async with self.client.post(
            self.url + path,
            json=body,
            headers={"Authorization": bearer(identity), **(headers or {})},
        ) as response:
            self.assertEqual(response.status, expected, await response.text())
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            result = await response.json()
            if expected != 200:
                self.p.contracts.check("common#error", result)
            return result

    async def test_actual_http_snapshot_and_receiver_context(self):
        self.p.models.publish(bearer("ADMIN"), config(self.now))
        response = await self.post("/internal/v1/model-config/snapshot", query(self.origin))
        self.p.contracts.check("model#config_response", response)
        self.assertEqual(response["request_id"], "snapshot-test")
        response = await self.post(
            "/internal/v1/origins/resolve", resolve(self.chat), "MEMORY_RESOLVER"
        )
        self.assertEqual(response["context"]["authenticated_service"], "companion")
        self.assertEqual(response["context"]["audience_service"], "memory")
        await self.post("/internal/v1/origins/resolve", resolve(self.chat), "WRONG_RESOLVER", 403)

    async def test_http_unconfigured_expired_revoked_and_browser_denial(self):
        path = "/internal/v1/model-config/snapshot"
        await self.post(path, query(self.origin, None), expected=503)
        self.p.models.publish(bearer("ADMIN"), config(self.now))
        await self.post(path, query(self.origin), "ADMIN", 403)
        await self.post(path, query(self.chat), expected=403)
        await self.post(
            path, query(self.origin), expected=403, headers={"Origin": "http://localhost"}
        )
        await self.post(path, query(self.origin), expected=403, headers={"Cookie": "session=fake"})
        value = config(self.now, version=8)
        value["usable_until"] = utc(self.now + 1)
        self.p.models.publish(bearer("ADMIN"), value)
        self.now = epoch(value["usable_until"])
        await self.post(path, query(self.origin, 8), expected=410)
        self.p.models.revoke(bearer("ADMIN"), 7)
        await self.post(path, query(self.origin, 7), expected=410)
        self.now += 60
        await self.post(path, query(self.origin), expected=403)

    async def test_http_strict_input_auth_size_and_no_new_write_wire(self):
        path = "/internal/v1/origins/resolve"
        for raw in (b'{"assertion_ref":"x","assertion_ref":"y"}', b'{"x":NaN}', b'"\xff"', b"[]"):
            async with self.client.post(
                self.url + path,
                data=raw,
                headers={
                    "Authorization": bearer("MEMORY_RESOLVER"),
                    "Content-Type": "application/json",
                },
            ) as response:
                self.assertEqual(response.status, 400)
        async with self.client.post(self.url + path, json=resolve(self.chat)) as response:
            self.assertEqual(response.status, 401)
        async with self.client.post(
            self.url + path,
            json=resolve(self.chat),
            headers=[
                ("Authorization", bearer("MEMORY_RESOLVER")),
                ("Authorization", bearer("ADMIN")),
            ],
        ) as response:
            self.assertEqual(response.status, 401)
        async with self.client.post(
            self.url + path,
            data=io.BytesIO(b" " * 1_048_577),
            headers={"Authorization": bearer("ADMIN"), "Content-Type": "application/json"},
        ) as response:
            self.assertEqual(response.status, 413)
        await self.post("/internal/v1/model-config/publish", config(self.now), "ADMIN", 404)
        await self.post(
            path, {**resolve(self.chat), "principal_id": "admin"}, "MEMORY_RESOLVER", 400
        )

    async def test_http_restart_reads_same_immutable_snapshot(self):
        self.p.models.publish(bearer("ADMIN"), config(self.now))
        first = await self.post("/internal/v1/model-config/snapshot", query(self.origin))
        await self.runner.cleanup()
        self.runner, self.url = await start_http(
            create_app(Platform(self.settings, clock=lambda: self.now))
        )
        self.addAsyncCleanup(self.runner.cleanup)
        second = await self.post("/internal/v1/model-config/snapshot", query(self.origin))
        self.assertEqual(first, second)

    async def test_cli_authentication_publication_and_real_server_subprocess(self):
        settings_path = Path(self.temp.name) / "settings.json"
        settings_path.write_text(json.dumps(self.settings), encoding="utf-8")
        input_path = Path(self.temp.name) / "input.json"

        async def cli(action, body=None, identity="ADMIN"):
            command = [
                sys.executable,
                "-m",
                "services.platform",
                "--settings",
                str(settings_path),
                "local",
                "--credential-env",
                "TS012_" + identity,
                action,
            ]
            if body is not None:
                input_path.write_text(json.dumps(body), encoding="utf-8")
                command += ["--input", str(input_path)]
            return await asyncio.to_thread(
                subprocess.run, command, cwd=ROOT, capture_output=True, text=True, timeout=15
            )

        unauthorized = await cli("publish", config(self.now), "CONNECTOR")
        self.assertEqual(unauthorized.returncode, 1)
        self.assertEqual(json.loads(unauthorized.stdout)["code"], "forbidden")
        forged = await cli("issue", {"entry_id": "config-entry", "principal_id": "admin"})
        self.assertEqual(forged.returncode, 1)
        issued = await cli("issue", {"entry_id": "config-entry"})
        self.assertEqual(issued.returncode, 0, issued.stderr)
        ref = json.loads(issued.stdout)["assertion_ref"]
        published = await cli("publish", config(self.now))
        self.assertEqual(published.returncode, 0, published.stderr)
        view = await cli("view-config")
        self.assertNotIn("secret-ref:", view.stdout)
        self.assertNotIn(TOKENS["ADMIN"], view.stdout)
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "services.platform",
                "--settings",
                str(settings_path),
                "serve",
                "--port",
                str(port),
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            async with asyncio.timeout(10):
                while True:
                    try:
                        async with self.client.post(
                            f"http://127.0.0.1:{port}/internal/v1/model-config/snapshot",
                            json=query(ref),
                            headers={"Authorization": bearer("GATEWAY")},
                        ) as response:
                            self.assertEqual(response.status, 200)
                            self.assertEqual((await response.json())["config_version"], 7)
                            break
                    except aiohttp.ClientConnectorError:
                        if process.poll() is not None:
                            self.fail("local server exited before accepting requests")
                        await asyncio.sleep(0.05)
        finally:
            process.terminate()
            await asyncio.to_thread(process.wait, 5)


@unittest.skipUnless(
    os.environ.get("TS012_GATEWAY_SRC"),
    "set TS012_GATEWAY_SRC to integrated gateway src; this is a separate real HTTP subchain",
)
class GatewaySubchain(unittest.IsolatedAsyncioTestCase):
    async def test_real_platform_real_gateway_recording_upstream(self):
        sys.path.insert(0, os.environ["TS012_GATEWAY_SRC"])
        self.addCleanup(sys.path.remove, os.environ["TS012_GATEWAY_SRC"])
        from tianshu_gateway.config import ClientGrant
        from tianshu_gateway.server import GATEWAY, Settings, create_app as gateway_app

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)
        recorded, config_requests = [], []
        response_body = {
            "id": "recorded-upstream",
            "object": "chat.completion",
            "created": 1,
            "model": "fixture-text-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "真实 HTTP 演练回包"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
        }

        async def upstream(request):
            recorded.append((await request.json(), dict(request.headers)))
            return web.json_response(response_body)

        upstream_app = web.Application()
        upstream_app.router.add_post("/v1/chat/completions", upstream)
        runner, upstream_url = await start_http(upstream_app)
        self.addAsyncCleanup(runner.cleanup)
        platform_settings = settings(temp.name, upstream_url + "/v1")
        platform = Platform(platform_settings)
        source_ref = platform.origins.issue(bearer("ADMIN"), "config-entry")["assertion_ref"]
        os.environ["TS012_PLATFORM_ORIGIN"] = source_ref
        self.addCleanup(os.environ.pop, "TS012_PLATFORM_ORIGIN", None)
        published = config(upstream=upstream_url + "/v1")
        platform.models.publish(bearer("ADMIN"), published)

        @web.middleware
        async def record_platform(request, handler):
            if request.path == "/internal/v1/model-config/snapshot":
                config_requests.append(json.loads(await request.read()))
            return await handler(request)

        app = create_app(platform)
        app.middlewares.insert(0, record_platform)
        runner, platform_url = await start_http(app)
        self.addAsyncCleanup(runner.cleanup)
        gateway_settings = Settings(
            str(CONTRACT),
            str(Path(temp.name) / "gateway.sqlite"),
            platform_url,
            "secret-ref:local/platform",
            "TS012_PLATFORM_ORIGIN",
            {
                "secret-ref:local/platform": "TS012_GATEWAY",
                "secret-ref:local/companion": "TS012_COMPANION",
                "secret-ref:fixture/provider-a": "TS012_UPSTREAM",
            },
            [
                {"base_url": url, "addresses": ["127.0.0.1"], "allow_private_http": True}
                for url in (platform_url, upstream_url + "/v1")
            ],
            [
                ClientGrant(
                    "companion", "secret-ref:local/companion", "provider-fixture", 7, True, (8, 9)
                )
            ],
            config_refresh_seconds=0,
        )
        gateway = gateway_app(gateway_settings)
        runner, gateway_url = await start_http(gateway)
        self.addAsyncCleanup(runner.cleanup)
        client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8), trust_env=False)
        self.addAsyncCleanup(client.close)

        async def send(request_id, version=7, expected=200):
            headers = {
                "Authorization": bearer("COMPANION"),
                "X-Request-ID": request_id,
                "X-Tianshu-Turn-ID": request_id,
                "X-Tianshu-Config-Version": str(version),
                "X-Tianshu-Workload": "companion.text",
            }
            async with client.post(
                gateway_url + "/v1/chat/completions",
                headers=headers,
                json={"messages": [{"role": "user", "content": "合成输入"}], "stream": False},
            ) as response:
                self.assertEqual(response.status, expected, await response.text())
                return await response.json()

        self.assertEqual(await send("subchain-ok"), response_body)
        # The integrated gateway reads at admission and revalidates immediately
        # before sending. Both requests must preserve the configuration authority.
        self.assertEqual(len(config_requests), 2)
        for request in config_requests:
            platform.contracts.check("model#config_request", request)
            self.assertEqual(request["query"]["origin"]["assertion_ref"], source_ref)
        self.assertEqual(recorded[0][0]["model"], "fixture-text-model")
        self.assertEqual(recorded[0][1]["Authorization"], bearer("UPSTREAM"))
        async with client.get(
            gateway_url + "/internal/v1/model-requests/subchain-ok",
            headers={"Authorization": bearer("COMPANION")},
        ) as response:
            receipt = await response.json()
            self.assertEqual(response.status, 200)
        platform.contracts.check("model#route_receipt", receipt)
        self.assertEqual((receipt["outcome"], receipt["config_version"]), ("succeeded", 7))
        self.assertEqual(receipt["usage"], {"input_tokens": 4, "output_tokens": 3})
        self.assertNotIn("secret-ref:", str(receipt))
        platform.models.publish(bearer("ADMIN"), config(upstream=upstream_url + "/v1", version=8))
        self.assertEqual(await send("subchain-old-version", 7), response_body)
        platform.models.revoke(bearer("ADMIN"), 7)
        await send("subchain-revoked", 7, 403)
        count = len(recorded)
        # A real dialogue reference cannot authorize configuration, even with a gateway token.
        os.environ["TS012_PLATFORM_ORIGIN"] = platform.origins.issue(
            bearer("CONNECTOR"), "chat-entry"
        )["assertion_ref"]
        await send("subchain-purpose-denied", 8, 403)
        self.assertEqual(len(recorded), count)
        os.environ["TS012_PLATFORM_ORIGIN"] = source_ref
        expired = config(upstream=upstream_url + "/v1", version=9)
        expired["usable_until"] = utc(time.time() + 0.3)
        platform.models.publish(bearer("ADMIN"), expired)
        await asyncio.sleep(0.35)
        await send("subchain-expired", 9, 403)
        self.assertEqual(len(recorded), count)
        self.assertEqual(gateway[GATEWAY].active, 0)


if __name__ == "__main__":
    unittest.main()

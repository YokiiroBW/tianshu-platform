"""Real verified TLS sockets; the remote Core is explicitly a synthetic HTTP fixture."""

import asyncio
import copy
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import aiohttp
from aiohttp import web

from fixtures import ENV, ROOT, bearer, resolve
from services.platform.contracts import Fault
from services.platform.server import create_app
from services.platform.service import Platform
from services.platform.transport import core_post, server_tls
from source_fixtures import core_fixture, current_request, input_request, register, source_settings


@unittest.skipUnless(
    os.environ.get("TS013_TLS_PYTHON"),
    "set TS013_TLS_PYTHON to a certificate-tool Python with cryptography; real TLS not verified",
)
class SourceHttpsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(
            os.environ, {**ENV, "TS013_CORE_DISPATCH": "synthetic-ts013-core-dispatch-credential"}
        )
        env.start()
        self.addCleanup(env.stop)
        await asyncio.to_thread(
            subprocess.run,
            [
                os.environ["TS013_TLS_PYTHON"],
                str(ROOT / "tests/backend/make_tls_fixture.py"),
                self.temp.name,
            ],
            check=True,
            capture_output=True,
            timeout=20,
        )
        self.cert = str(Path(self.temp.name) / "localhost.pem")
        self.tls_config = {
            "certificate_file": self.cert,
            "private_key_file": str(Path(self.temp.name) / "localhost-key.pem"),
        }
        self.ssl = ssl.create_default_context(cafile=self.cert)
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5), trust_env=False)
        self.addAsyncCleanup(self.client.close)
        self.now = time.time()
        self.settings = source_settings(self.temp.name)
        self.settings.update(mode="service_https", tls=self.tls_config)
        self.p = Platform(self.settings, clock=lambda: self.now)
        self.runner, self.url = await self.start_tls(create_app(self.p))
        self.calls, self.previous = [], None
        self.drop_response = False
        self.redirect = False
        self.trap_calls = 0

        async def core(request):
            self.assertEqual(
                request.headers["Authorization"], "Bearer " + os.environ["TS013_CORE_DISPATCH"]
            )
            if self.redirect:
                return web.Response(status=307, headers={"Location": self.url + "/trap"})
            ingest = await request.json()
            self.calls.append(copy.deepcopy(ingest))
            authority = await self.post(input_request(ingest), "COMPANION")
            response = core_fixture(ingest, authority, self.now, previous=self.previous)
            if self.previous is None:
                self.previous = copy.deepcopy(response)
            if self.drop_response:
                self.drop_response = False
                # Commit at fixture owner, then sever the actual TLS response.
                request.transport.close()
                return web.Response()
            return web.json_response(response)

        core_app = web.Application()
        core_app.router.add_post("/internal/v1/conversation/ingest-actors", core)
        _, self.core_url = await self.start_tls(core_app)
        self.p.sources.core = {
            "base_url": self.core_url,
            "token_env": "TS013_CORE_DISPATCH",
            "ca_file": self.cert,
            "timeout_seconds": 3,
        }

    async def start_tls(self, app):
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_tls(self.tls_config))
        await site.start()
        return runner, "https://127.0.0.1:" + str(runner.addresses[0][1])

    async def post(
        self,
        body,
        identity="MEMORY",
        expected=200,
        path="/internal/v1/source-access/read",
        headers=None,
    ):
        async with self.client.post(
            self.url + path,
            json=body,
            headers={"Authorization": bearer(identity), **(headers or {})},
            ssl=self.ssl,
        ) as response:
            result = await response.json()
            self.assertEqual(response.status, expected, result)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            return result

    async def test_real_tls_dispatch_backfill_input_current_viewer_and_denials(self):
        ingest = register(self.p, self.now)
        response = await self.p.sources.dispatch(bearer("CONNECTOR"), ingest)
        self.assertEqual(len(self.calls), 1)
        current = await self.post(current_request(response))
        self.assertTrue(all(g["state"] == "allowed" for g in current["grants"]))
        ref = response["outcomes"][0]["admission"]["accepted_origin"]["assertion_ref"]
        context = await self.post(
            resolve(ref), "MEMORY_RESOLVER", path="/internal/v1/origins/resolve"
        )
        viewer = {"origin": {"assertion_ref": ref}, "scope": context["context"]["allowed_scope"]}
        viewed = await self.post(current_request(response, viewer))
        self.assertEqual(viewed["viewer_context"]["authenticated_service"], "companion")
        await self.post(input_request(ingest), "MEMORY", 403)
        await self.post(current_request(response), "COMPANION", 403)
        await self.post(input_request(ingest), "COMPANION", 403, headers={"Cookie": "fake"})
        await self.post({**input_request(ingest), "issuer": "platform"}, "COMPANION", 400)
        await self.post({"operation": "register"}, "COMPANION", 404)
        await self.post(
            {"input": ingest["input"]}, "CONNECTOR", 404, path="/internal/v1/source-input/register"
        )
        await self.post(
            current_request(),
            expected=401,
            headers={"Authorization": "Bearer synthetic-wrong-credential"},
        )
        self.p.origins.revoke(bearer("ADMIN"), "entry", "actor-a")
        after = await self.post(current_request(response))
        self.assertEqual([g["state"] for g in after["grants"]], ["denied", "allowed"])
        self.assertGreater(after["head"]["sequence"], current["head"]["sequence"])

    async def test_lost_tls_response_restart_new_ref_same_key_recovers_mapping(self):
        ingest = register(self.p, self.now, targets=[])
        self.drop_response = True
        with self.assertRaises(Fault) as caught:
            await self.p.sources.dispatch(bearer("CONNECTOR"), ingest)
        self.assertEqual(caught.exception.code, "dependency_unavailable")
        await self.post(current_request(self.previous), expected=503)
        before = (await self.post(current_request()))["head"]
        self.now += 70
        await self.runner.cleanup()
        self.settings["input_entries"]["input-entry"].update(
            default_actor_ids=["actor:a", "actor:b"], routing_version=2
        )
        self.settings["core"] = self.p.sources.core
        self.p = Platform(self.settings, clock=lambda: self.now)
        self.runner, self.url = await self.start_tls(create_app(self.p))
        retry = register(self.p, self.now, targets=[])
        retry["command"]["request_id"] = "https-retry"
        response = await self.p.sources.dispatch(bearer("CONNECTOR"), retry)
        self.assertEqual(response["effective_actor_ids"], ["actor:a"])
        self.assertEqual(response["routing_version"], 1)
        self.assertEqual(response["outcomes"][0]["state"], "duplicate")
        current = await self.post(current_request(response))
        self.assertEqual(current["grants"][0]["state"], "allowed")
        self.assertEqual(current["head"]["generation"], before["generation"])
        self.assertGreater(current["head"]["sequence"], before["sequence"])

    async def test_tls_verification_fixed_target_no_redirect_and_plain_http_rejected(self):
        ingest = register(self.p, self.now)
        with self.assertRaises(aiohttp.ClientConnectorCertificateError):
            async with self.client.post(
                self.url + "/internal/v1/source-access/read",
                json=current_request(),
                headers={"Authorization": bearer("MEMORY")},
                ssl=ssl.create_default_context(),
            ):
                pass
        for settings in (
            {**self.p.sources.core, "base_url": "http://127.0.0.1"},
            {**self.p.sources.core, "base_url": self.core_url + "?token=forged"},
            {**self.p.sources.core, "token": "payload-secret"},
            {"base_url": self.core_url, "token_env": "TS013_CORE_DISPATCH"},
        ):
            with self.assertRaises(Fault):
                await core_post(settings, ingest)
        self.redirect = True
        with self.assertRaises(Fault):
            await self.p.sources.dispatch(bearer("CONNECTOR"), ingest)
        self.assertEqual(self.calls, [])
        # Starting the service app without TLS cannot promote forwarded headers.
        runner = web.AppRunner(create_app(self.p), access_log=None)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        async with self.client.post(
            "http://127.0.0.1:" + str(runner.addresses[0][1]) + "/internal/v1/source-access/read",
            json=current_request(),
            headers={"Authorization": bearer("MEMORY"), "X-Forwarded-Proto": "https"},
        ) as response:
            self.assertEqual(response.status, 403)

    async def test_real_tls_cli_start_read_stop_and_application_registration(self):
        settings_path = Path(self.temp.name) / "settings.json"
        settings_path.write_text(json.dumps(self.settings), encoding="utf-8")
        sample = register(self.p, self.now)
        input_path = Path(self.temp.name) / "register.json"
        input_path.write_text(
            json.dumps({"entry_id": "input-entry", "input": sample["input"]}), encoding="utf-8"
        )
        result = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "-m",
                "services.platform",
                "--settings",
                str(settings_path),
                "local",
                "--credential-env",
                "TS012_CONNECTOR",
                "register-input",
                "--input",
                str(input_path),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["proof"], "source_input")
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "services.platform",
            "--settings",
            str(settings_path),
            "serve",
            "--port",
            str(port),
            cwd=ROOT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            for _ in range(100):
                try:
                    async with self.client.post(
                        f"https://127.0.0.1:{port}/internal/v1/source-access/read",
                        json=current_request(),
                        headers={"Authorization": bearer("MEMORY")},
                        ssl=self.ssl,
                    ) as response:
                        self.assertEqual(response.status, 200)
                        break
                except aiohttp.ClientConnectorError:
                    await asyncio.sleep(0.05)
            else:
                self.fail("TLS CLI did not start")
        finally:
            if process.returncode is None:
                process.terminate()
            await asyncio.wait_for(process.communicate(), timeout=5)
        with self.assertRaises(aiohttp.ClientConnectorError):
            async with self.client.post(
                f"https://127.0.0.1:{port}/internal/v1/source-access/read",
                json=current_request(),
                ssl=self.ssl,
            ):
                pass

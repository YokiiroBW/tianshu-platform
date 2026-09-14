"""Platform authorization and real TLS transport with an explicitly synthetic upstream."""

import asyncio
import copy
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web

from fixtures import ENV, ROOT, bearer, settings
from services.platform.assets import RESPONSE_LIMIT
from services.platform.contracts import Fault, canonical
from services.platform.service import Platform
from services.platform.transport import server_tls


def asset_settings(directory, endpoint, ca):
    config = settings(directory)
    config["asset_connections"] = {
        "library-a": {"endpoint": endpoint, "ca_file": ca, "token_env": "TS064_ASSET_A"},
        "library-b": {"endpoint": endpoint, "ca_file": ca, "token_env": "TS064_ASSET_B"},
    }
    config["principals"]["reader"].update(actions=["asset.read"], asset_connections=["library-a"])
    return config


@unittest.skipUnless(os.environ.get("TS013_TLS_PYTHON"), "TLS certificate tool not configured")
class AssetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        environment = patch.dict(
            os.environ,
            {
                **ENV,
                "TS064_ASSET_A": "synthetic-distinct-asset-service-a",
                "TS064_ASSET_B": "synthetic-distinct-asset-service-b",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        subprocess.run(
            [
                os.environ["TS013_TLS_PYTHON"],
                str(ROOT / "tests/backend/make_tls_fixture.py"),
                self.temp.name,
            ],
            check=True,
            capture_output=True,
        )
        self.ca = str(Path(self.temp.name) / "localhost.pem")
        self.calls = []
        self.trap_calls = 0
        self.mode = "success"
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

        async def upstream(request):
            for name in request.headers:
                self.assertFalse(name.lower().startswith("sec-fetch-"))
                self.assertNotIn(name.lower(), {"cookie", "origin", "x-assetlibrary-csrf"})
            self.assertEqual(
                request.headers["Authorization"], "Bearer " + os.environ["TS064_ASSET_A"]
            )
            data = await request.json()
            self.calls.append(data)
            self.entered.set()
            mode = self.mode
            if mode == "delay":
                await self.release.wait()
            if mode == "redirect":
                return web.Response(status=307, headers={"Location": "/trap"})
            if mode == "huge":
                stream = web.StreamResponse(headers={"Content-Type": "application/json"})
                await stream.prepare(request)
                try:
                    for _ in range(18):
                        await stream.write(b" " * 65536)
                    await stream.write_eof()
                except (ConnectionResetError, ConnectionError):
                    pass
                return stream
            if mode == "length":
                return web.Response(
                    body=b" " * (RESPONSE_LIMIT + 1), content_type="application/json"
                )
            if mode == "malformed":
                return web.Response(
                    text='{"request_id":1,"request_id":2}', content_type="application/json"
                )
            if mode == "exact":
                result = {
                    "message_type": "control.result",
                    "request_id": data["request_id"],
                    "ok": True,
                    "body": {"padding": ""},
                }
                result["body"]["padding"] = "x" * (RESPONSE_LIMIT - len(canonical(result).encode()))
                return web.Response(
                    body=canonical(result).encode(), content_type="application/json"
                )
            if mode in {"401", "404", "503", "unknown"}:
                return web.json_response(
                    {
                        "message_type": "error",
                        "request_id": "unknown" if mode == "unknown" else data["request_id"],
                        "error": {
                            "code": "not_found",
                            "message": "secret should never leak",
                            "retryable": True,
                        },
                    },
                    status=401 if mode == "unknown" else int(mode),
                )
            return web.json_response(
                {
                    "message_type": "control.result",
                    "request_id": "wrong" if mode == "mismatch" else data["request_id"],
                    "ok": True,
                    "body": {
                        "items": [{"name": "说明_中文", "availability": "offline"}],
                        "next_cursor": "opaque-scope",
                    },
                }
            )

        app = web.Application()

        async def trap(request):
            self.trap_calls += 1
            return web.Response(status=403)

        app.router.add_post("/assetlink/v1/control", upstream)
        app.router.add_post("/trap", trap)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        await web.SockSite(
            runner,
            sock,
            ssl_context=server_tls(
                {
                    "certificate_file": self.ca,
                    "private_key_file": str(Path(self.temp.name) / "localhost-key.pem"),
                }
            ),
        ).start()
        self.config = asset_settings(
            self.temp.name, f"https://127.0.0.1:{port}/assetlink/v1/control", self.ca
        )
        self.platform = Platform(self.config)
        self.addCleanup(self.release.set)

    async def read(self, operation="libraries.list", body=None, **extra):
        return await self.platform.assets.read(
            bearer("READER"),
            {"connection_id": "library-a", "operation": operation, "body": body or {}, **extra},
        )

    def assert_failure(self, result, status):
        self.assertEqual(result["status"], status, result)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["body"])
        self.assertTrue(result["clear_previous"])
        self.assertEqual(result["retry"], "manual")
        self.assertNotIn("secret", str(result))

    async def test_five_reads_and_opaque_cursor_forwarding(self):
        for operation in (
            "libraries.list",
            "libraries.get",
            "entries.browse",
            "entries.get",
            "assets.search",
        ):
            result = await self.read(
                operation, {"page_size": 100, "cursor": "opaque-scope", "query": "说明_中文"}
            )
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["request_id"], result["upstream_request_id"])
            self.assertEqual(result["original_available"], "not_verified")
            self.assertEqual(self.calls[-1]["body"]["cursor"], "opaque-scope")

    async def test_principal_action_and_connection_are_independent(self):
        request = {"connection_id": "library-a", "operation": "libraries.list", "body": {}}
        for caller in ("ADMIN", "CONNECTOR", "COMPANION_RESOLVER"):
            self.assert_failure(await self.platform.assets.read(bearer(caller), request), 403)
        request["connection_id"] = "library-b"
        self.assert_failure(await self.platform.assets.read(bearer("READER"), request), 403)
        self.assert_failure(await self.read(asset_connections=["library-b"]), 400)
        self.assertEqual(self.calls, [])

    async def test_revoked_platform_principal_denied(self):
        self.platform.origins.revoke(bearer("ADMIN"), "principal", "reader")
        self.assert_failure(await self.read(), 401)
        self.assertEqual(self.calls, [])

    async def test_local_authority_busy_is_bounded_and_clears(self):
        with self.platform.store.connect(write=True):
            started = asyncio.get_running_loop().time()
            self.assert_failure(await self.read(), 503)
            self.assertLess(asyncio.get_running_loop().time() - started, 0.5)
        self.assertEqual(self.calls, [])

    async def test_local_parse_errors_keep_correlation(self):
        for data, status in ((b"{", 400), (b" " * 65537, 413)):
            result = await self.platform.assets.read(bearer("READER"), data)
            self.assert_failure(result, status)
            self.assertTrue(result["request_id"])
            self.assertIsNone(result["upstream_request_id"])

    async def test_only_reads_page_and_utf8_request_budget(self):
        self.assert_failure(await self.read("libraries.register"), 403)
        for size in (0, 101, True):
            self.assert_failure(await self.read(body={"page_size": size}), 400)
        self.assert_failure(await self.read(body={"query": "中" * 22000}), 413)
        self.assertEqual(self.calls, [])

    async def test_remote_errors_clear_and_do_not_retry(self):
        self.assertTrue((await self.read())["ok"])
        for mode in ("401", "404", "503", "unknown"):
            self.mode = mode
            result = await self.read()
            self.assert_failure(result, 401 if mode == "unknown" else int(mode))
            if mode == "unknown":
                self.assertEqual(result["upstream_request_id"], "unknown")
                self.assertNotEqual(result["request_id"], "unknown")
        self.assertEqual(len(self.calls), 5)
        self.mode = "success"
        self.assertTrue((await self.read())["ok"])

    async def test_response_budgets_and_invalid_envelopes(self):
        for mode, status in (
            ("huge", 413),
            ("length", 413),
            ("malformed", 502),
            ("mismatch", 502),
            ("redirect", 502),
        ):
            self.mode = mode
            self.assert_failure(await self.read(), status)
        self.assertEqual(len(self.calls), 5)

        self.assertEqual(self.trap_calls, 0)

    async def test_exact_request_and_response_boundaries(self):
        body = {"padding": ""}
        wire = {
            "message_type": "control.request",
            "request_id": "0" * 36,
            "operation": "libraries.list",
            "body": body,
            "timeout_ms": 5000,
        }
        body["padding"] = "x" * (65536 - len(canonical(wire).encode()))
        self.assertTrue((await self.read(body=body))["ok"])
        body["padding"] += "x"
        self.assert_failure(await self.read(body=body), 413)
        self.mode = "exact"
        self.assertTrue((await self.read())["ok"])

    async def test_bad_ca_and_reconnect(self):
        with tempfile.TemporaryDirectory() as wrong:
            subprocess.run(
                [
                    os.environ["TS013_TLS_PYTHON"],
                    str(ROOT / "tests/backend/make_tls_fixture.py"),
                    wrong,
                ],
                check=True,
                capture_output=True,
            )
            self.platform.assets.connections["library-a"]["ca_file"] = str(
                Path(wrong) / "localhost.pem"
            )
            self.assert_failure(await self.read(), 503)
        self.assertEqual(self.calls, [])
        self.platform.assets.connections["library-a"]["ca_file"] = self.ca
        self.assertTrue((await self.read())["ok"])

    async def test_total_deadline_discards_late_response(self):
        self.mode = "delay"
        started = asyncio.get_running_loop().time()
        result = await self.read()
        self.assert_failure(result, 504)
        self.assertLess(asyncio.get_running_loop().time() - started, 5.5)
        self.release.set()
        self.mode = "success"
        self.assertTrue((await self.read())["ok"])

    async def test_cancellation_discards_late_response_and_closes(self):
        self.mode = "delay"
        task = asyncio.create_task(self.read())
        await self.entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.release.set()
        self.mode = "success"
        self.assertTrue((await self.read())["ok"])

    async def test_inflight_platform_revocation_discards_body(self):
        self.mode = "delay"
        task = asyncio.create_task(self.read())
        await self.entered.wait()
        self.platform.origins.revoke(bearer("ADMIN"), "principal", "reader")
        self.release.set()
        self.assert_failure(await task, 401)

    async def test_configuration_and_shared_token_fail_closed(self):
        for endpoint in (
            "http://localhost/assetlink/v1/control",
            "https://user:pass@localhost/assetlink/v1/control",
            "https://localhost/other",
            "https://localhost/assetlink/v1/control?x=1",
        ):
            config = copy.deepcopy(self.config)
            config["asset_connections"]["library-a"]["endpoint"] = endpoint
            with self.assertRaises(Fault):
                Platform(config)
        config = copy.deepcopy(self.config)
        config["asset_connections"]["library-a"]["token_env"] = "TS012_READER"
        with self.assertRaises(Fault):
            Platform(config)
        with patch.dict(os.environ, {"TS064_ASSET_A": ENV["TS012_READER"]}):
            self.assert_failure(await self.read(), 503)
        self.assertEqual(self.calls, [])

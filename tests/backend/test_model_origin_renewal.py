"""Dedicated-scope authority and real TLS boundary; all identities/data are synthetic."""

import asyncio
import hashlib
import json
import os
import socket
import ssl
import subprocess
import tempfile
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import aiohttp
from aiohttp import web

from fixtures import CONTRACT, ENV, ROOT, bearer, config, query, settings
from services.platform.contracts import Fault, epoch, utc
from services.platform.model_origin_renewal import (
    PATH,
    enabled,
    renew,
    validate_request,
    validate_response,
)
from services.platform.server import create_app
from services.platform.service import Platform, validate_settings
from services.platform.transport import server_tls


def request(ref):
    return {"schema_version": 1, "request_id": str(uuid.uuid4()), "assertion_ref": ref}


class RenewalAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)
        self.now = 1_800_000_000.0
        self.settings = settings(self.temp.name)
        self.settings["entries"]["config-entry"].update(
            ttl_seconds=10, expires_at=utc(self.now + 25)
        )
        self.p = Platform(self.settings, clock=lambda: self.now)
        self.addCleanup(self.p.close)
        self.ref = self.p.origins.issue(bearer("ADMIN"), "config-entry")["assertion_ref"]

    def renew(self, body=None, identity="GATEWAY"):
        return renew(self.p.origins, bearer(identity), body or request(self.ref))

    def test_same_ref_multiple_cycles_ttl_ceiling_and_redacted_audit(self):
        body = request(self.ref)
        for advance, expected in ((5, 15), (5, 20), (5, 25), (5, 25)):
            self.now += advance
            result = self.renew(body)
            self.assertEqual(set(result), set(body) | {"expires_at"})
            self.assertEqual({k: result[k] for k in body}, body)
            self.assertEqual(epoch(result["expires_at"]), 1_800_000_000 + expected)
        with self.p.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM origins").fetchone()[0], 1)
            records = db.execute("SELECT object_id FROM audit WHERE operation='origin.renew'")
            self.assertTrue(all(self.ref not in row[0] and len(row[0]) == 64 for row in records))
        self.now += 5
        with self.assertRaises(Fault):
            self.renew()

    def test_each_live_revocation_stops_renewal_and_snapshot(self):
        self.p.models.publish(bearer("ADMIN"), config(self.now))
        for kind, object_id in (
            ("origin", self.ref),
            ("entry", "config-entry"),
            ("principal", "gateway"),
            ("principal", "admin"),
        ):
            with self.subTest(kind=kind, object_id=object_id):
                # Separate database/authority per counterexample, no direct DB restoration.
                with tempfile.TemporaryDirectory() as directory:
                    p = Platform(settings(directory), clock=lambda: self.now)
                    self.addCleanup(p.close)
                    ref = p.origins.issue(bearer("ADMIN"), "config-entry")["assertion_ref"]
                    p.models.publish(bearer("ADMIN"), config(self.now))
                    p.origins.revoke(bearer("ADMIN"), kind, ref if kind == "origin" else object_id)
                    with self.assertRaises(Fault):
                        renew(p.origins, bearer("GATEWAY"), request(ref))
                    with self.assertRaises(Fault):
                        p.models.snapshot(bearer("GATEWAY"), query(ref))

    def test_expired_ref_cannot_be_resurrected(self):
        self.now += 10
        with self.assertRaises(Fault) as caught:
            self.renew()
        self.assertEqual(caught.exception.status, 403)

    def test_gateway_never_gains_issue_authority(self):
        self.renew()
        with self.assertRaises(Fault):
            self.p.origins.issue(bearer("GATEWAY"), "config-entry")
        for identity in ("ADMIN", "CONNECTOR", "MEMORY", "READER"):
            with self.subTest(identity=identity), self.assertRaises(Fault):
                self.renew(identity=identity)

    def test_wrong_or_mixed_scope_and_digest_changes_rejected(self):
        chat = self.p.origins.issue(bearer("CONNECTOR"), "chat-entry")["assertion_ref"]
        with self.assertRaises(Fault):
            self.renew(request(chat))
        self.p.auth.entries["config-entry"]["audience"] = "group"
        with self.assertRaises(Fault):
            self.renew()
        with tempfile.TemporaryDirectory() as directory:
            cfg = settings(directory)
            cfg["entries"]["config-entry"]["routes"].append(
                {"caller": "companion", "receiver": "memory", "purpose": "dialogue"}
            )
            p = Platform(cfg, clock=lambda: self.now)
            self.addCleanup(p.close)
            ref = p.origins.issue(bearer("ADMIN"), "config-entry")["assertion_ref"]
            with self.assertRaises(Fault):
                renew(p.origins, bearer("GATEWAY"), request(ref))

    def test_concurrent_duplicates_stay_one_bounded_ref(self):
        body = request(self.ref)
        self.now += 5
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.renew(body), range(24)))
        self.assertTrue(all(result == results[0] for result in results))
        with self.p.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM origins").fetchone()[0], 1)
        self.p.origins.revoke(bearer("ADMIN"), "origin", self.ref)
        with self.assertRaises(Fault):
            self.renew(body)

    def test_strict_request_and_settings(self):
        good = request(self.ref)
        for key, value in (
            ("schema_version", True),
            ("schema_version", 1.0),
            ("request_id", "bad"),
            ("request_id", None),
            ("assertion_ref", "origin:wrong"),
            ("assertion_ref", None),
            ("ttl_seconds", 3600),
            ("scope", {}),
            ("owner", "admin"),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(Fault):
                validate_request({**good, key: value})
        for body in (None, [], {}, {k: v for k, v in good.items() if k != "request_id"}):
            with self.assertRaises(Fault):
                validate_request(body)
        self.assertFalse(enabled(self.settings))
        for value in (True, "true", 1, None):
            with self.assertRaises(Fault):
                validate_settings({**self.settings, "model_origin_renewal_http": value})
        validate_settings({**self.settings, "model_origin_renewal_http": False})

    def test_coordinator_frozen_contract_pinned_bytes_and_all_examples(self):
        source = CONTRACT.parents[1] / "model-origin-renewal/v1"
        raw = (source / "manifest.json").read_bytes()
        self.assertEqual(
            hashlib.sha256(raw).hexdigest(),
            "c5017724187c1386b647fcc5b41ab3cb1702f27d6192f3a87fcefb23e7a5a61c",
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            for name, expected in json.loads(raw)["files"].items():
                data = (source / name).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), expected)
                (target / name).write_bytes(data)
            examples = json.loads((target / "examples.json").read_bytes())
            validate_request(examples["request"])
            validate_response(examples["response"])
            for item in json.loads((target / "negative-examples.json").read_bytes()):
                with self.subTest(item=item), self.assertRaises(Fault):
                    {"request": validate_request, "response": validate_response}[item["kind"]](
                        item["document"]
                    )


@unittest.skipUnless(
    os.environ.get("TS013_TLS_PYTHON"), "explicit certificate-tool Python required"
)
class RenewalHttpsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, ENV)
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
        self.cfg = settings(self.temp.name)
        self.cfg.update(
            mode="service_https",
            model_origin_renewal_http=True,
            tls={
                "certificate_file": self.cert,
                "private_key_file": str(Path(self.temp.name) / "localhost-key.pem"),
            },
        )
        self.p = Platform(self.cfg)
        self.addCleanup(self.p.close)
        self.ref = self.p.origins.issue(bearer("ADMIN"), "config-entry")["assertion_ref"]
        self.runner, self.url = await self.start(create_app(self.p))
        self.client = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=ssl.create_default_context(cafile=self.cert)),
            trust_env=False,
        )
        self.addAsyncCleanup(self.client.close)

    async def start(self, app):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
        runner = web.AppRunner(
            app, access_log=None, handler_cancellation=True, shutdown_timeout=0.5
        )
        await runner.setup()
        await web.SockSite(runner, sock, ssl_context=server_tls(self.cfg["tls"])).start()
        self.addAsyncCleanup(runner.cleanup)
        return runner, f"https://127.0.0.1:{sock.getsockname()[1]}"

    async def post(self, body, status=200, **kwargs):
        headers = {"Authorization": bearer("GATEWAY"), **kwargs.pop("headers", {})}
        async with self.client.post(
            self.url + PATH, json=body, headers=headers, **kwargs
        ) as response:
            result = await response.json()
            self.assertEqual(response.status, status, result)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            if status != 200:
                self.p.contracts.check("common#error", result)
            return result

    async def test_tls_same_ref_snapshot_and_revocation(self):
        result = await self.post(request(self.ref))
        self.assertEqual(result["assertion_ref"], self.ref)
        self.p.models.publish(bearer("ADMIN"), config())
        async with self.client.post(
            self.url + "/internal/v1/model-config/snapshot",
            json=query(self.ref),
            headers={"Authorization": bearer("GATEWAY")},
        ) as r:
            self.assertEqual(r.status, 200)
        self.p.origins.revoke(bearer("ADMIN"), "origin", self.ref)
        await self.post(request(self.ref), 403)

    async def test_https_boundary_strict_default_closed_and_browser_refusal(self):
        body = request(self.ref)
        await self.post({**body, "ttl_seconds": 100}, 400)
        await self.post(body, 403, headers={"Cookie": "fake"})
        await self.post(body, 403, headers={"Origin": "https://localhost"})
        await self.post(body, 401, headers={"Authorization": "Bearer wrong"})
        await self.post({**body, "padding": "x" * 4096}, 413)
        async with self.client.post(
            self.url + PATH,
            data=b'{"schema_version":1,"schema_version":1}',
            headers={"Authorization": bearer("GATEWAY"), "Content-Type": "application/json"},
        ) as r:
            self.assertEqual(r.status, 400)
        self.p.settings = {**self.cfg, "model_origin_renewal_http": False}
        _, self.url = await self.start(create_app(self.p))
        await self.post(body, 404)

    async def test_slow_transaction_does_not_block_loop_and_capacity_is_retained(self):
        import threading

        started, release = threading.Event(), threading.Event()
        original = self.p.auth.authenticate

        def slow(*args, **kwargs):
            started.set()
            release.wait(2)
            return original(*args, **kwargs)

        with patch.object(self.p.auth, "authenticate", side_effect=slow):
            task = asyncio.create_task(self.post(request(self.ref)))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                start = time.monotonic()
                async with self.client.get(self.url + "/health/live") as r:
                    self.assertEqual(r.status, 200)
                self.assertLess(time.monotonic() - start, 0.5)
                self.assertEqual(self.p.local_work.active, 1)
            finally:
                release.set()
            await task

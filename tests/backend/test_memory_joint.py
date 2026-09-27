"""Optional real Platform issuer -> real Memory process -> synthetic source owners joint test.

Set TS_CONNECT_M_PATH to a fixed Memory worktree commit. No files are written there.
"""

import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
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


class MemoryJointTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_platform_issuer_and_memory_browser_process(self):
        memory_root = os.environ.get("TS_CONNECT_M_PATH")
        if not memory_root:
            self.skipTest("TS_CONNECT_M_PATH is not configured")
        memory_root = Path(memory_root).resolve()
        sys.path[:0] = [str(memory_root / "src"), str(memory_root / "tests")]
        self.addCleanup(lambda: sys.path.__delitem__(slice(0, 2)))
        from source_sync_harness import SyncHarness
        from tianshu_memory.contracts import Contracts

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        certificate = root / "localhost.pem"
        key = root / "localhost-key.pem"
        subprocess.run(
            [os.environ["TS013_TLS_PYTHON"], str(Path(__file__).with_name("make_tls_fixture.py")), str(root)],
            check=True,
            capture_output=True,
        )
        shutil.copyfile(certificate, root / "ca.pem")
        shutil.copyfile(certificate, root / "server.pem")
        shutil.copyfile(key, root / "server.key")
        contract_dir = Path(os.environ["TS012_CONTRACT_DIR"])
        contracts = Contracts(contract_dir)
        contracts.load_sources()
        documents = json.loads(
            (contract_dir.parents[1] / "source-sync/v1/examples/documents.json").read_text(encoding="utf-8")
        )
        examples = {item["id"]: item["document"] for item in documents}
        h = SyncHarness(root, contracts, examples, root)
        h.config["callers"]["platform"] = {
            "token": "synthetic-platform-browser-only-credential",
            "issuer": "platform",
            "issuer_token": ENV["TS012_MEMORY_RESOLVER"],
            "issuer_ca_file": str(certificate),
            "allowed_actors": ["actor:a"],
            "operations": ["browse"],
        }
        h.config["browser_readers"] = {"platform": {}}
        env = mock.patch.dict(
            os.environ,
            {**ENV, "TEST_MEMORY_BROWSER": "synthetic-platform-browser-only-credential"},
        )
        env.start()
        self.addCleanup(env.stop)
        monkeypatch = __import__("pytest").MonkeyPatch()
        self.addCleanup(monkeypatch.undo)
        with h.owners():
            h.config["callers"]["platform"]["issuer_url"] = h.config["callers"]["companion"]["issuer_url"]
            h.save()
            with h.runtime(monkeypatch):
                seeded, _, _ = h.seed()
                config = web_settings(str(root))
                config["principals"]["admin"]["actions"].append("memory.read")
                config["principals"]["memory_resolver"]["resolver"]["caller"] = "platform"
                config["entries"]["actor-a"]["routes"].append(
                    {"caller": "platform", "receiver": "memory", "purpose": "dialogue"}
                )
                config["web_memory"] = {
                    "enabled": True,
                    "base_url": "https://127.0.0.1:" + str(port()),
                    "token_env": "TEST_MEMORY_BROWSER",
                    "ca_file": str(certificate),
                    "entry_id": "actor-a",
                }
                platform = Platform(config)
                entry = config["entries"]["actor-a"]
                with platform.store.connect(write=True) as db:
                    db.execute(
                        "INSERT INTO identities VALUES(?,?,?)",
                        (canonical(entry["account"]), h.person, 1),
                    )
                    db.execute(
                        "INSERT INTO channels VALUES(?,?)",
                        (channel_key(entry), h.scope()["conversation_id"]),
                    )
                app = create_app(platform)
                runner, web_url = await start_http(app)
                self.addAsyncCleanup(runner.cleanup)
                config["web"]["origin"] = web_url
                issuer_port = port()
                server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                server_tls.load_cert_chain(certificate, key)
                await web.TCPSite(runner, "127.0.0.1", issuer_port, ssl_context=server_tls).start()
                h.config["callers"]["platform"]["issuer_url"] = (
                    f"https://127.0.0.1:{issuer_port}/internal/v1/origins/resolve"
                )
                h.config["browser_readers"]["platform"] = {
                    "account": entry["account"],
                    "actor_id": "actor:a",
                    "scopes": [h.scope()],
                }
                h.save()
                process_env = dict(
                    os.environ,
                    TIANSHU_MEMORY_CONFIG=str(h.config_path),
                    PYTHONPATH=str(memory_root / "src"),
                    PYTHONDONTWRITEBYTECODE="1",
                )
                memory_port = int(config["web_memory"]["base_url"].rsplit(":", 1)[1])
                process = subprocess.Popen(
                    [
                        sys.executable, "-m", "uvicorn", "tianshu_memory.app:configured_app",
                        "--factory", "--host", "127.0.0.1", "--port", str(memory_port),
                        "--ssl-certfile", str(certificate), "--ssl-keyfile", str(key),
                        "--no-access-log",
                    ],
                    env=process_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                try:
                    trust = ssl.create_default_context(cafile=certificate)
                    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
                        deadline = time.monotonic() + 10
                        while True:
                            if process.poll() is not None:
                                raise AssertionError(process.stdout.read().decode(errors="replace"))
                            try:
                                async with client.get(config["web_memory"]["base_url"] + "/health", ssl=trust) as response:
                                    if response.status == 200:
                                        break
                            except aiohttp.ClientError:
                                pass
                            if time.monotonic() >= deadline:
                                raise AssertionError("Memory process did not start")
                            await __import__("asyncio").sleep(0.05)
                        async with client.get(web_url + "/api/web/session") as response:
                            csrf = (await response.json())["csrf"]
                        async with client.post(
                            web_url + "/api/web/login",
                            json={"username": "synthetic-admin", "password": PASSWORD},
                            headers={"Origin": web_url, "X-CSRF-Token": csrf},
                        ) as response:
                            self.assertEqual(response.status, 200, await response.text())
                            csrf = (await response.json())["csrf"]

                        async def browser(name, body):
                            async with client.post(
                                web_url + "/api/web/memory/" + name,
                                json=body,
                                headers={"Origin": web_url, "X-CSRF-Token": csrf},
                            ) as response:
                                answer = await response.json()
                                self.assertEqual(response.status, 200, answer)
                                return answer

                        overview = await browser("overview", {})
                        self.assertEqual(overview["memory_group_count"], 1)
                        self.assertFalse(overview["counts_truncated"])
                        records = await browser("records", {"subject": None, "limit": 20, "cursor": None})
                        self.assertEqual(records["items"][0]["semantic_group_id"], seeded["group_ids"][0])
                        self.assertNotIn("scope", records)
                        self.assertNotIn("request_id", records)
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    process.stdout.close()

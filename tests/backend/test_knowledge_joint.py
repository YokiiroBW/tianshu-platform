"""Optional real Knowledge CLI HTTPS -> Platform browser read joint checks.

Set TS_CONNECT_M_PATH to a fixed Memory commit. Data, Git checkouts and keys are synthetic.
"""

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
from unittest import mock

import aiohttp

from fixtures import ENV, start_http
from services.platform.server import create_app
from services.platform.service import Platform
from web_fixtures import PASSWORD, web_settings


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class KnowledgeJointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        memory_root = os.environ.get("TS_CONNECT_M_PATH")
        if not memory_root:
            self.skipTest("TS_CONNECT_M_PATH is not configured")
        self.memory_root = Path(memory_root).resolve()
        sys.path[:0] = [str(self.memory_root / "src"), str(self.memory_root / "tests")]
        self.addCleanup(lambda: sys.path.__delitem__(slice(0, 2)))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        subprocess.run(
            [os.environ["TS013_TLS_PYTHON"], str(Path(__file__).with_name("make_tls_fixture.py")), self.temp.name],
            check=True, capture_output=True,
        )
        self.cert = str(self.root / "localhost.pem")
        self.key = str(self.root / "localhost-key.pem")
        from tianshu_memory.contracts import Contracts

        self.contracts = Contracts(Path(os.environ["TS012_CONTRACT_DIR"]))

    async def start(self, fixture, client_name, credential, projects):
        fixture.path.write_text(__import__("tianshu_memory.domain", fromlist=["canonical"]).canonical(fixture.config), encoding="utf-8")
        self.memory_port = port()
        env = mock.patch.dict(os.environ, {**ENV, "TEST_KNOWLEDGE_READER": credential})
        env.start()
        self.addCleanup(env.stop)
        process_env = dict(os.environ, PYTHONPATH=str(self.memory_root / "src"), PYTHONDONTWRITEBYTECODE="1")
        process = subprocess.Popen(
            [
                sys.executable, "-m", "tianshu_memory.knowledge_cli", "--config", str(fixture.path),
                "serve", "--client", client_name, "--port", str(self.memory_port),
                "--tls-certfile", self.cert, "--tls-keyfile", self.key,
                "--allowed-host", f"127.0.0.1:{self.memory_port}",
            ],
            cwd=self.memory_root,
            env=process_env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

        def close_process():
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()

        self.addCleanup(close_process)
        config = web_settings(str(self.root / "platform"))
        config["principals"]["admin"]["actions"].append("knowledge.read")
        config["web_knowledge"] = {
            "enabled": True,
            "base_url": f"https://127.0.0.1:{self.memory_port}",
            "token_env": "TEST_KNOWLEDGE_READER",
            "ca_file": self.cert,
            "projects": projects,
        }
        platform = Platform(config)
        runner, self.url = await start_http(create_app(platform))
        self.addAsyncCleanup(runner.cleanup)
        config["web"]["origin"] = self.url
        self.client = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.addAsyncCleanup(self.client.close)
        trust = ssl.create_default_context(cafile=self.cert)
        deadline = time.monotonic() + 10
        while True:
            if process.poll() is not None:
                raise AssertionError(process.stdout.read().decode(errors="replace"))
            try:
                async with self.client.get(config["web_knowledge"]["base_url"] + "/health", ssl=trust) as response:
                    if response.status == 200:
                        break
            except aiohttp.ClientError:
                pass
            if time.monotonic() > deadline:
                raise AssertionError("Knowledge CLI process did not start")
            await __import__("asyncio").sleep(0.05)
        async with self.client.get(self.url + "/api/web/session") as response:
            csrf = (await response.json())["csrf"]
        async with self.client.post(
            self.url + "/api/web/login",
            json={"username": "synthetic-admin", "password": PASSWORD},
            headers={"Origin": self.url, "X-CSRF-Token": csrf},
        ) as response:
            self.assertEqual(response.status, 200, await response.text())
            self.csrf = (await response.json())["csrf"]

    async def call(self, name, body, expected=200):
        async with self.client.post(
            self.url + "/api/web/knowledge/" + name,
            json=body,
            headers={"Origin": self.url, "X-CSRF-Token": self.csrf},
        ) as response:
            result = await response.json()
            self.assertEqual(response.status, expected, result)
            return result

    async def test_real_lesson_and_experience_queries(self):
        from test_lessons import REVIEW_SECRET, lessons, promote, shared_references, two_projects

        directory = self.root / "lessons"
        directory.mkdir()
        fixture = lessons.__wrapped__(directory, self.contracts)
        two_projects(fixture)
        promoted = promote(fixture, shared_references(fixture))
        fixture.config["knowledge"]["clients"]["operator"]["http_read_operations"] = [
            "lesson_query", "experience_query",
        ]
        platform_dir = self.root / "platform"
        platform_dir.mkdir()
        await self.start(
            fixture, "operator", REVIEW_SECRET,
            [{"project_id": name, "label": name} for name in ("alpha", "beta")],
        )
        lessons_result = await self.call("lessons", {"project_id": "alpha", "text": "receipt", "budget_bytes": 8192})
        self.assertTrue(lessons_result["result"]["lessons"])
        experiences = await self.call("experiences", {"project_id": "alpha", "text": "receipt", "budget_bytes": 8192})
        self.assertEqual(experiences["result"]["entries"][0]["entry_id"], promoted["entry_id"])
        fixture.config["knowledge"]["clients"]["operator"]["http_read_operations"] = []
        fixture.path.write_text(__import__("tianshu_memory.domain", fromlist=["canonical"]).canonical(fixture.config), encoding="utf-8")
        refused = await self.call("lessons", {"project_id": "alpha", "text": "receipt", "budget_bytes": 8192}, 503)
        self.assertEqual(refused["code"], "knowledge_operation_not_enabled")

    async def test_real_catalogue_and_document(self):
        from test_knowledge_catalog import SECRET, catalogue

        directory = self.root / "catalogue"
        directory.mkdir()
        fixture = catalogue.__wrapped__(directory, self.contracts)
        platform_dir = self.root / "platform"
        platform_dir.mkdir()
        await self.start(fixture, "alpha-writer", SECRET, [{"project_id": "alpha", "label": "Alpha"}])
        listed = await self.call("documents", {"project_id": "alpha", "limit": 8, "cursor": None})
        self.assertTrue(listed["result"]["items"])
        item = listed["result"]["items"][0]
        read = await self.call("document", {
            "project_id": "alpha", "document_id": item["document_id"],
            "expected_version": item["version"], "expected_hash": None,
            "limit": 8, "cursor": None,
        })
        self.assertTrue(read["result"]["blocks"])

    async def test_real_research_note_query(self):
        from test_research_notes import SECRET, imported, notes, record, unit

        directory = self.root / "notes"
        directory.mkdir()
        fixture = notes.__wrapped__(directory, self.contracts)
        imported(fixture)
        record(fixture, [unit(fixture)])
        platform_dir = self.root / "platform"
        platform_dir.mkdir()
        await self.start(fixture, "alpha-writer", SECRET, [{"project_id": "alpha", "label": "Alpha"}])
        query = await self.call("query", {"project_id": "alpha", "text": "receipt", "budget_bytes": 8192})
        self.assertTrue(query["result"]["blocks"])
        found = await self.call("notes", {"project_id": "alpha", "text": "receipt", "budget_bytes": 8192})
        self.assertTrue(found["result"]["notes"])

    async def test_real_continuation_handle_and_check(self):
        from test_knowledge_continuation import SECRET, checkouts, git_path

        directory = self.root / "checkouts"
        directory.mkdir()
        with mock.patch.dict(os.environ, {"TIANSHU_GIT": git_path()}):
            fixture = checkouts.__wrapped__(directory, self.contracts)
            fixture.config["knowledge"]["clients"]["writer"]["http_read_operations"] = [
                "continuation_recover", "continuation_check",
            ]
            platform_dir = self.root / "platform"
            platform_dir.mkdir()
            await self.start(
                fixture, "writer", SECRET,
                [{"project_id": "demo", "label": "Demo", "checkouts": [{"id": "agent-a", "label": "Agent A"}]}],
            )
            recovered = await self.call("continuation", {
                "project_id": "demo", "checkout_id": "agent-a", "text": "receipt", "budget_bytes": 16384,
            })
            self.assertEqual(recovered["result"]["checkout"]["id"], "agent-a")
            self.assertNotIn("seal", json.dumps(recovered))
            self.assertNotIn(str(fixture.first), json.dumps(recovered))
            checked = await self.call("continuation-check", {
                "project_id": "demo", "handle": recovered["result"]["handle"],
            })
            self.assertTrue(checked["result"]["valid"])
            wrong = await self.call("continuation", {
                "project_id": "demo", "checkout_id": "agent-b", "text": "receipt", "budget_bytes": 16384,
            }, 403)
            self.assertEqual(wrong["code"], "checkout_not_allowed")

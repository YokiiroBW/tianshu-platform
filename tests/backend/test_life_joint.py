"""Platform browser life reads against the real Companion HTTPS process and synthetic rows.

Set TS_COMPANION_PATH to a fixed Companion checkout. This test imports its fixture helpers
read-only, starts its actual ASGI server in a separate process, and never touches its database.
"""

import asyncio
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
from unittest.mock import patch

import aiohttp

from fixtures import ENV, start_http
from services.platform.server import create_app
from services.platform.service import Platform
from web_fixtures import PASSWORD, web_settings

TOKEN_ENV = "TS_CONNECT_LIFE_READER"
TOKEN = "synthetic-connect-life-reader-token-0001"


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def prepare_daily(database):
    """Settle real persistent plan and tied-position experience rows before serving."""
    from tianshu_companion.life import Life
    from tianshu_companion.store import Store

    now = time.time()
    store = Store(database)
    try:
        life = Life(store, lambda: now, None, None, asyncio.Semaphore(4))
        actor = store.get("life_actors", "actor:a")
        life.configure_actor(
            "actor:a",
            "room:study",
            personality_version=1,
            schedule=[
                dict(minute=0, activity="reading", controls={}),
                dict(minute=1439, activity="quiet evening", controls={}),
            ],
            expected=actor["version"],
        )
        life.synchronize_role("actor:a", enabled=True, personality_version=1)
        life.tick(force=True)
        for number in range(23):
            life.record_event(
                f"joint:{number:02d}",
                "world:home",
                f"Synthetic persisted experience {number:02d}",
                participants=["actor:a"],
            )
    finally:
        store.close()


class LifeJointTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_companion_life_reads_and_refusals(self):
        source = os.environ.get("TS_COMPANION_PATH")
        tls_python = os.environ.get("TS013_TLS_PYTHON")
        contract = os.environ.get("TS012_CONTRACT_DIR")
        if not source or not tls_python or not contract:
            raise unittest.SkipTest(
                "TS_COMPANION_PATH, TS013_TLS_PYTHON and TS012_CONTRACT_DIR required"
            )
        source = Path(source)
        if not (source / "src/tianshu_companion/life_read.py").is_file():
            raise unittest.SkipTest("Companion source is unavailable")
        for entry in (source / "src", source / "tests"):
            if str(entry) not in sys.path:
                sys.path.insert(0, str(entry))

        # These helpers seed the producer's real Store and Life models with isolated fictional
        # rows, including a published revision. They do not read or write its checkout.
        from test_life_read_https import prepare, read_facts

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(
                [tls_python, str(Path(__file__).with_name("make_tls_fixture.py")), directory],
                check=True,
                capture_output=True,
                timeout=30,
            )
            certificate = root / "localhost.pem"
            key = root / "localhost-key.pem"
            database = root / "companion.sqlite"
            facts = prepare(database)
            prepare_daily(database)
            config = {
                "contracts_path": contract,
                "database_path": str(database),
                "config_version": 1,
                "life_writing": False,
                "policy": {"silence_ms": 0},
                "roles": {"actor:a": {"version": 1, "capabilities": []}},
                "callers": {"story_reader": {"token_env": TOKEN_ENV}},
                "life_readers": {
                    "story_reader": {"reader_id": "reader:story", "actor_ids": ["actor:a"]}
                },
                "services": {
                    name: {
                        "url": "https://127.0.0.1:9",
                        "token_env": "TS_CONNECT_UNUSED_" + name.upper(),
                        "ca_file": str(certificate),
                    }
                    for name in ("platform", "memory", "gateway", "nonebot")
                },
            }
            config_file = root / "companion.json"
            config_file.write_text(json.dumps(config), encoding="utf-8")
            port = free_port()
            core_url = f"https://127.0.0.1:{port}"
            environment = {
                **os.environ,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": str(source / "src") + os.pathsep + os.environ.get("PYTHONPATH", ""),
                "TIANSHU_COMPANION_CONFIG": str(config_file),
                TOKEN_ENV: TOKEN,
                **{
                    "TS_CONNECT_UNUSED_" + name.upper(): "synthetic-unused-remote-token"
                    for name in ("platform", "memory", "gateway", "nonebot")
                },
            }
            log_file = root / "companion.log"
            with log_file.open("wb") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "tianshu_companion.app:create_app",
                        "--factory",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--workers",
                        "1",
                        "--ssl-certfile",
                        str(certificate),
                        "--ssl-keyfile",
                        str(key),
                    ],
                    env=environment,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                try:
                    context = ssl.create_default_context(cafile=str(certificate))
                    async with aiohttp.ClientSession(trust_env=False) as core_client:
                        for _ in range(150):
                            if process.poll() is not None:
                                self.fail(
                                    "Companion exited before serving: "
                                    + log_file.read_text(errors="replace")[-2500:]
                                )
                            try:
                                async with core_client.get(
                                    core_url + "/healthz", ssl=context
                                ) as response:
                                    if response.status == 200:
                                        break
                            except (aiohttp.ClientError, OSError):
                                await asyncio.sleep(0.05)
                        else:
                            self.fail("Companion did not become ready")
                    before = read_facts(database)
                    for _ in range(20):
                        await asyncio.sleep(0.2)
                        settled = read_facts(database)
                        if settled == before:
                            break
                        before = settled
                    else:
                        self.fail("Companion fixture did not settle before the read-only pass")
                    with patch.dict(os.environ, {**ENV, TOKEN_ENV: TOKEN}):
                        settings = web_settings(
                            directory,
                            static=Path(__file__).resolve().parents[2] / "apps/web/dist"
                            if os.environ.get("TS_LIFE_BROWSER") == "1"
                            else None,
                        )
                        settings["principals"]["admin"]["actions"].append("life.read")
                        settings["web_life"] = {
                            "enabled": True,
                            "base_url": core_url,
                            "token_env": TOKEN_ENV,
                            "ca_file": str(certificate),
                        }
                        platform = Platform(settings)
                        runner, url = await start_http(create_app(platform))
                        settings["web"]["origin"] = url
                        try:
                            if os.environ.get("TS_LIFE_BROWSER") == "1":
                                await self.real_browser_checks(url)
                            await self.browser_checks(url, platform, facts)
                        finally:
                            await runner.cleanup()
                    self.assertEqual(
                        before, read_facts(database), "read path changed Companion rows"
                    )
                finally:
                    process.terminate()
                    try:
                        await asyncio.to_thread(process.wait, 5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        await asyncio.to_thread(process.wait, 5)

    async def real_browser_checks(self, url):
        node = shutil.which("node")
        self.assertIsNotNone(node, "TS_LIFE_BROWSER=1 needs the installed Node runtime")
        project = Path(__file__).resolve().parents[2]
        process = await asyncio.create_subprocess_exec(
            node,
            "node_modules/@playwright/test/cli.js",
            "test",
            "--config",
            "apps/web/playwright.life-joint.config.ts",
            cwd=project,
            env={**os.environ, "TS_LIFE_JOINT_URL": url},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 90)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        self.assertEqual(process.returncode, 0, output.decode(errors="replace"))
        print("Life joint browser: desktop and mobile passed")

    async def browser_checks(self, url, platform, facts):
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
            async with client.get(url + "/api/web/session") as response:
                csrf = (await response.json())["csrf"]

            async def call(path, body, expected=200):
                nonlocal csrf
                async with client.post(
                    url + "/api/web/" + path,
                    json=body,
                    headers={"Origin": url, "X-CSRF-Token": csrf},
                ) as response:
                    answer = await response.json()
                    self.assertEqual(response.status, expected, answer)
                    if path == "login":
                        csrf = answer["csrf"]
                    return answer

            await call("login", {"username": "synthetic-admin", "password": PASSWORD})
            actors = await call("life/actors", {"limit": 20, "after_actor_id": None})
            self.assertEqual([item["actor_id"] for item in actors["items"]], ["actor:a"])
            state = await call("life/snapshot", {"actor_id": "actor:a"})
            self.assertEqual(state["activity"], "reading")
            self.assertEqual(state["state_basis"], "last_persisted")
            today = await call("life/today", {"actor_id": "actor:a"})
            self.assertEqual(today["plan"]["generation_state"], "unavailable")
            self.assertEqual(today["plan"]["generated_by"], "baseline")
            self.assertEqual(len(today["plan"]["entries"]), 2)
            self.assertEqual(
                [
                    entry["phase_id"]
                    for entry in today["plan"]["entries"]
                    if entry["state"] == "current"
                ],
                [today["plan"]["current_phase_id"]],
            )
            timeline = await call(
                "life/timeline",
                {
                    "actor_id": "actor:a",
                    "day": today["day"],
                    "limit": 20,
                    "after": None,
                },
            )
            self.assertEqual(len(timeline["items"]), 20)
            self.assertIsNotNone(timeline["next_after"])
            more = await call(
                "life/timeline",
                {
                    "actor_id": "actor:a",
                    "day": today["day"],
                    "limit": 20,
                    "after": timeline["next_after"],
                },
            )
            self.assertEqual(len(more["items"]), 3)
            self.assertIsNone(more["next_after"])
            items = timeline["items"] + more["items"]
            self.assertEqual(len({item["known_id"] for item in items}), 23)
            self.assertEqual(len({item["position"] for item in items}), 1)
            await call("life/today", {"actor_id": "actor:b"}, expected=404)
            await call(
                "life/timeline",
                {
                    "actor_id": "actor:b",
                    "day": today["day"],
                    "limit": 20,
                    "after": None,
                },
                expected=404,
            )
            diaries = await call(
                "life/diaries", {"actor_id": "actor:a", "limit": 20, "after": None}
            )
            row = next(item for item in diaries["items"] if item["diary_id"] == facts["diary_id"])
            self.assertEqual(row["published_revision_id"], facts["revision_id"])
            revision = await call(
                "life/revision",
                {
                    "actor_id": "actor:a",
                    "diary_id": facts["diary_id"],
                    "revision_id": facts["revision_id"],
                    "expected_diary_version": facts["version"],
                },
            )
            self.assertIn("fictional quiet day", revision["content"])
            await call(
                "life/revision",
                {
                    "actor_id": "actor:a",
                    "diary_id": facts["diary_id"],
                    "revision_id": facts["revision_id"],
                    "expected_diary_version": facts["version"] + 1,
                },
                expected=409,
            )
            await call("life/snapshot", {"actor_id": "actor:b"}, expected=404)
            platform.auth.principals["admin"]["actions"].remove("life.read")
            denied = await call("life/actors", {"limit": 20, "after_actor_id": None}, expected=403)
            self.assertEqual(denied["code"], "life_read_required")
            await call("life/today", {"actor_id": "actor:a"}, expected=403)


if __name__ == "__main__":
    unittest.main()

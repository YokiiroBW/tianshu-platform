"""Optional real Platform issuer -> real Memory process -> synthetic source owners joint test.

Set TS_CONNECT_M_PATH to a fixed Memory worktree commit. No files are written there.
"""

import asyncio
import json
import os
import sqlite3
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import aiohttp
from aiohttp import web

from fixtures import ENV, start_http
from services.platform.contracts import canonical
from services.platform.origins import channel_key
from services.platform.provider_catalog import ProviderCatalog
from services.platform.server import create_app
from services.platform.service import Platform
from services.platform.web_console import WebConsole
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
        from tianshu_memory.role_grants import RoleGrants

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        certificate = root / "localhost.pem"
        key = root / "localhost-key.pem"
        subprocess.run(
            [
                os.environ["TS013_TLS_PYTHON"],
                str(Path(__file__).with_name("make_tls_fixture.py")),
                str(root),
            ],
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
            (contract_dir.parents[1] / "source-sync/v1/examples/documents.json").read_text(
                encoding="utf-8"
            )
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
            "allow_runtime_roles": True,
        }
        h.config["browser_readers"] = {"platform": {}}
        h.config["role_grants_database_path"] = str(root / "role-grants.sqlite")
        env = mock.patch.dict(
            os.environ,
            {**ENV, "TEST_MEMORY_BROWSER": "synthetic-platform-browser-only-credential"},
        )
        env.start()
        self.addCleanup(env.stop)
        monkeypatch = __import__("pytest").MonkeyPatch()
        self.addCleanup(monkeypatch.undo)
        with h.owners():
            h.config["callers"]["platform"]["issuer_url"] = h.config["callers"]["companion"][
                "issuer_url"
            ]
            h.save()
            with h.runtime(monkeypatch):
                seeded, _, _ = h.seed()
                second, _, _ = h.seed(1)
                with h.store.transaction() as db:
                    group = db.execute(
                        "SELECT * FROM groups WHERE id=?", (seeded["group_ids"][0],)
                    ).fetchone()
                    record = db.execute(
                        "SELECT * FROM records WHERE id=?", (seeded["record_ids"][0],)
                    ).fetchone()
                    lineage = db.execute(
                        "SELECT * FROM lineage WHERE group_id=?", (seeded["group_ids"][0],)
                    ).fetchone()
                    group_id, record_id = "zz-joint-private-group", "zz-joint-private-record"
                    db.execute(
                        "INSERT INTO groups VALUES (?,?,?,?,?,?,?)",
                        (
                            group_id,
                            group["scope"],
                            "active",
                            canonical([record_id]),
                            group["category"],
                            group["field_key"],
                            group["item_key"],
                        ),
                    )
                    unit = json.loads(record["payload"])
                    unit.update(
                        record_id=record_id, semantic_group_id=group_id, statement="另一条合成记忆"
                    )
                    db.execute(
                        "INSERT INTO records VALUES (?,?,?,?,?)",
                        (
                            record_id,
                            group_id,
                            record["version"],
                            "active",
                            canonical(unit),
                        ),
                    )
                    db.execute(
                        "INSERT INTO lineage VALUES (?,?,?,?)",
                        (
                            group_id,
                            lineage["source_key"],
                            lineage["revision"],
                            lineage["epoch"],
                        ),
                    )
                    for actor, source_group, source_record, kind, subject_id in (
                        (
                            "actor:a",
                            seeded["group_ids"][0],
                            seeded["record_ids"][0],
                            "person",
                            h.person,
                        ),
                        (
                            "actor:b",
                            second["group_ids"][0],
                            second["record_ids"][0],
                            "person",
                            h.person,
                        ),
                    ):
                        suffix = "a" if actor == "actor:a" else "b"
                        profile_group, profile_record = (
                            f"y-profile-{suffix}",
                            f"y-profile-record-{suffix}",
                        )
                        subject = {
                            "kind": kind,
                            "person_id" if kind == "person" else "conversation_id": subject_id,
                        }
                        profile_scope = {
                            "actor_id": actor,
                            "profile_audience": "public_preference",
                            "conversation_id": None,
                            "profile_subject": subject,
                        }
                        original_group = db.execute(
                            "SELECT * FROM groups WHERE id=?", (source_group,)
                        ).fetchone()
                        original_record = db.execute(
                            "SELECT * FROM records WHERE id=?", (source_record,)
                        ).fetchone()
                        original_lineage = db.execute(
                            "SELECT * FROM lineage WHERE group_id=?", (source_group,)
                        ).fetchone()
                        profile_unit = json.loads(original_record["payload"])
                        profile_unit.pop("subject_person_id")
                        profile_unit.update(
                            record_id=profile_record,
                            semantic_group_id=profile_group,
                            subject=subject,
                            category="interest",
                            field_key="interest.coffee",
                            sharing="public_preference",
                            visibility="shared_projection",
                            sources=[
                                {
                                    "kind": "shareable_projection",
                                    "owner": "memory",
                                    "projection_ref": f"joint-profile-{suffix}",
                                    "projection_version": 1,
                                }
                            ],
                        )
                        h.contracts.validate("profiles#unit", profile_unit)
                        db.execute(
                            "INSERT INTO groups VALUES (?,?,?,?,?,?,?)",
                            (
                                profile_group,
                                canonical(profile_scope),
                                "active",
                                canonical([profile_record]),
                                "interest",
                                "interest.coffee",
                                original_group["item_key"],
                            ),
                        )
                        db.execute(
                            "INSERT INTO records VALUES (?,?,?,?,?)",
                            (
                                profile_record,
                                profile_group,
                                original_record["version"],
                                "active",
                                canonical(profile_unit),
                            ),
                        )
                        db.execute(
                            "INSERT INTO lineage VALUES (?,?,?,?)",
                            (
                                profile_group,
                                original_lineage["source_key"],
                                original_lineage["revision"],
                                original_lineage["epoch"],
                            ),
                        )
                        db.execute(
                            "INSERT INTO projections VALUES (?, ?, 1, 'active')",
                            (f"joint-profile-{suffix}", profile_record),
                        )
                        db.execute(
                            "INSERT INTO profile_shares VALUES (?,?,?,?,?,?,?)",
                            (
                                profile_group,
                                actor,
                                kind,
                                subject_id,
                                "public_preference",
                                None,
                                f"joint-approval-{suffix}",
                            ),
                        )
                grants = RoleGrants(h.config["role_grants_database_path"])
                grants.apply(
                    {
                        "request_id": "browser-role-b",
                        "actor_id": "actor:b",
                        "expected_version": 0,
                        "enabled": True,
                        "legacy": False,
                    },
                    {"actor:a"},
                )
                browser_node = os.environ.get("TS_MEMORY_ROLE_BROWSER_NODE")
                static = (
                    Path(__file__).resolve().parents[2] / "apps/web/dist" if browser_node else None
                )
                if browser_node:
                    self.assertTrue(
                        (static / "index.html").is_file(),
                        "build the web app before live browser verification",
                    )
                config = web_settings(str(root), static=static)
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
                    "runtime_roles": True,
                }
                platform = Platform(config)
                # The authenticated web session inspects model readiness for every visible
                # actor, even though this test never calls a model.
                platform.provider_catalog = ProviderCatalog(root / "providers", create=True)
                platform.role_runtime.config = {"enabled": True}
                with closing(sqlite3.connect(platform.role_runtime.path)) as role_db:
                    role_db.execute(
                        "CREATE TABLE roles (actor_id TEXT PRIMARY KEY, body TEXT NOT NULL)"
                    )
                    role_db.execute(
                        "INSERT INTO roles VALUES (?,?)",
                        (
                            "actor:b",
                            canonical(
                                {
                                    "actor_id": "actor:b",
                                    "operator": "admin",
                                    "name": "小岚",
                                    "version": 1,
                                    "state": "active",
                                    "enabled": True,
                                    "capabilities": ["dialogue", "memory.read"],
                                    "provider_id": None,
                                    "provider_revision": None,
                                }
                            ),
                        ),
                    )
                    role_db.commit()
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
                console = WebConsole(platform)
                app = create_app(platform, console=console)
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
                    "allow_runtime_roles": True,
                }
                h.save()
                process_env = dict(
                    os.environ,
                    TIANSHU_MEMORY_CONFIG=str(h.config_path),
                    PYTHONPATH=str(memory_root / "src"),
                    PYTHONDONTWRITEBYTECODE="1",
                )
                memory_port = int(config["web_memory"]["base_url"].rsplit(":", 1)[1])

                def start_memory():
                    return subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "uvicorn",
                            "tianshu_memory.app:configured_app",
                            "--factory",
                            "--host",
                            "127.0.0.1",
                            "--port",
                            str(memory_port),
                            "--ssl-certfile",
                            str(certificate),
                            "--ssl-keyfile",
                            str(key),
                            "--no-access-log",
                        ],
                        env=process_env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )

                process = start_memory()
                try:
                    trust = ssl.create_default_context(cafile=certificate)
                    async with aiohttp.ClientSession(
                        cookie_jar=aiohttp.CookieJar(unsafe=True)
                    ) as client:
                        deadline = time.monotonic() + 10
                        while True:
                            if process.poll() is not None:
                                raise AssertionError(process.stdout.read().decode(errors="replace"))
                            try:
                                async with client.get(
                                    config["web_memory"]["base_url"] + "/health", ssl=trust
                                ) as response:
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

                        async def browser(name, body, expected=200):
                            async with client.post(
                                web_url + "/api/web/memory/" + name,
                                json=body,
                                headers={"Origin": web_url, "X-CSRF-Token": csrf},
                            ) as response:
                                answer = await response.json()
                                self.assertEqual(response.status, expected, answer)
                                return answer

                        state = await browser("state", {})
                        self.assertEqual(
                            [row["id"] for row in state["roles"]], ["actor:a", "actor:b"]
                        )
                        chosen_b = {"role_id": "actor:b", "role_version": 1}
                        overview = await browser("overview", {})
                        self.assertEqual(overview["memory_group_count"], 2)
                        self.assertEqual(
                            (await browser("overview", chosen_b))["memory_group_count"], 1
                        )
                        self.assertFalse(overview["counts_truncated"])
                        records = await browser(
                            "records", {"subject": None, "limit": 20, "cursor": None}
                        )
                        self.assertEqual(
                            records["items"][0]["semantic_group_id"], seeded["group_ids"][0]
                        )
                        b_records = await browser(
                            "records", {**chosen_b, "subject": None, "limit": 20, "cursor": None}
                        )
                        self.assertEqual(
                            b_records["items"][0]["semantic_group_id"], second["group_ids"][0]
                        )
                        self.assertNotEqual(records["items"], b_records["items"])
                        a_subjects = await browser("subjects", {"limit": 20, "cursor": None})
                        b_subjects = await browser(
                            "subjects", {**chosen_b, "limit": 20, "cursor": None}
                        )
                        self.assertEqual(
                            [item["subject"]["kind"] for item in a_subjects["items"]], ["person"]
                        )
                        self.assertEqual(
                            [item["subject"]["kind"] for item in b_subjects["items"]], ["person"]
                        )
                        a_profile = await browser(
                            "records",
                            {
                                "subject": a_subjects["items"][0]["subject"],
                                "limit": 20,
                                "cursor": None,
                            },
                        )
                        b_profile = await browser(
                            "records",
                            {
                                **chosen_b,
                                "subject": b_subjects["items"][0]["subject"],
                                "limit": 20,
                                "cursor": None,
                            },
                        )
                        self.assertEqual(a_profile["items"][0]["semantic_group_id"], "y-profile-a")
                        self.assertEqual(b_profile["items"][0]["semantic_group_id"], "y-profile-b")
                        first_page = await browser(
                            "records", {"subject": None, "limit": 1, "cursor": None}
                        )
                        self.assertIsNotNone(first_page["next_cursor"])
                        second_page = await browser(
                            "records",
                            {
                                "subject": None,
                                "limit": 1,
                                "cursor": first_page["next_cursor"],
                            },
                        )
                        self.assertEqual(len(second_page["items"]), 1)
                        self.assertNotEqual(
                            first_page["items"][0]["semantic_group_id"],
                            second_page["items"][0]["semantic_group_id"],
                        )
                        crossed = await browser(
                            "records",
                            {
                                **chosen_b,
                                "subject": None,
                                "limit": 1,
                                "cursor": first_page["next_cursor"],
                            },
                            400,
                        )
                        self.assertEqual(crossed["code"], "invalid_input")
                        if browser_node:
                            await asyncio.to_thread(console.authority)
                            await asyncio.to_thread(console.dialogue.model_configured)
                            for actor in ("actor:a", "actor:b"):
                                await asyncio.to_thread(console.dialogue.model_configured, actor)
                            screenshots = (
                                Path(__file__).resolve().parents[2] / ".runtime/memory-role-browser"
                            )
                            browser_env = dict(
                                os.environ, TS_MEMORY_ROLE_B_GROUP=second["group_ids"][0]
                            )
                            checked = await asyncio.to_thread(
                                subprocess.run,
                                [
                                    browser_node,
                                    str(Path(__file__).with_name("memory_role_joint_browser.mjs")),
                                    web_url,
                                    str(screenshots),
                                ],
                                env=browser_env,
                                capture_output=True,
                                text=True,
                                encoding="utf-8",
                                errors="replace",
                                timeout=90,
                            )
                            self.assertEqual(
                                checked.returncode,
                                0,
                                (checked.stdout or "") + (checked.stderr or ""),
                            )
                        process.terminate()
                        process.wait(timeout=5)
                        process.stdout.close()
                        process = start_memory()
                        deadline = time.monotonic() + 10
                        while True:
                            if process.poll() is not None:
                                raise AssertionError(process.stdout.read().decode(errors="replace"))
                            try:
                                async with client.get(
                                    config["web_memory"]["base_url"] + "/health", ssl=trust
                                ) as response:
                                    if response.status == 200:
                                        break
                            except aiohttp.ClientError:
                                pass
                            if time.monotonic() >= deadline:
                                raise AssertionError("Memory restart did not become ready")
                            await __import__("asyncio").sleep(0.05)
                        self.assertEqual(
                            (await browser("overview", chosen_b))["memory_group_count"], 1
                        )
                        self.assertEqual(
                            (
                                await browser(
                                    "overview", {"role_id": "actor:forged", "role_version": 0}, 403
                                )
                            )["code"],
                            "forbidden",
                        )
                        grants.apply(
                            {
                                "request_id": "browser-role-b-off",
                                "actor_id": "actor:b",
                                "expected_version": 1,
                                "enabled": False,
                                "legacy": False,
                            },
                            {"actor:a"},
                        )
                        self.assertEqual(
                            (await browser("overview", chosen_b, 403))["code"], "upstream_forbidden"
                        )
                        self.assertEqual((await browser("overview", {}))["memory_group_count"], 2)
                        grants.apply(
                            {
                                "request_id": "browser-role-a-adopt",
                                "actor_id": "actor:a",
                                "expected_version": 0,
                                "enabled": True,
                                "legacy": True,
                            },
                            {"actor:a"},
                        )
                        grants.apply(
                            {
                                "request_id": "browser-role-a-off",
                                "actor_id": "actor:a",
                                "expected_version": 1,
                                "enabled": False,
                                "legacy": True,
                            },
                            {"actor:a"},
                        )
                        self.assertEqual(
                            (await browser("overview", {}, 403))["code"], "upstream_forbidden"
                        )
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

"""Real login/origin/TLS, fixed group mapping and current owner projection boundaries."""

import asyncio
import copy
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import aiohttp
from aiohttp import web

from fixtures import ENV, start_http
from services.platform.contracts import canonical
from services.platform import group_policy
from services.platform.bot_adapter_catalog import BotAdapterCatalog
from services.platform.bot_observation import BotObservation
from services.platform.origins import channel_key
from services.platform.server import create_app
from services.platform.service import Platform
from test_actor_capabilities import seed_roles
from web_fixtures import PASSWORD, web_settings


class GroupMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tool = os.environ.get("TS013_TLS_PYTHON")
        if not tool:
            raise unittest.SkipTest("TLS fixture Python not configured")
        self.temp = tempfile.TemporaryDirectory(prefix="ts316-groups-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        subprocess.run(
            [tool, str(Path(__file__).with_name("make_tls_fixture.py")), str(self.root)],
            check=True,
            capture_output=True,
        )
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = patch.dict(
            os.environ,
            {
                **ENV,
                "TS316_MEMORY": "synthetic-memory-group-reader-0001",
                "TS316_CHAT": "synthetic-chat-group-reader-000001",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        settings = web_settings(str(self.root))
        settings["principals"]["admin"]["actions"].append("memory.read")
        settings["principals"]["memory_resolver"]["resolver"]["caller"] = "platform"
        template = settings["entries"]["actor-a"]
        template["routes"].append(
            {"caller": "platform", "receiver": "memory", "purpose": "dialogue"}
        )
        group = copy.deepcopy(template)
        group.update(
            audience="group",
            routes=[{"caller": "platform", "receiver": "memory", "purpose": "dialogue"}],
        )
        group["channel"]["channel_conversation_id"] = "management:group-42"
        chat_channel = {
            "namespace": "qq",
            "binding_id": "binding-a",
            "channel_conversation_id": "group:42",
            "thread_id": None,
        }
        settings["entries"]["group-a"] = group
        cert = str(self.root / "localhost.pem")
        peer = {
            "base_url": f"https://127.0.0.1:{port}",
            "token_env": "TS316_MEMORY",
            "ca_file": cert,
        }
        settings["web_memory"] = {
            **peer,
            "enabled": True,
            "entry_id": "actor-a",
            "runtime_roles": True,
        }
        settings["web_group_memory"] = {
            "enabled": True,
            "chat": {**peer, "token_env": "TS316_CHAT"},
            "groups": [
                {
                    "id": "group-a",
                    "label": "合成群",
                    "entry_id": "group-a",
                    "conversation_id": "conversation:group-a",
                    "chat_channel": chat_channel,
                    "archive_mapping_id": "onebot:fixture",
                }
            ],
        }
        self.p = Platform(settings)
        seed_roles(self.p)
        for actor in ("actor:a", "actor:b"):
            self.p.role_runtime._put(
                {
                    **self.p.role_runtime.get(actor),
                    "operator": "admin",
                    "capabilities": ["memory.read"],
                }
            )
        with self.p.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO identities VALUES(?,?,?)",
                (canonical(template["account"]), "person:operator", 1),
            )
            db.execute(
                "INSERT INTO channels VALUES(?,?)", (channel_key(template), "conversation:private")
            )
            db.execute(
                "INSERT INTO channels VALUES(?,?)",
                (canonical(chat_channel), "conversation:group-a"),
            )
        self.channel = chat_channel
        self.requests = []
        self.pause = False
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.changed = False
        self.stats_unavailable = False
        self.bad_scope = False
        self.forget_state = "no_known_forget"
        self.reverse_filter = False
        self.contract_root = self.p.contracts.root
        self.chat_examples = {
            c["id"]: c["document"]
            for c in json.loads(
                (self.contract_root / "chat-history/candidate-v1/examples.json").read_text(
                    encoding="utf-8"
                )
            )["positive"]
        }
        self.topic_response = next(
            c["document"]
            for c in json.loads(
                (
                    self.contract_root / "group-memory-projection/candidate-v1/examples.json"
                ).read_text(encoding="utf-8")
            )["positive"]
            if c["definition"] == "response"
        )
        app = web.Application()
        app.router.add_post("/internal/evidence/{name}", self.chat)
        app.router.add_post("/internal/v1/memory/browser/{name}", self.memory)
        self.peer = web.AppRunner(app, access_log=None)
        await self.peer.setup()
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, str(self.root / "localhost-key.pem"))
        await web.TCPSite(self.peer, "127.0.0.1", port, ssl_context=tls).start()
        self.runner, self.url = await start_http(create_app(self.p))
        self.p.settings["web"]["origin"] = self.url
        self.client = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.addAsyncCleanup(self.cleanup)
        async with self.client.get(self.url + "/api/web/session") as response:
            csrf = (await response.json())["csrf"]
        async with self.client.post(
            self.url + "/api/web/login",
            json={"username": "synthetic-admin", "password": PASSWORD},
            headers={"Origin": self.url, "X-CSRF-Token": csrf},
        ) as response:
            answer = await response.json()
            self.assertEqual(response.status, 200, answer)
            self.csrf = answer["csrf"]

    async def cleanup(self):
        self.release.set()
        await self.client.close()
        await self.runner.cleanup()
        await self.peer.cleanup()
        self.p.close()
        await asyncio.to_thread(self.p.local_work.executor.shutdown, wait=True)

    async def memory(self, request):
        self.assertEqual(
            request.headers["Authorization"], "Bearer synthetic-memory-group-reader-0001"
        )
        body = await request.json()
        context = self.p.origins.resolve(
            "Bearer " + ENV["TS012_MEMORY_RESOLVER"],
            {
                "schema_version": 1,
                "request_id": body["request_id"],
                "assertion_ref": body["origin"]["assertion_ref"],
            },
        )["context"]
        self.assertEqual(context["allowed_scope"], body["scope"])
        self.assertEqual(context["verified_account"]["namespace"], "web")
        self.assertEqual(context["verified_channel"]["namespace"], "web")
        self.assertEqual(body["scope"]["conversation_id"], "conversation:group-a")
        self.requests.append((request.path, body))
        if self.pause:
            self.entered.set()
            await self.release.wait()
        if request.match_info["name"] == "archive-check":
            answer = {
                key: body[key] for key in ("schema_version", "request_id", "scope", "mapping_id")
            }
            answer.update(
                checked_at="2026-10-26T00:00:00Z",
                items=[
                    {
                        "evidence_id": item["evidence_id"],
                        "content_hash": item["content_hash"],
                        "forget_state": self.forget_state,
                        "mapping": "unmapped",
                        "checked_at": "2026-10-26T00:00:00Z",
                    }
                    for item in body["items"]
                ],
            )
            if self.reverse_filter:
                answer["items"][0]["evidence_id"] = (
                    "audit-evidence:0:" + "d" * 64 + ":foreign:" + "e" * 64
                )
        elif request.match_info["name"] == "topics":
            answer = copy.deepcopy(self.topic_response)
            answer.update(
                request_id=body["request_id"], scope=body["scope"], subject=body["subject"]
            )
            if self.bad_scope:
                answer["scope"] = {**body["scope"], "conversation_id": "conversation:other"}
        else:
            answer = {
                "schema_version": 1,
                "request_id": body["request_id"],
                "scope": body["scope"],
                "scope_version": 1,
                "verified_at": "2026-10-26T00:00:00Z",
                "items": [],
                "next_cursor": None,
            }
        return web.json_response(answer)

    async def chat(self, request):
        self.assertEqual(
            request.headers["Authorization"], "Bearer synthetic-chat-group-reader-000001"
        )
        body = await request.json()
        self.assertEqual(body["channel"], self.channel)
        self.requests.append((request.path, body))
        operation = request.match_info["name"]
        if operation == "group-stats" and self.stats_unavailable:
            return web.json_response({"detail": "synthetic failure"}, status=503)
        key = {
            "search": "search_response",
            "check": "check_response",
            "read-locator": "read_locator_current",
            "group-stats": "group_stats_response",
        }[operation]
        answer = copy.deepcopy(self.chat_examples[key])
        answer["channel"] = self.channel
        if operation == "group-stats":
            answer["window"] = {
                key: body[key]
                for key in (
                    "start_timestamp",
                    "end_timestamp",
                    "bucket_seconds",
                    "timezone_offset_minutes",
                )
            }
        if operation == "check" and self.changed:
            answer["results"][0].update(status="changed", current_stored_content_sha256=None)
        return web.json_response(answer)

    async def call(self, name, fields=None, status=200):
        body = {
            "role_id": "actor:a",
            "role_version": 7,
            **({"group_id": "group-a"} if name != "groups" else {}),
            **(fields or {}),
        }
        async with self.client.post(
            self.url + "/api/web/memory/" + name,
            json=body,
            headers={"Origin": self.url, "X-CSRF-Token": self.csrf},
        ) as response:
            answer = await response.json()
            self.assertEqual(response.status, status, answer)
            return answer

    async def test_three_owner_parts_use_current_registered_group_not_private_console_scope(self):
        directory = await self.call("groups")
        self.assertEqual([g["id"] for g in directory["groups"]], ["group-a"])
        answer = await self.call("group-insights", {"window": "7d"})
        self.assertEqual(
            [answer[k]["state"] for k in ("stats", "topics", "records")], ["available"] * 3
        )
        self.assertNotIn("scope", answer["topics"]["data"])
        self.assertNotIn("origin", canonical(answer))
        self.assertEqual(
            {r[1]["scope"]["audience"] for r in self.requests if "scope" in r[1]}, {"group"}
        )

    async def test_cannot_choose_another_actor_channel_account_or_arbitrary_group(self):
        await self.call("group-insights", {"window": "7d", "channel": self.channel}, 400)
        await self.call("group-insights", {"window": "7d", "role_id": "actor:b"}, 403)
        await self.call("group-insights", {"window": "7d", "group_id": "unregistered"}, 404)
        await self.call("group-insights", {"window": "7d", "role_version": 8}, 409)
        duplicate = copy.deepcopy(self.p.auth.entries["group-a"])
        self.p.auth.entries["unregistered-copy"] = duplicate
        with self.p.store.connect() as db:
            self.assertIsNone(
                self.p.origins.scope(db, duplicate, entry_id="unregistered-copy")["conversation_id"]
            )

    async def test_actual_qq_mapping_removed_or_reassigned_is_not_replaced_by_config_target(self):
        self.pause = True
        task = asyncio.create_task(self.call("group-insights", {"window": "24h"}, 409))
        await self.entered.wait()
        with self.p.store.connect(write=True) as db:
            db.execute(
                "UPDATE channels SET conversation_id=? WHERE channel=?",
                ("conversation:another-account", canonical(self.channel)),
            )
        self.release.set()
        await task
        self.assertEqual((await self.call("groups"))["groups"], [])

    async def test_permission_revocation_during_remote_wait_suppresses_completed_projection(self):
        self.pause = True
        task = asyncio.create_task(self.call("group-insights", {"window": "24h"}, 403))
        await self.entered.wait()
        self.p.origins.revoke("Bearer " + ENV["TS012_ADMIN"], "entry", "group-a")
        self.release.set()
        await task
        self.assertEqual((await self.call("groups"))["groups"], [])

    async def test_raw_archive_changed_after_search_is_never_returned_as_current(self):
        filters = {
            "keyword": None,
            "sender_id": None,
            "start_timestamp": None,
            "end_timestamp": None,
        }
        found = await self.call("group-history", {"filters": filters, "cursor": None, "limit": 20})
        self.assertEqual(found["records"][0]["sender_id"], "sender-a")
        self.changed = True
        await self.call("group-history", {"filters": filters, "cursor": None, "limit": 20}, 409)

    async def test_known_forgotten_or_unsupported_archive_does_not_reappear_and_bad_proof_is_not_empty(
        self,
    ):
        query = {
            "filters": {
                "keyword": None,
                "sender_id": None,
                "start_timestamp": None,
                "end_timestamp": None,
            },
            "cursor": None,
            "limit": 20,
        }
        self.forget_state = "withdrawn"
        answer = await self.call("group-history", query)
        self.assertEqual((answer["records"], answer["visibility"]["withdrawn"]), ([], 1))
        self.forget_state = "unsupported"
        answer = await self.call("group-history", query)
        self.assertEqual((answer["records"], answer["visibility"]["unsupported"]), ([], 1))
        self.forget_state, self.reverse_filter = "no_known_forget", True
        await self.call("group-history", query, 502)

    async def test_component_unavailable_is_not_empty_and_foreign_topic_scope_is_not_shown(self):
        self.stats_unavailable, self.bad_scope = True, True
        answer = await self.call("group-insights", {"window": "7d"})
        self.assertEqual(answer["stats"]["state"], "unavailable")
        self.assertEqual(answer["topics"]["state"], "unavailable")
        self.assertEqual(answer["records"]["state"], "available")

    async def test_observed_group_uses_core_frame_and_real_web_origin_without_static_chat_configuration(
        self,
    ):
        self.p.bot_adapters.catalog = BotAdapterCatalog(self.root / "observed-catalog")
        self.p.bot_observation = BotObservation(self.p)
        owner = self.p.bot_observation
        row = dict(
            kind="observation",
            id="obs:directory",
            name="实际观察账号",
            instance_id="sdk:actual",
            account_id="42000",
            group_policy=dict(
                version=2,
                default=group_policy.rule("observe", "actor:a"),
                groups={"42": group_policy.rule("observe", "actor:a")},
            ),
            private_policy=dict(observe=False, mode="observe_only", list=[], actor_id=None),
            read_enabled=True,
            enabled=True,
            pending=None,
            state="ready",
            host_revision=1,
            revision=1,
            archive_epoch=1,
        )
        owner.catalog.put(row)
        self.p.role_runtime.console.memory.groups.config = None

        async def core(operation, request):
            self.assertEqual(operation, "learning-frame")
            self.assertEqual(request["channel"], owner.segment_channel(row, "group:42", "actor:a"))
            self.assertIsNone(request["delivery_channel"])
            return dict(
                schema_version=1,
                valid=True,
                frame=dict(
                    actor_id="actor:a", audience="group", conversation_id="conversation:group-a"
                ),
                channel=request["channel"],
                delivery_channel=None,
            )

        with patch.object(self.p.bot_adapters, "_core", side_effect=core):
            directory = await self.call("groups")
        self.assertEqual(len(directory["groups"]), 1)
        group = directory["groups"][0]
        self.assertEqual(
            (group["mode"], group["conversation_id"]), ("observe", "conversation:group-a")
        )
        answer = await self.call("group-insights", dict(group_id=group["id"], window="7d"))
        self.assertEqual(answer["stats"]["state"], "unavailable")
        self.assertEqual(answer["stats"]["code"], "chat_not_configured")
        self.assertEqual(
            (answer["topics"]["state"], answer["records"]["state"]), ("available", "available")
        )
        # Disabling new work keeps historical read authority; an actor change
        # does not let the old delegated origin keep reading the new role.
        row["group_policy"]["groups"]["42"]["mode"] = "disabled"
        owner.catalog.put(row)
        await self.call("group-insights", dict(group_id=group["id"], window="7d"))
        row["group_policy"]["groups"]["42"]["actor_id"] = "actor:b"
        owner.catalog.put(row)
        await self.call("group-insights", dict(group_id=group["id"], window="7d"), 409)

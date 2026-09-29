"""Synthetic bot lifecycle, authority and durable delivery tests; no SDK or real sends."""

import asyncio
import copy
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from unittest.mock import AsyncMock, patch

import aiohttp

from fixtures import ENV, bearer, start_http
from services.platform.bots import Bots
from services.platform.contracts import Fault, canonical, utc
from services.platform.server import create_app
from services.platform.runtime_health import health_inputs
from services.platform.service import Platform, validate_settings
from web_fixtures import PASSWORD, web_settings


def bot_settings(directory):
    config = web_settings(directory)
    config["principals"]["admin"]["actions"].append("bot.manage")
    config["principals"]["admin"]["actions"] += ["qq.admin.view", "qq.admin.manage"]
    config["principals"]["companion"]["actions"] += ["dialogue.send", "qq.admin.check"]
    channel = {
        "namespace": "qq",
        "binding_id": "binding:bot-qq",
        "channel_conversation_id": "group:123",
        "thread_id": None,
    }
    entries = []
    for number in ("1", "2"):
        account = {"namespace": "qq", "immutable_account_id": "100" + number}
        input_id = "bot-input-" + number
        actor_id = "bot-actor-" + number
        config["input_entries"][input_id] = {
            "owner": "connector",
            "account": account,
            "channel": channel,
            "audience": "group",
            "ttl_seconds": 60,
            "actor_entries": [actor_id],
            "default_actor_ids": ["actor:a"],
            "routing_version": 1,
        }
        config["entries"][actor_id] = {
            "kind": "trusted_application",
            "owner": "connector",
            "account": account,
            "channel": channel,
            "actor_id": "actor:a",
            "audience": "group",
            "ttl_seconds": 60,
            "routes": [
                {"caller": "platform", "receiver": "companion", "purpose": "dialogue"},
                {"caller": "companion", "receiver": "memory", "purpose": "dialogue"},
                {"caller": "companion", "receiver": "platform", "purpose": "dialogue"},
            ],
        }
        entries.append(input_id)
    config["bot_connections"] = {
        "principal": "connector",
        "slots": {
            "qq-onebot-main": {
                "adapter": "nonebot",
                "platform_id": "onebot11-main",
                "self_id": "4242",
                "input_entry_ids": entries,
                "label": "测试群 · NoneBot",
            },
        },
    }
    return config


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)
        self.now = time.time()
        self.settings = bot_settings(self.temp.name)
        self.platform = Platform(self.settings, clock=lambda: self.now)
        self.addAsyncCleanup(self._close)
        created = self.platform.bots.create("qq-onebot-main", ["actor:a"])
        self.connection_id = created["connection_id"]
        self.token = "Bearer " + created["token"]
        self.platform.bots.change(self.connection_id, "enable")

    async def _close(self):
        self.platform.close()

    def event(self, account="1001"):
        return {
            "schema_version": 1,
            "connection_id": self.connection_id,
            "platform_id": "onebot11-main",
            "self_id": "4242",
            "event_id": "sdk:101",
            "revision": 1,
            "namespace": "qq",
            "conversation_id": "group:123",
            "thread_id": None,
            "account_id": account,
            "sent_at": utc(self.now),
            "text": "合成文本",
        }

    def send_request(self):
        with self.platform.store.connect(write=True) as db:
            db.execute(
                "INSERT OR REPLACE INTO identities VALUES(?,?,?)",
                (
                    canonical(self.settings["input_entries"]["bot-input-1"]["account"]),
                    "person:test",
                    1,
                ),
            )
            db.execute(
                "INSERT OR REPLACE INTO channels VALUES(?,?)",
                (
                    canonical(self.settings["input_entries"]["bot-input-1"]["channel"]),
                    "conversation:core",
                ),
            )
        origin = self.platform.origins.issue(bearer("CONNECTOR"), "bot-actor-1")
        return {
            "command": {
                "schema_version": 1,
                "request_id": "request:bot-reply",
                "idempotency_key": "command:bot-reply",
                "origin": {"assertion_ref": origin["assertion_ref"]},
                "deadline_at": utc(self.now + 30),
            },
            "conversation_id": "conversation:core",
            "turn_id": "turn:bot",
            "turn_sequence": 1,
            "reply_id": "reply:bot",
            "actor_id": "actor:a",
            "destination": self.settings["input_entries"]["bot-input-1"]["channel"],
            "segment_sequence": 1,
            "segment_count": 1,
            "text": "合成模型结果",
        }

    async def test_qq_administrator_web_grant_check_revoke_and_cas(self):
        runner, url = await start_http(create_app(self.platform))
        self.addAsyncCleanup(runner.cleanup)
        self.settings["web"]["origin"] = url
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
            async with client.get(url + "/api/web/session") as response:
                anonymous = await response.json()
            headers = {"Origin": url, "X-CSRF-Token": anonymous["csrf"]}
            async with client.post(
                url + "/api/web/qq-admin/view", json={}, headers=headers
            ) as response:
                self.assertEqual(response.status, 401)
            async with client.post(
                url + "/api/web/login",
                json={"username": "synthetic-admin", "password": PASSWORD},
                headers=headers,
            ) as response:
                self.assertEqual(response.status, 200)
                csrf = (await response.json())["csrf"]
            headers["X-CSRF-Token"] = csrf

            async def web_post(operation, body):
                async with client.post(
                    url + "/api/web/qq-admin/" + operation, json=body, headers=headers
                ) as response:
                    return response.status, await response.json()

            status, initial = await web_post("view", {})
            self.assertEqual(status, 200)
            self.assertEqual(initial["version"], 0)
            self.assertEqual(initial["grants"], [])
            request = {
                "qq_id": "1001",
                "expected_version": 0,
                "note": "synthetic operator",
                "actor_ids": ["actor:a"],
                "conversations": ["group:123"],
                "capabilities": ["identity.explain"],
            }
            status, granted = await web_post("grant", request)
            self.assertEqual(status, 200, granted)
            self.assertEqual(granted["version"], 1)
            self.assertEqual((await web_post("grant", request))[0], 409)
            self.assertEqual(
                (await web_post("grant", {**request, "qq_id": True, "expected_version": 1}))[0], 400
            )
            self.assertEqual(
                (await web_post("grant", {**request, "expected_version": 1, "capabilities": [[]]}))[
                    0
                ],
                400,
            )
            self.assertEqual(
                (await web_post("grant", {**request, "expected_version": 2**63}))[0], 400
            )
            with self.platform.store.connect(write=True) as db:
                db.execute(
                    "INSERT OR REPLACE INTO channels VALUES(?,?)",
                    (
                        canonical(self.settings["input_entries"]["bot-input-1"]["channel"]),
                        "conversation:core",
                    ),
                )
            issued = self.platform.origins.issue(bearer("CONNECTOR"), "bot-actor-1")
            check = {
                "schema_version": 1,
                "request_id": "qq-check-1",
                "assertion_ref": issued["assertion_ref"],
                "actor_id": "actor:a",
                "conversation_id": "conversation:core",
            }
            with self.platform.store.connect() as db:
                _, _, context = self.platform.origins.context(
                    db, issued["assertion_ref"], "companion", "platform", "dialogue"
                )
            self.assertEqual(context["allowed_scope"]["conversation_id"], "conversation:core")
            self.assertEqual(context["allowed_scope"]["actor_id"], "actor:a")

            async def internal(body, token="COMPANION"):
                async with aiohttp.ClientSession() as service_client:
                    async with service_client.post(
                        url + "/internal/v1/qq-admin/check",
                        json=body,
                        headers={"Authorization": bearer(token)},
                    ) as response:
                        return response.status, await response.json()

            self.assertEqual((await internal(check, "CONNECTOR"))[0], 403)
            self.assertEqual((await internal({**check, "conversation_id": "other"}))[0], 403)
            status, result = await internal(check)
            self.assertEqual(status, 200, result)
            self.assertTrue(result["is_admin"])
            self.assertEqual(result["version"], 1)
            status, revoked = await web_post("revoke", {"qq_id": "1001", "expected_version": 1})
            self.assertEqual(status, 200, revoked)
            self.assertEqual(revoked["version"], 2)
            self.assertFalse((await internal(check))[1]["is_admin"])
            self.assertEqual(type(self.platform.qq_admin)(self.platform)._view()["version"], 2)

    async def test_qq_display_alias_queued_only_after_accepted_event(self):
        self.platform.settings["qq_alias_memory"] = {
            "base_url": "https://memory.synthetic.invalid",
            "token_env": "TS_QQ_ALIAS_TEST",
        }
        self.platform.sources.dispatch = AsyncMock(
            return_value={"outcomes": [{"actor_id": "actor:a", "state": "accepted"}]}
        )
        body = {**self.event(), "schema_version": 2, "nickname": "同名", "group_card": "群名片"}
        accepted = await self.platform.bots.event(self.token, body)
        self.assertEqual(accepted["state"], "accepted")
        queued = self.platform.qq_admin.pending_aliases()
        self.assertEqual(len(queued), 1)
        self.assertNotIn("text", queued[0])
        with patch(
            "services.platform.qq_admin.observe_alias", new_callable=AsyncMock, return_value=True
        ) as writer:
            await self.platform.qq_admin.flush_aliases()
            writer.assert_awaited_once_with(self.platform, queued[0])
        self.assertEqual(self.platform.qq_admin.pending_aliases(), [])
        with self.assertRaises(Fault):
            await self.platform.bots.event(
                self.token, {**body, "event_id": "sdk:102", "account_id": "０１００１"}
            )
        self.assertEqual(self.platform.qq_admin.pending_aliases(), [])

    async def test_qq_alias_queue_rotates_past_failed_first_page(self):
        self.platform.settings["qq_alias_memory"] = {
            "base_url": "https://memory.synthetic.invalid",
            "token_env": "TS_QQ_ALIAS_TEST",
        }
        base = {**self.event(), "schema_version": 2, "nickname": "称呼", "group_card": None}
        for index in range(40):
            self.platform.qq_admin.queue_alias({**base, "event_id": f"sdk:{index + 1000}"})
        first = self.platform.qq_admin.pending_aliases()
        second = self.platform.qq_admin.pending_aliases()
        self.assertEqual(len(first), 32)
        self.assertEqual(len(second), 8)
        self.assertEqual(
            {item["event_id"] for item in first + second},
            {f"sdk:{index + 1000}" for index in range(40)},
        )

    async def test_event_dedup_author_binding_and_unknown_no_replay(self):
        bot = self.platform.bots
        self.platform.sources.dispatch = AsyncMock(
            return_value={"outcomes": [{"actor_id": "actor:a", "state": "accepted"}]}
        )
        first = await bot.event(self.token, self.event())
        self.assertEqual(first["state"], "accepted")
        self.assertEqual(
            (await bot.event(self.token, self.event()))["message_id"], first["message_id"]
        )
        self.assertEqual(self.platform.sources.dispatch.await_count, 1)
        second = await bot.event(self.token, {**self.event("1002"), "event_id": "sdk:102"})
        self.assertEqual(second["state"], "accepted")
        self.assertEqual(self.platform.sources.dispatch.await_count, 2)
        self.assertEqual(
            bot.event_status(
                self.token,
                {
                    "connection_id": self.connection_id,
                    "event_id": "sdk:101",
                    "account_id": "1001",
                },
            )["state"],
            "accepted",
        )
        with self.assertRaises(Fault):
            await bot.event(self.token, {**self.event(), "text": "changed"})
        with self.assertRaises(Fault):
            await bot.event(self.token, self.event("unknown-author"))
        with self.assertRaises(Fault):
            await bot.event(self.token, {**self.event(), "self_id": "other-bot"})
        self.platform.sources.dispatch = AsyncMock(side_effect=OSError())
        unknown = await bot.event(self.token, {**self.event(), "event_id": "sdk:unknown"})
        self.assertEqual(unknown["state"], "unknown")
        self.assertEqual(
            bot.event_status(
                self.token,
                {
                    "connection_id": self.connection_id,
                    "event_id": "sdk:unknown",
                    "account_id": "1001",
                },
            )["state"],
            "unknown",
        )
        await bot.event(self.token, {**self.event(), "event_id": "sdk:unknown"})
        self.assertEqual(self.platform.sources.dispatch.await_count, 1)

    async def test_reply_claim_ack_reconcile_restart_and_conflicts(self):
        bot = self.platform.bots
        request = self.send_request()
        first = bot.send(bearer("COMPANION"), request)
        self.assertEqual(first["state"], "unknown")
        self.assertIsNone(bot.reply_status(bearer("COMPANION"), request)["receipt"])
        delivery = bot.claim(
            self.token,
            {"connection_id": self.connection_id, "instance_id": "instance:1", "limit": 5},
        )["deliveries"][0]
        self.assertEqual(delivery["conversation_id"], "group:123")
        self.assertEqual(
            bot.claim(
                self.token,
                {"connection_id": self.connection_id, "instance_id": "instance:2", "limit": 5},
            )["deliveries"],
            [],
        )
        self.assertEqual(
            bot.claim_status(
                self.token,
                {
                    "connection_id": self.connection_id,
                    "reply_id": delivery["reply_id"],
                    "attempt_id": delivery["attempt_id"],
                },
            )["state"],
            "claimed",
        )
        ack = {
            "connection_id": self.connection_id,
            "reply_id": delivery["reply_id"],
            "attempt_id": delivery["attempt_id"],
            "state": "sent",
            "channel_message_ids": ["sdk:message:9"],
        }
        self.assertEqual(bot.ack(self.token, ack), {"reply_id": "reply:bot", "state": "sent"})
        self.assertEqual(bot.ack(self.token, ack), {"reply_id": "reply:bot", "state": "sent"})
        self.assertEqual(bot.view()["connections"][0]["delivery"]["sent"], 1)
        self.assertEqual(bot.send(bearer("COMPANION"), request)["state"], "sent")
        restarted = Bots(Platform(self.settings, clock=lambda: self.now))
        self.assertEqual(
            restarted.reply_status(bearer("COMPANION"), request)["receipt"]["channel_message_ids"],
            ["sdk:message:9"],
        )
        with self.assertRaises(Fault):
            bot.ack(self.token, {**ack, "channel_message_ids": ["sdk:other"]})
        with self.assertRaises(Fault):
            bot.send(bearer("COMPANION"), {**request, "text": "different"})
        with self.assertRaises(Fault):
            bot.reply_status(bearer("ADMIN"), request)
        with self.assertRaises(Fault):
            bot.reply_status(bearer("COMPANION"), {**request, "text": "different"})

    async def test_sdk_unknown_is_final_and_revoked_entry_fails_closed(self):
        bot = self.platform.bots
        request = self.send_request()
        bot.send(bearer("COMPANION"), request)
        delivery = bot.claim(
            self.token,
            {"connection_id": self.connection_id, "instance_id": "instance:1", "limit": 1},
        )["deliveries"][0]
        ack = {
            "connection_id": self.connection_id,
            "reply_id": delivery["reply_id"],
            "attempt_id": delivery["attempt_id"],
            "state": "unknown",
            "channel_message_ids": [],
        }
        bot.ack(self.token, ack)
        self.assertEqual(bot.ack(self.token, ack)["state"], "unknown")
        with self.assertRaises(Fault):
            bot.ack(self.token, {**ack, "state": "sent", "channel_message_ids": ["sdk:late"]})
        with self.platform.store.connect(write=True) as db:
            db.execute("INSERT INTO revoked_entries VALUES(?)", ("bot-input-1",))
        self.assertEqual(bot.view()["connections"][0]["state"], "failed")
        with self.assertRaises(Fault):
            await bot.event(self.token, self.event())

    async def test_claim_expiry_unknown_and_late_real_ack(self):
        bot = self.platform.bots
        request = self.send_request()
        bot.send(bearer("COMPANION"), request)
        delivery = bot.claim(
            self.token,
            {"connection_id": self.connection_id, "instance_id": "instance:1", "limit": 1},
        )["deliveries"][0]
        self.now += 61
        lookup = {
            "connection_id": self.connection_id,
            "reply_id": delivery["reply_id"],
            "attempt_id": delivery["attempt_id"],
        }
        self.assertEqual(bot.claim_status(self.token, lookup)["state"], "unknown")
        self.assertEqual(
            bot.claim(
                self.token,
                {"connection_id": self.connection_id, "instance_id": "instance:2", "limit": 1},
            )["deliveries"],
            [],
        )
        self.assertEqual(bot.claim_status(self.token, lookup)["state"], "unknown")
        self.assertEqual(
            bot.ack(self.token, {**lookup, "state": "sent", "channel_message_ids": ["sdk:late"]})[
                "state"
            ],
            "sent",
        )
        self.assertEqual(bot.claim_status(self.token, lookup)["state"], "sent")
        self.assertEqual(bot.view()["connections"][0]["delivery"]["sent"], 1)

    async def test_disable_and_credential_rotation(self):
        bot = self.platform.bots
        bot.change(self.connection_id, "disable")
        with self.assertRaises(Fault):
            bot.heartbeat(
                self.token, {"connection_id": self.connection_id, "instance_id": "instance:1"}
            )
        bot.change(self.connection_id, "enable")
        rotated = bot.change(self.connection_id, "rotate")
        with self.assertRaises(Fault):
            bot.heartbeat(
                self.token, {"connection_id": self.connection_id, "instance_id": "instance:1"}
            )
        self.assertEqual(
            bot.heartbeat(
                "Bearer " + rotated["token"],
                {"connection_id": self.connection_id, "instance_id": "instance:1"},
            )["state"],
            "online",
        )
        self.assertNotIn("token", str(bot.view()))
        self.assertIn(
            (self.platform.store.path + ".bots.sqlite", "bots"),
            health_inputs(self.settings).sidecars,
        )

    async def test_disable_rejects_new_work_but_accepts_claimed_late_receipt(self):
        bot = self.platform.bots
        request = self.send_request()
        bot.send(bearer("COMPANION"), request)
        delivery = bot.claim(
            self.token,
            {"connection_id": self.connection_id, "instance_id": "instance:1", "limit": 1},
        )["deliveries"][0]
        bot.change(self.connection_id, "disable")
        with self.assertRaises(Fault):
            bot.claim(
                self.token,
                {"connection_id": self.connection_id, "instance_id": "instance:1", "limit": 1},
            )
        with self.assertRaises(Fault):
            await bot.event(self.token, self.event())
        ack = {
            "connection_id": self.connection_id,
            "reply_id": delivery["reply_id"],
            "attempt_id": delivery["attempt_id"],
            "state": "sent",
            "channel_message_ids": ["sdk:sent-before-disable"],
        }
        self.assertEqual(bot.ack(self.token, ack)["state"], "sent")
        self.assertEqual(bot.view()["connections"][0]["delivery"]["sent"], 1)

    async def test_second_adapter_cannot_own_same_channel_and_actor(self):
        bot = self.platform.bots
        bot.slots["qq-astrbot-main"] = {
            "adapter": "astrbot",
            "platform_id": "astrbot-main",
            "self_id": "4242",
            "input_entry_ids": ["bot-input-1", "bot-input-2"],
            "label": "同群备用 AstrBot",
        }
        second = bot.create("qq-astrbot-main", ["actor:a"])
        with self.assertRaises(Fault) as caught:
            bot.change(second["connection_id"], "enable")
        self.assertEqual(caught.exception.code, "idempotency_conflict")
        bot.change(self.connection_id, "disable")
        self.assertTrue(bot.change(second["connection_id"], "enable")["enabled"])

    async def test_binding_id_cannot_create_second_owner_for_same_physical_bot(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = bot_settings(directory)
            other_entry = copy.deepcopy(settings["input_entries"]["bot-input-1"])
            other_entry["channel"]["binding_id"] = "binding:same-bot-other-core-route"
            other_entry["actor_entries"] = ["bot-actor-other"]
            settings["input_entries"]["bot-input-other"] = other_entry
            other_actor = copy.deepcopy(settings["entries"]["bot-actor-1"])
            other_actor["channel"] = copy.deepcopy(other_entry["channel"])
            settings["entries"]["bot-actor-other"] = other_actor
            settings["bot_connections"]["slots"]["qq-astrbot-same-bot"] = {
                "adapter": "astrbot",
                "platform_id": "astrbot-other-runtime",
                "self_id": "4242",
                "input_entry_ids": ["bot-input-other"],
                "label": "同机器人同群不同 Core 绑定",
            }
            platform = Platform(settings, clock=lambda: self.now)
            try:
                bot = platform.bots
                first = bot.create("qq-onebot-main", ["actor:a"])
                second = bot.create("qq-astrbot-same-bot", ["actor:a"])
                bot.change(first["connection_id"], "enable")
                with self.assertRaises(Fault) as caught:
                    bot.change(second["connection_id"], "enable")
                self.assertEqual(caught.exception.code, "idempotency_conflict")
                platform.sources.dispatch = AsyncMock(
                    return_value={"outcomes": [{"actor_id": "actor:a", "state": "accepted"}]}
                )
                event = {**self.event(), "connection_id": first["connection_id"]}
                self.assertEqual(
                    (await bot.event("Bearer " + first["token"], event))["state"], "accepted"
                )
                with self.assertRaises(Fault):
                    await bot.event(
                        "Bearer " + second["token"],
                        {
                            **event,
                            "connection_id": second["connection_id"],
                            "platform_id": "astrbot-other-runtime",
                        },
                    )
                self.assertEqual(platform.sources.dispatch.await_count, 1)
                # An already persisted duplicate from an older build also fails closed.
                with closing(sqlite3.connect(bot.path)) as db:
                    db.execute(
                        "UPDATE connections SET enabled=1 WHERE id=?", (second["connection_id"],)
                    )
                    db.commit()
                with self.assertRaises(Fault):
                    await bot.event("Bearer " + first["token"], {**event, "event_id": "sdk:later"})
                self.assertEqual(platform.sources.dispatch.await_count, 1)
            finally:
                platform.close()

    async def test_distinct_real_bot_ids_can_be_explicit_separate_owners(self):
        bot = self.platform.bots
        bot.slots["qq-other-bot"] = {
            "adapter": "astrbot",
            "platform_id": "astrbot-other-runtime",
            "self_id": "9999",
            "input_entry_ids": ["bot-input-1", "bot-input-2"],
            "label": "同群另一真实机器人",
        }
        other = bot.create("qq-other-bot", ["actor:a"])
        self.assertTrue(bot.change(other["connection_id"], "enable")["enabled"])

    async def test_event_database_lock_does_not_block_unrelated_loop_timer(self):
        bot = self.platform.bots
        self.platform.sources.dispatch = AsyncMock(
            return_value={"outcomes": [{"actor_id": "actor:a", "state": "accepted"}]}
        )
        ready = threading.Event()
        release = threading.Event()

        def hold_bot_write_lock():
            with closing(sqlite3.connect(bot.path, timeout=2)) as db:
                db.execute("BEGIN IMMEDIATE")
                ready.set()
                release.wait(0.45)
                db.commit()

        holder = threading.Thread(target=hold_bot_write_lock, daemon=True)
        holder.start()
        self.assertTrue(await asyncio.to_thread(ready.wait, 2))
        try:
            loop = asyncio.get_running_loop()
            started = loop.time()
            timer = loop.create_future()
            loop.call_later(0.05, lambda: timer.set_result(loop.time() - started))
            admission = asyncio.create_task(bot.event(self.token, self.event()))
            elapsed = await asyncio.wait_for(timer, 2)
            self.assertEqual((await asyncio.wait_for(admission, 2))["state"], "accepted")
            self.assertLess(elapsed, 0.2)
        finally:
            release.set()
            await asyncio.to_thread(holder.join, 2)

    async def test_actual_http_connector_and_web_management_boundaries(self):
        app = create_app(self.platform)
        runner, url = await start_http(app)
        self.addAsyncCleanup(runner.cleanup)
        self.settings["web"]["origin"] = url
        self.platform.sources.dispatch = AsyncMock(
            return_value={"outcomes": [{"actor_id": "actor:a", "state": "accepted"}]}
        )
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
            async with client.post(
                url + "/internal/v1/bot/events",
                json=self.event(),
                headers={"Authorization": self.token},
            ) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())["state"], "accepted")
            async with client.post(
                url + "/internal/v1/bot/events",
                json={**self.event(), "event_id": "sdk:long", "text": "长" * 5000},
                headers={"Authorization": self.token},
            ) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())["state"], "accepted")
            async with client.post(
                url + "/internal/v1/bot/events/status",
                json={
                    "connection_id": self.connection_id,
                    "event_id": "sdk:101",
                    "account_id": "1001",
                },
                headers={"Authorization": self.token},
            ) as response:
                self.assertEqual((await response.json())["state"], "accepted")
            async with client.post(
                url + "/internal/v1/bot/events",
                json=self.event(),
                headers={"Authorization": bearer("ADMIN")},
            ) as response:
                self.assertEqual(response.status, 401)
            async with client.post(
                url + "/internal/v1/bot/events",
                json=self.event(),
                headers={"Authorization": self.token, "Cookie": "x=y"},
            ) as response:
                self.assertEqual(response.status, 403)
            request = self.send_request()
            async with client.post(
                url + "/internal/v1/conversation/send",
                json=request,
                headers={"Authorization": bearer("COMPANION")},
            ) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())["state"], "unknown")
            async with client.post(
                url + "/internal/v1/conversation/reply-status",
                json=request,
                headers={"Authorization": bearer("COMPANION")},
            ) as response:
                self.assertIsNone((await response.json())["receipt"])
            async with client.post(
                url + "/internal/v1/bot/replies/claim",
                json={"connection_id": self.connection_id, "instance_id": "instance:1", "limit": 1},
                headers={"Authorization": self.token},
            ) as response:
                self.assertEqual(response.status, 200)
                delivery = (await response.json())["deliveries"][0]
            async with client.post(
                url + "/internal/v1/bot/replies/ack",
                json={
                    "connection_id": self.connection_id,
                    "reply_id": delivery["reply_id"],
                    "attempt_id": delivery["attempt_id"],
                    "state": "sent",
                    "channel_message_ids": ["sdk:http:1"],
                },
                headers={"Authorization": self.token},
            ) as response:
                self.assertEqual(response.status, 200)
            async with client.post(
                url + "/internal/v1/conversation/reply-status",
                json=request,
                headers={"Authorization": bearer("COMPANION")},
            ) as response:
                self.assertEqual(
                    (await response.json())["receipt"]["channel_message_ids"], ["sdk:http:1"]
                )
            async with client.get(url + "/api/web/session") as response:
                anonymous = await response.json()
            async with client.post(
                url + "/api/web/login",
                json={"username": "synthetic-admin", "password": PASSWORD},
                headers={"Origin": url, "X-CSRF-Token": anonymous["csrf"]},
            ) as response:
                self.assertEqual(response.status, 200)
                logged = await response.json()
            async with client.post(
                url + "/api/web/bots/view",
                json={},
                headers={"Origin": url, "X-CSRF-Token": logged["csrf"]},
            ) as response:
                self.assertEqual(response.status, 200)
                view = await response.json()
                self.assertEqual(view["management"]["code"], "management_required")
                self.assertNotIn(self.token[7:], str(view))
            async with client.post(
                url + "/api/web/bots/disable",
                json={"connection_id": self.connection_id},
                headers={"Origin": url, "X-CSRF-Token": logged["csrf"]},
            ) as response:
                self.assertEqual(response.status, 403)
            async with client.post(
                url + "/api/web/bots/unlock",
                json={"password": PASSWORD},
                headers={"Origin": url, "X-CSRF-Token": logged["csrf"]},
            ) as response:
                self.assertEqual(response.status, 200)
            async with client.post(
                url + "/api/web/bots/disable",
                json={"connection_id": self.connection_id},
                headers={"Origin": url, "X-CSRF-Token": logged["csrf"]},
            ) as response:
                self.assertEqual(response.status, 200)
            async with client.post(
                url + "/internal/v1/bot/heartbeat",
                json={"connection_id": self.connection_id, "instance_id": "instance:1"},
                headers={"Authorization": self.token},
            ) as response:
                self.assertEqual(response.status, 403)


class BotConfigurationTests(unittest.TestCase):
    def test_reject_wrong_slot_owner_and_author_mismatch(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, ENV):
            settings = bot_settings(directory)
            settings["bot_connections"]["slots"]["qq-onebot-main"]["input_entry_ids"] = [
                "input-entry"
            ]
            with self.assertRaises(Fault):
                validate_settings(settings)

    def test_qq_profile_credentials_must_be_distinct(self):
        from services.platform.qq_admin import validate_profiles_configuration

        with patch.dict(os.environ, {**ENV, "TS_QQ_PROFILE": ENV["TS012_ADMIN"]}):
            with self.assertRaises(Fault):
                validate_profiles_configuration(
                    {
                        "web_qq_profiles": {
                            "base_url": "https://memory.synthetic.invalid",
                            "token_env": "TS_QQ_PROFILE",
                        }
                    },
                    ["TS012_ADMIN"],
                )

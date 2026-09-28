"""Synthetic bot lifecycle, authority and durable delivery tests; no SDK or real sends."""

import os
import tempfile
import time
import unittest
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
    config["principals"]["companion"]["actions"].append("dialogue.send")
    channel = {
        "namespace": "qq",
        "binding_id": "binding:bot-qq",
        "channel_conversation_id": "group:123",
        "thread_id": None,
    }
    entries = []
    for number in ("1", "2"):
        account = {"namespace": "qq", "immutable_account_id": "user:" + number}
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
                "self_id": "bot:42",
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

    def event(self, account="user:1"):
        return {
            "schema_version": 1,
            "connection_id": self.connection_id,
            "platform_id": "onebot11-main",
            "self_id": "bot:42",
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
        second = await bot.event(self.token, {**self.event("user:2"), "event_id": "sdk:102"})
        self.assertEqual(second["state"], "accepted")
        self.assertEqual(self.platform.sources.dispatch.await_count, 2)
        self.assertEqual(
            bot.event_status(
                self.token,
                {
                    "connection_id": self.connection_id,
                    "event_id": "sdk:101",
                    "account_id": "user:1",
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
                    "account_id": "user:1",
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
            "self_id": "bot:42",
            "input_entry_ids": ["bot-input-1", "bot-input-2"],
            "label": "同群备用 AstrBot",
        }
        second = bot.create("qq-astrbot-main", ["actor:a"])
        with self.assertRaises(Fault) as caught:
            bot.change(second["connection_id"], "enable")
        self.assertEqual(caught.exception.code, "idempotency_conflict")
        bot.change(self.connection_id, "disable")
        self.assertTrue(bot.change(second["connection_id"], "enable")["enabled"])

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
                    "account_id": "user:1",
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

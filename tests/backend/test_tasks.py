"""Task centre: one read-only projection over the platform's own operation ledgers.

The task centre owns no database and no execution path. It reads the household connector's
durable control ledger, the Models owner's operation record and this console's own claim ledger
through the ports those modules expose, and it normalizes their words into five separate
statuses. These tests drive the real thing: a real loopback console, a synthetic HA that really
moves and really fails, and real HTTP for every assertion.

What is asserted here is the honesty of the projection. An acceptance is not an execution, a
receipt is not a result, an outcome that cannot be established stays unknown, a source that
cannot be read or was never connected says so instead of showing an empty list, a removed
registration leaves its history visible and labelled, and no credential or private control
parameter is projected. Paging is checked against concurrent writes: a cursor can neither skip
a record nor show one twice, and a record whose state changed while a later page was being read
still reports the new fact.
"""

import copy
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import aiohttp

from fixtures import ENV, start_http
from home_fixtures import ENV as HOME_ENV
from home_fixtures import HOME_KEY, home_app, start_home
from services.platform.contracts import utc
from services.platform.server import create_app
from services.platform.service import Platform
from services.platform.web_console import WebConsole
from web_fixtures import PASSWORD, home_settings

CLIENT = "11111111-2222-3333-4444-555555555555"
OTHER = "22222222-3333-4444-5555-666666666666"
THIRD = "33333333-4444-5555-6666-777777777777"
FOURTH = "44444444-5555-6666-7777-888888888888"
FIFTH = "55555555-6666-7777-8888-999999999999"
CHAT_TEMPLATE = "chat-local-text"
# The documented list projection: nothing else may reach the browser.
SUMMARY_FIELDS = {
    "task_id",
    "source",
    "kind",
    "title",
    "target",
    "status",
    "stage",
    "code",
    "created_at",
    "updated_at",
    "settled",
    "pending",
    "attention",
    "source_state",
    "cancel",
    "evidence",
    "module",
}


class TaskCentreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {**ENV, **HOME_ENV})
        env.start()
        self.addCleanup(env.stop)
        self.ha_app = home_app()
        self.ha = self.ha_app[HOME_KEY]
        self.ha_runner, self.ha_url = await start_home(self.ha_app)
        self.addAsyncCleanup(self.ha_runner.cleanup)
        await self.start(home_settings(self.temp.name, self.ha_url))
        self.client = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.addAsyncCleanup(self.client.close)

    # ------------------------------------------------------------- harness

    def variant(self, name, mutate=None, base_url=None):
        """Another deployment with its own databases, for settings a restart should not share."""
        directory = Path(self.temp.name) / name
        directory.mkdir(parents=True, exist_ok=True)
        config = home_settings(str(directory), base_url or self.ha_url)
        if mutate:
            mutate(config)
        return config

    def redeploy(self, mutate):
        """A restart of the same deployment: same database files, changed reviewed settings."""
        config = copy.deepcopy(self.config)
        mutate(config)
        return config

    async def start(self, config):
        """Boot a real loopback app; the console reads the same settings dict we mutate."""
        self.config = config
        self.platform = Platform(config)
        self.app = create_app(self.platform)
        self.runner, self.url = await start_http(self.app)
        self.addAsyncCleanup(self.runner.cleanup)
        config["web"]["origin"] = self.url
        self.console = next(
            handler.__self__
            for handler in (route.handler for route in self.app.router.routes())
            if isinstance(getattr(handler, "__self__", None), WebConsole)
        )
        return self.url

    async def call(self, path, body, csrf, expected=200, session=None, **headers):
        client = session or self.client
        async with client.post(
            self.url + "/api/web/" + path,
            json=body,
            headers={"Origin": self.url, "X-CSRF-Token": csrf, **headers},
        ) as response:
            data = await response.json()
            self.assertEqual(response.status, expected, data)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            return data

    async def current(self, client=None):
        async with (client or self.client).get(self.url + "/api/web/session") as response:
            return response.status, await response.json()

    async def login(self, client=None):
        _, anonymous = await self.current(client)
        return await self.call(
            "login",
            {"username": "synthetic-admin", "password": PASSWORD},
            anonymous["csrf"],
            session=client,
        )

    async def unlocked_login(self, client=None):
        logged = await self.login(client)
        await self.call("home/unlock", {"password": PASSWORD}, logged["csrf"], session=client)
        return logged

    async def tasks(self, logged, body=None, expected=200, session=None):
        return await self.call(
            "tasks/view",
            {"page_size": 10, **(body or {})},
            logged["csrf"],
            expected,
            session=session,
        )

    async def detail(self, logged, task_id, expected=200, session=None):
        return await self.call(
            "tasks/detail", {"task_id": task_id}, logged["csrf"], expected, session=session
        )

    async def view(self, logged, client=None):
        return await self.call("home/view", {}, logged["csrf"], session=client)

    async def refresh(self, logged, client=None):
        return await self.call("home/refresh", {}, logged["csrf"], session=client)

    async def control(self, logged, template, revision, client_id=CLIENT, expected=200, **extra):
        body = {"template_id": template, "expected_revision": revision, "client_id": client_id}
        body.update(extra)
        return await self.call("home/control", body, logged["csrf"], expected)

    async def set_home_state(self, entity_id, state):
        async with self.client.post(
            self.ha_url + "/fixture/state", json={"entity_id": entity_id, "state": state}
        ) as response:
            self.assertEqual(response.status, 200)

    async def release_home(self):
        async with self.client.post(self.ha_url + "/fixture/release", json={}) as response:
            self.assertEqual(response.status, 200)

    def entity(self, view, label):
        return next(item for item in view["entities"] if item["label"] == label)

    def task(self, view, task_id):
        return next(item for item in view["items"] if item["task_id"] == task_id)

    def source(self, view, source_id):
        return next(item for item in view["sources"] if item["id"] == source_id)

    def service_calls(self):
        return [
            row
            for row in self.ha.log
            if row["method"] == "POST" and row["path"].startswith("/api/services")
        ]

    def intents(self):
        with closing(sqlite3.connect(self.platform.store.path + ".home-controls.sqlite")) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM control_intents")]

    def intent(self, client_id=CLIENT):
        return next((row for row in self.intents() if row["client_id"] == client_id), None)

    def expire_lease(self, client_id=CLIENT):
        with closing(sqlite3.connect(self.platform.store.path + ".home-controls.sqlite")) as db:
            db.execute(
                "UPDATE control_intents SET lease_expires_at=0 WHERE client_id=?", (client_id,)
            )
            db.commit()

    def add_claim(
        self, client_id=FIFTH, version=9, digest="a" * 64, state="prepared", settled=None
    ):
        """Leave behind exactly what a publication whose receipt never became authoritative is."""
        now = self.platform.models.clock()
        with closing(sqlite3.connect(self.platform.store.path + ".web-models.sqlite")) as db:
            db.execute(
                "INSERT INTO publication_intents (client_id, semantic, target, version,"
                " expected_version, digest, published_at, usable_until, state, result,"
                " prepared_at, settled_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    client_id,
                    "publish",
                    "chat",
                    version,
                    None,
                    digest,
                    utc(now),
                    utc(now + 300),
                    state,
                    "published" if state == "committed" else None,
                    now,
                    settled,
                ),
            )
            db.commit()

    async def until(self, condition, timeout=8.0):
        import asyncio

        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if condition():
                return True
            await asyncio.sleep(0.05)
        self.fail("condition was not reached in time")

    async def transmit_then_die(self, logged, template, revision, client_id=CLIENT):
        """A command that was really sent, whose owner is gone: the state must never be guessed."""
        import asyncio

        self.ha.service_mode = "hold"
        before = len(self.service_calls())
        task = asyncio.create_task(self.control(logged, template, revision, client_id=client_id))
        try:
            await self.until(lambda: len(self.service_calls()) > before)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            self.ha.service_mode = None
        self.assertEqual(self.intent(client_id)["state"], "sending")
        self.expire_lease(client_id)

    async def revision(self, logged, label="书房灯"):
        return self.entity(await self.view(logged), label)["revision"]

    async def fresh_revision(self, logged, label="书房灯"):
        """The revision a browser would have to hold before it may write: read, then write."""
        return self.entity(await self.refresh(logged), label)["revision"]

    async def one_accepted(self, logged, client_id=CLIENT):
        """One recorded acceptance whose device never moved (HA answers 200, applies nothing)."""
        self.ha.mode = "unapplied"
        try:
            await self.refresh(logged)
            await self.control(logged, "study-light-on", await self.revision(logged), client_id)
        finally:
            self.ha.mode = "normal"
        await self.refresh(logged)

    # -------------------------------------------------------------- sources

    async def test_an_empty_platform_reports_real_empty_states_and_declared_gaps(self):
        logged = await self.login()
        view = await self.tasks(logged)
        self.assertEqual(view["items"], [])
        self.assertEqual(
            (view["page"]["returned"], view["page"]["has_more"], view["page"]["next_cursor"]),
            (0, False, None),
        )
        self.assertEqual(view["page"]["sort"], "recorded_desc")
        self.assertEqual(
            list(view["statuses"]), ["accepted", "in_progress", "observed", "unknown", "failed"]
        )
        # Nothing has been read and nothing has been published: both are real, readable facts.
        home = self.source(view, "platform.home")
        self.assertEqual(
            (home["connected"], home["state"], home["records"]), (True, "never_read", 0)
        )
        self.assertEqual(
            (home["kind"], home["cancel_code"]), ("device.control", "executor_does_not_cancel")
        )
        models = self.source(view, "platform.models")
        self.assertEqual(
            (models["connected"], models["state"], models["records"]), (True, "empty", 0)
        )
        self.assertEqual(models["cancel_code"], "recorded_fact")
        # The products with no contracted task vocabulary are named, never guessed at.
        for source_id in (
            "companion.core",
            "assets.remote",
            "resources.download",
            "platform.dialogue",
        ):
            with self.subTest(source=source_id):
                source = self.source(view, source_id)
                self.assertEqual(
                    (source["connected"], source["state"], source["records"], source["kind"]),
                    (False, "not_connected", None, None),
                )
                self.assertEqual(
                    source["code"],
                    "no_task_contract"
                    if source_id != "platform.dialogue"
                    else "no_operation_ledger",
                )
        # Filtering by a source that is not connected is an empty page, never an error.
        gap = await self.tasks(logged, {"source": "companion.core"})
        self.assertEqual((gap["items"], gap["page"]["has_more"]), ([], False))
        self.assertEqual(self.source(gap, "companion.core")["state"], "not_connected")

    async def test_a_connected_source_reports_its_own_reachability(self):
        logged = await self.login()
        await self.refresh(logged)
        online = self.source(await self.tasks(logged), "platform.home")
        self.assertEqual((online["state"], online["code"]), ("online", "ok"))
        self.ha.mode = "server_error"
        await self.refresh(logged)
        offline = self.source(await self.tasks(logged), "platform.home")
        self.assertEqual(offline["state"], "offline")
        self.assertTrue(offline["code"])
        self.ha.mode = "normal"
        await self.refresh(logged)
        self.assertEqual(self.source(await self.tasks(logged), "platform.home")["state"], "online")
        # A missing credential is its own state, not an offline device and not an empty list.
        await self.start(self.redeploy(lambda c: c["home"].update(token_env="TS018_ABSENT")))
        logged = await self.login()
        state = self.source(await self.tasks(logged), "platform.home")
        self.assertEqual(
            (state["state"], state["code"]), ("credential_missing", "device_credential_missing")
        )

    async def test_history_survives_a_removed_registration_and_says_so(self):
        logged = await self.unlocked_login()
        await self.one_accepted(logged)
        await self.start(self.redeploy(lambda c: c.pop("home")))
        logged = await self.login()
        view = await self.tasks(logged)
        source = self.source(view, "platform.home")
        self.assertEqual((source["state"], source["code"]), ("missing", "home_removed"))
        self.assertEqual(source["records"], 1)
        item = self.task(view, "home-control:" + CLIENT)
        self.assertEqual(item["source_state"], "source_missing")
        self.assertTrue(item["attention"])
        # The record is still readable in detail: a removed setting never erases real history.
        detail = await self.detail(logged, "home-control:" + CLIENT)
        self.assertEqual(detail["task"]["task_id"], "home-control:" + CLIENT)
        self.assertEqual(detail["task"]["request"]["entity_id"], "light.study")
        self.assertEqual(detail["task"]["status"], "accepted")

    async def test_a_record_whose_entity_left_the_registration_is_marked_not_hidden(self):
        logged = await self.unlocked_login()
        await self.one_accepted(logged)

        def narrow(config):
            config["home"]["entities"] = [
                item for item in config["home"]["entities"] if item["entity_id"] != "light.study"
            ]
            config["home"]["templates"] = [
                item for item in config["home"]["templates"] if item["entity_id"] != "light.study"
            ]

        await self.start(self.redeploy(narrow))
        logged = await self.login()
        item = self.task(await self.tasks(logged), "home-control:" + CLIENT)
        self.assertEqual(item["source_state"], "origin_missing")
        self.assertTrue(item["attention"])
        self.assertEqual(item["status"], "accepted")

    # --------------------------------------------------------------- records

    async def test_an_acceptance_is_not_reported_as_an_execution(self):
        logged = await self.unlocked_login()
        await self.one_accepted(logged)
        item = self.task(await self.tasks(logged), "home-control:" + CLIENT)
        self.assertEqual(
            (item["source"], item["kind"], item["title"], item["target"]),
            ("platform.home", "device.control", "打开书房灯", "light.study"),
        )
        self.assertEqual((item["status"], item["stage"]), ("accepted", "accepted"))
        # A recorded receipt is not a concluded outcome: it settled, the observation is still open.
        self.assertEqual((item["settled"], item["pending"]), (True, True))
        # The receipt did name the target state, and the device still shows the old one.
        self.assertEqual(
            (item["evidence"]["acceptance"], item["evidence"]["receipt"]), ("accepted", True)
        )
        self.assertEqual(item["evidence"]["observation"], "contradicted")
        self.assertTrue(item["attention"])
        self.assertEqual(set(item), SUMMARY_FIELDS)
        self.assertEqual(len(self.service_calls()), 1)

    async def test_a_target_already_reached_is_concluded_by_observation_only(self):
        logged = await self.unlocked_login()
        await self.set_home_state("light.study", "on")
        await self.refresh(logged)
        answer = await self.control(logged, "study-light-on", await self.revision(logged), OTHER)
        self.assertEqual(
            (answer["acceptance"], answer["code"]), ("observed", "recovered_at_target")
        )
        self.assertEqual(self.service_calls(), [])
        item = self.task(await self.tasks(logged), "home-control:" + OTHER)
        self.assertEqual((item["status"], item["stage"]), ("observed", "observed"))
        self.assertFalse(item["attention"])
        self.assertEqual(
            (item["evidence"]["acceptance"], item["evidence"]["observation"]),
            ("observed", "confirmed"),
        )
        self.assertEqual(self.source(await self.tasks(logged), "platform.home")["state"], "online")

    async def test_a_transmitted_command_without_a_receipt_stays_running_then_concludes(self):
        import asyncio

        logged = await self.unlocked_login()
        await self.refresh(logged)
        revision = await self.revision(logged)
        await self.transmit_then_die(logged, "study-light-on", revision)
        running = self.task(await self.tasks(logged), "home-control:" + CLIENT)
        self.assertEqual((running["status"], running["stage"]), ("in_progress", "executing"))
        self.assertEqual((running["settled"], running["pending"]), (False, True))
        self.assertEqual(running["evidence"]["receipt"], False)
        # The command that is still in flight took effect before anyone looked again, so the
        # only way to conclude it is an observation: nothing is ever transmitted twice.
        self.ha.states["light.study"]["state"] = "on"
        await asyncio.sleep(1.05)
        # The retry is the same reviewed intent, so it repeats the revision it was read with.
        retry = await self.control(logged, "study-light-on", revision)
        self.assertEqual((retry["acceptance"], retry["code"]), ("observed", "recovered_at_target"))
        self.assertEqual(len(self.service_calls()), 1)
        concluded = self.task(await self.tasks(logged), "home-control:" + CLIENT)
        self.assertEqual((concluded["status"], concluded["stage"]), ("observed", "observed"))
        self.assertEqual(concluded["settled"], True)
        self.assertGreater(concluded["updated_at"], concluded["created_at"])

    async def test_an_unreadable_receipt_is_unknown_and_is_never_resent(self):
        logged = await self.unlocked_login()
        self.ha.service_mode = "unreadable"
        await self.refresh(logged)
        answer = await self.control(
            logged, "study-light-on", await self.revision(logged), THIRD, expected=502
        )
        self.assertEqual(answer["code"], "receipt_unreadable")
        self.ha.service_mode = None
        item = self.task(await self.tasks(logged), "home-control:" + THIRD)
        self.assertEqual((item["status"], item["stage"]), ("unknown", "unknown"))
        self.assertEqual(item["code"], "receipt_unreadable")
        self.assertTrue(item["attention"])
        self.assertEqual(
            (item["evidence"]["receipt"], item["evidence"]["observation"]), (False, "unknown")
        )
        self.assertEqual(len(self.service_calls()), 1)
        # Re-reading the task centre changes nothing: no automatic resend exists anywhere.
        await self.refresh(logged)
        self.assertEqual(
            self.task(await self.tasks(logged), "home-control:" + THIRD)["status"], "unknown"
        )
        self.assertEqual(len(self.service_calls()), 1)

    async def test_a_rejected_command_is_failed_with_its_own_reason(self):
        logged = await self.unlocked_login()
        self.ha.service_mode = "unauthorized"
        await self.refresh(logged)
        answer = await self.control(
            logged, "study-light-on", await self.revision(logged), FOURTH, expected=502
        )
        self.assertEqual(answer["code"], "device_unauthorized")
        self.ha.service_mode = None
        item = self.task(await self.tasks(logged), "home-control:" + FOURTH)
        self.assertEqual((item["status"], item["stage"]), ("failed", "rejected"))
        self.assertEqual(item["code"], "device_unauthorized")
        self.assertEqual(item["evidence"]["observation"], "not_sent")
        self.assertEqual(self.intent(FOURTH)["state"], "rejected")

    async def test_publications_are_projected_from_the_platforms_own_record(self):
        logged = await self.unlocked_login()
        await self.call("models/unlock", {"password": PASSWORD}, logged["csrf"])
        published = await self.call(
            "models/publish",
            {"template_id": CHAT_TEMPLATE, "expected_version": None, "client_id": CLIENT},
            logged["csrf"],
        )
        task_id = "model-version:chat:" + str(published["version"])
        item = self.task(await self.tasks(logged), task_id)
        self.assertEqual((item["source"], item["kind"]), ("platform.models", "model.publish"))
        self.assertEqual(item["title"], "Chat 兼容配置")
        self.assertEqual((item["status"], item["stage"]), ("observed", "published"))
        self.assertEqual(item["code"], "intact")
        self.assertEqual(item["evidence"]["operation"], "config.publish")
        self.assertTrue(item["evidence"]["actor"])
        self.assertEqual(item["evidence"]["integrity"], "intact")
        self.assertEqual((item["evidence"]["revoked"], item["attention"]), (False, False))
        self.assertTrue(item["evidence"]["claim"])
        self.assertEqual(item["cancel"], {"supported": False, "code": "recorded_fact"})
        detail = await self.detail(logged, task_id)
        self.assertEqual(detail["task"]["request"]["version"], published["version"])
        self.assertTrue(detail["task"]["timeline"]["published_at"])
        self.assertEqual(detail["task"]["timeline"]["usable_until"], published["usable_until"])
        self.assertEqual(detail["task"]["module"]["page"], "#/settings/2")
        # A revocation is evidence on the same record, not a second task.
        await self.call(
            "models/revoke", {"target": "chat", "version": published["version"]}, logged["csrf"]
        )
        view = await self.tasks(logged)
        revoked = self.task(view, task_id)
        self.assertEqual((revoked["evidence"]["revoked"], revoked["attention"]), (True, True))
        self.assertEqual(revoked["status"], "observed")
        self.assertEqual(
            len([one for one in view["items"] if one["source"] == "platform.models"]), 1
        )

    async def test_a_claim_whose_receipt_never_became_authoritative_stays_unknown(self):
        await self.unlocked_login()
        self.add_claim(client_id=FIFTH)
        logged = await self.login()
        view = await self.tasks(logged)
        task_id = "model-intent:" + FIFTH
        item = self.task(view, task_id)
        self.assertEqual((item["status"], item["stage"]), ("unknown", "unverified"))
        self.assertEqual(item["code"], "receipt_missing")
        self.assertEqual(
            (item["settled"], item["pending"], item["attention"]), (False, False, True)
        )
        self.assertEqual(item["evidence"]["authoritative"], "absent")
        self.assertEqual(item["evidence"]["window"], "open")
        self.assertEqual(item["evidence"]["receipt"], "prepared")
        self.assertEqual(self.source(view, "platform.models")["records"], 1)
        detail = await self.detail(logged, task_id)
        self.assertEqual(detail["task"]["request"]["target"], "chat")
        self.assertEqual(detail["task"]["request"]["version"], 9)

    # ---------------------------------------------------------------- paging

    async def mixed_records(self, logged):
        """One record per real outcome, newest last, each produced by a real operation."""
        await self.one_accepted(logged)
        await self.set_home_state("light.study", "on")
        await self.control(logged, "study-light-on", await self.fresh_revision(logged), OTHER)
        self.ha.service_mode = "unreadable"
        await self.control(
            logged, "study-light-off", await self.fresh_revision(logged), THIRD, expected=502
        )
        self.ha.service_mode = "unauthorized"
        await self.control(
            logged, "study-light-on", await self.fresh_revision(logged), FOURTH, expected=502
        )
        self.ha.service_mode = None
        self.add_claim(client_id=FIFTH)
        return [
            "model-intent:" + FIFTH,
            "home-control:" + FOURTH,
            "home-control:" + THIRD,
            "home-control:" + OTHER,
            "home-control:" + CLIENT,
        ]

    async def test_paging_walks_every_record_exactly_once(self):
        logged = await self.unlocked_login()
        expected = await self.mixed_records(logged)
        whole = await self.tasks(logged, {"page_size": 50})
        self.assertEqual([item["task_id"] for item in whole["items"]], expected)
        self.assertEqual(whole["page"]["has_more"], False)
        walked, cursor, pages = [], None, 0
        while True:
            body = {"page_size": 2}
            if cursor:
                body["cursor"] = cursor
            page = await self.tasks(logged, body)
            pages += 1
            walked += [item["task_id"] for item in page["items"]]
            if not page["page"]["has_more"]:
                self.assertIsNone(page["page"]["next_cursor"])
                break
            cursor = page["page"]["next_cursor"]
            self.assertIsNotNone(cursor)
        self.assertEqual(pages, 3)
        self.assertEqual(walked, expected)
        self.assertEqual(len(set(walked)), len(expected))
        # The same order is reported under every filter, and each filter is exact.
        observed = await self.tasks(logged, {"status": "observed", "page_size": 50})
        self.assertEqual([item["task_id"] for item in observed["items"]], ["home-control:" + OTHER])
        failed = await self.tasks(logged, {"status": "failed", "page_size": 50})
        self.assertEqual([item["task_id"] for item in failed["items"]], ["home-control:" + FOURTH])
        published = await self.tasks(logged, {"source": "platform.models", "page_size": 50})
        self.assertEqual(
            [item["task_id"] for item in published["items"]], ["model-intent:" + FIFTH]
        )
        combined = await self.tasks(
            logged, {"status": "unknown", "source": "platform.home", "page_size": 50}
        )
        self.assertEqual([item["task_id"] for item in combined["items"]], ["home-control:" + THIRD])
        empty = await self.tasks(logged, {"status": "in_progress", "page_size": 50})
        self.assertEqual((empty["items"], empty["page"]["has_more"]), ([], False))

    async def test_a_cursor_is_bound_to_its_filter_and_refuses_nonsense(self):
        logged = await self.unlocked_login()
        await self.mixed_records(logged)
        first = await self.tasks(logged, {"page_size": 2})
        cursor = first["page"]["next_cursor"]
        self.assertTrue(cursor)
        # The same cursor under a different question is a conflict, not a silent mispage.
        for body in ({"status": "unknown"}, {"source": "platform.models"}):
            with self.subTest(body=body):
                refusal = await self.tasks(logged, {**body, "cursor": cursor}, expected=409)
                self.assertEqual(refusal["code"], "cursor_conflict")
        for body in (
            {"cursor": "not base64 at all!"},
            {"cursor": "YWJjZA"},
            {"cursor": "e30"},
            {"page_size": 0},
            {"page_size": 51},
            {"page_size": "2"},
            {"status": "done"},
            {"source": "platform.unknown"},
            {"cursor": cursor, "extra": 1},
        ):
            with self.subTest(body=body):
                refusal = await self.tasks(logged, body, expected=400)
                self.assertEqual(refusal["code"], "invalid_input")
        # The cursor itself is only a position, a watermark and a filter digest: no record rides along.
        import base64

        decoded = json.loads(base64.urlsafe_b64decode(cursor))
        self.assertEqual(set(decoded), {"v", "filter", "streams", "at"})
        self.assertTrue(set(decoded["streams"]) <= {"platform.home", "platform.models"})
        self.assertTrue(all(len(value) == 2 for value in decoded["streams"].values()))
        self.assertEqual(len(decoded["at"]), 2)
        self.assertNotIn("家", cursor + str(decoded))

    async def test_a_changed_record_and_a_new_record_do_not_confuse_a_cursor(self):
        import asyncio

        logged = await self.unlocked_login()
        self.ha.service_mode = "unreadable"
        await self.control(
            logged, "study-light-on", await self.fresh_revision(logged), THIRD, expected=502
        )
        self.ha.service_mode = "unauthorized"
        await self.control(
            logged, "study-light-off", await self.fresh_revision(logged), FOURTH, expected=502
        )
        self.ha.service_mode = None
        # A later whole second, so the running record is unambiguously the newest of the three.
        await asyncio.sleep(1.05)
        revision = await self.fresh_revision(logged)
        # The light is on, so turning it off is a real command that really leaves this process.
        await self.transmit_then_die(logged, "study-light-off", revision)
        # The order is whatever the whole read says it is; the walk below has to match it exactly.
        order = [item["task_id"] for item in (await self.tasks(logged, {"page_size": 50}))["items"]]
        self.assertEqual(
            order,
            ["home-control:" + CLIENT, "home-control:" + FOURTH, "home-control:" + THIRD],
        )
        first = await self.tasks(logged, {"page_size": 2})
        self.assertEqual([item["task_id"] for item in first["items"]], order[:2])
        running = self.task(first, "home-control:" + CLIENT)
        self.assertEqual((running["status"], running["settled"]), ("in_progress", False))
        cursor = first["page"]["next_cursor"]
        self.assertTrue(first["page"]["has_more"])
        # While the later page is being read: the running record really concludes, and a brand
        # new record is written. Neither may move the position of anything already ordered.
        self.ha.states["light.study"]["state"] = "off"
        await asyncio.sleep(1.05)
        # The same reviewed intent repeats the revision it was read with, so it is a retry.
        await self.control(logged, "study-light-off", revision)
        self.add_claim(client_id=FIFTH)
        second = await self.tasks(logged, {"page_size": 2, "cursor": cursor})
        self.assertEqual([item["task_id"] for item in second["items"]], order[2:])
        self.assertEqual(second["page"]["has_more"], False)
        walked = [item["task_id"] for item in first["items"] + second["items"]]
        # No record was skipped and none appeared twice, whatever changed while we walked.
        self.assertEqual(walked, order)
        self.assertEqual(len(set(walked)), len(walked))
        # The new record is newer than the cursor, so it arrives on the next first-page read,
        # together with the concluded state of the record that was running.
        fresh = await self.tasks(logged, {"page_size": 2})
        self.assertEqual(fresh["items"][0]["task_id"], "model-intent:" + FIFTH)
        concluded = self.task(await self.tasks(logged, {"page_size": 50}), "home-control:" + CLIENT)
        self.assertEqual((concluded["status"], concluded["stage"]), ("observed", "observed"))
        self.assertGreater(concluded["updated_at"], running["updated_at"])
        # Three attempts reached the device in total, and the concluding retry is not a fourth.
        self.assertEqual(len(self.service_calls()), 3)

    # --------------------------------------------------------- access and redaction

    async def test_the_task_centre_uses_the_real_session_and_revocation(self):
        _, anonymous = await self.current()
        for path, body in (
            ("tasks/view", {}),
            ("tasks/detail", {"task_id": "home-control:" + CLIENT}),
        ):
            with self.subTest(path=path):
                refusal = await self.call(path, body, anonymous["csrf"], expected=401)
                self.assertEqual(refusal["code"], "unauthorized")
        logged = await self.unlocked_login()
        await self.one_accepted(logged)
        self.assertTrue((await self.tasks(logged))["items"])
        # An expired session is a state, not a stale page: nothing is projected afterwards.
        live = next(iter(self.console.sessions.values()))
        live["expires"] = 0
        self.assertEqual((await self.tasks(logged, expected=401))["code"], "session_expired")
        self.assertEqual(
            (await self.detail(logged, "home-control:" + CLIENT, expected=401))["code"],
            "session_expired",
        )
        # A new login sees the same real record again; the record itself never changed.
        again = await self.login()
        self.assertTrue((await self.tasks(again))["items"])
        # A revoked browser session is refused even though the record still exists.
        await self.call("logout", {}, again["csrf"])
        self.assertEqual((await self.tasks(again, expected=401))["code"], "session_expired")

    async def test_a_refused_request_proof_is_never_a_view_of_anything(self):
        """The refusal statuses are the contract the panel classifies on.

        The task centre reads a rejected request as either "this session is gone" (401/403, so the
        view must be forgotten and the operator asked to log in again) or "this read failed" (the
        snapshot stays and is marked as stale). A wrong CSRF token or a foreign origin must
        therefore stay a 403 refusal and never become a 200 carrying somebody's records.
        """
        logged = await self.unlocked_login()
        await self.one_accepted(logged)
        self.assertTrue((await self.tasks(logged))["items"])
        for headers in (
            {"Origin": "http://127.0.0.1:9"},
            {"X-CSRF-Token": "synthetic-wrong-token"},
        ):
            with self.subTest(headers=headers):
                refusal = await self.call("tasks/view", {}, logged["csrf"], 403, **headers)
                self.assertEqual(refusal["code"], "forbidden")
                self.assertNotIn("items", refusal)
        # A refused proof is not a logout: the same session still reads its own records.
        self.assertTrue((await self.tasks(logged))["items"])

    async def test_no_credential_or_private_control_detail_is_projected(self):
        logged = await self.unlocked_login()
        await self.one_accepted(logged)
        view = await self.tasks(logged)
        detail = await self.detail(logged, "home-control:" + CLIENT)
        rendered = json.dumps([view, detail], ensure_ascii=False)
        for leaked in (
            "synthetic-ha-token",
            "http://127.0.0.1",
            "Authorization",
            "token_env",
            "base_url",
            "credential",
            "lease_expires_at",
            "expected_revision",
            "owner",
            "digest",
        ):
            with self.subTest(leak=leaked):
                self.assertNotIn(leaked, rendered)
        self.assertEqual(set(detail["task"]), SUMMARY_FIELDS | {"request", "timeline"})
        self.assertEqual(
            set(detail["task"]["request"]),
            {"template_id", "entity_id", "service", "requested_state"},
        )

    async def test_unknown_task_identifiers_are_real_errors(self):
        logged = await self.unlocked_login()
        self.assertEqual(
            (await self.detail(logged, "home-control:" + FIFTH, expected=404))["code"], "not_found"
        )
        self.assertEqual(
            (await self.detail(logged, "model-version:native:4", expected=404))["code"], "not_found"
        )
        self.assertEqual(
            (await self.detail(logged, "model-intent:" + FIFTH, expected=404))["code"], "not_found"
        )
        for body in (
            {"task_id": "nonsense"},
            {"task_id": ""},
            {"task_id": "home-control:not-a-uuid"},
            {"task_id": "home-control:" + CLIENT + "0"},
            {"task_id": "model-version:chat:0"},
            {"task_id": "model-version:chat:abc"},
            {"task_id": "model-version:unknown:1"},
            {"task_id": "model-intent:not-a-uuid"},
            {"task_id": 7},
            {"task_id": "home-control:" + CLIENT, "extra": 1},
            {},
        ):
            with self.subTest(body=body):
                expected = 404 if body.get("task_id") == "nonsense" else 400
                refusal = await self.call("tasks/detail", body, logged["csrf"], expected)
                self.assertEqual(
                    refusal["code"], "not_found" if expected == 404 else "invalid_input"
                )


class TaskWatermarkTests(unittest.TestCase):
    def test_global_watermark_seeks_an_uncontributed_stream_before_limiting(self):
        from types import SimpleNamespace
        from services.platform.tasks import Tasks

        records = {
            "platform.home": [
                {"task_id": "home-control:" + str(i), "created_at": i} for i in range(10, 15)
            ],
            "platform.models": [
                {"task_id": "model-version:config:" + str(i), "created_at": i} for i in range(1, 6)
            ],
        }
        tasks = Tasks(SimpleNamespace(models=SimpleNamespace(clock=lambda: 100)), None)

        def page(name, after, size, states):
            ordered = sorted(records[name], key=tasks._position, reverse=True)
            selected = [row for row in ordered if after is None or tasks._position(row) < after]
            return selected[:size], len(selected) > size

        with (
            patch.object(tasks, "_page", side_effect=page),
            patch.object(tasks, "_task", side_effect=lambda row: row),
            patch.object(tasks, "_summary", side_effect=lambda row: row),
            patch.object(tasks, "_sources", return_value=[]),
        ):
            first = tasks.view({"page_size": 2}, {})
            records["platform.models"].extend(
                {"task_id": "model-version:config:" + str(i), "created_at": i}
                for i in range(50, 65)
            )
            cursor = first["page"]["next_cursor"]
            walked = [row["task_id"] for row in first["items"]]
            while cursor:
                following = tasks.view({"page_size": 2, "cursor": cursor}, {})
                walked.extend(row["task_id"] for row in following["items"])
                cursor = following["page"]["next_cursor"]
            expected = [
                row["task_id"]
                for row in sorted(
                    [
                        row
                        for stream in records.values()
                        for row in stream
                        if row["created_at"] < 50
                    ],
                    key=tasks._position,
                    reverse=True,
                )
            ]
            self.assertEqual(walked, expected)

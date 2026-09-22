"""HA connector: registered readings, explicitly authorized control, durable recovery.

The browser is treated as untrusted input: it may only name a registered action template, the
reading revision it actually saw and a client id. The HA address, the credential variable and
every entity/service pair come from deployment settings.

Reading and controlling are separate facts (A10): a service-call receipt is an acceptance, an
observation is evidence, and an outcome that cannot be established stays unknown and is never
re-sent on its own. A claimed intent whose receipt was lost reconciles by observation first, so
recovery can never double a command the device already carries.
"""

import asyncio
import copy
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import aiohttp

from fixtures import ENV, start_http
from home_fixtures import ENV as HOME_ENV
from home_fixtures import HOME_KEY, home_app, reserve, start_home
from services.platform.contracts import Fault, utc
from services.platform.home import Home
from services.platform.server import create_app
from services.platform.service import Platform
from services.platform.web_console import WebConsole
from web_fixtures import PASSWORD, home_settings

CLIENT = "11111111-2222-3333-4444-555555555555"
OTHER = "22222222-3333-4444-5555-666666666666"
THIRD = "33333333-4444-5555-6666-777777777777"


class HomeTests(unittest.IsolatedAsyncioTestCase):
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

    def variant(self, name, base_url=None, mutate=None):
        directory = Path(self.temp.name) / name
        directory.mkdir(parents=True, exist_ok=True)
        config = home_settings(str(directory), base_url or self.ha_url)
        if mutate:
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

    async def view(self, logged, client=None):
        return await self.call("home/view", {}, logged["csrf"], session=client)

    async def refresh(self, logged, client=None):
        return await self.call("home/refresh", {}, logged["csrf"], session=client)

    async def control(
        self,
        logged,
        template,
        revision,
        client_id=CLIENT,
        expected=200,
        session=None,
        **extra,
    ):
        body = {"template_id": template, "expected_revision": revision, "client_id": client_id}
        if extra.pop("drop", None):
            body.pop("expected_revision")
        body.update(extra)
        return await self.call("home/control", body, logged["csrf"], expected, session=session)

    def entity(self, view, label):
        return next(item for item in view["entities"] if item["label"] == label)

    def control_of(self, view, client_id=CLIENT):
        return next(item for item in view["controls"] if item["control_id"] == client_id)

    def calls(self, method=None):
        return [
            row
            for row in self.ha.log
            if (method is None or row["method"] == method)
            and row["path"].startswith(("/api/states", "/api/services"))
        ]

    def services(self):
        return self.calls("POST")

    def intents(self):
        with closing(sqlite3.connect(self.platform.store.path + ".home-controls.sqlite")) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM control_intents")]

    def intent(self, client_id=CLIENT):
        return next(
            (row for row in self.intents() if row["client_id"] == client_id),
            None,
        )

    def age_reading(self, entity_id, seconds):
        """Only the sample time moves: the stored reading itself stays exactly as observed."""
        observed_at = utc(self.platform.models.clock() - seconds)
        with closing(sqlite3.connect(self.platform.store.path + ".home-controls.sqlite")) as db:
            db.execute(
                "UPDATE readings SET observed_at=? WHERE entity_id=?",
                (observed_at, entity_id),
            )
            db.commit()
        return observed_at

    def reject(self, code, method, *args, **kwargs):
        with self.assertRaises(Fault) as found:
            method(*args, **kwargs)
        self.assertEqual(found.exception.code, code)
        return found.exception

    async def until(self, condition, timeout=8.0):
        """Wait for a server-side effect; the connector keeps working after a client goes away."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if condition():
                return True
            await asyncio.sleep(0.05)
        self.fail("condition was not reached in time")

    async def release_home(self):
        async with self.client.post(self.ha_url + "/fixture/release", json={}) as response:
            self.assertEqual(response.status, 200)

    def expire_lease(self, client_id=CLIENT):
        """The owner is gone: its durable claim is past its lease and may be taken over."""
        with closing(sqlite3.connect(self.platform.store.path + ".home-controls.sqlite")) as db:
            db.execute(
                "UPDATE control_intents SET lease_expires_at=0 WHERE client_id=?", (client_id,)
            )
            db.commit()

    async def transmit_then_die(self, logged, template, revision, client_id=CLIENT, session=None):
        """Leave behind exactly what an owner that died mid-command leaves behind.

        The command has been transmitted and the durable record says so, while the fixture keeps
        holding that very request: whatever it does later is a late result nobody is waiting for.
        """
        self.ha.service_mode = "hold"
        task = asyncio.create_task(
            self.control(logged, template, revision, client_id=client_id, session=session)
        )
        try:
            await self.until(lambda: bool(self.services()))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            self.ha.service_mode = None
        self.assertEqual(self.intent(client_id)["state"], "sending")
        self.assertTrue(self.intent(client_id)["owner"])
        self.expire_lease(client_id)
        return task

    # ------------------------------------------------------------- reading

    async def test_registered_readings_are_real_observations_with_sample_times(self):
        logged = await self.login()
        view = await self.refresh(logged)
        light = self.entity(view, "书房灯")
        self.assertEqual(
            (light["availability"], light["state"], light["code"]), ("current", "off", "ok")
        )
        self.assertTrue(light["observed_at"].endswith("Z"))
        self.assertEqual(light["attempt_at"], light["observed_at"])
        self.assertTrue(light["control"])
        self.assertEqual(
            sorted(item["template_id"] for item in light["templates"]),
            ["study-light-off", "study-light-on"],
        )
        sensor = self.entity(view, "客厅温度")
        self.assertEqual(
            (sensor["availability"], sensor["value"], sensor["unit"]), ("current", 23.5, "°C")
        )
        # A read-only sensor never offers an action, whatever the registry contains.
        self.assertFalse(sensor["control"])
        self.assertEqual(sensor["templates"], [])
        self.assertEqual(view["connector"]["entities"], 3)
        self.assertEqual(len(self.calls("GET")), 3)
        # Reading is not control: a chat login alone still reports states.
        self.assertEqual(view["connector"]["code"], "control_required")
        self.assertEqual(view["connector"]["unlocked"], False)
        self.assertEqual(self.services(), [])

    async def test_unavailable_and_unknown_are_never_reported_as_off_or_zero(self):
        logged = await self.login()
        for entity_id, state in (
            ("light.study", "unavailable"),
            ("sensor.living_temperature", "unknown"),
        ):
            async with self.client.post(
                self.ha_url + "/fixture/state",
                json={"entity_id": entity_id, "state": state},
            ) as response:
                self.assertEqual(response.status, 200)
        view = await self.refresh(logged)
        light = self.entity(view, "书房灯")
        self.assertEqual(
            (light["availability"], light["state"], light["code"]),
            ("unavailable", None, "unavailable"),
        )
        sensor = self.entity(view, "客厅温度")
        self.assertEqual((sensor["availability"], sensor["value"]), ("unknown", None))
        # Neither an "off" switch nor a 0 reading may be manufactured from a missing state.
        self.assertNotEqual(light["state"], "off")
        self.assertIsNone(sensor["value"])

    async def test_an_unrecognized_switch_state_is_not_shown_as_off(self):
        self.ha.mode = "malformed"
        logged = await self.login()
        view = await self.refresh(logged)
        light = self.entity(view, "书房灯")
        self.assertEqual(
            (light["availability"], light["state"], light["code"]),
            ("unknown", None, "device_malformed_state"),
        )
        self.ha.mode = "normal"

    async def test_an_expired_reading_keeps_its_own_sample_time(self):
        logged = await self.login()
        first = await self.refresh(logged)
        revision = self.entity(first, "书房灯")["revision"]
        aged = self.age_reading("light.study", 600)
        view = await self.view(logged)
        light = self.entity(view, "书房灯")
        self.assertEqual(light["availability"], "stale")
        self.assertEqual(light["state"], "off")
        self.assertEqual(light["observed_at"], aged)
        self.assertEqual(light["revision"], revision)
        self.assertEqual(view["connector"]["stale_after_seconds"], 120)
        # Expiry is a display fact, not a new observation: nothing was re-read.
        self.assertEqual(len(self.calls("GET")), 3)

    async def test_an_unreachable_home_assistant_is_offline_and_recovers(self):
        # A real loopback address nobody serves: no fake status, no invented reading. Windows
        # drops the SYN instead of refusing it, so the honest code is timeout or unavailable.
        port = reserve()
        await self.start(self.variant("offline", f"http://127.0.0.1:{port}"))
        logged = await self.login()
        view = await self.refresh(logged)
        for label in ("书房灯", "热水壶", "客厅温度"):
            item = self.entity(view, label)
            self.assertEqual(item["availability"], "offline")
            self.assertIn(item["code"], {"device_timeout", "device_unavailable"})
            self.assertIsNone(item["state"])
            self.assertIsNone(item["value"])
            self.assertIsNone(item["observed_at"])
        self.assertEqual(view["connector"]["code"], "control_required")
        # Home Assistant comes up on exactly that address and the next read is current again.
        self.ha_runner, url = await start_home(home_app(), port)
        self.addAsyncCleanup(self.ha_runner.cleanup)
        self.assertEqual(url, f"http://127.0.0.1:{port}")
        recovered = await self.refresh(logged)
        self.assertEqual(self.entity(recovered, "书房灯")["availability"], "current")
        self.assertEqual(self.entity(recovered, "书房灯")["state"], "off")

    async def test_a_failed_read_keeps_the_last_reading_labeled_offline(self):
        logged = await self.login()
        first = await self.refresh(logged)
        observed_at = self.entity(first, "书房灯")["observed_at"]
        self.ha.mode = "server_error"
        view = await self.refresh(logged)
        light = self.entity(view, "书房灯")
        self.assertEqual(light["availability"], "offline")
        self.assertEqual(light["code"], "device_invalid_response")
        # The last known value stays visible, marked by availability and by its own sample
        # time: the failed attempt has its own, later timestamp.
        self.assertEqual(light["state"], "off")
        self.assertEqual(light["observed_at"], observed_at)
        self.assertNotEqual(light["attempt_at"], observed_at)
        self.assertEqual(light["revision"], self.entity(first, "书房灯")["revision"])
        self.ha.mode = "normal"
        self.assertEqual(
            self.entity(await self.refresh(logged), "书房灯")["availability"], "current"
        )

    async def test_readings_survive_a_restart_and_stay_marked_when_they_expire(self):
        logged = await self.login()
        first = await self.refresh(logged)
        observed_at = self.entity(first, "书房灯")["observed_at"]
        # A new process on the same store: the reading is not silently replaced by "unknown".
        await self.start(self.config)
        logged = await self.login()
        view = await self.view(logged)
        light = self.entity(view, "书房灯")
        self.assertEqual((light["availability"], light["state"]), ("current", "off"))
        self.assertEqual(light["observed_at"], observed_at)
        self.age_reading("light.study", 600)
        expired = self.entity(await self.view(logged), "书房灯")
        self.assertEqual((expired["availability"], expired["state"]), ("stale", "off"))

    async def test_redirects_are_refused_and_never_followed(self):
        self.ha.mode = "redirect"
        self.ha.redirect_target = "/login"
        logged = await self.login()
        view = await self.refresh(logged)
        self.assertEqual(self.entity(view, "书房灯")["code"], "device_redirect")
        self.assertEqual(self.entity(view, "书房灯")["availability"], "offline")
        # The redirect target was never requested by the connector.
        self.assertEqual([row for row in self.ha.log if row["path"] == "/login"], [])
        self.ha.mode = "normal"

    async def test_unreadable_oversized_or_mismatched_payloads_are_refused(self):
        logged = await self.login()
        for mode in ("unreadable", "oversized", "wrong_entity"):
            with self.subTest(mode=mode):
                self.ha.mode = mode
                view = await self.refresh(logged)
                light = self.entity(view, "书房灯")
                self.assertEqual(
                    (light["availability"], light["code"]), ("offline", "device_invalid_response")
                )
                self.assertIsNone(light["state"])
        self.ha.mode = "normal"

    async def test_a_missing_credential_is_reported_instead_of_showing_offline_devices(self):
        logged = await self.unlocked_login()
        with patch.dict(os.environ, {"TS017_HA_TOKEN": ""}):
            self.assertEqual(
                (await self.call("home/refresh", {}, logged["csrf"], 503))["code"],
                "device_credential_missing",
            )
            self.assertEqual(
                (await self.control(logged, "study-light-on", 0, expected=503))["code"],
                "device_credential_missing",
            )
        self.assertEqual(self.services(), [])

    # ------------------------------------------------------------- control

    async def test_control_needs_its_own_explicit_unlock_and_never_model_authority(self):
        logged = await self.login()
        view = await self.view(logged)
        self.assertEqual(view["connector"]["code"], "control_required")
        self.assertTrue(view["connector"]["control_available"])
        self.assertFalse(view["connector"]["unlocked"])
        self.assertEqual(
            (await self.control(logged, "study-light-on", 0, expected=403))["code"],
            "control_required",
        )
        self.assertEqual(self.services(), [])
        # Unlocking model management is a different authority and does not open this one.
        await self.call("models/unlock", {"password": PASSWORD}, logged["csrf"])
        self.assertEqual(
            (await self.control(logged, "study-light-on", 0, expected=403))["code"],
            "control_required",
        )
        unlocked = await self.call("home/unlock", {"password": PASSWORD}, logged["csrf"])
        self.assertEqual(unlocked["code"], "ready")
        self.assertEqual(self.services(), [])
        view = await self.view(logged)
        self.assertEqual(view["connector"]["code"], "ready")
        self.assertTrue(view["connector"]["unlocked"])
        self.assertFalse((await self.call("home/lock", {}, logged["csrf"]))["unlocked"])
        self.assertEqual(
            (await self.control(logged, "study-light-on", 0, expected=403))["code"],
            "control_required",
        )
        self.assertEqual(self.services(), [])

    async def test_unlock_requires_the_real_password_and_shares_the_login_budget(self):
        logged = await self.login()
        await self.call("home/unlock", {"password": "incorrect-password-017"}, logged["csrf"], 401)
        await self.call("home/unlock", {}, logged["csrf"], 400)
        await self.call("home/unlock", {"password": PASSWORD, "device": True}, logged["csrf"], 400)
        for _ in range(4):
            await self.call(
                "home/unlock", {"password": "incorrect-password-017"}, logged["csrf"], 401
            )
        await self.call("home/unlock", {"password": PASSWORD}, logged["csrf"], 429)
        await self.call(
            "login", {"username": "synthetic-admin", "password": PASSWORD}, logged["csrf"], 429
        )
        self.assertEqual((await self.view(logged))["connector"]["unlocked"], False)
        self.assertEqual(self.services(), [])

    async def test_an_accepted_control_is_an_acceptance_not_an_execution(self):
        # HA answers 200 and even names the target state, but the device never moves.
        self.ha.mode = "unapplied"
        logged = await self.unlocked_login()
        await self.refresh(logged)
        revision = self.entity(await self.view(logged), "书房灯")["revision"]
        result = await self.control(logged, "study-light-on", revision)
        self.assertEqual(result["acceptance"], "accepted")
        self.assertEqual(result["requested_state"], "on")
        self.assertTrue(result["target_reported"])
        self.assertEqual(result["observation"]["status"], "pending")
        self.assertEqual(result["code"], "ok")
        # Only a later observation can contradict it, and it is reported as a separate fact.
        view = await self.refresh(logged)
        self.assertEqual(self.entity(view, "书房灯")["state"], "off")
        recorded = self.control_of(view)
        self.assertEqual(recorded["acceptance"], "accepted")
        self.assertEqual(recorded["observation"]["status"], "contradicted")
        self.assertEqual(recorded["observation"]["state"], "off")
        self.ha.mode = "normal"

    async def test_a_later_observation_confirms_the_requested_state(self):
        logged = await self.unlocked_login()
        await self.refresh(logged)
        revision = self.entity(await self.view(logged), "书房灯")["revision"]
        result = await self.control(logged, "study-light-on", revision)
        self.assertEqual(
            (result["acceptance"], result["observation"]["status"]), ("accepted", "pending")
        )
        view = await self.refresh(logged)
        recorded = self.control_of(view)
        self.assertEqual(recorded["observation"]["status"], "confirmed")
        self.assertEqual(recorded["observation"]["state"], "on")
        self.assertEqual(self.entity(view, "书房灯")["state"], "on")
        self.assertEqual(len(self.services()), 1)

    async def test_two_browsers_with_a_stale_revision_conflict_instead_of_overwriting(self):
        self.other = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.addAsyncCleanup(self.other.close)
        first = await self.unlocked_login()
        second = await self.unlocked_login(self.other)
        await self.refresh(first)
        await self.refresh(second, self.other)
        revision = self.entity(await self.view(first), "书房灯")["revision"]
        self.assertEqual(
            self.entity(await self.view(second, self.other), "书房灯")["revision"], revision
        )
        self.assertEqual(
            (await self.control(first, "study-light-on", revision))["acceptance"], "accepted"
        )
        observed = await self.refresh(first)
        self.assertEqual(self.entity(observed, "书房灯")["revision"], revision + 1)
        # The second browser still holds the reading it saw before the light changed.
        stale = await self.control(
            second, "study-light-off", revision, client_id=OTHER, expected=409, session=self.other
        )
        self.assertEqual(stale["code"], "state_conflict")
        self.assertEqual(len(self.services()), 1)
        self.assertIsNone(self.intent(OTHER))
        # Re-reading is the only way forward, and then a new intent is a new control.
        reread = await self.view(second, self.other)
        current = self.entity(reread, "书房灯")["revision"]
        self.assertEqual(current, revision + 1)
        accepted = await self.control(
            second, "study-light-off", current, client_id=THIRD, session=self.other
        )
        self.assertEqual(accepted["acceptance"], "accepted")
        self.assertEqual(len(self.services()), 2)

    async def test_an_unknown_outcome_is_recorded_and_never_resent(self):
        self.ha.mode = "slow"
        self.ha.delay = 3.0
        logged = await self.unlocked_login()
        await self.refresh(logged)
        revision = self.entity(await self.view(logged), "书房灯")["revision"]
        first = await self.control(logged, "study-light-on", revision, expected=504)
        self.assertEqual(first["code"], "device_timeout")
        self.assertEqual(len(self.services()), 1)
        self.assertEqual(self.intent()["state"], "unknown")
        self.assertEqual(self.intent()["code"], "device_timeout")
        # The same client id replays the unknown outcome; it never becomes a second command.
        replay = await self.control(logged, "study-light-on", revision, expected=504)
        self.assertEqual(replay["code"], "device_timeout")
        self.assertEqual(len(self.services()), 1)
        view = await self.view(logged)
        recorded = self.control_of(view)
        self.assertEqual((recorded["acceptance"], recorded["code"]), ("unknown", "device_timeout"))
        self.assertEqual(recorded["observation"]["status"], "unknown")
        self.ha.mode = "normal"

    async def test_a_lost_receipt_recovers_by_observation_without_a_second_command(self):
        logged = await self.unlocked_login()
        await self.refresh(logged)
        revision = self.entity(await self.view(logged), "书房灯")["revision"]
        # The durable receipt write is lost after HA already accepted the command.
        with patch.object(Home, "_record", lambda self, *args: False):
            first = await self.control(logged, "study-light-on", revision)
        self.assertEqual(first["acceptance"], "accepted")
        self.assertFalse(first["durable"])
        self.assertEqual(len(self.services()), 1)
        # The durable state still says a command was transmitted, so it is never sent again.
        self.assertEqual(self.intent()["state"], "sending")
        self.expire_lease()
        retry = await self.control(logged, "study-light-on", revision)
        self.assertEqual(retry["acceptance"], "observed")
        self.assertEqual(retry["code"], "recovered_at_target")
        self.assertEqual(len(self.services()), 1)
        self.assertEqual(self.intent()["state"], "observed")

    async def test_an_interruption_before_the_command_issues_it_exactly_once(self):
        logged = await self.unlocked_login()
        await self.refresh(logged)
        revision = self.entity(await self.view(logged), "书房灯")["revision"]
        # The claim is durable, then the process dies before anything is transmitted.
        with patch.object(Home, "_observe", side_effect=Fault("device_timeout", 504)):
            interrupted = await self.control(logged, "study-light-on", revision, expected=504)
        self.assertEqual(interrupted["code"], "device_timeout")
        self.assertEqual(self.services(), [])
        # Nothing was transmitted, so the claim is given back instead of blocking the intent.
        self.assertEqual(self.intent()["state"], "prepared")
        retried = await self.control(logged, "study-light-on", revision)
        self.assertEqual(retried["acceptance"], "accepted")
        self.assertEqual(len(self.services()), 1)
        self.assertEqual(len(self.intents()), 1)
        replayed = await self.control(logged, "study-light-on", revision)
        self.assertEqual((replayed["acceptance"], replayed["deduplicated"]), ("accepted", True))
        self.assertEqual(len(self.services()), 1)

    async def test_one_client_id_is_one_reviewed_intent(self):
        logged = await self.unlocked_login()
        await self.refresh(logged)
        revision = self.entity(await self.view(logged), "书房灯")["revision"]
        first = await self.control(logged, "study-light-on", revision)
        again = await self.control(logged, "study-light-on", revision)
        self.assertEqual(again["control_id"], first["control_id"])
        self.assertTrue(again["deduplicated"])
        self.assertEqual(len(self.services()), 1)
        conflict = await self.control(logged, "study-light-off", revision, expected=409)
        self.assertEqual(conflict["code"], "idempotency_conflict")
        self.assertEqual(len(self.services()), 1)
        # A stale revision with the same client id is a different intent, so it conflicts too.
        self.assertEqual(
            (await self.control(logged, "study-light-on", revision + 5, expected=409))["code"],
            "idempotency_conflict",
        )
        self.assertEqual(len(self.services()), 1)

    async def test_the_same_client_id_never_becomes_two_commands(self):
        """The coordinator's reproduction: two in-flight requests, one reviewed intent."""
        self.ha.service_mode = "hold"
        logged = await self.unlocked_login()
        await self.refresh(logged)
        revision = self.entity(await self.view(logged), "书房灯")["revision"]
        first = asyncio.create_task(self.control(logged, "study-light-on", revision))
        await self.until(lambda: bool(self.services()))
        self.assertEqual(self.intent()["state"], "sending")
        second = asyncio.create_task(self.control(logged, "study-light-on", revision))
        # The duplicate waits for the owner instead of observing and transmitting on its own.
        await asyncio.sleep(0.5)
        self.assertEqual(len(self.services()), 1)
        await self.release_home()
        results = await asyncio.gather(first, second)
        host, copy = results[0], results[1]
        self.assertEqual((host["acceptance"], host["deduplicated"]), ("accepted", False))
        self.assertEqual(copy["control_id"], host["control_id"])
        self.assertEqual((copy["acceptance"], copy["deduplicated"]), ("accepted", True))
        self.assertEqual(len(self.services()), 1)
        self.assertEqual(len(self.intents()), 1)

    async def test_two_instances_sharing_the_ledger_still_send_one_command(self):
        """Execution ownership is durable: a second process over the same ledger never sends."""
        self.ha.service_mode = "hold"
        logged = await self.unlocked_login()
        await self.refresh(logged)
        revision = self.entity(await self.view(logged), "书房灯")["revision"]
        first = asyncio.create_task(self.control(logged, "study-light-on", revision))
        await self.until(lambda: bool(self.services()))
        # A second platform instance over the same store, ledger and HA connection, with its own
        # settings copy so the first instance's origin and sessions stay untouched.
        await self.start(copy.deepcopy(self.config))
        other = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.addAsyncCleanup(other.close)
        second = await self.unlocked_login(other)
        duplicate = asyncio.create_task(
            self.control(second, "study-light-on", revision, session=other)
        )
        await asyncio.sleep(0.5)
        self.assertEqual(len(self.services()), 1)
        await self.release_home()
        host, replayed = await asyncio.gather(first, duplicate)
        self.assertEqual((host["acceptance"], replayed["acceptance"]), ("accepted", "accepted"))
        self.assertTrue(replayed["deduplicated"])
        self.assertEqual(len(self.services()), 1)

    async def test_a_transmitted_command_without_its_receipt_stays_unconfirmed(self):
        """The device not being at the target proves nothing: reconcile, never re-send."""
        logged = await self.unlocked_login()
        await self.refresh(logged)
        revision = self.entity(await self.view(logged), "书房灯")["revision"]
        await self.transmit_then_die(logged, "study-light-on", revision)
        # A later process finds `sending`, sees the device still off, and refuses to conclude it.
        await self.start(self.config)
        logged = await self.unlocked_login()
        retry = await self.control(logged, "study-light-on", revision, expected=503)
        self.assertEqual(retry["code"], "control_unverified")
        self.assertEqual(self.intent()["state"], "unknown")
        self.assertEqual(len(self.services()), 1)
        # Replaying that unknown outcome never becomes a second command either.
        self.assertEqual(
            (await self.control(logged, "study-light-on", revision, expected=503))["code"],
            "control_unverified",
        )
        self.assertEqual(len(self.services()), 1)
        # The late result lands afterwards; a receipt that arrives late still upgrades the record.
        await self.release_home()
        await self.until(lambda: self.intent()["state"] == "accepted")
        self.assertTrue(self.intent()["target_reported"])
        self.assertEqual(len(self.services()), 1)

    async def test_a_transmitted_command_whose_target_is_reached_is_concluded_by_observation(self):
        logged = await self.unlocked_login()
        await self.refresh(logged)
        revision = self.entity(await self.view(logged), "书房灯")["revision"]
        await self.transmit_then_die(logged, "study-light-on", revision)
        # The command that is still in flight took effect before anyone looked again.
        self.ha.states["light.study"]["state"] = "on"
        await self.start(self.config)
        logged = await self.unlocked_login()
        recovered = await self.control(logged, "study-light-on", revision)
        self.assertEqual(
            (recovered["acceptance"], recovered["code"]), ("observed", "recovered_at_target")
        )
        self.assertEqual(self.intent()["state"], "observed")
        self.assertEqual(len(self.services()), 1)
        self.assertEqual(
            (await self.control(logged, "study-light-on", revision))["deduplicated"], True
        )
        self.assertEqual(len(self.services()), 1)

    async def test_a_cancelled_server_task_never_leads_to_a_second_command(self):
        """Cancelling the work in flight leaves an owned claim and no second command."""
        logged = await self.unlocked_login()
        await self.refresh(logged)
        revision = self.entity(await self.view(logged), "书房灯")["revision"]
        await self.transmit_then_die(logged, "study-light-on", revision)
        # Nothing was answered as success, and the device has not moved.
        before = await self.view(logged)
        self.assertEqual(self.control_of(before)["acceptance"], "executing")
        self.assertEqual(self.entity(before, "书房灯")["state"], "off")
        # A new platform instance takes the expired claim over and concludes by observation.
        await self.start(self.config)
        logged = await self.unlocked_login()
        retry = await self.control(logged, "study-light-on", revision, expected=503)
        self.assertEqual(retry["code"], "control_unverified")
        self.assertEqual(len(self.services()), 1)
        await self.release_home()
        await self.until(lambda: self.ha.states["light.study"]["state"] == "on")
        view = await self.refresh(logged)
        self.assertEqual(self.entity(view, "书房灯")["state"], "on")
        self.assertEqual(len(self.services()), 1)

    async def test_an_expired_claim_can_be_taken_over_but_never_transmits_on_its_own(self):
        """An `observing` claim that died before transmitting may safely be run once."""
        logged = await self.unlocked_login()
        await self.refresh(logged)
        revision = self.entity(await self.view(logged), "书房灯")["revision"]
        # The owner dies inside the observation: the claim is held, nothing was transmitted.
        self.ha.read_mode = "hold"
        task = asyncio.create_task(self.control(logged, "study-light-on", revision))
        await self.until(lambda: self.intent() is not None)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.intent()["state"], "observing")
        self.assertEqual(self.services(), [])
        # The owner never comes back; after the lease expires a new request may take over.
        self.ha.read_mode = None
        self.expire_lease()
        accepted = await self.control(logged, "study-light-on", revision)
        self.assertEqual(accepted["acceptance"], "accepted")
        self.assertEqual(len(self.services()), 1)
        self.assertEqual(len(self.intents()), 1)

    async def test_losing_the_control_unlock_during_the_observation_stops_the_command(self):
        logged = await self.unlocked_login()
        await self.refresh(logged)
        revision = self.entity(await self.view(logged), "书房灯")["revision"]
        # The pre-send observation is asynchronous: the unlock is dropped while it is in flight.
        self.ha.read_mode = "hold"
        reads = len(self.calls("GET"))
        task = asyncio.create_task(self.control(logged, "study-light-on", revision, expected=403))
        await self.until(lambda: len(self.calls("GET")) > reads)
        await self.call("home/lock", {}, logged["csrf"])
        self.ha.read_mode = None
        await self.release_home()
        refused = await task
        self.assertEqual(refused["code"], "control_required")
        self.assertEqual(self.services(), [])
        # Nothing left this process, so the claim is given back instead of blocking the intent.
        self.assertEqual(self.intent()["state"], "prepared")
        self.assertEqual(self.intent()["owner"], None)

    async def test_locking_during_transmission_ledger_wait_sends_no_device_command(self):
        logged = await self.unlocked_login()
        entered, blocker = threading.Event(), []
        original = Home._transmit

        def held(home, intent, owner):
            connection = sqlite3.connect(
                home.ledger_path, isolation_level=None, check_same_thread=False
            )
            connection.execute("BEGIN IMMEDIATE")
            blocker.append(connection)
            entered.set()
            return original(home, intent, owner)

        with patch.object(Home, "_transmit", held):
            pending = asyncio.create_task(self.control(logged, "study-light-on", 0, expected=403))
            try:
                async with asyncio.timeout(3):
                    while not entered.is_set():
                        await asyncio.sleep(0.005)
                await self.call("home/lock", {}, logged["csrf"])
                blocker[0].rollback()
                self.assertEqual("control_required", (await pending)["code"])
                self.assertEqual([], self.services())
            finally:
                for connection in blocker:
                    connection.close()

    async def test_a_reading_that_changed_during_the_observation_refuses_to_act(self):
        logged = await self.unlocked_login()
        await self.refresh(logged)
        revision = self.entity(await self.view(logged), "书房灯")["revision"]
        # The reading the operator acted on is checked again after the observation, before the
        # command leaves: a device that dropped out meanwhile is never commanded blindly.
        self.ha.read_mode = "hold"
        reads = len(self.calls("GET"))
        task = asyncio.create_task(self.control(logged, "study-light-on", revision, expected=409))
        await self.until(lambda: len(self.calls("GET")) > reads)
        self.ha.states["light.study"]["state"] = "unavailable"
        self.ha.read_mode = None
        await self.release_home()
        conflict = await task
        self.assertEqual(conflict["code"], "state_conflict")
        self.assertEqual(self.services(), [])
        self.assertEqual(self.intent()["state"], "prepared")
        current = self.entity(await self.view(logged), "书房灯")
        self.assertEqual((current["availability"], current["code"]), ("unavailable", "unavailable"))
        self.assertGreater(current["revision"], revision)

    async def test_a_new_read_after_the_unlock_lapsed_is_required_before_acting(self):
        """The refusal above is not a dead end: re-reading and unlocking works again."""
        logged = await self.unlocked_login()
        await self.refresh(logged)
        revision = self.entity(await self.view(logged), "书房灯")["revision"]
        self.ha.read_mode = "hold"
        reads = len(self.calls("GET"))
        task = asyncio.create_task(self.control(logged, "study-light-on", revision, expected=403))
        await self.until(lambda: len(self.calls("GET")) > reads)
        await self.call("home/lock", {}, logged["csrf"])
        self.ha.read_mode = None
        await self.release_home()
        self.assertEqual((await task)["code"], "control_required")
        self.assertEqual(self.services(), [])
        await self.call("home/unlock", {"password": PASSWORD}, logged["csrf"])
        current = self.entity(await self.view(logged), "书房灯")["revision"]
        accepted = await self.control(logged, "study-light-on", current)
        self.assertEqual(accepted["acceptance"], "accepted")
        self.assertEqual(len(self.services()), 1)

    async def test_a_leased_transmitted_intent_reports_the_outcome_to_duplicates(self):
        """A duplicate request learns the recorded unknown outcome, and still never sends."""
        self.ha.service_mode = "slow"
        self.ha.delay = 3.0
        logged = await self.unlocked_login()
        await self.refresh(logged)
        revision = self.entity(await self.view(logged), "书房灯")["revision"]
        first = await self.control(logged, "study-light-on", revision, expected=504)
        self.assertEqual(first["code"], "device_timeout")
        self.assertEqual(self.intent()["state"], "unknown")
        self.assertEqual(len(self.services()), 1)
        self.assertEqual(
            (await self.control(logged, "study-light-on", revision, expected=504))["code"],
            "device_timeout",
        )
        self.assertEqual(len(self.services()), 1)
        self.ha.service_mode = None

    async def test_cancelling_before_the_receipt_leaves_no_false_success(self):
        self.ha.service_mode = "hold"
        logged = await self.unlocked_login()
        await self.refresh(logged)
        revision = self.entity(await self.view(logged), "书房灯")["revision"]
        task = asyncio.create_task(self.control(logged, "study-light-on", revision))
        await self.until(lambda: bool(self.services()))
        self.assertEqual(len(self.services()), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        # Cancelled before the receipt: the durable record says the command was transmitted,
        # nothing was answered as success, and the device has not moved.
        self.assertEqual(self.intent()["state"], "sending")
        before = await self.view(logged)
        self.assertEqual(self.control_of(before)["acceptance"], "executing")
        self.assertEqual(self.entity(before, "书房灯")["state"], "off")
        # HA completes the command it already received; only a later read can show that.
        await self.release_home()
        await self.until(lambda: self.intent()["state"] == "accepted")
        self.assertEqual(self.intent()["state"], "accepted")
        view = await self.refresh(logged)
        recorded = self.control_of(view)
        self.assertEqual(
            (recorded["acceptance"], recorded["observation"]["status"]), ("accepted", "confirmed")
        )
        self.assertEqual(self.entity(view, "书房灯")["state"], "on")
        # The same client id replays that outcome; it never issues a second command.
        replay = await self.control(logged, "study-light-on", revision)
        self.assertEqual((replay["acceptance"], replay["deduplicated"]), ("accepted", True))
        self.assertEqual(len(self.services()), 1)
        self.ha.service_mode = None

    async def test_an_explicit_new_request_is_possible_after_a_cancel(self):
        self.ha.service_mode = "hold"
        logged = await self.unlocked_login()
        await self.refresh(logged)
        revision = self.entity(await self.view(logged), "书房灯")["revision"]
        task = asyncio.create_task(self.control(logged, "study-light-on", revision))
        await self.until(lambda: bool(self.services()))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.intents()), 1)
        # The operator decides again: a fresh reviewed intent is the only way to repeat a
        # command, and it is their explicit choice rather than an automatic resend.
        await self.release_home()
        await self.until(lambda: self.ha.states["light.study"]["state"] == "on")
        self.ha.service_mode = None
        reread = await self.refresh(logged)
        current = self.entity(reread, "书房灯")["revision"]
        accepted = await self.control(logged, "study-light-off", current, client_id=OTHER)
        self.assertEqual(accepted["acceptance"], "accepted")
        self.assertEqual(len(self.intents()), 2)
        self.assertEqual(len(self.services()), 2)

    async def test_a_rejected_service_call_is_recorded_as_not_sent(self):
        logged = await self.unlocked_login()
        await self.refresh(logged)
        revision = self.entity(await self.view(logged), "书房灯")["revision"]
        # HA refuses the credential: the command was served by nobody and has no effect.
        self.ha.service_mode = "unauthorized"
        refused = await self.control(logged, "study-light-on", revision, expected=502)
        self.assertEqual(refused["code"], "device_unauthorized")
        self.assertEqual(self.intent()["state"], "rejected")
        self.assertEqual(len(self.services()), 1)
        again = await self.control(logged, "study-light-on", revision, expected=502)
        self.assertEqual(again["code"], "device_unauthorized")
        self.assertEqual(len(self.services()), 1)
        view = await self.view(logged)
        self.assertEqual(self.control_of(view)["acceptance"], "rejected")
        self.assertEqual(self.control_of(view)["observation"]["status"], "not_sent")
        self.ha.service_mode = None

    async def test_an_unreadable_receipt_is_unknown_and_never_resent(self):
        logged = await self.unlocked_login()
        await self.refresh(logged)
        revision = self.entity(await self.view(logged), "书房灯")["revision"]
        # A 200 whose body cannot be read: HA may well have executed the command.
        self.ha.service_mode = "unreadable"
        result = await self.control(logged, "study-light-on", revision, expected=502)
        self.assertEqual(result["code"], "receipt_unreadable")
        self.assertEqual(self.intent()["state"], "unknown")
        self.assertEqual(len(self.services()), 1)
        await self.control(logged, "study-light-on", revision, expected=502)
        self.assertEqual(len(self.services()), 1)
        self.ha.service_mode = None

    async def test_a_failure_before_the_command_leaves_the_claim_unsent(self):
        logged = await self.unlocked_login()
        await self.refresh(logged)
        revision = self.entity(await self.view(logged), "书房灯")["revision"]
        # HA answers 500 to everything, including the reconciling read: no command is sent.
        self.ha.mode = "server_error"
        failed = await self.control(logged, "study-light-on", revision, expected=502)
        self.assertEqual(failed["code"], "device_invalid_response")
        self.assertEqual(self.services(), [])
        self.assertEqual(self.intent()["state"], "prepared")
        self.ha.mode = "normal"
        accepted = await self.control(logged, "study-light-on", revision)
        self.assertEqual(accepted["acceptance"], "accepted")
        self.assertEqual(len(self.services()), 1)

    # ------------------------------------------------------------- boundaries

    async def test_the_browser_cannot_name_a_target_a_credential_or_an_extra_field(self):
        logged = await self.unlocked_login()
        await self.refresh(logged)
        revision = self.entity(await self.view(logged), "书房灯")["revision"]
        bodies = (
            {
                "template_id": "study-light-on",
                "expected_revision": revision,
                "client_id": CLIENT,
                "entity_id": "lock.front_door",
            },
            {
                "template_id": "study-light-on",
                "expected_revision": revision,
                "client_id": CLIENT,
                "service": "lock",
            },
            {
                "template_id": "study-light-on",
                "expected_revision": revision,
                "client_id": CLIENT,
                "base_url": "http://evil.invalid",
            },
            {
                "template_id": "study-light-on",
                "expected_revision": revision,
                "client_id": CLIENT,
                "token": "stolen",
            },
            {
                "template_id": "study-light-on",
                "expected_revision": revision,
                "client_id": CLIENT,
                "expected_state": "on",
            },
            {"template_id": "study-light-on", "client_id": CLIENT},
            {"template_id": "study-light-on", "expected_revision": -1, "client_id": CLIENT},
            {"template_id": "study-light-on", "expected_revision": "1", "client_id": CLIENT},
            {
                "template_id": "study-light-on",
                "expected_revision": revision,
                "client_id": "not-a-uuid",
            },
            {"template_id": "study-light-on", "expected_revision": revision},
            {"expected_revision": revision, "client_id": CLIENT},
        )
        for body in bodies:
            with self.subTest(body=sorted(body)):
                await self.call("home/control", body, logged["csrf"], 400)
        self.assertEqual(
            (await self.control(logged, "unregistered-template", revision, expected=404))["code"],
            "not_found",
        )
        # A registered sensor never has a template, and an unknown entity is not addressable.
        self.assertEqual(
            (await self.control(logged, "sensor.living_temperature", revision, expected=404))[
                "code"
            ],
            "not_found",
        )
        self.assertEqual(self.services(), [])

    async def test_no_credential_endpoint_or_entity_target_is_projected(self):
        logged = await self.unlocked_login()
        view = await self.refresh(logged)
        revision = self.entity(view, "书房灯")["revision"]
        result = await self.control(logged, "study-light-on", revision)
        again = await self.view(logged)
        rendered = json.dumps([view, result, again], ensure_ascii=False)
        for leaked in (
            self.ha_url,
            HOME_ENV["TS017_HA_TOKEN"],
            "TS017_HA_TOKEN",
            "Authorization",
            "Bearer ",
        ):
            with self.subTest(leak=leaked):
                self.assertNotIn(leaked, rendered)
        self.assertTrue(view["connector"]["readable"])
        self.assertEqual(
            sorted(self.entity(view, "书房灯")),
            [
                "attempt_at",
                "availability",
                "code",
                "control",
                "entity_id",
                "kind",
                "label",
                "observed_at",
                "revision",
                "state",
                "templates",
                "unit",
                "value",
            ],
        )

    async def test_anonymous_sessions_and_session_loss_close_every_home_port(self):
        _, anonymous = await self.current()
        bodies = {
            "home/view": {},
            "home/refresh": {},
            "home/unlock": {"password": PASSWORD},
            "home/lock": {},
            "home/control": {
                "template_id": "study-light-on",
                "expected_revision": 0,
                "client_id": CLIENT,
            },
        }
        for path, body in bodies.items():
            with self.subTest(path=path):
                await self.call(path, body, anonymous["csrf"], 401)
        logged = await self.unlocked_login()
        await self.call("home/view", {}, "not-the-csrf", 403)
        await self.call("home/view", {}, logged["csrf"], 403, Origin="http://evil.invalid")
        await self.call("home/view", {}, logged["csrf"], 403, Host="evil.invalid")
        await self.call("home/view", {}, logged["csrf"], 403, Authorization="Bearer synthetic")
        live = next(iter(self.console.sessions.values()))
        live["devices"] = 0
        await self.refresh(logged)
        self.assertEqual(
            (await self.control(logged, "study-light-on", 0, expected=403))["code"],
            "control_required",
        )
        live["expires"] = 0
        await self.call("home/view", {}, logged["csrf"], 401)
        self.assertEqual(self.services(), [])

    async def test_a_revoked_login_cannot_control_devices(self):
        logged = await self.unlocked_login()
        await self.refresh(logged)
        revision = self.entity(await self.view(logged), "书房灯")["revision"]
        with self.platform.store.connect(write=True) as db:
            db.execute("INSERT INTO revoked_principals VALUES('admin')")
        lost = await self.control(logged, "study-light-on", revision, expected=401)
        self.assertEqual(lost["code"], "unauthorized")
        self.assertEqual(self.services(), [])

    async def test_an_operator_without_device_authority_has_no_control_port(self):
        def strip(config):
            config["principals"]["admin"]["actions"].remove("device.control")

        await self.start(self.variant("unauthorized", mutate=strip))
        logged = await self.login()
        view = await self.refresh(logged)
        self.assertEqual(view["connector"]["code"], "operator_not_authorized")
        self.assertFalse(view["connector"]["control_available"])
        # Registered readings stay available: only the write port is closed.
        self.assertEqual(self.entity(view, "书房灯")["availability"], "current")
        self.assertEqual(
            (await self.call("home/unlock", {"password": PASSWORD}, logged["csrf"], 403))["code"],
            "operator_not_authorized",
        )
        await self.refresh(logged)
        revision = self.entity(await self.view(logged), "书房灯")["revision"]
        self.assertEqual(
            (await self.control(logged, "study-light-on", revision, expected=403))["code"],
            "operator_not_authorized",
        )
        self.assertEqual(self.services(), [])

    async def test_a_disabled_connector_reads_but_never_writes(self):
        await self.start(self.variant("readonly", mutate=lambda c: c["home"].update(enabled=False)))
        logged = await self.login()
        view = await self.refresh(logged)
        self.assertEqual(view["connector"]["code"], "control_disabled")
        self.assertFalse(view["connector"]["control_available"])
        self.assertEqual(self.entity(view, "书房灯")["availability"], "current")
        self.assertEqual(
            (await self.call("home/unlock", {"password": PASSWORD}, logged["csrf"], 403))["code"],
            "control_disabled",
        )
        self.assertEqual(
            (await self.control(logged, "study-light-on", 0, expected=403))["code"],
            "control_disabled",
        )
        self.assertEqual(self.services(), [])

    async def test_a_deployment_without_the_section_has_no_home_entry_at_all(self):
        config = self.variant("absent", mutate=lambda c: c.pop("home"))
        await self.start(config)
        logged = await self.login()
        for path, body in (
            ("home/view", {}),
            ("home/refresh", {}),
            ("home/unlock", {"password": PASSWORD}),
            ("home/lock", {}),
            (
                "home/control",
                {"template_id": "study-light-on", "expected_revision": 0, "client_id": CLIENT},
            ),
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    (await self.call(path, body, logged["csrf"], 403))["code"], "home_disabled"
                )
        self.assertFalse(os.path.exists(self.platform.store.path + ".home-controls.sqlite"))

    async def test_the_connector_adds_no_internal_contract_port(self):
        async with self.client.post(
            self.url + "/internal/v1/home/control",
            json={"template_id": "study-light-on"},
            headers={"Authorization": "Bearer synthetic", "Content-Type": "application/json"},
        ) as response:
            self.assertEqual(response.status, 404)

    async def test_no_console_means_no_home_port_and_no_ledger(self):
        config = self.variant("no-console", mutate=lambda c: c.pop("web"))
        platform = Platform(config)
        app = create_app(platform)
        runner, url = await start_http(app)
        self.addAsyncCleanup(runner.cleanup)
        async with self.client.post(
            url + "/api/web/home/view",
            json={},
            headers={"Origin": url, "X-CSRF-Token": "anonymous"},
        ) as response:
            self.assertEqual(response.status, 503)
            self.assertEqual((await response.json())["code"], "web_not_configured")
        self.assertFalse(os.path.exists(platform.store.path + ".home-controls.sqlite"))

    async def test_settings_are_reviewed_at_startup(self):
        cases = {
            "remote-cleartext": {"base_url": "http://192.0.2.10:8123"},
            "cleartext-not-reviewed": {"allow_private_http": False},
            "credentials-in-url": {"base_url": "http://user:pass@127.0.0.1:8123"},
            "path-in-url": {"base_url": "http://127.0.0.1:8123/api"},
            "query-in-url": {"base_url": "http://127.0.0.1:8123/?a=1"},
            "bad-scheme": {"base_url": "ftp://127.0.0.1:8123"},
            "unreviewed-address": {"reviewed_addresses": ["192.0.2.10"]},
            "token-env": {"token_env": "not-an-env-name"},
            "unlock-ttl": {"unlock_ttl_seconds": 10},
            "timeout": {"timeout_seconds": 0},
            "max-age": {"status_max_age_seconds": 5},
            "no-entities": {"entities": []},
            "duplicate-entity": {
                "entities": [
                    {"entity_id": "light.study", "label": "A", "kind": "light"},
                    {"entity_id": "light.study", "label": "B", "kind": "light"},
                ]
            },
            "lock-domain": {
                "entities": [{"entity_id": "lock.front_door", "label": "门锁", "kind": "lock"}]
            },
            "kind-mismatch": {
                "entities": [{"entity_id": "light.study", "label": "A", "kind": "switch"}]
            },
            "entity-shape": {
                "entities": [{"entity_id": "light.Study", "label": "A", "kind": "light"}]
            },
            "entity-extra": {
                "entities": [
                    {"entity_id": "light.study", "label": "A", "kind": "light", "armed": True}
                ]
            },
            "unit": {
                "entities": [
                    {"entity_id": "sensor.t", "label": "T", "kind": "sensor", "unit": "x" * 20}
                ]
            },
            "unknown-key": {"administrator": True},
            "enabled-type": {"enabled": "yes"},
            "template-on-missing-entity": {
                "templates": [
                    {
                        "template_id": "x",
                        "label": "X",
                        "entity_id": "light.missing",
                        "service": "turn_on",
                    }
                ]
            },
            "template-on-sensor": {
                "templates": [
                    {
                        "template_id": "x",
                        "label": "X",
                        "entity_id": "sensor.living_temperature",
                        "service": "turn_on",
                    }
                ]
            },
            "lock-service": {
                "templates": [
                    {
                        "template_id": "x",
                        "label": "X",
                        "entity_id": "light.study",
                        "service": "lock",
                    }
                ]
            },
            "toggle-service": {
                "templates": [
                    {
                        "template_id": "x",
                        "label": "X",
                        "entity_id": "light.study",
                        "service": "toggle",
                    }
                ]
            },
            "script-service": {
                "templates": [
                    {
                        "template_id": "x",
                        "label": "X",
                        "entity_id": "light.study",
                        "service": "turn_on_script",
                    }
                ]
            },
            "template-id": {
                "templates": [
                    {
                        "template_id": "not a valid id",
                        "label": "X",
                        "entity_id": "light.study",
                        "service": "turn_on",
                    }
                ]
            },
            "template-extra": {
                "templates": [
                    {
                        "template_id": "x",
                        "label": "X",
                        "entity_id": "light.study",
                        "service": "turn_on",
                        "data": {"brightness": 255},
                    }
                ]
            },
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                config = self.variant(name)
                config["home"].update(change)
                with self.assertRaises(Fault):
                    create_app(Platform(config))
        # A duplicate registration id is refused without reaching the platform at all.
        config = self.variant("duplicate-template")
        config["home"]["templates"].append(dict(config["home"]["templates"][0]))
        with self.assertRaises(Fault):
            create_app(Platform(config))
        config = self.variant("too-many-entities")
        config["home"]["entities"] = [
            {"entity_id": f"switch.synthetic_{index}", "label": f"设备 {index}", "kind": "switch"}
            for index in range(33)
        ]
        with self.assertRaises(Fault):
            create_app(Platform(config))
        config = self.variant("missing-section-keys")
        config["home"].pop("token_env")
        with self.assertRaises(Fault):
            create_app(Platform(config))

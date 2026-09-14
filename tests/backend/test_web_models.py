"""Console model management: templates, gated writes, versions, replay and revocation.

The browser is treated as untrusted input here: it may only name a registered template,
the version it last read and a client id. Provider endpoints, credential references and
the validity window come from deployment settings, and every projection is redacted.

The authoritative configuration store and the console ledger are separate databases, so a
publication can commit while its receipt is lost (I/O failure or process death between the
two writes). Those windows are exercised explicitly: a committed request must recover as the
same result, must never be reported as a false failure, and must never create a new version.
"""

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import aiohttp

from fixtures import (
    ENV,
    NATIVE_PROVIDER,
    TOKENS,
    bearer,
    config as chat_config,
    native_config,
    native_query,
    query,
    start_http,
)
from services.platform.contracts import Fault, loads
from services.platform.server import create_app
from services.platform.service import Platform
from services.platform.web_console import WebConsole
from services.platform.web_models import WebModels
from web_fixtures import CHAT_PROVIDER, PASSWORD, web_settings

CHAT_TEMPLATE = "chat-local-text"
NATIVE_TEMPLATE = "native-local-text"
CLIENT = "11111111-2222-3333-4444-555555555555"
TARGETS = {"chat": ("configs", CHAT_TEMPLATE), "native": ("native_configs", NATIVE_TEMPLATE)}


def client_for(target):
    """Distinct well-formed client ids per target, so each intent is its own request."""
    return ("%08d" % (1 if target == "chat" else 2)) + "-1111-2222-3333-444444444444"


class WebModelsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)
        self.other = None
        await self.start(web_settings(self.temp.name))
        self.client = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.addAsyncCleanup(self.client.close)

    def variant(self, name, mutate=None):
        directory = Path(self.temp.name) / name
        directory.mkdir(parents=True)
        config = web_settings(str(directory))
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
        # The published tests bind the console to the actual ephemeral port the same way.
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
        """One chat login plus the explicit unlock: the two authorities stay separate."""
        logged = await self.login(client)
        await self.call("models/unlock", {"password": PASSWORD}, logged["csrf"], session=client)
        return logged

    async def view(self, logged, client=None):
        return await self.call("models/view", {}, logged["csrf"], session=client)

    async def publish(
        self, logged, template, expected, client_id=CLIENT, expected_status=200, session=None
    ):
        return await self.call(
            "models/publish",
            {"template_id": template, "expected_version": expected, "client_id": client_id},
            logged["csrf"],
            expected_status,
            session=session,
        )

    def target(self, view, name):
        return next(item for item in view["targets"] if item["target"] == name)

    def rows(self, table):
        with self.platform.store.connect() as db:
            return [
                dict(row)
                for row in db.execute(f"SELECT version,revoked FROM {table} ORDER BY version")
            ]

    def versions(self, table):
        return [row["version"] for row in self.rows(table)]

    def reject(self, code, method, *args, **kwargs):
        with self.assertRaises(Fault) as found:
            method(*args, **kwargs)
        self.assertEqual(found.exception.code, code)
        return found.exception

    def intent(self, client_id=CLIENT):
        with closing(sqlite3.connect(self.platform.store.path + ".web-models.sqlite")) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM publication_intents WHERE client_id=?", (client_id,)
            ).fetchone()
        return dict(row) if row else None

    def lose_receipts(self):
        """Reproduce the interruption window: claim and publish, but never settle the receipt."""
        return patch.object(WebModels, "_record", lambda self, intent, result: None)

    async def assert_recovered(self, logged, target, template, client_id, version):
        """Retry the same request with the same session and require the committed result."""
        replay = await self.publish(
            logged,
            template,
            None if version == 1 else version - 1,
            client_id=client_id,
            expected_status=200,
        )
        self.assertEqual(replay["version"], version)
        self.assertEqual(replay["state"], "replayed")
        self.assertEqual(replay["target"], target)
        self.assertEqual(self.versions(TARGETS[target][0]), [version])
        return replay

    async def test_lost_receipt_still_recovers_the_committed_publication(self):
        # Both targets: the receipt is lost, the authoritative publication stays recoverable.
        logged = await self.unlocked_login()
        for target, (table, template) in TARGETS.items():
            with self.subTest(target=target):
                client_id = client_for(target)
                with self.lose_receipts():
                    first = await self.publish(
                        logged, template, None, client_id=client_id, expected_status=200
                    )
                self.assertEqual(first["state"], "published")
                self.assertEqual(first["version"], 1)
                self.assertEqual(self.versions(table), [1])
                # The crash window leaves exactly this durable state: claimed, not settled.
                self.assertEqual(self.intent(client_id)["state"], "prepared")
                self.assertIsNone(self.intent(client_id)["result"])
                await self.assert_recovered(logged, target, template, client_id, 1)

    async def test_receipt_write_failure_is_never_displayed_as_a_failed_publish(self):
        # An I/O failure on the receipt must not contradict the authoritative state.
        logged = await self.unlocked_login()

        def broken(self, intent, result):
            raise OSError("simulated ledger failure after the authoritative commit")

        with patch.object(WebModels, "_record", broken):
            result = await self.publish(logged, NATIVE_TEMPLATE, None)
        self.assertEqual((result["state"], result["version"]), ("published", 1))
        self.assertEqual(self.versions("native_configs"), [1])
        view = await self.view(logged)
        self.assertEqual(self.target(view, "native")["current_version"], 1)
        await self.assert_recovered(logged, "native", NATIVE_TEMPLATE, CLIENT, 1)

    async def test_restart_between_commit_and_receipt_recovers_the_same_version(self):
        logged = await self.unlocked_login()
        with self.lose_receipts():
            first = await self.publish(logged, CHAT_TEMPLATE, None)
        self.assertEqual(first["version"], 1)
        # A new process on the same databases: in-memory state is gone, the intent is not.
        await self.start(self.config)
        logged = await self.unlocked_login()
        await self.assert_recovered(logged, "chat", CHAT_TEMPLATE, CLIENT, 1)

    async def test_interruption_before_the_authoritative_commit_publishes_exactly_once(self):
        logged = await self.unlocked_login()
        models = type(self.platform.models)
        for target, (table, template) in TARGETS.items():
            with self.subTest(target=target):
                client_id = client_for(target)
                operation = "native_publish" if target == "native" else "publish"
                with patch.object(
                    models, operation, side_effect=Fault("dependency_unavailable", 503)
                ):
                    failed = await self.publish(
                        logged, template, None, client_id=client_id, expected_status=503
                    )
                self.assertEqual(failed["code"], "dependency_unavailable")
                # The failure display matches reality: nothing was published.
                self.assertEqual(self.versions(table), [])
                self.assertEqual(self.intent(client_id)["state"], "prepared")
                retried = await self.publish(logged, template, None, client_id=client_id)
                self.assertEqual((retried["state"], retried["version"]), ("published", 1))
                self.assertEqual(self.versions(table), [1])
                again = await self.publish(logged, template, None, client_id=client_id)
                self.assertEqual((again["state"], again["version"]), ("replayed", 1))
                self.assertEqual(self.versions(table), [1])

    async def test_receipt_without_its_publication_is_never_reported_as_success(self):
        logged = await self.unlocked_login()
        published = await self.publish(logged, CHAT_TEMPLATE, None)
        self.assertEqual(published["version"], 1)
        # An authoritative restore that lost the publication must not be answered as success
        # and must not be answered as a plain conflict either.
        with self.platform.store.connect(write=True) as db:
            db.execute("DELETE FROM configs WHERE version=1")
        refused = await self.publish(logged, CHAT_TEMPLATE, None, expected_status=503)
        self.assertEqual(refused["code"], "publication_unverified")
        self.assertEqual(self.versions("configs"), [])

    async def test_a_version_taken_by_another_publication_is_a_real_conflict(self):
        # The intended version is claimed, but a different publication takes it before the
        # retry lands: refusing is the only honest answer, and no second version may appear.
        logged = await self.unlocked_login()
        models = type(self.platform.models)
        with patch.object(models, "publish", side_effect=Fault("dependency_unavailable", 503)):
            await self.publish(logged, CHAT_TEMPLATE, None, expected_status=503)
        self.assertEqual(self.versions("configs"), [])
        other = chat_config(version=1)
        self.platform.models.publish(bearer("ADMIN"), other)
        with self.platform.store.connect() as db:
            stored = db.execute("SELECT digest FROM configs WHERE version=1").fetchone()["digest"]
        self.assertNotEqual(stored, self.intent(CLIENT)["digest"])
        refused = await self.publish(logged, CHAT_TEMPLATE, None, expected_status=409)
        self.assertEqual(refused["code"], "version_conflict")
        self.assertEqual(self.versions("configs"), [1])
        self.assertEqual(self.intent(CLIENT)["state"], "prepared")

    async def test_prepared_intent_with_an_expired_window_is_refused(self):
        # The reviewed window is immutable: a stale one needs a fresh preview, never a reuse.
        logged = await self.unlocked_login()
        with self.lose_receipts():
            first = await self.publish(logged, NATIVE_TEMPLATE, None)
        self.assertEqual(first["version"], 1)
        with self.platform.store.connect(write=True) as db:
            db.execute("DELETE FROM native_configs WHERE version=1")
        with closing(sqlite3.connect(self.platform.store.path + ".web-models.sqlite")) as db:
            db.execute(
                "UPDATE publication_intents SET published_at='2000-01-01T00:00:00Z',"
                " usable_until='2000-01-01T00:05:00Z' WHERE client_id=?",
                (CLIENT,),
            )
            db.commit()
        refused = await self.publish(logged, NATIVE_TEMPLATE, None, expected_status=409)
        self.assertEqual(refused["code"], "version_conflict")
        self.assertEqual(self.versions("native_configs"), [])

    async def test_management_is_closed_by_default_and_without_config_authority(self):
        # No web_models section at all: the capability is absent, not merely locked.
        await self.start(self.variant("closed", lambda c: c.pop("web_models")))
        logged = await self.login()
        view = await self.view(logged)
        self.assertEqual(view["management"]["code"], "management_disabled")
        self.assertFalse(view["management"]["available"])
        self.assertEqual(view["management"]["templates"], [])
        self.assertEqual(
            [self.target(view, name)["availability"] for name in ("chat", "native")],
            ["unconfigured", "unconfigured"],
        )
        for path, body in (
            ("models/unlock", {"password": PASSWORD}),
            ("models/preview", {"template_id": CHAT_TEMPLATE}),
            (
                "models/publish",
                {"template_id": CHAT_TEMPLATE, "expected_version": None, "client_id": CLIENT},
            ),
            ("models/revoke", {"target": "chat", "version": 1}),
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    (await self.call(path, body, logged["csrf"], 403))["code"],
                    "management_disabled",
                )
        self.assertEqual(self.rows("configs"), [])
        self.assertEqual(self.rows("native_configs"), [])
        self.assertFalse(os.path.exists(self.platform.store.path + ".web-models.sqlite"))

        # An operator without the configuration actions is refused, never silently upgraded.

        def strip(config):
            for action in ("config.publish", "config.revoke", "config.view"):
                config["principals"]["admin"]["actions"].remove(action)

        await self.start(self.variant("unauthorized", strip))
        logged = await self.login()
        view = await self.view(logged)
        self.assertEqual(view["management"]["code"], "operator_not_authorized")
        self.assertFalse(view["management"]["available"])
        self.assertEqual(
            (await self.call("models/unlock", {"password": PASSWORD}, logged["csrf"], 403))["code"],
            "operator_not_authorized",
        )
        self.assertEqual(self.rows("configs"), [])

    async def test_anonymous_session_origin_csrf_and_credential_boundaries(self):
        _, anonymous = await self.current()
        bodies = {
            "models/view": {},
            "models/unlock": {"password": PASSWORD},
            "models/preview": {"template_id": CHAT_TEMPLATE},
            "models/publish": {
                "template_id": CHAT_TEMPLATE,
                "expected_version": None,
                "client_id": CLIENT,
            },
            "models/revoke": {"target": "chat", "version": 1},
            "models/lock": {},
        }
        for path, body in bodies.items():
            with self.subTest(path=path):
                await self.call(path, body, anonymous["csrf"], 401)
        logged = await self.unlocked_login()
        await self.call("models/view", {}, "not-the-csrf", 403)
        await self.call("models/view", {}, logged["csrf"], 403, Origin="http://evil.invalid")
        await self.call("models/view", {}, logged["csrf"], 403, Host="evil.invalid")
        await self.call("models/view", {}, logged["csrf"], 403, Authorization=bearer("ADMIN"))
        await self.call("models/publish", {}, logged["csrf"], 400)
        self.assertEqual(self.rows("configs"), [])
        self.assertEqual(self.rows("native_configs"), [])

    async def test_chat_login_alone_never_carries_management_rights(self):
        logged = await self.login()
        view = await self.view(logged)
        self.assertEqual(view["management"]["code"], "management_required")
        self.assertTrue(view["management"]["available"])
        self.assertFalse(view["management"]["unlocked"])
        self.assertEqual(
            sorted(item["template_id"] for item in view["management"]["templates"]),
            [CHAT_TEMPLATE, NATIVE_TEMPLATE],
        )
        await self.call("models/preview", {"template_id": CHAT_TEMPLATE}, logged["csrf"], 403)
        await self.publish(logged, CHAT_TEMPLATE, None, expected_status=403)
        await self.call("models/revoke", {"target": "chat", "version": 1}, logged["csrf"], 403)
        self.assertEqual(self.rows("configs"), [])
        # The explicit unlock opens the write port, and locking closes it again.
        unlocked = await self.call("models/unlock", {"password": PASSWORD}, logged["csrf"])
        self.assertEqual(unlocked["code"], "ready")
        self.assertTrue((await self.view(logged))["management"]["unlocked"])
        self.assertFalse((await self.call("models/lock", {}, logged["csrf"]))["unlocked"])
        self.assertEqual((await self.view(logged))["management"]["code"], "management_required")
        await self.call("models/preview", {"template_id": CHAT_TEMPLATE}, logged["csrf"], 403)

    async def test_unlock_requires_the_real_password_and_shares_the_login_budget(self):
        logged = await self.login()
        await self.call(
            "models/unlock", {"password": "incorrect-password-014"}, logged["csrf"], 401
        )
        await self.call("models/unlock", {}, logged["csrf"], 400)
        await self.call("models/unlock", {"password": PASSWORD, "admin": True}, logged["csrf"], 400)
        for _ in range(4):
            await self.call(
                "models/unlock", {"password": "incorrect-password-014"}, logged["csrf"], 401
            )
        # Five failures inside the window: the sixth attempt is refused for every caller.
        await self.call("models/unlock", {"password": PASSWORD}, logged["csrf"], 429)
        await self.call(
            "login", {"username": "synthetic-admin", "password": PASSWORD}, logged["csrf"], 429
        )
        self.assertEqual((await self.view(logged))["management"]["unlocked"], False)
        self.assertEqual(self.rows("configs"), [])

    async def test_unlock_and_session_expiry_are_reported_not_assumed(self):
        logged = await self.unlocked_login()
        live = next(iter(self.console.sessions.values()))
        live["management"] = 0
        self.assertEqual(
            (
                await self.call(
                    "models/preview", {"template_id": CHAT_TEMPLATE}, logged["csrf"], 403
                )
            )["code"],
            "management_required",
        )
        self.assertEqual((await self.view(logged))["management"]["code"], "management_required")
        await self.call("models/unlock", {"password": PASSWORD}, logged["csrf"])
        live["expires"] = 0
        await self.call("models/view", {}, logged["csrf"], 401)
        # An expired session never becomes a successful write.
        await self.publish(logged, CHAT_TEMPLATE, None, expected_status=401)
        self.assertEqual(self.rows("configs"), [])

    async def test_real_http_projections_are_redacted(self):
        self.platform.models.publish(bearer("ADMIN"), chat_config())
        self.platform.models.native_publish(bearer("ADMIN"), native_config())
        logged = await self.unlocked_login()
        view = await self.view(logged)
        preview = await self.call(
            "models/preview", {"template_id": NATIVE_TEMPLATE}, logged["csrf"]
        )
        published = await self.publish(logged, CHAT_TEMPLATE, 7)
        rendered = json.dumps([view, preview, published], ensure_ascii=False)
        for leaked in (
            "https://upstream.example.invalid/v1",
            "secret-ref:",
            "credential-namespace-fixture",
            "assertion_ref",
            *TOKENS.values(),
        ):
            with self.subTest(leak=leaked):
                self.assertNotIn(leaked, rendered)
        for key in ("base_url", "credential_ref", "credential_namespace", "digest", "request_id"):
            with self.subTest(key=key):
                self.assertNotIn(key, rendered)
        chat = self.target(view, "chat")
        self.assertEqual(chat["current_version"], 7)
        self.assertEqual(chat["versions"][0]["availability"], "available")
        self.assertEqual(
            sorted(chat["versions"][0]["bindings"][0]),
            ["fallback", "model_id", "provider_id", "timeout_ms", "workload"],
        )
        self.assertEqual(sorted({item["target"] for item in view["targets"]}), ["chat", "native"])

    async def test_preview_only_names_a_registered_template_and_writes_nothing(self):
        logged = await self.unlocked_login()
        preview = await self.call("models/preview", {"template_id": CHAT_TEMPLATE}, logged["csrf"])
        self.assertIsNone(preview["expected_version"])
        self.assertEqual(preview["version"], 1)
        self.assertEqual(preview["target"], "chat")
        self.assertEqual(self.rows("configs"), [])
        self.assertEqual(self.rows("native_configs"), [])
        self.assertEqual(
            (await self.call("models/preview", {"template_id": "missing"}, logged["csrf"], 404))[
                "code"
            ],
            "not_found",
        )
        for body in (
            {"template_id": CHAT_TEMPLATE, "version": 9},
            {"template_id": CHAT_TEMPLATE, "provider_id": CHAT_PROVIDER},
            {"template_id": CHAT_TEMPLATE, "base_url": "https://evil.invalid/v1"},
            {"template_id": CHAT_TEMPLATE, "credential_ref": "secret-ref:evil/key"},
            {"template_id": CHAT_TEMPLATE, "verified_capabilities": ["reasoning"]},
            {"template_id": {"template_id": CHAT_TEMPLATE}},
            {"template_id": CHAT_TEMPLATE, "expected_version": 1},
        ):
            with self.subTest(body=sorted(body)):
                await self.call("models/preview", body, logged["csrf"], 400)
        self.assertEqual(self.rows("configs"), [])

    async def test_publish_uses_the_authoritative_writer_for_each_target(self):
        self.platform.models.publish(bearer("ADMIN"), chat_config())
        self.platform.models.native_publish(bearer("ADMIN"), native_config())
        logged = await self.unlocked_login()
        chat = await self.publish(logged, CHAT_TEMPLATE, 7)
        self.assertEqual((chat["target"], chat["version"], chat["state"]), ("chat", 8, "published"))
        self.assertEqual(self.versions("configs"), [7, 8])
        self.assertEqual(self.versions("native_configs"), [7])
        native = await self.publish(
            logged, NATIVE_TEMPLATE, 7, client_id="55555555-6666-7777-8888-999999999999"
        )
        self.assertEqual((native["target"], native["version"]), ("native", 8))
        self.assertEqual(self.versions("native_configs"), [7, 8])
        self.assertEqual(self.versions("configs"), [7, 8])
        # Stored documents are the published contract shapes, built from registration data.
        # The Models owner stores them without the correlation-only request_id, exactly as
        # the CLI path does, so re-adding it is what makes the stored shape schema-valid.
        with self.platform.store.connect() as db:
            documents = {
                table: loads(
                    db.execute(f"SELECT document FROM {table} WHERE version=8").fetchone()[0]
                )
                for table in ("configs", "native_configs")
            }
        for table, name in (
            ("configs", "model#config_response"),
            ("native_configs", "model-protocol#config_response"),
        ):
            with self.subTest(table=table):
                self.assertNotIn("request_id", documents[table])
                self.platform.contracts.check(
                    name, {**documents[table], "request_id": "stored-shape-check"}
                )
        self.assertEqual(documents["configs"]["config_version"], 8)
        self.assertNotIn("native_config_version", documents["configs"])
        self.assertEqual(documents["native_configs"]["native_config_version"], 8)
        self.assertNotIn("config_version", documents["native_configs"])
        provider = documents["native_configs"]["providers"][0]
        self.assertEqual(provider["provider_id"], NATIVE_PROVIDER)
        self.assertEqual(provider["state_references"], "reject")
        self.assertEqual(provider["verified_capabilities"], ["text", "stream"])
        self.assertEqual(documents["configs"]["bindings"][0]["workload"], "companion.text")
        self.assertEqual(documents["native_configs"]["bindings"][0]["workload"], "native.responses")
        view = await self.view(logged)
        self.assertEqual(self.target(view, "chat")["current_version"], 8)
        self.assertEqual(self.target(view, "native")["current_version"], 8)
        self.assertEqual(
            [item["version"] for item in self.target(view, "chat")["versions"]], [8, 7]
        )

    async def test_replayed_publish_returns_the_recorded_version(self):
        logged = await self.unlocked_login()
        first = await self.publish(logged, CHAT_TEMPLATE, None)
        self.assertEqual((first["state"], first["version"]), ("published", 1))
        again = await self.publish(logged, CHAT_TEMPLATE, None)
        self.assertEqual((again["state"], again["version"]), ("replayed", 1))
        self.assertTrue(again["deduplicated"])
        self.assertEqual(self.versions("configs"), [1])
        # A replayed key must describe the same intent, not a different publication.
        conflict = await self.publish(logged, NATIVE_TEMPLATE, None, expected_status=409)
        self.assertEqual(conflict["code"], "idempotency_conflict")
        self.assertEqual(self.rows("native_configs"), [])
        # A fresh key is a new publication, not a replay.
        fresh = await self.publish(
            logged, CHAT_TEMPLATE, 1, client_id="66666666-7777-8888-9999-000000000000"
        )
        self.assertEqual((fresh["state"], fresh["version"]), ("published", 2))
        self.assertEqual(self.versions("configs"), [1, 2])

    async def test_two_browsers_with_a_stale_version_conflict(self):
        self.platform.models.publish(bearer("ADMIN"), chat_config())
        self.other = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.addAsyncCleanup(self.other.close)
        first = await self.unlocked_login()
        second = await self.unlocked_login(self.other)
        self.assertEqual(self.target(await self.view(first), "chat")["current_version"], 7)
        self.assertEqual(
            self.target(await self.view(second, self.other), "chat")["current_version"], 7
        )
        self.assertEqual((await self.publish(first, CHAT_TEMPLATE, 7))["version"], 8)
        stale = await self.publish(
            second,
            CHAT_TEMPLATE,
            7,
            client_id="22222222-3333-4444-5555-666666666666",
            expected_status=409,
            session=self.other,
        )
        self.assertEqual(stale["code"], "version_conflict")
        self.assertEqual(self.versions("configs"), [7, 8])
        # Re-reading before publishing is the only way forward; the refresh is a real read.
        refreshed = await self.view(second, self.other)
        self.assertEqual(self.target(refreshed, "chat")["current_version"], 8)
        self.assertEqual(
            (
                await self.publish(
                    second,
                    CHAT_TEMPLATE,
                    8,
                    client_id="33333333-4444-5555-6666-777777777777",
                    session=self.other,
                )
            )["version"],
            9,
        )

    async def test_revocation_removes_the_version_from_every_reader(self):
        self.platform.models.publish(bearer("ADMIN"), chat_config())
        self.platform.models.native_publish(bearer("ADMIN"), native_config())
        origin = self.platform.origins.issue(bearer("ADMIN"), "config-entry")["assertion_ref"]
        logged = await self.unlocked_login()
        await self.publish(logged, CHAT_TEMPLATE, 7)
        await self.publish(
            logged, NATIVE_TEMPLATE, 7, client_id="77777777-8888-9999-0000-111111111111"
        )
        self.assertEqual(
            self.platform.models.snapshot(bearer("GATEWAY"), query(origin, 8))["config_version"], 8
        )
        self.assertEqual(
            self.platform.models.native_snapshot(bearer("NATIVE"), native_query(origin, 8))[
                "native_config_version"
            ],
            8,
        )
        self.assertTrue(
            (await self.call("models/revoke", {"target": "chat", "version": 8}, logged["csrf"]))[
                "revoked"
            ]
        )
        self.assertTrue(
            (await self.call("models/revoke", {"target": "native", "version": 8}, logged["csrf"]))[
                "revoked"
            ]
        )
        # A revoked configuration is no longer readable by any consumer.
        self.reject("forbidden", self.platform.models.snapshot, bearer("GATEWAY"), query(origin, 8))
        self.reject(
            "forbidden",
            self.platform.models.native_snapshot,
            bearer("NATIVE"),
            native_query(origin, 8),
        )
        view = await self.view(logged)
        for name in ("chat", "native"):
            self.assertEqual(self.target(view, name)["availability"], "revoked")
            self.assertEqual(self.target(view, name)["versions"][0]["availability"], "revoked")
        # Revoking an unknown version fails instead of reporting success.
        self.assertEqual(
            (
                await self.call(
                    "models/revoke", {"target": "chat", "version": 99}, logged["csrf"], 404
                )
            )["code"],
            "not_found",
        )
        self.assertEqual(
            (
                await self.call(
                    "models/revoke", {"target": "other", "version": 1}, logged["csrf"], 400
                )
            )["code"],
            "invalid_input",
        )
        # The revoked version stays revoked; only a new version moves the target forward.
        latest = await self.publish(
            logged, CHAT_TEMPLATE, 8, client_id="44444444-5555-6666-7777-888888888888"
        )
        self.assertEqual(latest["version"], 9)
        self.assertEqual(self.versions("configs"), [7, 8, 9])
        self.assertEqual([row["revoked"] for row in self.rows("configs")], [0, 1, 0])
        self.assertEqual(
            (
                await self.call(
                    "models/revoke", {"target": "native", "version": 99}, logged["csrf"], 404
                )
            )["code"],
            "not_found",
        )

    async def test_management_state_survives_restart_without_replay(self):
        logged = await self.unlocked_login()
        self.assertEqual((await self.publish(logged, NATIVE_TEMPLATE, None))["version"], 1)
        await self.start(self.config)
        logged = await self.unlocked_login()
        self.assertEqual(self.target(await self.view(logged), "native")["current_version"], 1)
        replay = await self.publish(logged, NATIVE_TEMPLATE, None)
        self.assertEqual((replay["state"], replay["version"]), ("replayed", 1))
        self.assertEqual(self.versions("native_configs"), [1])

    async def test_management_adds_no_internal_contract_port(self):
        async with self.client.post(
            self.url + "/internal/v1/web-models/publish",
            json={"template_id": CHAT_TEMPLATE},
            headers={"Authorization": bearer("ADMIN"), "Content-Type": "application/json"},
        ) as response:
            self.assertEqual(response.status, 404)
        # The published internal ports keep their own boundary and never accept a browser.
        async with self.client.post(
            self.url + "/internal/v1/model-config/native/snapshot",
            json=native_query("origin-browser"),
            headers={
                "Authorization": bearer("NATIVE"),
                "Origin": self.url,
                "Content-Type": "application/json",
            },
        ) as response:
            self.assertEqual(response.status, 403)

    async def test_operator_credential_comes_from_the_environment_not_the_request(self):
        logged = await self.unlocked_login()
        self.assertEqual(self.console.models.operator_header(), "Bearer " + TOKENS["ADMIN"])
        with patch.dict(os.environ, {"TS012_ADMIN": ""}):
            self.assertEqual(self.console.models.operator_header(), "Bearer ")
            await self.publish(logged, CHAT_TEMPLATE, None, expected_status=401)
        self.assertEqual(self.rows("configs"), [])

    async def test_settings_templates_are_reviewed_at_startup(self):
        cases = {
            "unknown-provider": {"provider_id": "missing-provider"},
            "wrong-target-provider": {"provider_id": NATIVE_PROVIDER},
            "unregistered-model": {"model_id": "other-model"},
            "capability": {"verified_capabilities": ["reasoning"]},
            "lifetime": {"lifetime_seconds": 86400},
            "timeout": {"timeout_ms": 0},
            "template-id": {"template_id": "not a valid id"},
            "injected-endpoint": {"base_url": "https://evil.invalid/v1"},
            "injected-credential": {"credential_ref": "secret-ref:evil/key"},
            "injected-namespace": {"credential_namespace": "other-namespace"},
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                config = self.variant(name)
                template = config["web_models"]["templates"][0]
                for key, value in change.items():
                    if key == "template_id":
                        config["web_models"]["templates"][1]["template_id"] = value
                    else:
                        template[key] = value
                with self.assertRaises(Fault) as found:
                    create_app(Platform(config))
                self.assertIn(found.exception.code, {"invalid_input", "dependency_unavailable"})

    async def test_target_pins_duplicates_and_shape_errors_are_refused(self):
        for name, mutate in (
            (
                "native-on-chat-provider",
                lambda c: c["web_models"]["templates"][1].update(provider_id=CHAT_PROVIDER),
            ),
            (
                "chat-on-native-provider",
                lambda c: c["web_models"]["templates"][0].update(provider_id=NATIVE_PROVIDER),
            ),
            (
                "duplicate-id",
                lambda c: c["web_models"]["templates"][1].update(
                    template_id=c["web_models"]["templates"][0]["template_id"]
                ),
            ),
            ("unlock-ttl", lambda c: c["web_models"].update(unlock_ttl_seconds=10)),
            ("enabled-type", lambda c: c["web_models"].update(enabled="yes")),
            ("no-templates", lambda c: c["web_models"].update(templates=[])),
            ("unknown-key", lambda c: c["web_models"].update(administrator=True)),
            (
                "missing-field",
                lambda c: c["web_models"]["templates"][0].pop("verified_capabilities"),
            ),
        ):
            with self.subTest(case=name):
                with self.assertRaises(Fault):
                    create_app(Platform(self.variant(name, mutate)))

    async def test_management_settings_without_a_console_have_no_entry(self):
        # Model management is a console section: without a web console there is no port,
        # no ledger file and nothing that could report success.
        section = self.variant("no-console", lambda c: c.pop("web"))
        platform = Platform(section)
        app = create_app(platform)
        runner, url = await start_http(app)
        self.addAsyncCleanup(runner.cleanup)
        async with self.client.post(
            url + "/api/web/models/view",
            json={},
            headers={"Origin": url, "X-CSRF-Token": "anonymous"},
        ) as response:
            self.assertEqual(response.status, 503)
            self.assertEqual((await response.json())["code"], "web_not_configured")
        self.assertFalse(os.path.exists(platform.store.path + ".web-models.sqlite"))
        with platform.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM configs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM native_configs").fetchone()[0], 0)

    async def test_disabled_section_still_lists_versions_without_a_write_port(self):
        section = self.variant("readonly")
        section["web_models"]["enabled"] = False
        await self.start(section)
        self.platform.models.publish(bearer("ADMIN"), chat_config())
        logged = await self.login()
        view = await self.view(logged)
        self.assertEqual(view["management"]["code"], "management_disabled")
        self.assertEqual(view["management"]["templates"], [])
        chat = self.target(view, "chat")
        self.assertEqual(chat["current_version"], 7)
        self.assertEqual(chat["versions"][0]["availability"], "available")
        await self.publish(logged, CHAT_TEMPLATE, 7, expected_status=403)
        self.assertEqual(self.versions("configs"), [7])

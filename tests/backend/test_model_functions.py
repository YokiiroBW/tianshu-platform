"""Real catalogue, HTTP authorization and pinned runtime selection; synthetic providers."""

import json
import os
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp

from fixtures import ENV, bearer, start_http
from services.platform.contracts import Fault
from services.platform.provider_authority import ProviderAuthority
from services.platform.provider_catalog import ProviderCatalog
from services.platform.server import create_app
from services.platform.service import Platform
from web_fixtures import PASSWORD, web_settings


class ModelFunctionsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(
            os.environ, {**ENV, "TS_FUNCTION_GATEWAY": "synthetic-management-token-only"}
        )
        env.start()
        self.addCleanup(env.stop)
        self.root = Path(self.temp.name)
        self.catalog = ProviderCatalog(self.root / "providers", create=True)
        self.providers = []
        for label in ("chat", "specialist"):
            item = self.catalog.save(
                client_id=label,
                name=label,
                base_url="https://example.invalid/v1",
                model_id=label,
                api_key="synthetic-only",
            )
            self.catalog.record_test(
                client_id="test-" + label,
                provider_id=item["provider_id"],
                expected_revision=1,
                outcome="succeeded",
            )
            self.providers.append(item)
        self.catalog.set_default(
            client_id="default",
            provider_id=self.providers[0]["provider_id"],
            expected_revision=1,
            expected_default_revision=0,
        )
        settings = web_settings(self.root)
        settings["provider_self_service"] = {
            "directory": str(self.catalog.directory),
            "gateway_url": "http://127.0.0.1:1",
            "gateway_token_env": "TS_FUNCTION_GATEWAY",
        }
        settings["principals"]["companion"]["actions"].append("config.select")
        settings["principals"]["gateway"]["actions"].append("provider.runtime")
        self.platform = Platform(settings)
        self.authority = ProviderAuthority(self.platform)
        self.runner, self.url = await start_http(create_app(self.platform))
        self.addAsyncCleanup(self.runner.cleanup)
        settings["web"]["origin"] = self.url
        self.client = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.addAsyncCleanup(self.client.close)

    def binding(self, function="code", provider=1, revision=0, **changes):
        body = dict(
            client_id=str(uuid.uuid4()),
            function_id=function,
            provider_id=self.providers[provider]["provider_id"] if provider is not None else None,
            expected_revision=1 if provider is not None else None,
            expected_binding_revision=revision,
        )
        body.update(changes)
        return body

    def selection(self, function=None, turn=None):
        body = dict(
            turn_id=turn or "turn:" + uuid.uuid4().hex,
            actor_id="actor:a",
            person_id="person:a",
            audience="self_private",
            conversation_id="conversation:a",
            caller_service="companion",
            workload="companion.text",
        )
        if function is not None:
            body["function_id"] = function
        result = self.authority.select(bearer("COMPANION"), body)
        runtime = self.authority.runtime(
            bearer("GATEWAY"),
            {
                **{key: body[key] for key in ("turn_id", "caller_service", "workload")},
                "config_version": result["config_version"],
            },
        )
        return runtime

    async def test_defaults_persistence_reset_conflict_and_invalidated_revision(self):
        self.assertEqual(6, len(self.catalog.view()["functions"]))
        body = self.binding()
        first = self.catalog.functions.set_binding(**body)
        self.assertEqual(first, self.catalog.functions.set_binding(**body))
        reopened = ProviderCatalog(self.catalog.directory)
        code = next(x for x in reopened.view()["functions"] if x["function_id"] == "code")
        self.assertEqual(self.providers[1]["provider_id"], code["effective_provider_id"])
        with self.assertRaises(Fault) as caught:
            reopened.functions.set_binding(**self.binding())
        self.assertEqual("revision_conflict", caught.exception.code)
        item = self.providers[1]
        self.catalog.save(
            client_id="edit",
            provider_id=item["provider_id"],
            expected_revision=1,
            name=item["name"],
            base_url=item["base_url"],
            model_id="changed",
        )
        code = next(x for x in reopened.view()["functions"] if x["function_id"] == "code")
        self.assertFalse(code["configured"])
        with self.assertRaises(Fault):
            self.selection("code")
        self.catalog.functions.set_binding(**self.binding(provider=None, revision=1))
        self.assertEqual("chat", self.selection("code")["model_id"])

    async def test_function_routes_and_pinned_grants_preserve_chat_and_role_override(self):
        for function in ("tools", "code", "search", "writing", "memory"):
            self.catalog.functions.set_binding(**self.binding(function))
            self.assertEqual("specialist", self.selection(function)["model_id"])
        self.assertEqual("chat", self.selection()["model_id"])
        first = self.selection("code", "turn:pinned")
        self.catalog.functions.set_binding(**self.binding(provider=0, revision=1))
        self.assertEqual(first, self.selection("code", "turn:pinned"))
        self.assertEqual("chat", self.selection("code")["model_id"])
        self.platform.role_runtime = SimpleNamespace(
            get=lambda _: {"provider_id": self.providers[0]["provider_id"], "provider_revision": 1},
            active=lambda _: True,
        )
        self.catalog.functions.set_binding(**self.binding("chat", revision=1))
        self.assertEqual("chat", self.selection()["model_id"])
        self.assertEqual("specialist", self.selection("writing")["model_id"])
        with self.assertRaises(Fault):
            self.selection("unknown")

    async def test_additive_upgrade_backs_up_old_catalogue_and_preserves_default(self):
        with closing(sqlite3.connect(self.catalog.database)) as db, db:
            db.execute("DROP TABLE model_function_bindings")
        ProviderCatalog.verify_existing(self.catalog.directory)
        upgraded = ProviderCatalog(self.catalog.directory)
        self.assertTrue(upgraded.view()["default"]["configured"])
        backups = list(self.catalog.directory.glob("providers.pre-functions-*.sqlite"))
        self.assertEqual(1, len(backups))
        ProviderCatalog(self.catalog.directory)
        self.assertEqual(
            backups, list(self.catalog.directory.glob("providers.pre-functions-*.sqlite"))
        )

    async def test_main_chat_reuses_default_and_inherited_functions_follow_it(self):
        result = self.catalog.functions.set_binding(**self.binding("chat", revision=1))
        view = self.catalog.view()
        self.assertEqual(result["revision"], view["default"]["revision"])
        self.assertEqual(self.providers[1]["provider_id"], view["default"]["provider_id"])
        self.assertTrue(
            all(
                item["effective_provider_id"] == self.providers[1]["provider_id"]
                for item in view["functions"]
            )
        )
        self.assertEqual("specialist", self.selection()["model_id"])
        with self.assertRaises(Fault):
            self.catalog.set_default(
                client_id="stale-default",
                provider_id=self.providers[0]["provider_id"],
                expected_revision=1,
                expected_default_revision=1,
            )

    async def test_http_save_reload_authorization_and_redaction(self):
        async with self.client.get(self.url + "/api/web/session") as response:
            session = await response.json()

        async def post(path, body, expected=200):
            async with self.client.post(
                self.url + "/api/web/" + path,
                json=body,
                headers={"Origin": self.url, "X-CSRF-Token": session["csrf"]},
            ) as response:
                result = await response.json()
                self.assertEqual(expected, response.status, result)
                return result

        session = await post("login", {"username": "synthetic-admin", "password": PASSWORD})
        await post("providers/function", self.binding(), 403)
        await post("models/unlock", {"password": PASSWORD})
        await post("providers/function", self.binding())
        view = await post("providers/view", {})
        code = next(item for item in view["functions"] if item["function_id"] == "code")
        self.assertTrue(code["configured"])
        self.assertFalse(code["connected"])
        self.assertNotIn("synthetic-only", json.dumps(view))
        await post("providers/function", self.binding(), 409)
        await post("providers/function", self.binding("unknown"), 400)
        await post("providers/function", self.binding(provider=None, revision=1))

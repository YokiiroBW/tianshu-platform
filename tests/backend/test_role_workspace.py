"""Deletion fences remote owners; function choices remain isolated per role."""

import tempfile
import unittest
import uuid
from pathlib import Path
from test_role_runtime import body, fixture
from services.platform.contracts import Fault
from services.platform.model_functions import FUNCTIONS, effective_binding
from services.platform.role_runtime import RoleRuntime


class RoleWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.platform, self.manager, self.console, self.peers = fixture(Path(self.temporary.name))
        old_view = self.platform.provider_catalog.view

        def view():
            result = old_view()
            result["functions"] = [
                {"function_id": identity, "provider_id": None, "provider_revision": None}
                for identity, *_ in FUNCTIONS
            ]
            return result

        self.platform.provider_catalog.view = view

    async def create(self, provider="provider-a"):
        return (await self.manager.route(self.console, "/api/web/roles/apply", body(provider), {}))[
            "role"
        ]

    async def test_delete_replays_and_fences_both_owners_without_erasing_history(self):
        role = await self.create()
        payload = {
            "actor_id": role["actor_id"],
            "client_id": str(uuid.uuid4()),
            "expected_version": role["version"],
        }
        result = (await self.manager.route(self.console, "/api/web/roles/delete", payload, {}))[
            "role"
        ]
        self.assertTrue(result["deleted"])
        self.assertFalse(self.manager.active(role["actor_id"]))
        self.assertEqual(self.manager.directory(), [])
        self.assertFalse(self.peers.roles[role["actor_id"]]["enabled"])
        self.assertFalse(self.peers.grants[role["actor_id"]]["enabled"])
        replay = (await self.manager.route(self.console, "/api/web/roles/delete", payload, {}))[
            "role"
        ]
        self.assertEqual(replay, result)
        self.assertIsNotNone(self.manager.get(role["actor_id"]))
        restarted = RoleRuntime(self.platform)
        self.assertEqual(restarted.directory(), [])
        self.assertTrue(restarted.get(role["actor_id"])["deleted"])
        self.assertFalse(restarted.active(role["actor_id"]))
        with self.assertRaises(Fault):
            await self.manager.route(
                self.console,
                "/api/web/roles/apply",
                body("provider-a", actor=role["actor_id"], version=result["version"]),
                {},
            )

    async def test_delete_waits_for_failed_revoke_then_retry_completes(self):
        role = await self.create()
        original = self.manager._remote

        async def unavailable(settings, path, payload):
            if path.endswith("/authorize") and payload.get("enabled") is False:
                raise Fault("dependency_unavailable", 503)
            return await original(settings, path, payload)

        self.manager._remote = unavailable
        payload = {
            "actor_id": role["actor_id"],
            "client_id": str(uuid.uuid4()),
            "expected_version": role["version"],
        }
        pending = (await self.manager.route(self.console, "/api/web/roles/delete", payload, {}))[
            "role"
        ]
        self.assertEqual(pending["state"], "pending")
        self.assertFalse(pending.get("deleted", False))
        self.assertFalse(self.manager.active(role["actor_id"]))
        self.manager._remote = original
        result = (
            await self.manager.route(
                self.console,
                "/api/web/roles/retry",
                {"actor_id": role["actor_id"], "client_id": pending["client_id"]},
                {},
            )
        )["role"]
        self.assertTrue(result["deleted"])

    async def test_function_selection_is_role_scoped_and_preserved_on_role_edit(self):
        first, second = await self.create(), await self.create()
        payload = {
            "actor_id": first["actor_id"],
            "function_id": "writing",
            "provider_id": "provider-b",
            "provider_revision": 4,
            "expected_version": 0,
            "client_id": str(uuid.uuid4()),
        }
        result = await self.manager.route(self.console, "/api/web/roles/models/apply", payload, {})
        self.assertEqual(result["version"], 1)
        self.assertEqual(
            (await self.manager.route(self.console, "/api/web/roles/models/apply", payload, {}))[
                "version"
            ],
            1,
        )
        view = self.platform.provider_catalog.view()
        self.assertEqual(
            effective_binding(view, "writing", self.manager.get(first["actor_id"]))["provider_id"],
            "provider-b",
        )
        self.assertEqual(
            effective_binding(view, "writing", self.manager.get(second["actor_id"]))["provider_id"],
            "provider-a",
        )
        stale = {**payload, "client_id": str(uuid.uuid4())}
        with self.assertRaises(Fault):
            await self.manager.route(self.console, "/api/web/roles/models/apply", stale, {})
        updated = (
            await self.manager.route(
                self.console,
                "/api/web/roles/apply",
                body("provider-a", actor=first["actor_id"], version=first["version"]),
                {},
            )
        )["role"]
        self.assertEqual(updated["model_functions"]["writing"]["provider_id"], "provider-b")
        reset = {
            **payload,
            "provider_id": None,
            "provider_revision": None,
            "expected_version": 1,
            "client_id": str(uuid.uuid4()),
        }
        await self.manager.route(self.console, "/api/web/roles/models/apply", reset, {})
        self.assertEqual(
            effective_binding(view, "writing", self.manager.get(first["actor_id"]))["provider_id"],
            "provider-a",
        )

    async def test_global_function_default_and_role_override_have_explicit_precedence(self):
        role = await self.create()
        view = self.platform.provider_catalog.view()
        next(row for row in view["functions"] if row["function_id"] == "writing").update(
            provider_id="provider-b", provider_revision=4
        )
        self.assertEqual(effective_binding(view, "writing", role)["source"], "system_function")
        role["model_functions"] = {"writing": {"provider_id": "provider-a", "provider_revision": 2}}
        self.assertEqual(effective_binding(view, "writing", role)["source"], "role_function")

    async def test_main_model_edits_invalidate_stale_forms_in_both_editors(self):
        role = await self.create()
        payload = {
            "actor_id": role["actor_id"],
            "function_id": "chat",
            "provider_id": "provider-b",
            "provider_revision": 4,
            "expected_version": 0,
            "client_id": str(uuid.uuid4()),
        }
        await self.manager.route(self.console, "/api/web/roles/models/apply", payload, {})
        with self.assertRaises(Fault) as stale:
            await self.manager.route(
                self.console,
                "/api/web/roles/apply",
                body("provider-a", actor=role["actor_id"], version=role["version"]),
                {},
            )
        self.assertEqual(stale.exception.code, "version_conflict")
        latest = self.manager.get(role["actor_id"])
        await self.manager.route(
            self.console,
            "/api/web/roles/apply",
            body("provider-a", actor=role["actor_id"], version=latest["version"]),
            {},
        )
        payload.update(client_id=str(uuid.uuid4()), expected_version=1)
        with self.assertRaises(Fault) as stale:
            await self.manager.route(self.console, "/api/web/roles/models/apply", payload, {})
        self.assertEqual(stale.exception.code, "version_conflict")


if __name__ == "__main__":
    unittest.main()

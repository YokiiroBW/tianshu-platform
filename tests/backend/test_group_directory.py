"""Metadata discovery is independent of message capture and Memory readiness."""

import asyncio
import copy
from contextlib import closing
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import test_bot_observation as observation_fixture
from services.platform import group_policy
from services.platform.contracts import Fault


class GroupDirectoryTests(unittest.TestCase):
    def setUp(self):
        fixture = observation_fixture.ObservationTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.manager, self.platform, self.row = fixture.manager, fixture.platform, fixture.row
        self.platform.bots = SimpleNamespace(_web_code=lambda *_: "management_required")
        self.platform.role_runtime = SimpleNamespace(active_actors=lambda: [], get=lambda _: None)
        self.row["group_policy"] = {"version": 2, "default": group_policy.rule(), "groups": {}}
        self.row["private_policy"] = observation_fixture.policy(observe=False)
        self.manager.catalog.put(self.row)
        self.metadata = dict(
            group_id="20002",
            name="合成测试群",
            member_count=42,
            max_member_count=200,
            first_seen_at=100,
            last_seen_at=110,
            updated_at=110,
        )

    def response(self, **changes):
        return dict(
            schema_version=1,
            instance_id="instance-a",
            account_id="10001",
            groups=[self.metadata],
            next_cursor=None,
            **changes,
        )

    def test_disabled_unbound_metadata_survives_restart_without_creating_content(self):
        self.row["pending"] = {"request_id": "unchanged"}
        self.row["revision"] = 7
        self.manager.catalog.put(self.row)
        self.manager._plugin = AsyncMock(return_value=self.response())
        asyncio.run(self.manager.groups.refresh(self.row))
        saved = self.manager.get(self.row["id"])
        self.assertEqual(
            (saved["revision"], saved["pending"], saved["private_policy"]),
            (7, self.row["pending"], self.row["private_policy"]),
        )
        from services.platform.group_directory import GroupDirectory

        reopened = GroupDirectory(self.manager)
        result = reopened.view(None, {}, {"limit": 100, "cursor": None})
        self.assertTrue(result["available"])
        self.assertFalse(result["unlocked"])
        group = result["groups"][0]
        self.assertEqual(
            (group["name"], group["member_count"], group["rule"]["mode"]),
            ("合成测试群", 42, "disabled"),
        )
        self.assertIsNone(group["memory_group_id"])
        self.assertFalse(group["has_archive"])
        with closing(self.manager._db()) as db:
            for table in ("sources", "discovered"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        # A mismatched account cannot add or replace metadata.
        self.manager._plugin.return_value = {**self.response(), "account_id": "99999"}
        asyncio.run(reopened.refresh(self.row))
        self.assertEqual(self.manager.get(self.row["id"])["known_groups"]["20002"], self.metadata)

    def test_per_group_save_preserves_other_group_private_and_idempotent_revision(self):
        self.row["known_groups"] = {"20002": self.metadata}
        other = group_policy.rule("reply", "actor:a", ["另一群"])
        self.row["group_policy"]["groups"]["20003"] = other
        self.row["private_policy"] = observation_fixture.policy(mode="blacklist", actor="actor:a")
        self.manager.catalog.put(self.row)
        self.manager._disable_invalid_replies = Mock()

        async def apply(row):
            row["pending"] = None
            row["state"] = "ready"
            self.manager.catalog.put(row)
            return row

        self.manager._apply = apply
        body = dict(
            owner_id=self.row["id"],
            group_id="20002",
            expected_revision=1,
            rule=group_policy.rule("observe", "actor:a"),
            client_id="directory-save",
        )
        self.manager._gate = Mock()
        projected = asyncio.run(
            self.manager.route(None, "/api/web/bot-observation/group-policy", body, {})
        )
        self.manager._gate.assert_called_once()
        self.assertEqual(projected["group"]["rule"]["actor_id"], "actor:a")
        self.assertTrue(projected["group"]["memory_group_id"].startswith("observed-group:"))
        saved = self.manager.get(self.row["id"])
        self.assertEqual(saved["group_policy"]["groups"]["20003"], other)
        self.assertEqual(saved["private_policy"], self.row["private_policy"])
        self.assertEqual(saved["revision"], 2)
        self.assertEqual(asyncio.run(self.manager.groups.change(body))["revision"], 2)
        with self.assertRaises(Fault) as error:
            asyncio.run(self.manager.groups.change({**body, "client_id": "stale-save"}))
        self.assertEqual(error.exception.code, "version_conflict")
        # A disabled unknown group retains only historical owner attribution.
        fallback = group_policy.effective(
            {"version": 2, "default": group_policy.rule("active", "actor:a"), "groups": {}}, "20004"
        )
        self.assertEqual((fallback["mode"], fallback["actor_id"]), ("disabled", "actor:a"))

    def test_role_filter_and_profile_targets_keep_other_bindings_out_of_pages(self):
        self.row["known_groups"] = {
            str(i): {**self.metadata, "group_id": str(i)} for i in (20001, 20002, 20003)
        }
        self.row["group_policy"]["groups"] = {
            "20001": group_policy.rule("observe", "actor:b"),
            "20002": group_policy.rule("observe", "actor:a"),
        }
        self.manager.catalog.put(self.row)
        query = {"limit": 1, "cursor": None, "role_id": "actor:a"}
        page = self.manager.groups.view(None, {}, query)
        self.assertEqual([r["group_id"] for r in page["groups"]], ["20002"])
        self.assertIsNone(page["next_cursor"])
        self.assertEqual(
            [
                r["group_id"]
                for r in self.manager.groups.view(None, {}, {**query, "role_id": None})["groups"]
            ],
            ["20003"],
        )
        with closing(self.manager._db()) as db, db:
            db.executemany(
                "INSERT INTO discovered VALUES(?,?,?,?,?,?)",
                [
                    (self.row["id"], "group:20001", "10010", 1, 1, 100),
                    (self.row["id"], "group:20002", "10011", 1, 1, 100),
                    (self.row["id"], "group:20002", "10012", 0, 1, 100),
                ],
            )
        self.assertEqual(
            self.manager.profile_targets("actor:a", ["10010", "10011", "10012"]),
            {"10011": {("10001", "group:20002")}},
        )
        first = asyncio.run(self.manager.discovered({"id": self.row["id"], **query}))
        self.assertEqual(first["items"], [])
        self.assertIsNotNone(first["next_cursor"])
        second = asyncio.run(
            self.manager.discovered({"id": self.row["id"], **query, "cursor": first["next_cursor"]})
        )
        self.assertEqual([r["author"] for r in second["items"]], ["10011"])

    def test_metadata_network_does_not_block_other_account_poll_and_is_drained(self):
        second = copy.deepcopy(self.row)
        second.update(id="obs:second", account_id="10002")
        second["group_policy"]["groups"]["20003"] = group_policy.rule("observe", "actor:a")
        self.manager.catalog.put(second)

        async def run():
            never = asyncio.Event()
            started = asyncio.Event()
            polls = []

            async def plugin(row, operation, body):
                if operation == "groups":
                    started.set()
                    await never.wait()
                elif operation == "poll":
                    polls.append(row["account_id"])
                    return {"events": []}
                else:
                    self.fail(operation)

            self.manager._plugin = plugin
            self.manager._process_batch = AsyncMock()
            await asyncio.wait_for(self.manager.pump_once(), 2)
            await asyncio.wait_for(started.wait(), 2)
            self.assertEqual(polls, ["10002"])
            self.assertTrue(self.manager.groups.tasks)
            await self.manager.close()
            self.assertFalse(self.manager.groups.tasks)

        asyncio.run(run())

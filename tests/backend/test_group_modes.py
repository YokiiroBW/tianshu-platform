"""Four modes use the original observation, delivery and archive owners."""

import copy
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fixtures import CONTRACT
import test_bot_observation as fixtures
from services.platform import group_policy
from services.platform.bot_observation import BotObservation, decision
from services.platform.contracts import Contracts, Fault
from services.platform.observation_processing import current, processing


class GroupModesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fixture = fixtures.ObservationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.owner, self.p, self.row, self.event = (
            fixture.manager,
            fixture.platform,
            fixture.row,
            fixture.event,
        )
        self.p.contracts = Contracts(CONTRACT)
        self.p.role_runtime = SimpleNamespace(
            get=lambda _: None, active=lambda _: True, active_actors=lambda: []
        )
        self.p.bots = SimpleNamespace(slots={}, config=None)
        self.p.sources = SimpleNamespace(entries={})
        self.row["group_policy"] = dict(
            version=2, default=group_policy.rule("observe", "actor:a"), groups={}
        )
        self.row.update(group_default_revision=1, group_revisions={})
        self.owner.catalog.put(self.row)

    def test_legacy_mapping_never_enables_active_and_private_stop_is_first(self):
        for old, expected, special in [
            (fixtures.policy(observe=False), "disabled", "disabled"),
            (fixtures.policy(), "observe", "observe"),
            (fixtures.policy(mode="whitelist", names=["7"]), "observe", "reply"),
            (fixtures.policy(mode="blacklist", names=["7"]), "reply", "observe"),
        ]:
            with self.subTest(old=old):
                new = group_policy.normalize(old)
                self.assertEqual(group_policy.effective(new, "8")["mode"], expected)
                self.assertEqual(group_policy.effective(new, "7")["mode"], special)
                self.assertNotIn(
                    "active",
                    [new["default"]["mode"], *[item["mode"] for item in new["groups"].values()]],
                )
        self.assertFalse(
            decision(fixtures.policy(observe=False, mode="blacklist"), "30003", True, "private")[
                "reply_triggered"
            ]
        )

    def test_rule_validation_requires_explicit_group_actor_and_literal_unique_keywords(self):
        for changes in [
            dict(mode="unknown"),
            dict(keywords=[""]),
            dict(keywords=["x", "x"]),
            dict(keywords=["a"] * 33),
        ]:
            value = dict(
                version=2,
                default=group_policy.rule(),
                groups={"7": {**group_policy.rule("reply", "actor:a"), **changes}},
            )
            with self.subTest(changes=changes), self.assertRaises(Fault):
                group_policy.validate(value, saving=True)
        with self.assertRaises(Fault):
            group_policy.validate(
                dict(version=2, default=group_policy.rule(), groups={"7": group_policy.rule()}),
                saving=True,
            )
        literal = dict(
            version=2, default=group_policy.rule("reply", "actor:a", ["[a].*"]), groups={}
        )
        self.assertFalse(group_policy.decision(literal, "7", False, "aaaa")["reply_triggered"])
        self.assertTrue(
            group_policy.decision(literal, "7", False, "文本[a].*原文")["reply_triggered"]
        )

    def test_per_group_generations_preserve_other_group_and_never_revive_old_delivery(self):
        old = copy.deepcopy(self.row["group_policy"])
        old["groups"] = {
            "7": group_policy.rule("reply", "actor:a"),
            "8": group_policy.rule("reply", "actor:a"),
        }
        self.row["group_policy"] = old
        first_a = self.owner._reply_connection_id(self.row, "group:7", "actor:a")
        first_b = self.owner._reply_connection_id(self.row, "group:8", "actor:a")
        stopped = copy.deepcopy(old)
        stopped["groups"]["7"]["mode"] = "disabled"
        group_policy.advance(self.row, old, stopped)
        self.row.update(group_policy=stopped, host_revision=2)
        self.assertEqual(first_b, self.owner._reply_connection_id(self.row, "group:8", "actor:a"))
        group_policy.advance(self.row, stopped, old)
        self.row.update(group_policy=old, host_revision=3)
        self.assertNotEqual(
            first_a, self.owner._reply_connection_id(self.row, "group:7", "actor:a")
        )
        self.assertEqual(first_b, self.owner._reply_connection_id(self.row, "group:8", "actor:a"))

    async def test_mode_at_entrance_prevents_all_downstream_work_and_routes_only_real_triggers(
        self,
    ):
        for mode, mentioned, text, expected in [
            ("disabled", True, "ask", (0, 0, 0)),
            ("observe", True, "ask", (1, 0, 0)),
            ("reply", False, "ordinary", (1, 0, 0)),
            ("reply", False, "字面关键词", (1, 1, 0)),
            ("reply", True, "ask", (1, 1, 0)),
            ("active", False, "ordinary", (1, 0, 1)),
            ("active", True, "ask", (1, 1, 0)),
        ]:
            with self.subTest(mode=mode, mentioned=mentioned, text=text):
                self.row["group_policy"]["default"] = group_policy.rule(mode, "actor:a", ["关键词"])
                self.owner.catalog.put(self.row)
                event = {
                    **self.event(native=mode + str(mentioned) + str(len(text))),
                    "mentioned": mentioned,
                    "text": text,
                }
                companion = AsyncMock(
                    side_effect=lambda value: dict(
                        source_ref=value["source_ref"], state="accepted", archive_state="archived"
                    )
                )
                participation = SimpleNamespace(observe=AsyncMock())
                self.p.participation = participation
                with (
                    patch.object(self.owner, "_refresh_host_status", AsyncMock()),
                    patch.object(self.owner, "_deliver_replies", AsyncMock()),
                    patch.object(self.owner, "_companion", companion),
                    patch.object(self.owner, "_reply_event", AsyncMock(return_value=True)) as reply,
                    patch.object(
                        self.owner, "_plugin", AsyncMock(return_value={"acknowledged": ["entry"]})
                    ),
                    patch.object(self.owner, "record", wraps=self.owner.record) as record,
                ):
                    await self.owner._process_batch(
                        self.row, [dict(id="entry", event=event, reply_claimed=True)]
                    )
                    self.assertEqual(
                        (record.call_count, reply.await_count, participation.observe.await_count),
                        expected,
                    )
                    self.assertEqual(companion.await_count, expected[0])
        self.row["private_policy"] = fixtures.policy(
            observe=False, mode="blacklist", actor="actor:a"
        )
        self.owner.catalog.put(self.row)
        with (
            patch.object(self.owner, "_refresh_host_status", AsyncMock()),
            patch.object(self.owner, "_deliver_replies", AsyncMock()),
            patch.object(self.owner, "record") as record,
            patch.object(
                self.owner, "_plugin", AsyncMock(return_value={"acknowledged": ["private"]})
            ),
        ):
            await self.owner._process_batch(
                self.row,
                [
                    dict(
                        id="private",
                        event={**self.event(conversation="private:30003"), "mentioned": False},
                    )
                ],
            )
            record.assert_not_called()

    async def test_all_disabled_account_is_idle_after_apply_and_old_host_is_incompatible(self):
        self.row["group_policy"]["default"]["mode"] = "disabled"
        self.row["private_policy"]["observe"] = False
        self.owner.catalog.put(self.row)
        with patch.object(self.owner, "_plugin", AsyncMock()) as peer:
            await self.owner.pump_once()
            peer.assert_not_awaited()
        with patch.object(
            self.owner,
            "_plugin",
            AsyncMock(
                return_value=dict(
                    protocol="tianshu.bot-observation/v2",
                    instance_id=self.row["instance_id"],
                    adapter="nonebot",
                    accounts=[dict(id=self.row["account_id"])],
                )
            ),
        ):
            with self.assertRaises(Fault) as error:
                await self.owner._capabilities(self.row)
            self.assertEqual(error.exception.code, "adapter_incompatible")

    def test_current_processing_is_distinct_from_historical_read_and_binding_is_exact(self):
        source = self.owner.record(self.row, self.event())
        frame = dict(
            instance_id=self.row["instance_id"],
            self_id=self.row["account_id"],
            conversation_id="group:20002",
            actor_id="actor:a",
        )
        self.assertTrue(current(self.owner, frame)["processing_enabled"])
        stopped = copy.deepcopy(self.row)
        stopped["group_policy"]["default"]["mode"] = "disabled"
        self.owner.catalog.put(stopped)
        self.assertEqual(
            (current(self.owner, frame)["mode"], current(self.owner, frame)["code"]),
            ("disabled", None),
        )
        self.assertTrue(
            self.owner.verify(
                "Bearer memory", dict(operation="segment", **frame, sources=[source])
            )["valid"]
        )
        stopped["group_policy"]["default"] = group_policy.rule("observe")
        self.owner.catalog.put(stopped)
        self.assertEqual(current(self.owner, frame)["code"], "actor_unbound")
        stopped["group_policy"]["default"] = group_policy.rule("observe", "actor:b")
        self.owner.catalog.put(stopped)
        self.assertEqual(current(self.owner, frame)["code"], "actor_changed")

    def test_processing_channel_resolves_actual_owner_and_batch_keeps_each_result(self):
        channel = self.owner.segment_channel(self.row, "group:20002", "actor:a")
        frame = dict(channel=channel, actor_id="actor:a")
        other = dict(
            instance_id=self.row["instance_id"],
            self_id=self.row["account_id"],
            conversation_id="group:99",
            actor_id="actor:b",
        )
        request = dict(schema_version=1, request_id="processing:actual", frames=[frame, other])
        result = processing(self.owner, "Bearer memory", request)
        self.assertEqual([item["frame"] for item in result["frames"]], request["frames"])
        self.assertTrue(result["frames"][0]["processing_enabled"])
        self.assertEqual(result["frames"][1]["code"], "actor_changed")
        with self.assertRaises(Fault):
            processing(self.owner, "Bearer memory", {**request, "frames": [frame, frame]})
        external = dict(
            namespace="qq",
            binding_id="external:actual",
            channel_conversation_id="exact-import-room",
            thread_id=None,
        )
        self.p.sources.entries["external"] = dict(owner="connector:import", channel=external)
        allowed = processing(
            self.owner,
            "Bearer memory",
            {**request, "frames": [dict(channel=external, actor_id="actor:a")]},
        )["frames"][0]
        self.assertEqual((allowed["processing_enabled"], allowed["code"]), (True, "not_managed"))

    async def test_disabled_null_actor_keeps_historical_binding_and_apply_generations(self):
        proof = self.owner.record(self.row, self.event())

        async def plugin(row, operation, request):
            if operation == "capabilities":
                return dict(
                    protocol="tianshu.bot-observation/v2",
                    instance_id=row["instance_id"],
                    adapter="nonebot",
                    accounts=[dict(id=row["account_id"])],
                    group_policy_version=2,
                    ordinary_claim_available=False,
                )
            return {key: value for key, value in request.items() if key != "request_id"}

        self.owner._plugin = plugin
        stopped = dict(
            version=2,
            default=group_policy.rule("disabled"),
            groups={"20002": group_policy.rule("disabled")},
        )
        changed = await self.owner.policy(
            dict(
                id=self.row["id"],
                expected_revision=1,
                group_policy=stopped,
                private_policy=self.row["private_policy"],
                client_id="stop:actual",
            )
        )
        self.assertEqual(changed["group_policy"]["groups"]["20002"]["actor_id"], "actor:a")
        response = self.owner.verify(
            "Bearer memory",
            dict(
                operation="segment",
                actor_id="actor:a",
                instance_id=self.row["instance_id"],
                self_id=self.row["account_id"],
                conversation_id="group:20002",
                sources=[proof],
            ),
        )
        self.assertTrue(response["valid"])

    def test_existing_sqlite_tuple_constraint_migrates_without_losing_old_native_mapping(self):
        with closing(self.owner._db()) as db, db:
            db.execute("DROP TABLE reply_connections")
            db.execute(
                "CREATE TABLE reply_connections(connection_id TEXT PRIMARY KEY,observation_id TEXT NOT NULL,conversation TEXT NOT NULL,actor_id TEXT NOT NULL,UNIQUE(observation_id,conversation,actor_id))"
            )
            db.execute(
                "INSERT INTO reply_connections VALUES(?,?,?,?)",
                ("bot:old", self.row["id"], "group:20002", "actor:a"),
            )
        with patch.object(BotObservation, "_install_reply"):
            migrated = BotObservation(self.p)
        migrated._prepare_reply_scope(self.row, "group:20002", "30003", "actor:a")
        with closing(migrated._db()) as db:
            rows = db.execute("SELECT * FROM reply_connections ORDER BY connection_id").fetchall()
            self.assertEqual(len(rows), 2)
            self.assertIn("bot:old", [row["connection_id"] for row in rows])
        self.assertTrue(
            list(
                __import__("pathlib")
                .Path(self.p.store.path)
                .parent.glob("*.pre-group-modes-*.sqlite")
            )
        )

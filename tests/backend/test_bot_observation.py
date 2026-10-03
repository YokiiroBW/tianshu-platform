"""Account observation policy and source authority without chat dispatch."""

import asyncio
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from services.platform.bot_adapter_catalog import BotAdapterCatalog
from services.platform.bot_observation import BotObservation, decision
from services.platform.contracts import Fault


class Store:
    def __init__(self, path):
        self.path = str(path)

    @contextmanager
    def connect(self, **_):
        yield None


class Auth:
    def authenticate(self, header, _db, action):
        if header != "Bearer memory" or action != "observation.verify":
            raise Fault("unauthorized", 401)
        return "memory", {"kind": "service", "service": "memory"}


class Platform:
    def __init__(self, folder):
        self.store = Store(Path(folder) / "platform.sqlite")
        self.auth = Auth()
        self.bot_adapters = type(
            "Adapters",
            (),
            {
                "catalog": BotAdapterCatalog(Path(folder) / "adapters"),
                "config": {"actors": [{"id": "actor:a", "label": "A"}]},
            },
        )()


def policy(*, observe=True, mode="observe_only", names=(), actor=None):
    return {"observe": observe, "mode": mode, "list": list(names), "actor_id": actor}


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.platform = Platform(self.temp.name)
        self.manager = BotObservation(self.platform)
        self.row = {
            "kind": "observation",
            "id": "obs:test",
            "name": "Synthetic",
            "adapter": "nonebot",
            "instance_id": "instance-a",
            "account_id": "10001",
            "enabled": True,
            "revision": 1,
            "host_revision": 1,
            "observation_epoch": 1,
            "archive_epoch": 1,
            "read_enabled": True,
            "state": "ready",
            "pending": None,
            "last_error": None,
            "last_checked_at": None,
            "group_policy": policy(),
            "private_policy": policy(),
        }
        self.manager.catalog.put(self.row)
        with closing(self.manager._db()) as db, db:
            db.execute(
                "INSERT INTO observation_versions VALUES(?,?,?,?,?)", (self.row["id"], 1, 1, 1, 1)
            )

    def event(self, *, native="44", conversation="group:20002", self_id="10001"):
        return {
            "schema_version": 2,
            "platform_id": "instance-a",
            "self_id": self_id,
            "namespace": "qq",
            "conversation_id": conversation,
            "account_id": "30003",
            "event_id": native,
            "revision": 1,
            "sent_at": "2026-09-29T01:00:00Z",
            "text": "hello",
            "content_state": "text",
            "mentioned": True,
            "scope_revision": 1,
        }

    def test_decision_empty_lists_and_trigger(self):
        assert decision(policy(), "20002", True, "group") == {
            "observe": True,
            "reply_permitted": False,
            "reply_triggered": False,
        }
        assert not decision(policy(mode="whitelist"), "20002", True, "group")["reply_permitted"]
        assert decision(policy(mode="blacklist"), "20002", True, "group")["reply_triggered"]
        assert not decision(policy(mode="blacklist"), "20002", False, "group")["reply_triggered"]
        assert decision(policy(mode="blacklist"), "30003", False, "private")["reply_triggered"]

    def private_request(self, *, current=None, enabled=True, actor="actor:a"):
        self.platform.role_runtime = SimpleNamespace(active_actors=lambda: [])
        self.manager.adapters._gate = Mock()
        self.manager._disable_invalid_replies = Mock()

        async def applied(row):
            row["state"] = "ready"
            row["pending"] = None
            self.manager.catalog.put(row)
            return row

        self.manager._apply = applied
        self.row["group_policy"] = policy(mode="whitelist", names=["20002"], actor="actor:a")
        if current is not None:
            self.row["private_policy"] = current
        self.manager.catalog.put(self.row)
        event = self.event(conversation="private:30003")
        event["mentioned"] = False
        self.manager.record(self.row, event)
        return {
            "id": self.row["id"],
            "qq_id": "30003",
            "expected_revision": 1,
            "enabled": enabled,
            "actor_id": actor,
            "client_id": "private-access:1",
        }

    def private_route(self, body):
        return self.manager.route(None, "/api/web/bot-observation/private-access", body, {})

    def test_private_access_starts_only_selected_contact_and_replays(self):
        request = self.private_request(current=policy(names=["40004"], observe=False))
        result = asyncio.run(self.private_route(request))["connection"]
        self.assertEqual(
            result["private_policy"],
            policy(observe=False, mode="whitelist", names=["30003"], actor="actor:a"),
        )
        self.assertEqual(result["group_policy"], self.row["group_policy"])
        self.assertEqual(result["observation_epoch"], self.row["observation_epoch"])
        self.assertEqual(asyncio.run(self.private_route(request))["connection"], result)
        with self.assertRaises(Fault) as raised:
            asyncio.run(self.private_route(dict(request, enabled=False)))
        self.assertEqual(raised.exception.code, "idempotency_conflict")

    def test_private_access_whitelist_changes_do_not_affect_other_people(self):
        request = self.private_request(
            current=policy(mode="whitelist", names=["40004"], actor="actor:a"), actor=None
        )
        added = asyncio.run(self.private_route(request))["connection"]
        self.assertEqual(added["private_policy"]["list"], ["40004", "30003"])
        removed = asyncio.run(
            self.private_route(
                dict(request, enabled=False, expected_revision=2, client_id="private-access:2")
            )
        )["connection"]
        self.assertEqual(removed["private_policy"]["list"], ["40004"])
        self.assertEqual(removed["private_policy"]["actor_id"], "actor:a")
        self.assertEqual(removed["group_policy"], self.row["group_policy"])

    def test_private_access_blacklist_preserves_other_blocks_and_default_role(self):
        request = self.private_request(
            current=policy(mode="blacklist", names=["40004", "30003"], actor="actor:a")
        )
        allowed = asyncio.run(self.private_route(request))["connection"]
        self.assertEqual(allowed["private_policy"]["list"], ["40004"])
        blocked = asyncio.run(
            self.private_route(
                dict(request, enabled=False, expected_revision=2, client_id="private-access:2")
            )
        )["connection"]
        self.assertEqual(blocked["private_policy"]["list"], ["40004", "30003"])
        self.assertEqual(blocked["private_policy"]["actor_id"], "actor:a")

    def test_private_access_cannot_silently_change_account_role(self):
        request = self.private_request(
            current=policy(mode="whitelist", names=["40004"], actor="actor:a"), actor="actor:b"
        )
        with self.assertRaises(Fault) as raised:
            asyncio.run(self.private_route(request))
        self.assertEqual(raised.exception.code, "reply_role_conflict")
        self.assertEqual(self.manager.get(self.row["id"]), self.row)

    def test_private_access_requires_private_contact_in_current_archive(self):
        request = self.private_request()
        for body, expected in (
            (dict(request, qq_id="40004"), "private_contact_not_found"),
            (dict(request, actor_id=None), "reply_role_required"),
            (dict(request, expected_revision=0), "version_conflict"),
        ):
            with self.subTest(expected=expected), self.assertRaises(Fault) as raised:
                asyncio.run(self.private_route(body))
            self.assertEqual(raised.exception.code, expected)
        self.manager.catalog.put(dict(self.row, archive_epoch=2))
        with self.assertRaises(Fault) as raised:
            asyncio.run(self.private_route(request))
        self.assertEqual(raised.exception.code, "private_contact_not_found")
        self.assertEqual(self.manager.get(self.row["id"])["revision"], 1)

    def test_private_access_uses_existing_management_gate(self):
        request = self.private_request()
        self.manager.adapters._gate.side_effect = Fault("management_locked", 403)
        with self.assertRaises(Fault) as raised:
            asyncio.run(self.private_route(request))
        self.assertEqual(
            (raised.exception.code, raised.exception.status), ("management_locked", 403)
        )
        self.assertEqual(self.manager.get(self.row["id"]), self.row)

    def test_private_access_serializes_concurrent_updates(self):
        request = self.private_request()

        async def run():
            entered, release = asyncio.Event(), asyncio.Event()
            apply = self.manager._apply

            async def slow_apply(row):
                entered.set()
                await release.wait()
                return await apply(row)

            self.manager._apply = slow_apply
            first = asyncio.create_task(self.private_route(request))
            await entered.wait()
            second = asyncio.create_task(
                self.private_route(dict(request, enabled=False, client_id="private-access:2"))
            )
            release.set()
            await first
            with self.assertRaises(Fault) as raised:
                await second
            self.assertEqual(raised.exception.code, "version_conflict")

        asyncio.run(run())
        self.assertEqual(self.manager.get(self.row["id"])["private_policy"]["list"], ["30003"])

    def test_pause_finishes_old_source_but_revocation_denies_read(self):
        first = self.manager.record(self.row, self.event())

        def source(item):
            return {key: item[key] for key in ("source_ref", "source_digest", "scope_version")}

        assert self.manager.verify("Bearer memory", {"operation": "ingest", **source(first)})[
            "valid"
        ]
        paused = dict(self.row)
        paused["revision"] = 2
        paused["host_revision"] = 2
        paused["group_policy"] = policy(observe=False)
        self.manager.catalog.put(paused)
        # Host queued before pause; source is still an authenticated old observation.
        second = self.manager.record(paused, self.event(native="45"))
        assert self.manager.verify("Bearer memory", {"operation": "ingest", **source(second)})[
            "valid"
        ]
        page = asyncio.run(
            self.manager.discovered({"id": paused["id"], "limit": 10, "cursor": None})
        )
        assert page["items"][0]["count"] == 2
        revoked = dict(paused, archive_epoch=2, read_enabled=False, revision=3)
        self.manager.catalog.put(revoked)
        with self.assertRaises(Fault):
            self.manager.verify("Bearer memory", {"operation": "read", **source(first)})

    def test_cross_account_event_is_rejected(self):
        with self.assertRaises(Fault):
            self.manager.record(self.row, self.event(self_id="90009"))

    def test_archive_rechecks_history_after_remote_query(self):
        self.platform.settings = {
            "core": {"base_url": "https://companion.test", "token_env": "OBSERVATION_TEST_TOKEN"}
        }
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_query(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return {"items": [{"text": "old archive"}], "next_cursor": None}

        self.manager.adapters._call = slow_query

        async def run():
            task = asyncio.create_task(
                self.manager.archive(
                    {
                        "id": self.row["id"],
                        "conversation_id": "group:20002",
                        "limit": 10,
                        "cursor": None,
                    }
                )
            )
            await entered.wait()
            self.manager.catalog.put(dict(self.row, archive_epoch=2, read_enabled=False))
            release.set()
            with self.assertRaises(Fault) as raised:
                await task
            self.assertEqual(
                (raised.exception.code, raised.exception.status), ("scope_changed", 409)
            )

        with patch.dict("os.environ", {"OBSERVATION_TEST_TOKEN": "x" * 32}):
            asyncio.run(run())

    def test_policy_tightened_while_companion_accepts_never_dispatches_reply(self):
        active = dict(
            self.row, group_policy=policy(mode="whitelist", names=["20002"], actor="actor:a")
        )
        self.manager.catalog.put(active)
        entered = asyncio.Event()
        release = asyncio.Event()
        acknowledged = []

        async def plugin(_row, operation, request):
            if operation == "poll":
                return {"events": [{"id": "host:1", "event": self.event(), "reply_claimed": True}]}
            if operation == "ack":
                acknowledged.extend(request["event_ids"])
                return {"acknowledged": request["event_ids"]}
            if operation == "status":
                return {"found": True, "account": {"revision": 2, "pending": 0, "dropped": 0}}
            raise AssertionError(operation)

        async def companion(_payload):
            entered.set()
            await release.wait()
            return {
                "source_ref": _payload["source_ref"],
                "state": "accepted",
                "archive_state": "pending_memory",
            }

        async def no_replies(_row):
            return None

        self.manager._plugin = plugin
        self.manager._companion = companion
        self.manager._deliver_replies = no_replies

        async def scenario():
            task = asyncio.create_task(self.manager.pump_once())
            await entered.wait()
            tightened = dict(
                active, revision=2, host_revision=2, group_policy=policy(mode="observe_only")
            )
            self.manager.catalog.put(tightened)
            release.set()
            await task

        asyncio.run(scenario())
        self.assertEqual(acknowledged, ["host:1"])
        self.assertEqual(self.manager.get(active["id"])["state"], "ready")

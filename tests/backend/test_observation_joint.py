"""Synthetic three-product handoff with real host queue and Memory guarded ledger."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path

from services.platform.bot_adapter_catalog import BotAdapterCatalog
from services.platform.bot_observation import BotObservation

PLATFORM = Path(__file__).resolve().parents[2]
COMPANION = os.environ.get("TS_OBSERVATION_COMPANION")
MEMORY = os.environ.get("TS_OBSERVATION_MEMORY")
if COMPANION and MEMORY:
    sys.path.insert(0, str(Path(COMPANION) / "integrations" / "shared"))
    sys.path.insert(0, str(Path(COMPANION) / "src"))
    sys.path.insert(0, str(Path(MEMORY) / "src"))
    from tianshu_adapter_rpc import AdapterService, PREFIX
    from tianshu_companion.observation import Observations
    from tianshu_companion.contracts import Fault as CompanionFault
    from tianshu_memory.contracts import Contracts
    from tianshu_memory.observations import ObservationLedger
    from tianshu_memory.store import Store as MemoryStore


class PlatformStore:
    def __init__(self, path):
        self.path = str(path)

    @contextmanager
    def connect(self, **_):
        yield None


class PlatformAuth:
    def authenticate(self, header, _db, action):
        if header != "Bearer memory" or action != "observation.verify":
            raise AssertionError("unexpected verifier identity")
        return "memory", {"kind": "service", "service": "memory"}


class PlatformFixture:
    def __init__(self, root):
        self.store = PlatformStore(root / "platform.sqlite")
        self.auth = PlatformAuth()
        self.bot_adapters = type(
            "Adapters",
            (),
            {
                "catalog": BotAdapterCatalog(root / "catalog"),
                "config": {"actors": [{"id": "actor:a", "label": "A"}]},
            },
        )()


class MemoryClient:
    def __init__(self, ledger):
        self.ledger = ledger
        self.offline = True

    async def call(self, path, body, **_):
        if self.offline:
            raise CompanionFault("dependency_unavailable")
        if path == "/internal/v2/memory/observations":
            return self.ledger.ingest("companion", body)
        if path == "/internal/v2/memory/observations/query":
            return self.ledger.query("companion", body)
        raise AssertionError(path)


@unittest.skipUnless(COMPANION and MEMORY, "set both product source paths for joint run")
class ObservationJointTests(unittest.TestCase):
    def test_offline_pause_recovery_and_no_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform = PlatformFixture(root)
            manager = BotObservation(platform)
            default = {"observe": True, "mode": "observe_only", "list": [], "actor_id": None}
            row = {
                "kind": "observation",
                "id": "obs:synthetic",
                "name": "synthetic",
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
                "group_policy": default,
                "private_policy": default,
            }
            manager.catalog.put(row)
            with closing(manager._db()) as db, db:
                db.execute(
                    "INSERT INTO observation_versions VALUES(?,?,?,?,?)", (row["id"], 1, 1, 1, 1)
                )
            contracts = Contracts(Path(os.environ["TS012_CONTRACT_DIR"]))
            memory_store = MemoryStore(root / "memory.sqlite")
            memory_store.migrate_profiles(root / "profiles.bak")
            contracts.load_sources()
            memory_store.migrate_sources(root / "sources.bak", contracts)
            memory_store.migrate_observations(root / "observations.bak")
            ledger = ObservationLedger(
                memory_store, lambda request: manager.verify("Bearer memory", request)
            )
            client = MemoryClient(ledger)
            inbox = Observations(root / "companion.sqlite", client)
            sends = []

            async def accounts():
                return [{"id": "10001", "platform": "qq", "label": "synthetic"}]

            async def send(*args):
                sends.append(args)
                return "receipt"

            host = AdapterService(root / "host.sqlite", "nonebot", accounts, send)
            host.instance_id = "instance-a"

            async def scenario():
                def encoded(value):
                    return json.dumps(value, ensure_ascii=False).encode()

                header = "Bearer " + host.access_key
                status, _ = await host.handle(
                    PREFIX + "/observation/apply",
                    header,
                    encoded(
                        {
                            "request_id": "initial",
                            "account_id": "10001",
                            "revision": 1,
                            "enabled": True,
                            "group_policy": {k: default[k] for k in ("observe", "mode", "list")},
                            "private_policy": {k: default[k] for k in ("observe", "mode", "list")},
                        }
                    ),
                )
                self.assertEqual(status, 200)
                self.assertTrue(
                    await host.capture_observation(
                        "10001", "group:20002", "30003", "44", "2026-09-29T01:00:00Z", "hello", True
                    )
                )
                self.assertFalse(
                    await host.observation_claimed("10001", "group:20002", "30003", "44")
                )
                self.assertTrue(
                    await host.capture_observation(
                        "10001",
                        "private:30003",
                        "30003",
                        "45",
                        "2026-09-29T01:00:01Z",
                        "hello",
                        False,
                    )
                )
                status, polled = await host.handle(
                    PREFIX + "/observation/poll",
                    header,
                    encoded({"account_id": "10001", "limit": 20}),
                )
                self.assertEqual(status, 200)
                for item in polled["events"]:
                    source = manager.record(row, item["event"])
                    self.assertEqual(
                        inbox.ingest("platform", {**source, "event": item["event"]})[
                            "archive_state"
                        ],
                        "pending_memory",
                    )
                status, ack = await host.handle(
                    PREFIX + "/observation/ack",
                    header,
                    encoded(
                        {
                            "account_id": "10001",
                            "event_ids": [item["id"] for item in polled["events"]],
                        }
                    ),
                )
                self.assertEqual(status, 200)
                self.assertEqual(len(ack["acknowledged"]), 2)
                await inbox.flush()  # Memory offline: both stay pending.
                with closing(inbox._db()) as db:
                    self.assertEqual(
                        db.execute(
                            "SELECT count(*) FROM inbox WHERE archive_state='pending_memory'"
                        ).fetchone()[0],
                        2,
                    )
                paused = dict(
                    row, revision=2, host_revision=2, group_policy={**default, "observe": False}
                )
                manager.catalog.put(paused)
                client.offline = False
                # Advance the synthetic retry schedule without waiting in real time.
                with closing(inbox._db()) as db, db:
                    db.execute(
                        "UPDATE inbox SET next_attempt_at=0 WHERE archive_state='pending_memory'"
                    )
                await inbox.flush()  # Already accepted old sources complete after pause.
                with closing(inbox._db()) as db:
                    self.assertEqual(
                        db.execute(
                            "SELECT count(*) FROM inbox WHERE archive_state='archived'"
                        ).fetchone()[0],
                        2,
                    )
                group = ledger.query(
                    "companion",
                    {
                        "instance_id": "instance-a",
                        "self_id": "10001",
                        "conversation_id": "group:20002",
                        "limit": 20,
                        "cursor": None,
                    },
                )
                private = ledger.query(
                    "companion",
                    {
                        "instance_id": "instance-a",
                        "self_id": "10001",
                        "conversation_id": "private:30003",
                        "limit": 20,
                        "cursor": None,
                    },
                )
                self.assertEqual(len(group["items"]), 1)
                self.assertEqual(len(private["items"]), 1)
                self.assertEqual(sends, [])
                with memory_store.transaction() as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
                    self.assertEqual(
                        db.execute("SELECT count(*) FROM turn_inputs").fetchone()[0], 0
                    )

            try:
                asyncio.run(scenario())
            finally:
                host.close()

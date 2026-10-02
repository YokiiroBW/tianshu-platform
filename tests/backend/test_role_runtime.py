"""Role application has durable stages and does not publish a partial role."""

import asyncio
import json
import uuid
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from jsonschema import Draft202012Validator

from services.platform.contracts import Fault
from services.platform.bot_adapter_catalog import BotAdapterCatalog
from services.platform.bot_observation import BotObservation, decision
from services.platform.role_runtime import RoleRuntime

CONTRACT = Path(__file__).resolve().parents[2] / "docs/contracts-candidates/role-runtime/v1"


def test_candidate_examples_match_exchange_schema():
    schema = json.loads((CONTRACT / "schema.json").read_text(encoding="utf-8"))
    examples = json.loads((CONTRACT / "examples.json").read_text(encoding="utf-8"))
    for name, example in examples.items():
        Draft202012Validator({**schema, "$ref": f"#/$defs/{name}"}).validate(example)


class LocalWork:
    async def run(self, function, *args):
        return function(*args)


class Catalog:
    def view(self):
        return {
            "providers": [
                {"provider_id": "provider-a", "name": "Provider A", "revision": 2, "model_id": "model-a", "ready": True},
                {"provider_id": "provider-b", "name": "Provider B", "revision": 4, "model_id": "model-b", "ready": True},
            ],
            "default": {"configured": True, "provider_id": "provider-a", "provider_revision": 2},
        }

    def _selectable(self, provider):
        return provider["ready"]


class Peers:
    def __init__(self):
        self.roles = {}
        self.grants = {}
        self.receipts = {}

    async def call(self, settings, path, payload):
        actor = payload.get("actor_id")
        if path.endswith("/role-runtime/manage"):
            if payload["operation"] == "list":
                return {
                    "roles": list(self.roles.values()),
                    "legacy_roles": [{"id": "actor:household", "name": "Household", "version": 1,
                                      "published_revision": "revision-household"}],
                    "profiles": [
                        {
                            "id": "persona-profile:a",
                            "name": "Profile",
                            "version": 1,
                            "draft_revision": "revision-profile-a",
                            "published_revision": None,
                        },
                    ],
                }
            request_id = payload["request_id"]
            if request_id in self.receipts:
                return self.receipts[request_id]
            current = self.roles.get(actor)
            assert (current or {}).get("version", 0) == payload["expected_version"]
            result = {
                "actor_id": actor,
                "version": payload["expected_version"] + 1,
                "enabled": payload["enabled"],
                "name": payload["name"],
                "profile_id": payload["profile_id"],
                "profile_version": payload["profile_version"],
                "application_id": payload["application_id"],
                "operator": payload["operator"],
                "capabilities": payload["capabilities"],
                "profile_revision": "revision-household" if payload["profile_id"] is None else "revision-profile-a",
            }
            self.roles[actor] = result
            self.receipts[request_id] = result
            return result
        if payload.get("operation") == "status":
            return self.grants.get(actor, {"actor_id": actor, "version": 0, "enabled": False})
        request_id = payload["request_id"]
        if request_id in self.receipts:
            return self.receipts[request_id]
        current = self.grants.get(actor)
        assert (current or {}).get("version", 0) == payload["expected_version"]
        result = {
            "actor_id": actor,
            "version": payload["expected_version"] + 1,
            "enabled": payload["enabled"],
        }
        self.grants[actor] = result
        self.receipts[request_id] = result
        return result


def fixture(tmp_path):
    settings = {
        "role_runtime": {
            "enabled": True,
            "memory": {"base_url": "https://memory.test", "token_env": "MEMORY_ROLE_TOKEN"},
        },
        "core": {"base_url": "https://companion.test", "token_env": "CORE_ROLE_TOKEN"},
        "web": {"input_entries": ["web-input"]},
    }
    entry = {
        "actor_id": "actor:household",
        "owner": "admin",
        "account": {"namespace": "web", "immutable_account_id": "owner"},
        "channel": {
            "namespace": "web",
            "binding_id": "web-binding",
            "channel_conversation_id": "private:owner",
            "thread_id": None,
        },
        "audience": "self_private",
        "kind": "local_operator",
        "ttl_seconds": 60,
        "routes": [],
    }
    source = {
        "owner": "admin",
        "account": entry["account"],
        "channel": entry["channel"],
        "audience": "self_private",
        "ttl_seconds": 60,
        "actor_entries": ["household"],
        "default_actor_ids": ["actor:household"],
        "routing_version": 1,
    }
    platform = SimpleNamespace(
        settings=settings,
        store=SimpleNamespace(path=str(tmp_path / "platform.sqlite")),
        provider_catalog=Catalog(),
        local_work=LocalWork(),
        auth=SimpleNamespace(
            principals={"admin": {"actions": ["role.manage"]}}, entries={"household": entry}
        ),
        sources=SimpleNamespace(entries={"web-input": source}),
    )
    manager = RoleRuntime(platform)
    console = SimpleNamespace(
        config={"principal": "admin"},
        input_entries=["web-input"],
        session_valid=lambda session: True,
    )
    manager.console = console
    peers = Peers()
    manager._remote = peers.call
    return platform, manager, console, peers


def body(provider, *, actor=None, version=0, enabled=True):
    return {
        "client_id": str(uuid.uuid4()),
        "actor_id": actor,
        "expected_version": version,
        "name": provider,
        "profile_id": "persona-profile:a",
        "profile_version": 1,
        "provider_id": provider,
        "provider_revision": 2 if provider == "provider-a" else 4,
        "enabled": enabled,
        "capabilities": ["dialogue", "memory.read"],
    }


def test_two_roles_apply_replay_restart_and_disable(tmp_path):
    async def scenario():
        platform, manager, console, peers = fixture(tmp_path)
        a, b = body("provider-a"), body("provider-b")
        first = (await manager.route(console, "/api/web/roles/apply", a, {}))["role"]
        second = (await manager.route(console, "/api/web/roles/apply", b, {}))["role"]
        assert first["state"] == second["state"] == "active"
        assert first["provider_id"] != second["provider_id"]
        assert len(manager.active_actors()) == 2
        assert len(console.input_entries) == 3
        assert (await manager.route(console, "/api/web/roles/apply", a, {}))["role"] == first
        manager.config["enabled"] = False
        assert not manager.active(first["actor_id"])
        assert manager.active_actors() == []
        manager.config["enabled"] = True
        restarted = RoleRuntime(platform)
        restarted.console = SimpleNamespace(
            config={"principal": "admin"},
            input_entries=["web-input"],
            session_valid=lambda session: True,
        )
        restarted._remote = peers.call
        assert not restarted.active(first["actor_id"])
        await restarted.resume_pending()
        assert restarted.active(first["actor_id"])
        assert restarted.active(second["actor_id"])
        disable = body("provider-a", actor=first["actor_id"], version=1, enabled=False)
        result = (await restarted.route(restarted.console, "/api/web/roles/apply", disable, {}))[
            "role"
        ]
        assert result["state"] == "disabled"
        assert not restarted.active(first["actor_id"])
        assert restarted.active(second["actor_id"])
        assert not peers.grants[first["actor_id"]]["enabled"]

    asyncio.run(scenario())


def test_preapply_provider_failure_can_be_reconfigured(tmp_path):
    async def scenario():
        _, manager, console, _ = fixture(tmp_path)
        original = manager._provider

        def unavailable(row):
            raise Fault("provider_unavailable", 409)

        manager._provider = unavailable
        first = body("provider-a")
        failed = (await manager.route(console, "/api/web/roles/apply", first, {}))["role"]
        assert failed["state"] == "failed"
        assert failed["stage"] == "start"
        assert failed["error_code"] == "provider_unavailable"
        assert manager.active_actors() == []
        manager._provider = original
        fixed = body("provider-b", actor=failed["actor_id"], version=1)
        active = (await manager.route(console, "/api/web/roles/apply", fixed, {}))["role"]
        assert active["state"] == "active"
        assert active["provider_id"] == "provider-b"

    asyncio.run(scenario())


def test_adopt_existing_actor_keeps_static_web_source(tmp_path):
    async def scenario():
        platform, manager, console, peers = fixture(tmp_path)
        initial = (await manager.route(console, "/api/web/roles/view", {}, {}))
        assert initial["legacy_roles"][0]["id"] == "actor:household"
        adopt = body("provider-b", actor="actor:household")
        adopt["profile_id"] = None
        adopt["profile_version"] = None
        active = (await manager.route(console, "/api/web/roles/apply", adopt, {}))["role"]
        assert active["state"] == "active"
        assert active["legacy"] is True
        assert active["profile_revision"] == "revision-household"
        assert active["provider_id"] == "provider-b"
        assert console.input_entries == ["web-input"]
        assert platform.auth.entries["household"]["actor_id"] == "actor:household"
        assert peers.grants["actor:household"]["enabled"]
        disabled = body("provider-b", actor="actor:household", version=1, enabled=False)
        disabled["profile_id"] = None
        disabled["profile_version"] = None
        result = (await manager.route(console, "/api/web/roles/apply", disabled, {}))["role"]
        assert result["state"] == "disabled"
        assert console.input_entries == ["web-input"]
        assert not peers.grants["actor:household"]["enabled"]
        restarted = RoleRuntime(platform)
        assert not restarted.active("actor:household")
        assert restarted.get("actor:household")["state"] == "disabled"

    asyncio.run(scenario())


def test_lost_peer_receipts_replay_exact_stage_without_second_effect(tmp_path):
    async def scenario():
        _, manager, console, peers = fixture(tmp_path)
        original = peers.call
        lost = {"pause": False, "memory": False}

        async def lose_once(settings, path, payload):
            result = await original(settings, path, payload)
            request_id = payload.get("request_id", "")
            for stage in lost:
                if request_id.endswith(":" + stage) and not lost[stage]:
                    lost[stage] = True
                    raise Fault("dependency_unavailable", 503)
            return result

        manager._remote = lose_once
        request = body("provider-a")
        pending = (await manager.route(console, "/api/web/roles/apply", request, {}))["role"]
        assert pending["state"] == "pending" and pending["stage"] == "start"
        first_retry = (await manager.route(console, "/api/web/roles/retry", {
            "actor_id": pending["actor_id"], "client_id": request["client_id"],
        }, {}))["role"]
        assert first_retry["state"] == "pending" and first_retry["stage"] == "core_paused"
        active = (await manager.route(console, "/api/web/roles/retry", {
            "actor_id": pending["actor_id"], "client_id": request["client_id"],
        }, {}))["role"]
        assert active["state"] == "active"
        assert active["companion_version"] == 2
        assert active["memory_version"] == 1
        assert peers.roles[active["actor_id"]]["version"] == 2
        assert peers.grants[active["actor_id"]]["version"] == 1
        assert lost == {"pause": True, "memory": True}

    asyncio.run(scenario())


def test_enabling_role_does_not_promote_observe_only_account(tmp_path):
    async def scenario():
        platform, manager, console, _ = fixture(tmp_path)
        platform.bot_adapters = SimpleNamespace(
            catalog=BotAdapterCatalog(tmp_path / "adapters"),
            config={"actors": [{"id": "actor:household", "label": "Household"}]},
        )
        observation = BotObservation(platform)
        observe_only = {"observe": True, "mode": "observe_only", "list": [], "actor_id": None}
        row = {
            "kind": "observation", "id": "obs:existing", "name": "Existing observation",
            "adapter": "nonebot", "instance_id": "synthetic", "account_id": "10001",
            "enabled": True, "revision": 1, "host_revision": 1,
            "observation_epoch": 1, "archive_epoch": 1, "read_enabled": True,
            "state": "ready", "pending": None, "last_error": None,
            "last_checked_at": None, "group_policy": observe_only,
            "private_policy": observe_only,
        }
        observation.catalog.put(row)
        before = observation.get(row["id"])
        result = (await manager.route(console, "/api/web/roles/apply",
                                      body("provider-a"), {}))["role"]
        assert result["state"] == "active"
        assert observation.get(row["id"]) == before
        assert not decision(observation.get(row["id"])["group_policy"], "20002", True,
                            "group")["reply_permitted"]
        with closing(observation._db()) as db:
            assert db.execute("SELECT COUNT(*) FROM reply_connections").fetchone()[0] == 0

    asyncio.run(scenario())


def test_cancel_pending_after_provider_revocation_reboots_and_fences_old_intent(tmp_path):
    async def scenario():
        platform, manager, console, peers = fixture(tmp_path)
        original_remote = peers.call
        lost = False

        async def lose_memory_receipt(settings, path, payload):
            nonlocal lost
            result = await original_remote(settings, path, payload)
            if path.endswith("/role-runtime/authorize") and payload.get("request_id", "").endswith(":memory") and not lost:
                lost = True
                raise Fault("dependency_unavailable", 503)
            return result

        manager._remote = lose_memory_receipt
        original = body("provider-a")
        pending = (await manager.route(console, "/api/web/roles/apply", original, {}))["role"]
        assert pending["state"] == "pending" and pending["stage"] == "core_paused"
        assert peers.grants[pending["actor_id"]]["enabled"]
        manager._remote = original_remote
        original_provider = manager._provider

        def revoked(_):
            raise Fault("provider_unavailable", 409)

        manager._provider = revoked
        still_pending = (await manager.route(console, "/api/web/roles/retry", {
            "actor_id": pending["actor_id"], "client_id": original["client_id"],
        }, {}))["role"]
        assert still_pending["stage"] == "memory_applied"
        assert still_pending["error_code"] == "provider_unavailable"
        cancel = {"actor_id": pending["actor_id"], "expected_version": 1,
                  "client_id": str(uuid.uuid4())}
        lost_cancel_core = False
        lost_cancel_memory = False

        async def lose_cancel_receipts(settings, path, payload):
            nonlocal lost_cancel_core, lost_cancel_memory
            result = await original_remote(settings, path, payload)
            request_id = payload.get("request_id", "")
            if ":cancel-core:" in request_id and not lost_cancel_core:
                lost_cancel_core = True
                raise Fault("dependency_unavailable", 503)
            if ":cancel-memory:" in request_id and not lost_cancel_memory:
                lost_cancel_memory = True
                raise Fault("dependency_unavailable", 503)
            return result

        manager._remote = lose_cancel_receipts
        cancelling = (await manager.route(console, "/api/web/roles/cancel", cancel, {}))["role"]
        assert cancelling["state"] == "pending"
        assert cancelling["stage"] == "cancel_core"
        assert not peers.roles[pending["actor_id"]]["enabled"]
        restarted = RoleRuntime(platform)
        restarted.console = SimpleNamespace(
            config={"principal": "admin"}, input_entries=["web-input"],
            session_valid=lambda session: True,
        )
        restarted._remote = lose_cancel_receipts
        restarted._provider = revoked
        await restarted.resume_pending()
        assert restarted.get(pending["actor_id"])["stage"] == "cancel_memory"
        assert not peers.grants[pending["actor_id"]]["enabled"]
        restarted = RoleRuntime(platform)
        restarted.console = SimpleNamespace(
            config={"principal": "admin"}, input_entries=["web-input"],
            session_valid=lambda session: True,
        )
        restarted._remote = original_remote
        restarted._provider = revoked
        await restarted.resume_pending()
        disabled = restarted.get(pending["actor_id"])
        assert disabled["state"] == "disabled"
        assert not peers.grants[pending["actor_id"]]["enabled"]
        assert not restarted.active(pending["actor_id"])
        assert (await restarted.route(restarted.console, "/api/web/roles/apply", original,
                                     {}))["role"]["state"] == "disabled"
        try:
            await restarted.route(restarted.console, "/api/web/roles/retry", {
                "actor_id": pending["actor_id"], "client_id": original["client_id"],
            }, {})
        except Fault as error:
            assert error.code == "forbidden"
        else:
            raise AssertionError("Old retry must not bypass cancel")
        restarted._provider = original_provider
        corrected = body("provider-b", actor=pending["actor_id"], version=2)
        active = (await restarted.route(restarted.console, "/api/web/roles/apply",
                                        corrected, {}))["role"]
        assert active["state"] == "active" and active["provider_id"] == "provider-b"

    asyncio.run(scenario())


def test_active_disable_waits_for_both_denials_without_provider_or_profile(tmp_path):
    async def scenario():
        platform, manager, console, peers = fixture(tmp_path)
        original = body("provider-a")
        active = (await manager.route(console, "/api/web/roles/apply", original, {}))["role"]
        actor = active["actor_id"]

        def revoked(_):
            raise AssertionError("A disabled role must not inspect its provider")

        manager._provider = revoked
        original_remote = peers.call
        grant_unavailable = True

        async def fail_memory_deny(settings, path, payload):
            if (grant_unavailable and path.endswith("/role-runtime/authorize")
                    and ":cancel-memory:" in payload.get("request_id", "")):
                raise Fault("dependency_unavailable", 503)
            return await original_remote(settings, path, payload)

        manager._remote = fail_memory_deny
        disable = {**original, "client_id": str(uuid.uuid4()), "actor_id": actor,
                   "expected_version": 1, "enabled": False,
                   "profile_version": 999, "provider_revision": 999}
        pending = (await manager.route(console, "/api/web/roles/apply", disable, {}))["role"]
        assert pending["state"] == "pending"
        assert pending["stage"] == "cancel_memory"
        assert pending["profile_version"] == original["profile_version"]
        assert not manager.active(actor)
        assert not peers.roles[actor]["enabled"]
        assert peers.grants[actor]["enabled"]
        grant_unavailable = False
        restarted = RoleRuntime(platform)
        restarted.console = SimpleNamespace(
            config={"principal": "admin"}, input_entries=["web-input"],
            session_valid=lambda session: True,
        )
        restarted._remote = original_remote
        restarted._provider = revoked
        await restarted.resume_pending()
        assert restarted.get(actor)["state"] == "disabled"
        assert not peers.grants[actor]["enabled"]
        assert (await restarted.route(restarted.console, "/api/web/roles/apply", original,
                                     {}))["role"]["state"] == "disabled"

    asyncio.run(scenario())


def test_cancel_first_static_adoption_writes_core_denial_before_memory(tmp_path):
    async def scenario(committed):
        directory = tmp_path / str(committed)
        directory.mkdir()
        platform, manager, console, peers = fixture(directory)
        original_remote = peers.call
        request = body("provider-a", actor="actor:household")
        request["profile_id"] = None
        request["profile_version"] = None

        async def lose_first_core_pause(settings, path, payload):
            if payload.get("request_id") == request["client_id"] + ":pause":
                if committed:
                    await original_remote(settings, path, payload)
                raise Fault("dependency_unavailable", 503)
            return await original_remote(settings, path, payload)

        manager._remote = lose_first_core_pause
        pending = (await manager.route(console, "/api/web/roles/apply", request, {}))["role"]
        assert pending["state"] == "pending" and pending["stage"] == "start"
        assert ("actor:household" in peers.roles) == committed
        manager._remote = original_remote
        cancel = {"client_id": str(uuid.uuid4()), "actor_id": "actor:household",
                  "expected_version": pending["version"]}
        stopped = (await manager.route(console, "/api/web/roles/cancel", cancel, {}))["role"]
        assert stopped["state"] == "disabled"
        assert peers.roles["actor:household"]["enabled"] is False
        assert peers.roles["actor:household"]["profile_id"] is None
        assert peers.roles["actor:household"]["version"] == (2 if committed else 1)
        assert peers.grants["actor:household"]["enabled"] is False
        assert (await manager.route(console, "/api/web/roles/apply", request,
                                    {}))["role"]["state"] == "disabled"
        restarted = RoleRuntime(platform)
        assert restarted.get("actor:household")["state"] == "disabled"

    asyncio.run(scenario(False))
    asyncio.run(scenario(True))


def test_cancel_uncreated_dynamic_role_does_not_write_core_role(tmp_path):
    async def scenario():
        _, manager, console, peers = fixture(tmp_path)
        original_remote = peers.call
        request = body("provider-a")

        async def unavailable_first_pause(settings, path, payload):
            if payload.get("request_id") == request["client_id"] + ":pause":
                raise Fault("dependency_unavailable", 503)
            return await original_remote(settings, path, payload)

        manager._remote = unavailable_first_pause
        pending = (await manager.route(console, "/api/web/roles/apply", request, {}))["role"]
        assert pending["state"] == "pending" and pending["stage"] == "start"
        manager._remote = original_remote
        stopped = (await manager.route(console, "/api/web/roles/cancel", {
            "client_id": str(uuid.uuid4()), "actor_id": pending["actor_id"],
            "expected_version": pending["version"],
        }, {}))["role"]
        assert stopped["state"] == "disabled"
        assert pending["actor_id"] not in peers.roles
        assert peers.grants[pending["actor_id"]]["enabled"] is False

    asyncio.run(scenario())


def test_role_stage_sqlite_wait_keeps_loop_responsive_and_retryable(tmp_path):
    import sqlite3
    import threading
    import time

    from services.platform.local_work import LocalWork as RealLocalWork

    async def scenario():
        platform, manager, console, peers = fixture(tmp_path)
        pool = RealLocalWork()
        platform.local_work = pool
        locked = threading.Event()
        released = threading.Event()
        writer = None

        async def remote(settings, path, payload):
            nonlocal writer
            answer = await peers.call(settings, path, payload)
            if payload.get("request_id", "").endswith(":pause"):

                def hold_lock():
                    with sqlite3.connect(manager.path) as db:
                        db.execute("BEGIN IMMEDIATE")
                        locked.set()
                        time.sleep(0.3)
                    released.set()

                writer = threading.Thread(target=hold_lock)
                writer.start()
                assert await asyncio.to_thread(locked.wait, 2)
            return answer

        manager._remote = remote
        request = body("provider-a")
        task = asyncio.create_task(manager.route(console, "/api/web/roles/apply", request, {}))
        try:
            assert await asyncio.to_thread(locked.wait, 2)
            pulses = 0
            while not released.is_set():
                await asyncio.sleep(0.01)
                if not released.is_set():
                    pulses += 1
            assert pulses >= 5, "SQLite stage write blocked the event loop"
            result = (await task)["role"]
            assert result["state"] == "active"
            assert manager.get(result["actor_id"])["state"] == "active"
            assert (await manager.route(console, "/api/web/roles/apply", request, {}))[
                "role"
            ] == result
        finally:
            if writer:
                await asyncio.to_thread(writer.join, 2)
            pool.close()

    asyncio.run(scenario())


def test_running_role_without_dialogue_does_not_require_a_model_or_receive_web_binding(tmp_path):
    async def scenario():
        platform, manager, console, peers = fixture(tmp_path)
        platform.provider_catalog.view = lambda: {"providers": [], "default": {"configured": False}}
        request = body("provider-a")
        request.update(capabilities=[], provider_id=None, provider_revision=None)
        role = (await manager.route(console, "/api/web/roles/apply", request, {}))["role"]
        assert role["state"] == "active"
        assert peers.roles[role["actor_id"]]["capabilities"] == []
        assert peers.roles[role["actor_id"]]["enabled"] is True
        assert manager.active(role["actor_id"])
        assert console.input_entries == ["web-input"]
        assert not any(key.startswith("role-") for key in platform.auth.entries)
    asyncio.run(scenario())

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

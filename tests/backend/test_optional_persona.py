import asyncio
import json
import uuid

import pytest
from jsonschema import Draft202012Validator

from services.platform.contracts import Fault
from services.platform.role_runtime import RoleRuntime
from test_role_runtime import CONTRACT, body, fixture


def test_optional_profile_create_replay_reopen_bind_and_validate(tmp_path):
    async def scenario():
        platform, manager, console, peers = fixture(tmp_path)
        request = {**body("provider-a", enabled=False), "profile_id": None, "profile_version": None,
                   "provider_id": None, "provider_revision": None}
        schema = json.loads((CONTRACT / "schema.json").read_text())
        Draft202012Validator({**schema, "$ref": "#/$defs/platform_apply"}).validate(request)
        first = (await manager.route(console, "/api/web/roles/apply", request, {}))["role"]
        assert first["state"] == "disabled" and first["profile_id"] is None
        assert manager.active_actors() == [] and console.input_entries == ["web-input"]
        assert not peers.grants[first["actor_id"]]["enabled"]
        assert (await manager.route(console, "/api/web/roles/apply", request, {}))["role"] == first
        reopened = RoleRuntime(platform)
        reopened.console = console
        reopened._remote = peers.call
        assert reopened.get(first["actor_id"])["profile_id"] is None
        attach = body("provider-a", actor=first["actor_id"], version=1, enabled=False)
        bound = (await reopened.route(console, "/api/web/roles/apply", attach, {}))["role"]
        assert bound["profile_id"] == "persona-profile:a" and bound["state"] == "disabled"
        for changes in ({"name": " "}, {"profile_version": 1}):
            with pytest.raises(Fault) as error:
                manager._begin({**request, **changes, "client_id": str(uuid.uuid4())})
            assert error.value.code == "invalid_input"
    asyncio.run(scenario())

"""Management use cases reuse current roles, confirmed QQ identities and origin issuance."""

import copy
import uuid

from ..contracts import digest, require
from .client import Client
from .contract import Contract


class Relationships:
    def __init__(self, platform):
        self.p = platform
        self.config = copy.deepcopy(platform.settings.get("web_relationships"))
        self.client = (
            Client(
                self.config["memory"],
                Contract(self.config["candidate_schema_path"]),
                platform.other_credentials,
            )
            if self.config and self.config["enabled"]
            else None
        )

    def gate(self, console, session):
        require(console.session_valid(session), "session_expired", 401)
        require(
            self.client is not None and self.config == self.p.settings.get("web_relationships"),
            "relationships_not_configured",
            503,
        )
        principal = self.p.auth.principals.get(console.config["principal"], {})
        require(
            principal.get("kind") == "operator"
            and principal.get("service") == "platform"
            and {"role.manage", "memory.read", "qq.admin.view"}
            <= set(principal.get("actions", [])),
            "forbidden",
            403,
        )
        console.memory.prove_access(session)
        self.p.qq_admin.check_access(console, session, "qq.admin.view")
        return digest([self.config, self.client.credential(), principal])

    async def people(self, console, session, after):
        pin = await self.p.local_work.run(self.gate, console, session)
        answer = await self.p.qq_admin.route(
            console, "/api/web/qq-admin/profiles", {"limit": 100, "after": after}, session
        )
        require(
            pin == await self.p.local_work.run(self.gate, console, session), "scope_changed", 409
        )
        return {
            "items": [
                {
                    "id": item["person_id"],
                    "label": item["display_name"] or item["qq_id"],
                    "after": after,
                }
                for item in answer["items"]
            ],
            "next_after": answer["next_cursor"],
        }

    async def route(self, console, name, body, session):
        require(type(body) is dict, "invalid_input", 400)
        pin = await self.p.local_work.run(self.gate, console, session)
        if name == "catalog":
            require(body == {}, "invalid_input", 400)
            people = await self.people(console, session, None)
            state = await self.p.local_work.run(console.memory.state)
            return {"roles": state["roles"], **people}
        if name == "people":
            require(set(body) == {"after"}, "invalid_input", 400)
            return await self.people(console, session, body["after"])
        selection = {"role_id", "role_version", "person_id", "people_after"}
        extra = {"command", "client_id"} if name == "manage" else set()
        require(name in {"view", "manage"} and set(body) == selection | extra, "invalid_input", 400)
        require(
            type(body["role_id"]) is str
            and type(body["role_version"]) is int
            and type(body["person_id"]) is str,
            "invalid_input",
            400,
        )
        roles = await self.p.local_work.run(
            console.memory.role_choice, body["role_id"], body["role_version"]
        )
        require(roles["available"], "forbidden", 403)
        people = await self.people(console, session, body["people_after"])
        require(body["person_id"] in {item["id"] for item in people["items"]}, "forbidden", 403)
        actor, version = body["role_id"], body["role_version"]
        async with console.memory.scoped_origin(actor, version, session) as (reference, _):
            require(
                pin == await self.p.local_work.run(self.gate, console, session),
                "scope_changed",
                409,
            )
            pair = {"actor_id": actor, "person_id": body["person_id"]}
            payload = {
                "schema_version": 1,
                "request_id": "relationship:" + uuid.uuid4().hex,
                "origin": {"assertion_ref": reference},
            }
            if name == "view":
                payload.update(pair=pair, managed=True)
                result = await self.client.call("history", payload)
            else:
                require(
                    type(body["client_id"]) is str
                    and 1 <= len(body["client_id"]) <= 64
                    and type(body["command"]) is dict,
                    "invalid_input",
                    400,
                )
                require(not ({"pair", "request_id"} & set(body["command"])), "invalid_input", 400)
                request_id = "relationship:" + digest(
                    [console.config["principal"], body["client_id"]]
                )
                command = dict(body["command"], pair=pair, request_id=request_id)
                self.client.contract.check("RelationshipCommand", command)
                payload.update(request_id=request_id, command=command)
                result = {"projection": await self.client.call("manage", payload)}
            require(
                pin == await self.p.local_work.run(self.gate, console, session),
                "scope_changed",
                409,
            )
        return result

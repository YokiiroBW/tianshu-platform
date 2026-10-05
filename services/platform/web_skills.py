"""Role-authorized skill management and existing origin-bound credential storage."""

import copy
import uuid

from .contracts import require


class WebSkills:
    def __init__(self, life):
        self.life, self.p = life, life.p

    async def catalog(self, actor):
        request = {
            "schema_version": 1,
            "request_id": "skills-read:" + uuid.uuid4().hex,
            "actor_id": actor,
            "resource": "list",
        }
        self.p.contracts.check("skills#read_request", request)
        return await self.life._call("skills/read", request, management=True)

    @staticmethod
    def target(result, kind, identifier):
        if kind == "skill":
            item = next((row for row in result["skills"] if row["id"] == identifier), None)
            require(item is not None, "not_found", 404)
            return item["config"], item
        item = next((row for row in result["sources"] if row["source_id"] == identifier), {})
        return item, item

    async def route(self, name, body, session):
        require(
            isinstance(body, dict) and isinstance(body.get("actor_id"), str),
            "invalid_input",
            400,
        )
        actor = body["actor_id"]
        async with self.life.scoped_actor(actor, session):
            await self.p.local_work.run(self.life.guard, session)
            request = {"schema_version": 1, "actor_id": actor}
            if name == "read":
                require(
                    {"actor_id", "resource"} <= set(body)
                    and set(body) <= {"actor_id", "resource", "skill_id"},
                    "invalid_input",
                    400,
                )
                request.update(
                    request_id="skills-read:" + uuid.uuid4().hex,
                    resource=body["resource"],
                    **({"skill_id": body["skill_id"]} if "skill_id" in body else {}),
                )
                endpoint = "read"
            elif name == "credentials":
                kind = "skill" if "skill_id" in body else "source"
                field = kind + "_id"
                require(
                    set(body) == {"actor_id", field}
                    and isinstance(body[field], str)
                    and 1 <= len(body[field]) <= 128,
                    "invalid_input",
                    400,
                )
                result = (await self.catalog(actor))["result"]
                await self.p.local_work.run(self.life.guard, session)
                current, _ = self.target(result, kind, body[field])
                return await self.p.local_work.run(
                    self.p.service_credentials.view_skill,
                    actor,
                    kind + ":" + body[field],
                    current,
                )
            elif name == "manage":
                require(
                    set(body) == {"actor_id", "operation", "expected_version", "value", "client_id"}
                    and body["operation"] in {"skill.enable", "skill.disable", "source.refresh"},
                    "invalid_input",
                    400,
                )
                request.update(
                    request_id=body["client_id"],
                    operation=body["operation"],
                    expected_version=body["expected_version"],
                    value=body["value"],
                )
                endpoint = "manage"
            else:
                require(
                    name == "configure"
                    and set(body)
                    == {
                        "actor_id",
                        "operation",
                        "value",
                        "credential",
                        "catalog_revision",
                        "expected_version",
                        "client_id",
                    }
                    and body["operation"] in {"skill.update", "source.configure"}
                    and isinstance(body["value"], dict),
                    "invalid_input",
                    400,
                )
                value = copy.deepcopy(body["value"])
                if body["operation"] == "skill.update":
                    require(
                        set(value) == {"definition", "enabled", "config"}
                        and isinstance(value["definition"], dict)
                        and isinstance(value["config"], dict)
                        and set(value["config"]) == {"provider", "base_url", "options"},
                        "invalid_input",
                        400,
                    )
                    kind, identifier = "skill", value["definition"].get("id")
                    config = value["config"]
                    url = config["base_url"]
                else:
                    require(
                        set(value)
                        == {
                            "source_id",
                            "name",
                            "manifest_url",
                            "enabled",
                            "expected_sha256",
                        },
                        "invalid_input",
                        400,
                    )
                    kind, identifier = "source", value["source_id"]
                    config, url = value, value["manifest_url"]
                config["credential_ref"] = None
                request.update(
                    request_id=body["client_id"],
                    operation=body["operation"],
                    expected_version=body["expected_version"],
                    value=value,
                )
                self.p.contracts.check("skills#manage_request", request)
                result = (await self.catalog(actor))["result"]
                await self.p.local_work.run(self.life.guard, session)
                current, item = self.target(result, kind, identifier)
                same_version = body["expected_version"] == result["actor_version"]
                if kind == "skill":
                    require(
                        item["definition"]["handler_id"] != "image.generate",
                        "invalid_input",
                        400,
                    )
                    if same_version:
                        require(value["definition"] == item["definition"], "version_conflict", 409)
                target = kind + ":" + identifier
                if same_version:
                    reference = await self.p.local_work.run(
                        self.p.service_credentials.save_skill,
                        actor,
                        target,
                        url,
                        body["credential"],
                        body["catalog_revision"],
                        body["client_id"],
                        current,
                    )
                else:
                    # The owner's existing receipt resolves a replay; a new stale
                    # form is rejected without touching encrypted credentials.
                    reference = await self.p.local_work.run(
                        self.p.service_credentials.skill_reference,
                        actor,
                        target,
                        url,
                        body["credential"],
                        current,
                    )
                config["credential_ref"] = reference
                endpoint = "manage"
            self.p.contracts.check("skills#" + endpoint + "_request", request)
            answer = await self.life._call("skills/" + endpoint, request, management=True)
            await self.p.local_work.run(self.life.guard, session)
            return answer

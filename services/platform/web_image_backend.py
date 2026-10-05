"""Actor-scoped image management over the existing Life authority and credential store."""

import uuid

from .contracts import require


class WebImageBackend:
    def __init__(self, life):
        self.life = life
        self.p = life.p

    async def connection(self, actor):
        request = {
            "schema_version": 1,
            "actor_id": actor,
            "request_id": "image-connection:" + uuid.uuid4().hex,
            "resource": "status",
            "workflow_id": None,
        }
        self.p.contracts.check("image-backend#read_request", request)
        return await self.life._call("image-backend/read", request, management=True)

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
            if name == "status":
                require(set(body) == {"actor_id"}, "invalid_input", 400)
                connection = await self.connection(actor)
                await self.p.local_work.run(self.life.guard, session)
                return await self.p.local_work.run(
                    self.p.service_credentials.view_connection, connection["result"]
                )
            if name == "save":
                require(
                    set(body)
                    == {
                        "actor_id",
                        "value",
                        "credential",
                        "catalog_revision",
                        "expected_version",
                        "client_id",
                    }
                    and isinstance(body["value"], dict)
                    and set(body["value"]) == {"base_url", "enabled"},
                    "invalid_input",
                    400,
                )
                request.update(
                    request_id=body["client_id"],
                    operation="connection.configure",
                    expected_version=body["expected_version"],
                    value={**body["value"], "credential_ref": None},
                )
                # Validate before touching the encrypted catalog. The browser never
                # supplies a reference and only Companion can resolve the saved token.
                self.p.contracts.check("image-backend#manage_request", request)
                connection = await self.connection(actor)
                await self.p.local_work.run(self.life.guard, session)
                if body["expected_version"] != connection["result"].get("version"):
                    # Let the owner's existing receipt return an accepted original
                    # request, or reject a stale new form, before changing secrets.
                    reference = await self.p.local_work.run(
                        self.p.service_credentials.connection_reference,
                        body["value"],
                        body["credential"],
                    )
                else:
                    reference = await self.p.local_work.run(
                        self.p.service_credentials.save_connection,
                        body["value"],
                        body["credential"],
                        body["catalog_revision"],
                        body["client_id"],
                        connection["result"],
                    )
                request["value"]["credential_ref"] = reference
                endpoint = "manage"
            elif name == "manage":
                require(
                    set(body) == {"actor_id", "operation", "expected_version", "value", "client_id"}
                    and body["operation"] != "connection.configure",
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
            elif name == "read":
                require(set(body) == {"actor_id", "resource", "workflow_id"}, "invalid_input", 400)
                request.update(
                    request_id="image-read:" + uuid.uuid4().hex,
                    resource=body["resource"],
                    workflow_id=body["workflow_id"],
                )
                endpoint = "read"
            else:
                require(
                    name == "compile"
                    and set(body) == {"actor_id", "intent", "parameters", "assist_model"},
                    "invalid_input",
                    400,
                )
                request.update(
                    request_id="image-compile:" + uuid.uuid4().hex,
                    intent=body["intent"],
                    parameters=body["parameters"],
                    assist_model=body["assist_model"],
                )
                endpoint = "compile"
            self.p.contracts.check("image-backend#" + endpoint + "_request", request)
            answer = await self.life._call("image-backend/" + endpoint, request, management=True)
            await self.p.local_work.run(self.life.guard, session)
            answer["result"].pop("credential_ref", None)
            return answer

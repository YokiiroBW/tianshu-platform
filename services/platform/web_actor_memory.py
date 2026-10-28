"""Operator window onto Memory's actor domain; never reads or edits user memory."""

import ssl
import uuid

import aiohttp

from .auth import secret
from .contracts import Fault, loads, require


class WebActorMemory:
    def __init__(self, platform, console, *, session_factory=None):
        self.p, self.console = platform, console
        self.session_factory = session_factory

    def guard(self, actor, session, *, writing=False):
        self.console.life_management.guard(session)
        return self.p.role_runtime.managed_actor(
            self.console,
            session,
            actor,
            capabilities=("memory.read", "memory.write") if writing else ("memory.read",),
        )

    async def route(self, name, body, session):
        shapes = {
            "list": {"actor_id", "states", "kinds", "cursor"},
            "interests": {"actor_id"},
            "episode": {"actor_id", "ref"},
            "impact": {"actor_id", "ref"},
            "revise": {"actor_id", "ref", "client_id", "expected_domain_version", "value"},
            "forget": {"actor_id", "ref", "client_id", "expected_domain_version"},
        }
        require(name in shapes, "not_found", 404)
        require(isinstance(body, dict) and set(body) == shapes[name], "invalid_input", 400)
        require(isinstance(body["actor_id"], str), "invalid_input", 400)
        writing = name in {"revise", "forget"}
        role = await self.p.local_work.run(self.guard, body["actor_id"], session, writing=writing)
        operations = {
            "list": "manage_list",
            "interests": "interests",
            "episode": "get_episode",
            "impact": "memory_impact",
            "revise": "revise_memory",
            "forget": "forget_memory",
        }
        request = {
            "schema_version": 1,
            "request_id": str(uuid.uuid4()),
            "actor_id": body["actor_id"],
            "role_version": role["version"],
            "operation": operations[name],
            "context": {"purpose": "management", "audience": {"kind": "actor_only"}},
        }
        if name in {"list", "interests"}:
            request.update(limit=32, budget_bytes=196608)
            if name == "list":
                request.update({key: body[key] for key in ("states", "kinds", "cursor")})
        elif name == "episode":
            ref = body["ref"]
            self.p.actor_memory_authority.protocol.check("memory_ref", ref)
            require(ref["kind"] == "episode", "invalid_input", 400)
            request.update(episode_id=ref["id"], expected_revision=ref["revision"])
        else:
            request["ref"] = body["ref"]
        if writing:
            request.update(
                idempotency_key=body["client_id"],
                expected_domain_version=body["expected_domain_version"],
            )
            if name == "revise":
                value = body["value"]
                require(
                    isinstance(value, dict)
                    and {"text", "importance", "retention"}
                    <= set(value)
                    <= {"text", "importance", "retention", "interest"},
                    "invalid_input",
                    400,
                )
                request.update(value)
        answer = await self.call(request)
        current = await self.p.local_work.run(
            self.guard, body["actor_id"], session, writing=writing
        )
        require(current == role, "version_conflict", 409)
        if name == "episode":
            fact = answer["episode"]["fact"]
            answer = {
                "episode": {
                    "fact": {
                        key: fact[key]
                        for key in (
                            "summary",
                            "provenance",
                            "relation",
                            "outcome",
                            "occurred_at",
                            "learned_at",
                            "content_reads",
                        )
                    }
                }
            }
        if name == "impact" and "memory.write" not in role.get("capabilities", []):
            answer = {**answer, "can_revise": False, "can_forget": False}
        return answer

    async def call(self, request):
        protocol = self.p.actor_memory_authority.protocol
        protocol.check("request", request)
        config = self.p.role_runtime.config["memory"]
        token = secret(config["token_env"])
        require(token is not None, "dependency_unavailable", 503)
        tls = (
            None
            if self.session_factory
            else ssl.create_default_context(cafile=config.get("ca_file"))
        )
        try:
            async with (self.session_factory or aiohttp.ClientSession)(
                timeout=aiohttp.ClientTimeout(total=config.get("timeout_seconds", 10)),
                trust_env=False,
            ) as client:
                async with client.post(
                    config["base_url"].rstrip("/") + "/internal/v1/actor-self-memory/operate",
                    json=request,
                    headers={"Authorization": "Bearer " + token},
                    ssl=tls,
                    allow_redirects=False,
                ) as response:
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(8192):
                        raw.extend(chunk)
                        require(len(raw) <= 1024 * 1024, "invalid_upstream", 502)
                    require(response.content_type == "application/json", "invalid_upstream", 502)
                    try:
                        answer = loads(bytes(raw))
                    except Fault:
                        raise Fault("invalid_upstream", 502) from None
                    if response.status != 200:
                        require(isinstance(answer, dict), "invalid_upstream", 502)
                        require(
                            answer.get("code")
                            in {
                                "forbidden",
                                "invalid_input",
                                "version_conflict",
                                "idempotency_conflict",
                                "dependency_unavailable",
                                "not_found",
                                "capacity_exceeded",
                                "budget_exceeded",
                                "stale_evidence",
                                "source_changed",
                                "source_withdrawn",
                            },
                            "invalid_upstream",
                            502,
                        )
                        raise Fault(answer["code"], response.status)
                    try:
                        protocol.check("response", answer)
                    except Fault:
                        raise Fault("invalid_upstream", 502) from None
                    require(
                        all(answer.get(key) == request[key] for key in ("request_id", "actor_id")),
                        "invalid_upstream",
                        502,
                    )
                    if "command" in answer:
                        require(
                            answer["command"] == request["operation"]
                            and answer["idempotency_key"] == request["idempotency_key"],
                            "invalid_upstream",
                            502,
                        )
                    else:
                        require(
                            all(
                                answer.get(key) == request[key]
                                for key in ("role_version", "operation", "context")
                            ),
                            "invalid_upstream",
                            502,
                        )
                    if request["operation"] == "get_episode":
                        episode = answer["episode"]
                        require(
                            episode["episode_id"] == request["episode_id"]
                            and episode["revision"] == request["expected_revision"]
                            and episode["actor_id"] == request["actor_id"]
                            and episode["fact"]["actor_id"] == request["actor_id"],
                            "invalid_upstream",
                            502,
                        )
                    if request["operation"] == "memory_impact":
                        require(answer["ref"] == request["ref"], "invalid_upstream", 502)
                    return answer
        except (aiohttp.ClientError, ssl.SSLError, OSError, TimeoutError):
            raise Fault("dependency_unavailable", 503) from None

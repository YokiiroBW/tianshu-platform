"""Current role and audience authority for BOT-owned memory, without content claims."""

from .candidate_protocol import CandidateProtocol
from .contracts import canonical, digest, require

PATH = "/internal/v1/actor-self-memory/authority"
PACKAGE = "actor-self-memory/candidate-v1"
MANIFEST = "34fec40dbf16ab0109026dd721873e238e5a6fecd86ad3d58495e99103d9b3e5"
READ_OPERATIONS = frozenset(
    {
        "get_episode",
        "check_episode",
        "get_receipt",
        "query",
        "check_memory",
        "interests",
        "manage_list",
        "memory_impact",
    }
)
WRITE_OPERATIONS = frozenset(
    {
        "append_episode",
        "withdraw_episode",
        "apply_source_revision",
        "append_derived",
        "withdraw_memory",
        "revise_memory",
        "forget_memory",
        "maintain",
    }
)


class ActorMemoryAuthority:
    def __init__(self, platform):
        self.p = platform
        self.protocol = CandidateProtocol(platform.contracts.root, PACKAGE, MANIFEST)

    def validate(self, request):
        require(len(canonical(request).encode()) <= 16 * 1024, "capacity_exceeded", 413)
        self.protocol.check("authority_request", request)

    def _role(self, request):
        registry = self.p.role_runtime
        role = registry.get(request["actor_id"])
        if role is None:
            return None, "missing", []
        require(role["version"] == request["role_version"], "version_conflict", 409)
        if not (
            registry.config
            and registry.config["enabled"]
            and role["enabled"]
            and role["state"] == "active"
        ):
            return role, "disabled", []
        capabilities = set(role.get("capabilities", []))
        operations = set()
        if "memory.read" in capabilities:
            operations.update(READ_OPERATIONS)
        if "memory.write" in capabilities:
            operations.update(WRITE_OPERATIONS)
        return role, "enabled", sorted(operations)

    def operate(self, header, request):
        self.validate(request)
        p = self.p
        with p.store.connect() as db:
            identity, caller = p.auth.authenticate(header, db, "actor.memory.authorize")
            require(
                caller["kind"] == "service" and caller["service"] in {"memory", "companion"},
                "forbidden",
                403,
            )
            role, state, operations = self._role(request)
            policy = {
                "role": role,
                "policy": p.auth.policy_digest,
                "effective": p.auth.effective_digest(),
                "caller": identity,
            }
            base = {
                key: request[key]
                for key in ("schema_version", "request_id", "actor_id", "role_version")
            }
            if request["operation"] == "actor_check":
                return self.protocol.response(
                    "actor_check_result",
                    {
                        **base,
                        "state": state,
                        "allowed_operations": operations,
                        "policy_version": digest(policy),
                    },
                )
            context = request["context"]
            audience = context["audience"]
            allowed = state == "enabled" and bool(READ_OPERATIONS.intersection(operations))
            verified = None
            if audience["kind"] == "public":
                # This grants a context check, never a claim that any source is public.
                # Memory still intersects every basis with its current owner disclosure.
                require(context["purpose"] in {"diary", "management"}, "forbidden", 403)
                verified = context
            elif allowed and audience["kind"] != "actor_only":
                _, _, verified = p.origins.context(
                    db, audience["assertion_ref"], "companion", "memory", "dialogue"
                )
                scope = verified["allowed_scope"]
                require(
                    scope["actor_id"] == request["actor_id"]
                    and scope["conversation_id"] == audience["conversation_id"],
                    "forbidden",
                    403,
                )
                if audience["kind"] == "group":
                    require(scope["audience"] == "group", "forbidden", 403)
                else:
                    require(
                        scope["audience"] == "self_private"
                        and scope["person_id"] == audience["person_id"],
                        "forbidden",
                        403,
                    )
            require(self._role(request) == (role, state, operations), "version_conflict", 409)
            return self.protocol.response(
                "context_check_result",
                {
                    **base,
                    "context": context,
                    "allowed": allowed,
                    "access_version": digest({"policy": policy, "context": verified}),
                },
            )

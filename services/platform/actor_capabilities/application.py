"""Service identity and current role authorization; execution stays with plugins."""

from ..contracts import canonical, require
from ..candidate_protocol import CandidateProtocol
from ..plugins.actor_bridge import PluginActorBridge, principal
from ..plugins.schema import check, fill_defaults
from .protocol import MANIFEST, PACKAGE, REQUEST_LIMIT


class Authorization:
    def __init__(self, application, header, actor, version, binding):
        self.application, self.header = application, header
        self.actor, self.version, self.binding = actor, version, binding

    def __call__(self):
        self.application.guard(self.header, self.actor, self.version)
        resolved = self.application.bridge.resolve(self.actor, self.binding)
        self.application.bridge.ready(*resolved)
        return True

    def authorize_capability(self, name, operation, value):
        # Instance-scoped job helpers must not cross a role's execution ownership or
        # create an instance automation that loses the originating role authority.
        if name == "jobs.manage":
            require(operation not in {"schedule.put", "schedule.remove"}, "forbidden", 403)
            if operation in {"get", "cancel"}:
                document, _ = self.application.bridge.owned(self.actor, value["invocation_id"])
                require(document["binding_id"] == self.binding["binding_id"], "forbidden", 403)

    def authorize_action(self, instance, action, binding):
        require(
            instance == self.binding["instance_id"] and binding == self.binding["binding_id"],
            "forbidden",
            403,
        )
        reference = {**self.binding, "action_id": action}
        resolved = self.application.bridge.resolve(self.actor, reference)
        self.application.bridge.ready(*resolved)


class ActorCapabilities:
    def __init__(self, platform):
        self.p = platform
        self.protocol = CandidateProtocol(platform.contracts.root, PACKAGE, MANIFEST)
        self.bridge = PluginActorBridge(platform.plugins)

    def role(self, actor, version=None):
        registry = self.p.role_runtime
        row = registry.get(actor)
        require(
            registry.config
            and registry.config["enabled"]
            and row
            and row["enabled"]
            and row["state"] == "active",
            "forbidden",
            403,
        )
        require(version is None or row["version"] == version, "version_conflict", 409)
        return row

    def guard(self, header, actor, version=None):
        with self.p.store.connect() as db:
            _, caller = self.p.auth.authenticate(header, db, "actor.capabilities")
            require(
                caller["kind"] == "service" and caller["service"] == "companion", "forbidden", 403
            )
        return self.role(actor, version)

    def directory(self):
        return [
            {"actor_id": row["actor_id"], "name": row["name"], "version": row["version"]}
            for row in self.p.role_runtime.directory()
            if row["state"] == "active"
            and row["enabled"]
            and self.p.role_runtime.config
            and self.p.role_runtime.config["enabled"]
        ]

    def validate_selection(self, bindings):
        require(isinstance(bindings, list), "invalid_input", 400)
        for binding in bindings:
            require(isinstance(binding, dict), "invalid_input", 400)
            scope = binding.get("scope")
            require(isinstance(scope, dict), "invalid_input", 400)
            if scope.get("kind") == "character" and binding.get("enabled"):
                self.role(scope.get("character_id"))
                require(isinstance(binding.get("entries"), list), "invalid_input", 400)

    def validate(self, request):
        require(len(canonical(request).encode()) <= REQUEST_LIMIT, "capacity_exceeded", 413)
        self.protocol.check("request", request)

    async def operate(self, header, request, *, execution_state=lambda state: None):
        self.validate(request)
        actor, operation = request["actor_id"], request["operation"]
        role = await self.p.local_work.run(self.guard, header, actor, request.get("role_version"))
        if operation == "discover":
            capabilities = await self.bridge.discover(actor)
            await self.p.local_work.run(self.guard, header, actor, role["version"])
            return self.protocol.response(
                "discovery_result",
                {
                    "schema_version": 1,
                    "request_id": request["request_id"],
                    "actor_id": actor,
                    "role_version": role["version"],
                    "capabilities": capabilities,
                },
            )
        host = self.p.plugins
        if operation == "invoke":
            reference = request["binding"]
            resolved = await self.p.local_work.run(self.bridge.resolve, actor, reference)
            await self.p.local_work.run(self.bridge.ready, *resolved)
            action = resolved[3]
            arguments = fill_defaults(action["input_schema"], request["arguments"])
            check(action["input_schema"], arguments)
            authorize = Authorization(self, header, actor, role["version"], reference)
            execution_state("unknown")
            receipt = await host.executions.invoke(
                reference["instance_id"],
                reference["action_id"],
                arguments,
                request["intent_key"],
                principal(actor),
                authorize=authorize,
                binding_id=reference["binding_id"],
            )
            document = await host.local(host.repository.call, receipt["id"])
        else:
            document, reference = await host.local(
                self.bridge.owned, actor, request["invocation_id"]
            )
            if operation == "reconcile":
                authorize = Authorization(self, header, actor, role["version"], reference)
                await host.local(authorize)
                execution_state("unknown")
                await host.executions.recover(document["id"], principal(actor), authorize)
                document = await host.local(host.repository.call, document["id"])
            elif operation == "cancel":
                execution_state("unknown")
                await host.executions.cancel(document["id"])
                document = await host.local(host.repository.call, document["id"])
        require(document is not None, "unknown", 503)
        execution_state(
            "unknown"
            if document["effect"] == "unknown"
            or document["execution"] in {"accepted", "running", "queued"}
            else "completed"
        )
        await self.p.local_work.run(self.guard, header, actor, role["version"])
        await host.local(self.bridge.owned, actor, document["id"])
        answer = await host.local(self.bridge.receipt, request, document)
        return self.protocol.response("receipt", answer)

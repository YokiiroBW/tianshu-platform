"""Exact dynamic version bindings owned by platform; no cross-service database access."""

import time

from .contracts import digest, require
from .model_functions import validate_function


class ProviderAuthority:
    def __init__(self, platform, clock=time.time):
        self.platform = platform
        self.catalog = platform.provider_catalog
        self.clock = clock

    def _identity(self, header, action, service):
        with self.platform.store.connect() as db:
            _, principal = self.platform.auth.authenticate(header, db, action)
            require(
                principal["kind"] == "service" and principal["service"] == service, "forbidden", 403
            )

    def select(self, header, body):
        require(self.catalog is not None, "dependency_unavailable", 503)
        require(
            isinstance(body, dict)
            and set(body) - {"function_id"}
            == {
                "turn_id",
                "actor_id",
                "person_id",
                "audience",
                "conversation_id",
                "caller_service",
                "workload",
            },
            "invalid_input",
            400,
        )
        require(
            body["caller_service"] == "companion" and body["workload"] == "companion.text",
            "forbidden",
            403,
        )
        for field in ("turn_id", "actor_id", "person_id", "conversation_id"):
            self.platform.contracts.check("common#id", body[field])
        require(body["audience"] in {"group", "self_private"}, "invalid_input", 400)
        self._identity(header, "config.select", "companion")
        function_id = validate_function(body.get("function_id", "chat"))
        view = self.catalog.view()
        selected = view["default"]
        binding = next(item for item in view["functions"] if item["function_id"] == function_id)
        if binding["provider_id"] is not None:
            selected = binding
        role_runtime = getattr(self.platform, "role_runtime", None)
        role = role_runtime.get(body["actor_id"]) if role_runtime is not None else None
        if role is not None:
            require(role_runtime.active(body["actor_id"]), "forbidden", 403)
            if role["provider_id"] is not None and (
                function_id == "chat" or binding["provider_id"] is None
            ):
                provider = next(
                    (
                        item
                        for item in view["providers"]
                        if item["provider_id"] == role["provider_id"]
                    ),
                    None,
                )
                require(
                    provider is not None
                    and provider["revision"] == role["provider_revision"]
                    and self.catalog._selectable(provider),
                    "provider_unavailable",
                    409,
                )
                selected = {
                    "configured": True,
                    "provider_id": role["provider_id"],
                    "provider_revision": role["provider_revision"],
                }
        now = self.clock()
        # Existing static deployments may already have used the new numeric range.
        # Once enabled, Models.publish reserves it, so this read is stable for new grants.
        with self.platform.store.connect() as db:
            static_max = db.execute("SELECT max(version) FROM configs").fetchone()[0] or 0
        with self.catalog._transaction() as db:
            grant = db.execute(
                "SELECT * FROM provider_turn_grants WHERE turn_id=?", (body["turn_id"],)
            ).fetchone()
            if grant is not None:
                require(grant["scope_digest"] == digest(body), "version_conflict", 409)
                row = db.execute(
                    "SELECT * FROM provider_publications WHERE version=?", (grant["version"],)
                ).fetchone()
                require(
                    row is not None and not row["revoked"] and row["usable_until"] > now + 65,
                    "forbidden",
                    410,
                )
                version, until = row["version"], row["usable_until"]
                provider_id, revision = row["provider_id"], row["provider_revision"]
            else:
                require(selected["configured"], "dependency_unavailable", 503)
                provider_id, revision = selected["provider_id"], selected["provider_revision"]
                row = db.execute(
                    "SELECT * FROM provider_publications WHERE provider_id=? AND "
                    "provider_revision=? AND revoked=0 AND usable_until>? "
                    "ORDER BY version DESC LIMIT 1",
                    (provider_id, revision, now + 120),
                ).fetchone()
                if row is None:
                    version = db.execute(
                        "SELECT max(version) FROM provider_publications"
                    ).fetchone()[0]
                    version = max(version or 0, static_max, 1_000_000_000) + 1
                    until = now + min(self.platform.models.max_lifetime, 3600)
                    require(until - now >= 120, "dependency_unavailable", 503)
                    db.execute(
                        "INSERT INTO provider_publications VALUES (?,?,?,?,0)",
                        (version, provider_id, revision, until),
                    )
                else:
                    version, until = row["version"], row["usable_until"]
                db.execute(
                    "INSERT INTO provider_turn_grants VALUES (?,?,?,?,?)",
                    (
                        body["turn_id"],
                        digest(body),
                        version,
                        body["caller_service"],
                        body["workload"],
                    ),
                )
        # A concurrent catalog edit cannot turn this binding into a new revision.
        self.catalog.execution_context(provider_id, revision)
        return {
            "config_version": version,
            "expires_at": until,
            "revoked": False,
            "caller_service": "companion",
            "workload": "companion.text",
        }

    def runtime(self, header, body):
        require(self.catalog is not None, "dependency_unavailable", 503)
        require(
            isinstance(body, dict)
            and set(body) == {"config_version", "caller_service", "workload", "turn_id"},
            "invalid_input",
            400,
        )
        require(
            body["caller_service"] == "companion" and body["workload"] == "companion.text",
            "forbidden",
            403,
        )
        self.platform.contracts.check("common#id", body["turn_id"])
        require(
            type(body["config_version"]) is int and body["config_version"] > 0, "invalid_input", 400
        )
        self._identity(header, "provider.runtime", "gateway")
        with self.catalog._transaction() as db:
            row = db.execute(
                "SELECT * FROM provider_publications WHERE version=?", (body["config_version"],)
            ).fetchone()
            require(row is not None, "forbidden", 403)
            grant = db.execute(
                "SELECT * FROM provider_turn_grants WHERE turn_id=?", (body["turn_id"],)
            ).fetchone()
            require(
                grant is not None
                and grant["version"] == body["config_version"]
                and grant["caller_service"] == body["caller_service"]
                and grant["workload"] == body["workload"],
                "forbidden",
                403,
            )
            require(not row["revoked"] and row["usable_until"] > self.clock(), "forbidden", 410)
            revision = row["provider_revision"]
            until = row["usable_until"]
        context = self.catalog.execution_context(row["provider_id"], revision)
        return {
            "config_version": body["config_version"],
            "provider_id": context.provider_id,
            "provider_revision": context.revision,
            "protocol": context.protocol,
            "base_url": context.base_url,
            "model_id": context.model_id,
            "api_key": context.api_key,
            "usable_until": until,
        }

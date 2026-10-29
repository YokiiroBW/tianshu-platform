"""Per-role function choices stored with their existing role intent."""

import json
import uuid
from contextlib import closing
from .contracts import canonical, digest, require
from .model_functions import FUNCTIONS, effective_binding, validate_function


class RoleModels:
    def __init__(self, runtime):
        self.runtime = runtime

    def view(self, actor):
        role = self.runtime.get(actor)
        require(role and not role.get("deleted"), "not_found", 404)
        catalog = self.runtime.p.provider_catalog
        view = catalog.view()
        providers = {row["provider_id"]: row for row in view["providers"]}
        functions = []
        for identity, name, description, connected in FUNCTIONS:
            selected = effective_binding(view, identity, role)
            override = role.get("model_functions", {}).get(identity)
            provider = providers.get(selected.get("provider_id"))
            functions.append(
                {
                    "function_id": identity,
                    "name": name,
                    "description": description,
                    "connected": connected,
                    "provider_id": (
                        role.get("provider_id")
                        if identity == "chat"
                        else (override or {}).get("provider_id")
                    ),
                    "provider_revision": (
                        role.get("provider_revision")
                        if identity == "chat"
                        else (override or {}).get("provider_revision")
                    ),
                    "effective_provider_id": selected.get("provider_id"),
                    "effective_provider_revision": selected.get("provider_revision"),
                    "source": selected["source"],
                    "configured": bool(
                        provider
                        and provider["revision"] == selected.get("provider_revision")
                        and catalog._selectable(provider)
                    ),
                }
            )
        return {
            "actor_id": actor,
            "name": role["name"],
            "version": role.get("model_version", 0),
            "functions": functions,
            "providers": [
                {
                    "id": row["provider_id"],
                    "name": row["name"],
                    "model": row["model_id"],
                    "revision": row["revision"],
                    "available": catalog._selectable(row),
                }
                for row in providers.values()
                if row.get("kind", "text") == "text"
            ],
        }

    def apply(self, body, principal):
        require(
            isinstance(body, dict)
            and set(body)
            == {
                "actor_id",
                "function_id",
                "provider_id",
                "provider_revision",
                "expected_version",
                "client_id",
            },
            "invalid_input",
            400,
        )
        require(
            isinstance(body["actor_id"], str)
            and type(body["expected_version"]) is int
            and body["expected_version"] >= 0,
            "invalid_input",
            400,
        )
        identity = validate_function(body["function_id"])
        try:
            client = str(uuid.UUID(body["client_id"]))
        except (ValueError, TypeError, AttributeError):
            require(False, "invalid_input", 400)
        require(client == body["client_id"], "invalid_input", 400)
        require(
            (body["provider_id"] is None and body["provider_revision"] is None)
            or (isinstance(body["provider_id"], str) and type(body["provider_revision"]) is int),
            "invalid_input",
            400,
        )
        if body["provider_id"] is not None:
            provider = next(
                (
                    r
                    for r in self.runtime.p.provider_catalog.view()["providers"]
                    if r["provider_id"] == body["provider_id"]
                ),
                None,
            )
            require(
                provider
                and provider.get("kind", "text") == "text"
                and provider["revision"] == body["provider_revision"]
                and self.runtime.p.provider_catalog._selectable(provider),
                "provider_unavailable",
                409,
            )
        signature = digest(["role-model", body])
        with closing(self.runtime._db()) as db:
            db.execute("BEGIN IMMEDIATE")
            saved = db.execute(
                "SELECT body FROM roles WHERE actor_id=?", (body["actor_id"],)
            ).fetchone()
            role = json.loads(saved[0]) if saved else None
            require(
                role and not role.get("deleted") and role["operator"] == principal, "not_found", 404
            )
            prior = db.execute(
                "SELECT signature FROM intents WHERE client_id=?", (client,)
            ).fetchone()
            if prior:
                require(prior[0] == signature, "idempotency_conflict", 409)
                return
            require(role["state"] != "pending", "role_configuring", 409)
            require(
                role.get("model_version", 0) == body["expected_version"], "version_conflict", 409
            )
            if identity == "chat":
                role["provider_id"], role["provider_revision"] = (
                    body["provider_id"],
                    body["provider_revision"],
                )
                role["version"] += 1
            else:
                overrides = dict(role.get("model_functions", {}))
                if body["provider_id"] is None:
                    overrides.pop(identity, None)
                else:
                    overrides[identity] = {
                        "provider_id": body["provider_id"],
                        "provider_revision": body["provider_revision"],
                    }
                role["model_functions"] = overrides
            role["model_version"] = role.get("model_version", 0) + 1
            db.execute("INSERT INTO intents VALUES (?,?,?)", (client, signature, body["actor_id"]))
            db.execute(
                "UPDATE roles SET body=? WHERE actor_id=?", (canonical(role), body["actor_id"])
            )
            db.commit()

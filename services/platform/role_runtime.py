"""Web role coordinator. Each peer owns its fact; this journal owns only the application intent."""

import asyncio
import copy
import json
import sqlite3
import uuid
from contextlib import closing

from .contracts import Fault, canonical, digest, require
from .transport import core_settings, management_call

PREFIX = "/api/web/roles/"
CAPABILITIES = frozenset({"dialogue", "memory.read", "memory.write", "direct"})


def is_placeholder_actor(actor_id, role):
    """The installation's bootstrap identity is not a character until explicitly adopted.

    Other static actors remain real registrations, with or without a name/runtime intent.
    This affects browser directories only, never source authority or durable role facts.
    """
    return actor_id == "actor:household" and role is None


class RoleRuntime:
    def __init__(self, platform):
        self.p = platform
        self.config = platform.settings.get("role_runtime")
        self.path = platform.store.path + ".roles.sqlite"
        self.lock = asyncio.Lock()
        if self.config is None:
            return
        require(
            isinstance(self.config, dict)
            and {"enabled", "memory"} <= set(self.config) <= {"enabled", "memory", "knowledge"}
            and type(self.config["enabled"]) is bool,
            "invalid_input",
            400,
        )
        core_settings(self.config["memory"])
        if self.config.get("knowledge") is not None:
            core_settings(self.config["knowledge"])
        require(platform.settings.get("core") is not None, "invalid_input", 400)
        require(platform.provider_catalog is not None, "invalid_input", 400)
        with closing(self._db()) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS roles (actor_id TEXT PRIMARY KEY, body TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS intents (client_id TEXT PRIMARY KEY, signature TEXT NOT NULL, actor_id TEXT NOT NULL)"
            )
            # Startup alone cannot prove that both peers still carry the applied version.
            rows = db.execute("SELECT actor_id,body FROM roles").fetchall()
            for actor, document in rows:
                row = json.loads(document)
                if row["state"] == "active" or (
                    row["state"] == "disabled"
                    and self.config.get("knowledge") is not None
                    and row.get("knowledge_version") is None
                ):
                    row["state"] = "pending"
                    row["stage"] = "verify"
                    db.execute("UPDATE roles SET body=? WHERE actor_id=?", (canonical(row), actor))

    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def get(self, actor):
        if self.config is None:
            return None
        with closing(self._db()) as db:
            row = db.execute("SELECT body FROM roles WHERE actor_id=?", (actor,)).fetchone()
        return json.loads(row[0]) if row else None

    def active(self, actor):
        row = self.get(actor)
        return bool(
            self.config
            and self.config["enabled"]
            and row
            and row["state"] == "active"
            and row["enabled"]
        )

    def active_actors(self):
        if self.config is None or not self.config["enabled"]:
            return []
        with closing(self._db()) as db:
            rows = [
                json.loads(row[0]) for row in db.execute("SELECT body FROM roles ORDER BY actor_id")
            ]
        return [
            {"id": row["actor_id"], "label": row["name"]}
            for row in rows
            if row["state"] == "active" and row["enabled"]
        ]

    def directory(self):
        """Read only the current local role intents for a narrow browser projection."""
        if self.config is None:
            return []
        with closing(self._db()) as db:
            return [
                json.loads(row[0]) for row in db.execute("SELECT body FROM roles ORDER BY actor_id")
            ]

    def _put(self, row):
        with closing(self._db()) as db, db:
            db.execute(
                "INSERT INTO roles VALUES (?,?) ON CONFLICT(actor_id) DO UPDATE SET body=excluded.body",
                (row["actor_id"], canonical(row)),
            )

    def _gate(self, console, session):
        require(self.config is not None and self.config["enabled"], "dependency_unavailable", 503)
        require(console.session_valid(session), "session_expired", 401)
        principal = self.p.auth.principals[console.config["principal"]]
        require("role.manage" in principal["actions"], "forbidden", 403)

    def _entry_id(self, actor, source_id):
        return "role-" + digest([actor, source_id])[:32]

    def _install_web(self, row):
        if not row["enabled"] or row["state"] != "active":
            return
        web = self.p.settings.get("web")
        if web is None:
            return
        for source_id in self.p.settings["web"]["input_entries"]:
            source = self.p.sources.entries[source_id]
            if any(
                self.p.auth.entries[item]["actor_id"] == row["actor_id"]
                for item in source["actor_entries"]
            ):
                continue
            template = self.p.auth.entries[source["actor_entries"][0]]
            new_id = self._entry_id(row["actor_id"], source_id)
            if new_id in self.p.sources.entries:
                if new_id not in self.console.input_entries:
                    self.console.input_entries.append(new_id)
                continue
            auth_id = new_id + ":actor"
            actor = copy.deepcopy(template)
            actor["actor_id"] = row["actor_id"]
            self.p.auth.entries[auth_id] = actor
            derived = copy.deepcopy(source)
            derived["actor_entries"] = [auth_id]
            derived["default_actor_ids"] = [row["actor_id"]]
            self.p.sources.entries[new_id] = derived
            self.console.input_entries.append(new_id)

    def _remove_web(self, actor):
        web = getattr(self, "console", None)
        if web is None or web.config is None:
            return
        for source_id in self.p.settings["web"]["input_entries"]:
            new_id = self._entry_id(actor, source_id)
            entry = self.p.sources.entries.pop(new_id, None)
            if entry:
                for auth_id in entry["actor_entries"]:
                    self.p.auth.entries.pop(auth_id, None)
            if new_id in web.input_entries:
                web.input_entries.remove(new_id)

    async def _remote(self, settings, path, payload):
        # Kept as the coordinator's fault-injection seam for durable-stage tests.
        return await management_call(settings, path, payload)

    async def _knowledge_apply(self, row, *, enabled, cancelling=False):
        """Synchronize the independent original owner's existing RoleGrants journal."""
        settings = self.config.get("knowledge")
        if settings is None:
            return
        key = "cancel_knowledge_body" if cancelling else "knowledge_body"
        if key not in row:
            status = await self._remote(
                settings,
                "/internal/v1/role-runtime/authorize",
                {"operation": "status", "actor_id": row["actor_id"]},
            )
            row[key] = {
                "request_id": row["client_id"]
                + (f":cancel-knowledge:{row['cancel_epoch']}" if cancelling else ":knowledge"),
                "actor_id": row["actor_id"],
                "expected_version": status["version"],
                "enabled": enabled,
                "legacy": row["legacy"],
            }
            await self.p.local_work.run(self._put, row)
        try:
            answer = await self._remote(settings, "/internal/v1/role-runtime/authorize", row[key])
        except Fault as error:
            if cancelling and error.code == "version_conflict":
                row.pop(key)
                row["cancel_epoch"] += 1
                await self.p.local_work.run(self._put, row)
            raise
        row["knowledge_version"] = answer["version"]
        await self.p.local_work.run(self._put, row)

    def _provider(self, row):
        if not row["enabled"] or "dialogue" not in row["capabilities"]:
            return
        view = self.p.provider_catalog.view()
        if row["provider_id"] is None:
            require(view["default"]["configured"], "provider_unavailable", 409)
            return
        item = next((x for x in view["providers"] if x["provider_id"] == row["provider_id"]), None)
        require(
            item is not None
            and item["revision"] == row["provider_revision"]
            and self.p.provider_catalog._selectable(item),
            "provider_unavailable",
            409,
        )

    async def _resume(self, row):
        # Every call uses a stable idempotency key; a lost response is reconciled by replay.
        core = self.p.settings["core"]
        memory = self.config["memory"]
        actor = row["actor_id"]
        if row["stage"].startswith("cancel_"):
            return await self._resume_cancel(row, core, memory)

        def core_body(stage, enabled, expected):
            return {
                "operation": "apply",
                "request_id": row["client_id"] + ":" + stage,
                "application_id": row["client_id"],
                "operator": row["operator"],
                "actor_id": actor,
                "expected_version": expected,
                "name": row["name"],
                "profile_id": row["profile_id"],
                "profile_version": row["profile_version"],
                "enabled": enabled,
                "capabilities": row["capabilities"],
            }

        if row["stage"] == "verify":
            await self.p.local_work.run(self._provider, row)
            companion = await self._remote(
                core, "/internal/v1/role-runtime/manage", {"operation": "list"}
            )
            memory_status = await self._remote(
                memory,
                "/internal/v1/role-runtime/authorize",
                {"operation": "status", "actor_id": actor},
            )
            current = next(
                (item for item in companion.get("roles", []) if item.get("actor_id") == actor), None
            )
            require(
                current is not None
                and current["version"] == row["companion_version"]
                and current["enabled"] == row["enabled"]
                and current["profile_revision"] == row["profile_revision"]
                and memory_status.get("version") == row["memory_version"]
                and memory_status.get("enabled") == row["enabled"],
                "dependency_unavailable",
                503,
            )
            if self.config.get("knowledge") is not None:
                if row.get("knowledge_version") is None:
                    # Existing roles are enrolled in the separately deployed owner once.
                    await self._knowledge_apply(row, enabled=row["enabled"])
                status = await self._remote(
                    self.config["knowledge"],
                    "/internal/v1/role-runtime/authorize",
                    {"operation": "status", "actor_id": actor},
                )
                require(
                    status["version"] == row["knowledge_version"]
                    and status["enabled"] == row["enabled"],
                    "dependency_unavailable",
                    503,
                )
            row["stage"] = "complete"
            await self.p.local_work.run(self._put, row)
        if row["stage"] == "start":
            await self.p.local_work.run(self._provider, row)
            answer = await self._remote(
                core,
                "/internal/v1/role-runtime/manage",
                core_body("pause", False, row["companion_version"]),
            )
            row["companion_version"] = answer["version"]
            row["profile_revision"] = answer["profile_revision"]
            row["stage"] = "core_paused"
            await self.p.local_work.run(self._put, row)
        if row["stage"] == "core_paused":
            answer = await self._remote(
                memory,
                "/internal/v1/role-runtime/authorize",
                {
                    "request_id": row["client_id"] + ":memory",
                    "actor_id": actor,
                    "expected_version": row["memory_version"],
                    "enabled": row["enabled"],
                    "legacy": row["legacy"],
                },
            )
            row["memory_version"] = answer["version"]
            row["stage"] = "memory_applied"
            await self.p.local_work.run(self._put, row)
        if row["stage"] == "memory_applied":
            await self._knowledge_apply(row, enabled=row["enabled"])
            await self.p.local_work.run(self._provider, row)
            row["stage"] = "provider_checked"
            await self.p.local_work.run(self._put, row)
        if row["stage"] == "provider_checked":
            if row["enabled"]:
                answer = await self._remote(
                    core,
                    "/internal/v1/role-runtime/manage",
                    core_body("enable", True, row["companion_version"]),
                )
                row["companion_version"] = answer["version"]
            row["stage"] = "complete"
            await self.p.local_work.run(self._put, row)
        if row["stage"] == "complete":
            row["state"] = "active" if row["enabled"] else "disabled"
            await self.p.local_work.run(self._put, row)
            self._install_web(row)
        return row

    async def _resume_cancel(self, row, core, memory):
        """Fence old in-flight writes by advancing both owners' exact versions."""
        actor = row["actor_id"]
        if row["stage"] == "cancel_reconcile":
            catalog = await self._remote(
                core, "/internal/v1/role-runtime/manage", {"operation": "list"}
            )
            current = next(
                (item for item in catalog.get("roles", []) if item.get("actor_id") == actor), None
            )
            if current is None:
                require(row["cancel_source_stage"] == "start", "dependency_unavailable", 503)
                legacy = any(item.get("id") == actor for item in catalog.get("legacy_roles", []))
                if row["legacy"]:
                    # A static role without a runtime record is still allowed by Core.
                    # Install an explicit disabled fact before revoking Memory.
                    require(legacy, "dependency_unavailable", 503)
                    current = {
                        "version": 0,
                        "name": row["name"],
                        "profile_id": None,
                        "profile_version": None,
                        "capabilities": row["capabilities"],
                    }
                else:
                    # A late first pause for a new role can only write disabled.
                    # Neither Memory nor enable could have been dispatched yet.
                    require(not legacy, "dependency_unavailable", 503)
                    row["stage"] = "cancel_memory_prepare"
            if current is not None:
                row["cancel_core_body"] = {
                    "operation": "apply",
                    "request_id": f"{row['client_id']}:cancel-core:{row['cancel_epoch']}",
                    "application_id": row["client_id"],
                    "operator": row["operator"],
                    "actor_id": actor,
                    "expected_version": current["version"],
                    "name": current["name"],
                    "profile_id": current["profile_id"],
                    "profile_version": current.get("profile_version", row["profile_version"]),
                    "enabled": False,
                    "capabilities": current["capabilities"],
                }
                row["stage"] = "cancel_core"
            await self.p.local_work.run(self._put, row)
        if row["stage"] == "cancel_core":
            try:
                answer = await self._remote(
                    core, "/internal/v1/role-runtime/manage", row["cancel_core_body"]
                )
            except Fault as error:
                if error.code == "version_conflict":
                    row["cancel_epoch"] += 1
                    row["stage"] = "cancel_reconcile"
                    await self.p.local_work.run(self._put, row)
                    raise Fault("dependency_unavailable", 503) from None
                raise
            row["companion_version"] = answer["version"]
            row["profile_revision"] = answer["profile_revision"]
            row["stage"] = "cancel_memory_prepare"
            await self.p.local_work.run(self._put, row)
        if row["stage"] == "cancel_memory_prepare":
            status = await self._remote(
                memory,
                "/internal/v1/role-runtime/authorize",
                {"operation": "status", "actor_id": actor},
            )
            row["cancel_memory_body"] = {
                "request_id": f"{row['client_id']}:cancel-memory:{row['cancel_epoch']}",
                "actor_id": actor,
                "expected_version": status["version"],
                "enabled": False,
                "legacy": row["legacy"],
            }
            row["stage"] = "cancel_memory"
            await self.p.local_work.run(self._put, row)
        if row["stage"] == "cancel_memory":
            try:
                answer = await self._remote(
                    memory, "/internal/v1/role-runtime/authorize", row["cancel_memory_body"]
                )
            except Fault as error:
                if error.code == "version_conflict":
                    row["cancel_epoch"] += 1
                    row["stage"] = "cancel_memory_prepare"
                    await self.p.local_work.run(self._put, row)
                    raise Fault("dependency_unavailable", 503) from None
                raise
            row["memory_version"] = answer["version"]
            row["stage"] = "cancel_knowledge"
            await self.p.local_work.run(self._put, row)
        if row["stage"] == "cancel_knowledge":
            await self._knowledge_apply(row, enabled=False, cancelling=True)
            row["stage"] = "complete"
            await self.p.local_work.run(self._put, row)
        if row["stage"] == "complete":
            row["state"] = "disabled"
            await self.p.local_work.run(self._put, row)
        return row

    async def resume_pending(self):
        if self.config is None or not self.config["enabled"]:
            return
        async with self.lock:
            rows = await self.p.local_work.run(self.directory)
            for row in rows:
                if row["state"] != "pending":
                    continue
                try:
                    await self._resume(row)
                except Fault as error:
                    await self.p.local_work.run(self._record_error, row, error)

    def _record_error(self, row, error):
        # A pre-apply conflict has changed no peer. A new intent can fix stale
        # profile/provider input while this failed intent remains replayable.
        if row["stage"] == "start" and error.code in {
            "version_conflict",
            "invalid_input",
            "not_found",
            "provider_unavailable",
        }:
            row["state"] = "failed"
        row["error_code"] = error.code
        self._put(row)

    def _begin(self, body):
        require(
            isinstance(body, dict)
            and set(body)
            == {
                "client_id",
                "actor_id",
                "expected_version",
                "name",
                "profile_id",
                "profile_version",
                "provider_id",
                "provider_revision",
                "enabled",
                "capabilities",
            },
            "invalid_input",
            400,
        )
        try:
            client = str(uuid.UUID(body["client_id"]))
            require(client == body["client_id"], "invalid_input", 400)
        except (ValueError, TypeError):
            raise Fault("invalid_input", 400) from None
        require(
            type(body["expected_version"]) is int
            and (
                body["actor_id"] is None
                and body["expected_version"] == 0
                or isinstance(body["actor_id"], str)
                and body["actor_id"].startswith("actor:")
                and len(body["actor_id"]) <= 128
                and body["expected_version"] >= 0
            ),
            "invalid_input",
            400,
        )
        require(
            isinstance(body["name"], str)
            and 1 <= len(body["name"].strip()) <= 80
            and (
                body["profile_id"] is None
                and body["profile_version"] is None
                or isinstance(body["profile_id"], str)
                and body["profile_id"].startswith("persona-profile:")
                and type(body["profile_version"]) is int
                and body["profile_version"] > 0
            )
            and type(body["enabled"]) is bool
            and isinstance(body["capabilities"], list)
            and all(isinstance(capability, str) for capability in body["capabilities"])
            and len(body["capabilities"]) == len(set(body["capabilities"]))
            and set(body["capabilities"]) <= {"dialogue", "memory.read", "memory.write"}
            and (
                body["provider_id"] is None
                and body["provider_revision"] is None
                or isinstance(body["provider_id"], str)
                and type(body["provider_revision"]) is int
                and body["provider_revision"] > 0
            ),
            "invalid_input",
            400,
        )
        actor = body["actor_id"] or "actor:role-" + client.replace("-", "")
        signature = digest(body)
        with closing(self._db()) as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT signature,actor_id FROM intents WHERE client_id=?", (client,)
            ).fetchone()
            if prior:
                require(prior[0] == signature, "idempotency_conflict", 409)
                row = db.execute("SELECT body FROM roles WHERE actor_id=?", (prior[1],)).fetchone()
                return json.loads(row[0])
            existing = db.execute("SELECT body FROM roles WHERE actor_id=?", (actor,)).fetchone()
            old = json.loads(existing[0]) if existing else None
            require(
                old is not None
                or body["actor_id"] is None
                or body["profile_id"] is None
                and body["expected_version"] == 0,
                "invalid_input",
                400,
            )
            require(
                (old or {}).get("version", 0) == body["expected_version"], "version_conflict", 409
            )
            require(
                old is None or old["state"] in {"active", "disabled", "failed"},
                "role_configuring",
                409,
            )
            row = {
                **body,
                "operator": self.console.config["principal"],
                "actor_id": actor,
                "version": body["expected_version"] + 1,
                "state": "pending",
                "stage": "start",
                "error_code": None,
                "profile_revision": old.get("profile_revision") if old else None,
                "companion_version": old["companion_version"] if old else 0,
                "memory_version": old["memory_version"] if old else 0,
                "knowledge_version": old.get("knowledge_version") if old else None,
                "legacy": old["legacy"] if old else body["actor_id"] is not None,
            }
            if old is not None and old["state"] == "active" and not body["enabled"]:
                # Stop the current installed snapshot. The form may carry stale
                # provider/profile fields, but disable must never reapply them.
                row = {
                    **old,
                    "client_id": client,
                    "operator": self.console.config["principal"],
                    "version": old["version"] + 1,
                    "enabled": False,
                    "state": "pending",
                    "stage": "cancel_reconcile",
                    "error_code": None,
                    "cancel_source_stage": "active",
                    "cancel_epoch": 0,
                }
            db.execute("INSERT INTO intents VALUES (?,?,?)", (client, signature, actor))
            db.execute(
                "INSERT INTO roles VALUES (?,?) ON CONFLICT(actor_id) DO UPDATE SET body=excluded.body",
                (actor, canonical(row)),
            )
            db.commit()
        self._remove_web(actor)
        return row

    def _begin_cancel(self, body):
        require(
            isinstance(body, dict)
            and set(body) == {"client_id", "actor_id", "expected_version"}
            and isinstance(body["actor_id"], str)
            and type(body["expected_version"]) is int,
            "invalid_input",
            400,
        )
        try:
            client = str(uuid.UUID(body["client_id"]))
            require(client == body["client_id"], "invalid_input", 400)
        except (TypeError, ValueError):
            raise Fault("invalid_input", 400) from None
        signature = digest(body)
        with closing(self._db()) as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT signature,actor_id FROM intents WHERE client_id=?", (client,)
            ).fetchone()
            if prior:
                require(prior[0] == signature, "idempotency_conflict", 409)
                saved = db.execute(
                    "SELECT body FROM roles WHERE actor_id=?", (prior[1],)
                ).fetchone()
                return json.loads(saved[0])
            saved = db.execute(
                "SELECT body FROM roles WHERE actor_id=?", (body["actor_id"],)
            ).fetchone()
            require(saved is not None, "not_found", 404)
            old = json.loads(saved[0])
            require(old["version"] == body["expected_version"], "version_conflict", 409)
            require(old["state"] == "pending", "role_configuring", 409)
            row = {
                **old,
                "client_id": client,
                "operator": self.console.config["principal"],
                "version": old["version"] + 1,
                "enabled": False,
                "state": "pending",
                "stage": "cancel_reconcile",
                "error_code": None,
                "cancel_source_stage": old["stage"],
                "cancel_epoch": 0,
            }
            db.execute("INSERT INTO intents VALUES (?,?,?)", (client, signature, body["actor_id"]))
            db.execute(
                "UPDATE roles SET body=? WHERE actor_id=?", (canonical(row), body["actor_id"])
            )
            db.commit()
        self._remove_web(body["actor_id"])
        return row

    async def route(self, console, path, body, session):
        await self.p.local_work.run(self._gate, console, session)
        name = path[len(PREFIX) :] if path.startswith(PREFIX) else ""
        require(name in {"view", "apply", "retry", "cancel"}, "not_found", 404)
        if name == "view":
            from .provider_catalog import model_capabilities

            require(body == {}, "invalid_input", 400)
            companion = await self._remote(
                self.p.settings["core"], "/internal/v1/role-runtime/manage", {"operation": "list"}
            )
            rows = await self.p.local_work.run(self.directory)
            providers = await self.p.local_work.run(self.p.provider_catalog.view)
            return {
                "roles": rows,
                "legacy_roles": companion.get("legacy_roles", []),
                "profiles": [
                    {
                        "id": p["id"],
                        "name": p["name"],
                        "version": p["version"],
                        "revision": p["draft_revision"] or p["published_revision"],
                    }
                    for p in companion["profiles"]
                ],
                "providers": [
                    {
                        "id": p["provider_id"],
                        "name": p["name"],
                        "model": p["model_id"],
                        "revision": p["revision"],
                        "available": self.p.provider_catalog._selectable(p),
                        "protocol": p["protocol"],
                        "model_capabilities": model_capabilities(p),
                    }
                    for p in providers["providers"]
                ],
                "default_available": providers["default"]["configured"],
                "default_provider_id": providers["default"].get("provider_id"),
                "default_provider_revision": providers["default"].get("provider_revision"),
                "capabilities": ["dialogue", "memory.read", "memory.write"],
            }
        async with self.lock:
            if name == "apply":
                require(isinstance(body, dict), "invalid_input", 400)
                if (
                    body.get("actor_id") is not None
                    and body.get("expected_version") == 0
                    and body.get("profile_id") is None
                ):
                    companion = await self._remote(
                        self.p.settings["core"],
                        "/internal/v1/role-runtime/manage",
                        {"operation": "list"},
                    )
                    require(
                        any(
                            item["id"] == body["actor_id"]
                            for item in companion.get("legacy_roles", [])
                        ),
                        "forbidden",
                        403,
                    )
                row = await self.p.local_work.run(self._begin, body)
            elif name == "cancel":
                row = await self.p.local_work.run(self._begin_cancel, body)
            else:
                require(
                    isinstance(body, dict) and set(body) == {"actor_id", "client_id"},
                    "invalid_input",
                    400,
                )
                row = await self.p.local_work.run(self.get, body["actor_id"])
                require(row is not None and row["client_id"] == body["client_id"], "forbidden", 403)
            if row["state"] == "pending":
                try:
                    row = await self._resume(row)
                except Fault as error:
                    await self.p.local_work.run(self._record_error, row, error)
            require(
                await self.p.local_work.run(console.session_valid, session), "session_expired", 401
            )
            return {"role": row}

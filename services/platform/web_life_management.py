"""Session-bound life management and authorized reads from Companion's owner."""

import hashlib
import ssl
import uuid
from contextlib import asynccontextmanager

import aiohttp

from .contracts import Fault, loads, require
from .transport import management_call


class WebLifeManagement:
    def __init__(self, platform, console):
        self.p, self.console = platform, console

    def available(self):
        principal = self.p.auth.principals.get(self.console.config["principal"], {})
        return bool(
            self.p.settings.get("core") is not None
            and self.console.life.code() == "ready"
            and {"role.manage", "life.read"} <= set(principal.get("actions", []))
        )

    def guard(self, session):
        require(self.console.session_valid(session), "session_expired", 401)
        require(self.available(), "forbidden", 403)

    def _selection(self, actor_id):
        """Use the logged-in operator's real actor registration, never browser scope claims."""
        p, console = self.p, self.console
        candidates = []
        for source_id in console.input_entries:
            source = p.sources.entries.get(source_id)
            if source is None or source["owner"] != console.config["principal"]:
                continue
            for entry_id in source["actor_entries"]:
                if p.auth.entries[entry_id]["actor_id"] == actor_id:
                    candidates.append((source_id, entry_id))
        if candidates:
            source_id, entry_id = candidates[0]
            header, _, selected, scope = console.dialogue.selection(
                {"conversation": source_id, "actor": actor_id}, dialogue=False
            )
            require(selected == entry_id, "scope_changed", 409)
            return header, entry_id, scope
        # Reuse RoleRuntime's stable derived operator/source registration, reconstructed
        # from its durable role record on restart. Async work never depends on a UUID entry.
        role = p.role_runtime.get(actor_id)
        require(
            role is not None
            and role["operator"] == console.config["principal"]
            and p.role_runtime.active(actor_id),
            "forbidden",
            403,
        )
        p.role_runtime._install_web(role)
        require(
            any(
                p.auth.entries[item]["actor_id"] == actor_id
                for source_id in console.input_entries
                for item in p.sources.entries[source_id]["actor_entries"]
            ),
            "forbidden",
            403,
        )
        return self._selection(actor_id)

    @asynccontextmanager
    async def scoped_actor(self, actor_id, session, *, require_identity=False):
        await self.p.local_work.run(self.console.life.prove_access, session)
        header, entry_id, scope = await self.p.local_work.run(self._selection, actor_id)
        origin = await self.p.local_work.run(self.p.origins.issue, header, entry_id)
        if require_identity and (scope["person_id"] is None or scope["conversation_id"] is None):
            payload = {
                "schema_version": 2,
                "actor_id": actor_id,
                "query": {
                    "schema_version": 1,
                    "request_id": "life-conversation:" + uuid.uuid4().hex,
                    "origin": {"assertion_ref": origin["assertion_ref"]},
                },
            }
            self.p.contracts.check("life-runtime#conversation_ensure_request", payload)
            result = await self._call("conversation/ensure", payload)
            await self.p.local_work.run(self._confirm_scope, entry_id, result)
            header, entry_id, scope = await self.p.local_work.run(self._selection, actor_id)
            origin = await self.p.local_work.run(self.p.origins.issue, header, entry_id)
        yield origin, scope
        await self.p.local_work.run(self.console.life.prove_access, session)
        with self.p.store.connect() as db:
            entry = self.p.auth.entry(db, entry_id)
            require(self.p.origins.scope(db, entry) == scope, "scope_changed", 409)

    def _confirm_scope(self, entry_id, result):
        """Persist only the two existing owners' validated metadata, never a fake input."""
        from .contracts import canonical
        from .origins import channel_key

        with self.p.store.connect(write=True) as db:
            entry = self.p.auth.entry(db, entry_id)
            scope = result["scope"]
            require(
                result["channel"] == entry["channel"]
                and scope["actor_id"] == entry["actor_id"]
                and scope["audience"] == entry["audience"]
                and scope["person_id"]
                and scope["conversation_id"],
                "invalid_upstream",
                502,
            )
            account, channel = canonical(entry["account"]), channel_key(entry)
            current = db.execute(
                "SELECT person_id,version FROM identities WHERE account=?", (account,)
            ).fetchone()
            require(
                current is None
                or tuple(current) == (scope["person_id"], result["binding_version"]),
                "scope_changed",
                409,
            )
            attested = db.execute(
                "SELECT person_id FROM ingest_subjects WHERE account=?", (account,)
            ).fetchone()
            require(attested is None or attested[0] == scope["person_id"], "scope_changed", 409)
            current_channel = db.execute(
                "SELECT conversation_id FROM channels WHERE channel=?", (channel,)
            ).fetchone()
            require(
                current_channel is None or current_channel[0] == scope["conversation_id"],
                "scope_changed",
                409,
            )
            db.execute(
                "INSERT OR IGNORE INTO identities VALUES(?,?,?)",
                (account, scope["person_id"], result["binding_version"]),
            )
            db.execute(
                "INSERT OR IGNORE INTO channels VALUES(?,?)", (channel, scope["conversation_id"])
            )

    async def _call(self, path, payload, *, management=False, binary=False):
        require(
            path
            in {
                "manage",
                "read",
                "media/read",
                "content/read",
                "conversation/ensure",
                "proactive/control",
            },
            "invalid_input",
            400,
        )
        config = self.p.settings["core"] if management else self.console.life.config
        from .auth import secret

        token = secret(config["token_env"]) if management else self.console.life._credential()
        require(token is not None, "dependency_unavailable", 503)
        # Eight representations may each contain 2 MiB of decoded bytes. Base64
        # expands them by 4/3; retain the complete response or reject explicitly.
        limit = 32 * 1024 * 1024 if binary or path == "content/read" else 1024 * 1024
        try:
            tls = ssl.create_default_context(cafile=config.get("ca_file"))
            async with aiohttp.ClientSession(
                trust_env=False,
                timeout=aiohttp.ClientTimeout(total=config.get("timeout_seconds", 10)),
            ) as client:
                async with client.post(
                    config["base_url"].rstrip("/") + "/internal/v2/life/" + path,
                    json=payload,
                    headers={"Authorization": "Bearer " + token},
                    ssl=tls,
                    allow_redirects=False,
                ) as response:
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        data.extend(chunk)
                        require(len(data) <= limit, "budget_exceeded", 413)
                    if response.status != 200:
                        answer = loads(bytes(data))
                        code = answer.get("code") if isinstance(answer, dict) else None
                        require(
                            code
                            in {
                                "not_found",
                                "forbidden",
                                "unauthorized",
                                "version_conflict",
                                "idempotency_conflict",
                                "scope_changed",
                                "invalid_input",
                                "dependency_unavailable",
                                "budget_exceeded",
                                "unsupported_operation",
                                "timeout",
                                "too_many_requests",
                            },
                            "invalid_upstream",
                            502,
                        )
                        raise Fault(code, response.status)
                    if binary:
                        content_type = response.content_type
                        digest = response.headers.get("X-Content-SHA256", "")
                        require(
                            content_type in {"image/png", "image/jpeg", "image/webp"}
                            and hashlib.sha256(data).hexdigest() == digest,
                            "invalid_upstream",
                            502,
                        )
                        return bytes(data), content_type, digest
                    answer = loads(bytes(data))
                    schema = {
                        "read": "read_response",
                        "manage": "manage_response",
                        "content/read": "content_read_response",
                        "conversation/ensure": "conversation_ensure_response",
                        "proactive/control": "control_read_response",
                    }[path]
                    try:
                        self.p.contracts.check("life-runtime#" + schema, answer)
                    except Fault:
                        raise Fault("invalid_upstream", 502) from None
                    require(
                        answer["request_id"] == payload.get("query", payload)["request_id"]
                        and answer["actor_id"] == payload["actor_id"],
                        "invalid_upstream",
                        502,
                    )
                    if path == "read" and answer["resource"] == "image_backend":
                        for item in answer["items"]:
                            item["credential_ref"] = None
                    return answer
        except (aiohttp.ClientError, ssl.SSLError, OSError, TimeoutError):
            raise Fault("dependency_unavailable", 503) from None

    async def runtime(self, name, body, session):
        require(
            isinstance(body, dict) and isinstance(body.get("actor_id"), str), "invalid_input", 400
        )
        actor = body["actor_id"]
        value = body.get("value", {})
        needs_identity = (
            name == "manage"
            and isinstance(value, dict)
            and (
                value.get("scope") is not None
                or value.get("source_scope") is not None
                or value.get("mode") == "together"
            )
        )
        async with self.scoped_actor(actor, session, require_identity=needs_identity) as (
            origin,
            scope,
        ):
            if name == "manage":
                await self.p.local_work.run(self.guard, session)
                require(
                    set(body)
                    == {"actor_id", "operation", "expected_version", "value", "client_id"},
                    "invalid_input",
                    400,
                )
                require(
                    isinstance(body["client_id"], str) and len(body["client_id"]) <= 128,
                    "invalid_input",
                    400,
                )
                require(body["operation"] != "image.backend.configure", "invalid_input", 400)
                require(isinstance(body["value"], dict), "invalid_input", 400)
                value = dict(body["value"])
                if "scope" in value:
                    if value["scope"] is not None:
                        require(
                            scope["person_id"] and scope["conversation_id"],
                            "memory_identity_not_ready",
                            503,
                        )
                        value["scope"] = scope
                if "person_id" in value:
                    require(value["person_id"] == scope["person_id"], "forbidden", 403)
                if "query" in value:
                    value["query"] = {
                        "schema_version": 1,
                        "request_id": body["client_id"],
                        "origin": {"assertion_ref": origin["assertion_ref"]},
                    }
                if "source_scope" in value and value["source_scope"] is not None:
                    value["source_scope"] = scope
                if "participants" in value:
                    # A together session is scoped to the authenticated operator.
                    value["participants"] = (
                        [scope["person_id"]] if value.get("mode") == "together" else []
                    )
                payload = {
                    "schema_version": 2,
                    "request_id": body["client_id"],
                    "actor_id": actor,
                    "operation": body["operation"],
                    "expected_version": body["expected_version"],
                    "value": value,
                }
                self.p.contracts.check("life-runtime#manage_request", payload)
                result = await self._call("manage", payload, management=True)
                await self.p.local_work.run(self.guard, session)
                return result
            selection = {"actor_id", "resource", "object_id", "expected_version", "limit", "after"}
            require(
                set(body) - {"chapter_view"} == selection
                if name == "read"
                else set(body) == {"actor_id", "media_id"}
                if name == "media"
                else set(body) == {"actor_id", "content_ref", "reading_id", "range"},
                "invalid_input",
                400,
            )
            payload = {
                "schema_version": 2,
                "query": {
                    "schema_version": 1,
                    "request_id": "life:" + uuid.uuid4().hex,
                    "origin": {"assertion_ref": origin["assertion_ref"]},
                },
                "actor_id": actor,
                "scope": scope if scope["person_id"] and scope["conversation_id"] else None,
            }
            if name == "read":
                payload.update({key: body[key] for key in selection - {"actor_id"}})
                if "chapter_view" in body:
                    payload["chapter_view"] = body["chapter_view"]
                management_read = (
                    body.get("chapter_view") == "current" or body["resource"] == "image_backend"
                )
                if management_read:
                    await self.p.local_work.run(self.guard, session)
                schema, path = "read_request", "read"
            elif name == "media":
                payload.update(media_id=body["media_id"])
                schema, path = "media_request", "media/read"
            else:
                payload.update(
                    content_ref=body["content_ref"],
                    reading_id=body["reading_id"],
                    range=body["range"],
                )
                schema, path = "content_read_request", "content/read"
            self.p.contracts.check("life-runtime#" + schema, payload)
            answer = await self._call(
                path, payload, management=name == "read" and management_read, binary=name == "media"
            )
            if name == "read" and management_read:
                await self.p.local_work.run(self.guard, session)
            return answer

    async def image_backend(self, name, body, session):
        require(
            isinstance(body, dict) and isinstance(body.get("actor_id"), str), "invalid_input", 400
        )
        async with self.scoped_actor(body["actor_id"], session):
            await self.p.local_work.run(self.guard, session)
            credentials = self.p.service_credentials
            if name == "status":
                require(set(body) == {"actor_id"}, "invalid_input", 400)
                return await self.p.local_work.run(credentials.view, body["actor_id"])
            require(
                name == "save"
                and set(body)
                == {
                    "actor_id",
                    "value",
                    "credential",
                    "catalog_revision",
                    "expected_version",
                    "client_id",
                },
                "invalid_input",
                400,
            )
            value = body["value"]
            require(
                isinstance(value, dict)
                and set(value) == {"base_url", "profile", "checkpoint", "enabled"},
                "invalid_input",
                400,
            )
            draft = {**value, "credential_ref": None}
            payload = {
                "schema_version": 2,
                "request_id": body["client_id"],
                "actor_id": body["actor_id"],
                "operation": "image.backend.configure",
                "expected_version": body["expected_version"],
                "value": draft,
            }
            self.p.contracts.check("life-runtime#manage_request", payload)
            reference = await self.p.local_work.run(
                credentials.save,
                body["actor_id"],
                value,
                body["credential"],
                body["catalog_revision"],
                body["client_id"],
            )
            payload["value"]["credential_ref"] = reference
            result = await self._call("manage", payload, management=True)
            await self.p.local_work.run(self.guard, session)
            return result

    async def retry(self, body, session):
        await self.p.local_work.run(self.guard, session)
        require(
            isinstance(body, dict)
            and set(body)
            == {
                "actor_id",
                "plan_id",
                "phase_id",
                "expected_version",
            },
            "invalid_input",
            400,
        )
        self.p.contracts.check("life-read#retry_request", body)
        # The management token cannot substitute for the independent actor read grant.
        # Check the same read route before mutation and again before returning its receipt.
        actor = {"actor_id": body["actor_id"]}
        await self.console.life.route("/api/web/life/today", actor, session)
        result = await management_call(
            self.p.settings["core"],
            "/internal/v1/life-generation/retry",
            body,
        )
        await self.p.local_work.run(self.guard, session)
        await self.console.life.route("/api/web/life/today", actor, session)
        try:
            self.p.contracts.check("life-read#retry_response", result)
        except Fault:
            raise Fault("invalid_upstream", 502) from None
        require(
            result["actor_id"] == body["actor_id"] and result["plan_id"] == body["plan_id"],
            "invalid_upstream",
            502,
        )
        return result

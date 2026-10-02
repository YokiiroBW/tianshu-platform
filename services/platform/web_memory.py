"""Browser's read-only window over Memory's fixed, scoped browser reader.

Platform owns the login and fresh origin; Memory owns the account, actor, scope and source checks.
The browser may select a listed role id and version. It cannot supply a service identity,
account, scope, origin or upstream URL; Platform derives those from current authority.
"""

import asyncio
import copy
import ssl
import uuid
from datetime import datetime, timezone
from contextlib import asynccontextmanager

import aiohttp

from .auth import secret
from .contracts import Fault, loads, require
from .web_readers import RESPONSE_LIMIT, WebReader, _cursor, _limit, _text

PREFIX = "/api/web/memory/"
ROUTES = frozenset({"overview", "subjects", "records"})


def validate_memory_binding(settings, auth):
    config = settings.get("web_memory")
    if config is None:
        return
    web = settings.get("web")
    require(isinstance(web, dict), "invalid_input", 400)
    principal = auth.principals.get(web.get("principal"))
    entry = auth.entries.get(config["entry_id"])
    require(
        principal is not None
        and entry is not None
        and principal["kind"] == "operator"
        and principal["service"] == "platform"
        and entry["owner"] == web["principal"]
        and entry["account"] == principal["account"]
        and entry["kind"] == "local_operator"
        and entry["audience"] == "self_private"
        and {"caller": "platform", "receiver": "memory", "purpose": "dialogue"} in entry["routes"],
        "invalid_input",
        400,
    )


class WebMemory(WebReader):
    def __init__(self, platform, console):
        super().__init__("memory", platform, console)

    def state(self):
        answer = super().state()
        entry = self.platform.auth.entries.get(self.config["entry_id"]) if self.config else None
        answer["actor_id"] = entry["actor_id"] if entry and self._authorized() else None
        answer["roles"] = self._directory() if entry and self._authorized() else []
        return answer

    def _directory(self):
        p, config = self.platform, self.config
        principal = self.console.config["principal"]
        template = p.auth.entries[config["entry_id"]]
        default = template["actor_id"]
        rows = {
            row["actor_id"]: row
            for row in p.role_runtime.directory()
            if row.get("operator") == principal
        }
        actors = [default]
        if (
            config.get("runtime_roles", False)
            and p.role_runtime.config
            and p.role_runtime.config["enabled"]
        ):
            actors.extend(actor for actor in rows if actor != default)
        result = []
        for actor in actors:
            row = rows.get(actor)
            reason = None
            if row is not None:
                if not p.role_runtime.config or not p.role_runtime.config["enabled"]:
                    reason = "role_unavailable"
                elif row.get("state") == "disabled" or (
                    row.get("state") == "active" and not row.get("enabled")
                ):
                    reason = "role_disabled"
                elif row.get("state") != "active":
                    reason = "role_configuring"
                elif "memory.read" not in row.get("capabilities", []):
                    reason = "memory_read_disabled"
            result.append(
                {
                    "id": actor,
                    "label": row["name"] if row else "默认角色",
                    "version": row["version"] if row else 0,
                    "available": reason is None,
                    "reason": reason,
                }
            )
        return result

    def role_choice(self, actor, version):
        roles = {row["id"]: row for row in self._directory()}
        require(actor in roles, "forbidden", 403)
        row = roles[actor]
        require(version == row["version"], "scope_changed", 409)
        require(row["available"], row["reason"], 403)
        return row

    def _scope(self, actor, version):
        p, config = self.platform, self.config
        self.role_choice(actor, version)
        principal_id = self.console.config["principal"]
        principal = p.auth.principals[principal_id]
        header = "Bearer " + (secret(principal["token_env"]) or "")
        with p.store.connect() as db:
            identity, _ = p.auth.authenticate(header, db, "origin.issue", operator=True)
            require(identity == principal_id, "forbidden", 403)
            entry = p.auth.entry(db, config["entry_id"])
            require(
                entry["owner"] == identity
                and entry["account"] == principal["account"]
                and entry["kind"] == "local_operator"
                and entry["audience"] == "self_private",
                "forbidden",
                403,
            )
            p.auth.route(entry, "platform", "memory", "dialogue")
            scope = p.origins.scope(db, entry)
            scope["actor_id"] = actor
            require(
                scope["person_id"] is not None and scope["conversation_id"] is not None,
                "memory_identity_not_ready",
                503,
            )
        return header, scope, entry

    def _selection(self, actor, version, temporary_entry):
        header, scope, template = self._scope(actor, version)
        entry_id = temporary_entry or self.config["entry_id"]
        if temporary_entry is not None:
            require(actor != template["actor_id"], "scope_changed", 409)
            entry = copy.deepcopy(template)
            entry["actor_id"] = actor
            self.platform.auth.entries[entry_id] = entry
        try:
            origin = self.platform.origins.issue(header, entry_id)
            _, current, _ = self._scope(actor, version)
            require(current == scope, "scope_changed", 409)
            return origin["assertion_ref"], scope
        except BaseException:
            if temporary_entry is not None:
                self.platform.auth.entries.pop(entry_id, None)
            raise

    def _still_issued(self, reference, scope):
        with self.platform.store.connect() as db:
            _, _, context = self.platform.origins.context(
                db, reference, "platform", "memory", "dialogue"
            )
            require(context["allowed_scope"] == scope, "scope_changed", 409)

    @asynccontextmanager
    async def scoped_origin(self, actor, version, session):
        """Issue and recheck a selected role's origin, owning temporary entry cleanup."""
        await self.platform.local_work.run(self.prove_access, session)
        require(self.active < 4, "too_many_requests", 429)
        self.active += 1
        default = self.platform.auth.entries[self.config["entry_id"]]["actor_id"]
        temporary = "memory-view-" + uuid.uuid4().hex if actor != default else None
        try:
            async with self.slots:
                await self.platform.local_work.run(self.prove_access, session)
                selection = asyncio.create_task(
                    self.platform.local_work.run(self._selection, actor, version, temporary)
                )
                try:
                    reference, scope = await asyncio.shield(selection)
                except BaseException:

                    def discard_late(done):
                        if not done.cancelled():
                            done.exception()
                        if temporary is not None:
                            self.platform.auth.entries.pop(temporary, None)

                    selection.add_done_callback(discard_late)
                    raise
                await self.platform.local_work.run(self.prove_access, session)
                yield reference, scope
                await self.platform.local_work.run(self.prove_access, session)
                _, current, _ = await self.platform.local_work.run(self._scope, actor, version)
                require(current == scope, "scope_changed", 409)
                await self.platform.local_work.run(self._still_issued, reference, scope)
        finally:
            if temporary is not None:
                self.platform.auth.entries.pop(temporary, None)
            self.active -= 1

    async def route(self, path, body, session):
        name = path[len(PREFIX) :] if path.startswith(PREFIX) else ""
        require(name == "state" or name in ROUTES, "not_found", 404)
        require(isinstance(body, dict), "invalid_input", 400)
        if name == "state":
            require(body == {}, "invalid_input", 400)
            return await self.platform.local_work.run(self.state)
        await self.platform.local_work.run(self.prove_access, session)
        request, actor, version = await self.platform.local_work.run(self._request, name, body)
        try:
            async with self.scoped_origin(actor, version, session) as (reference, scope):
                request.update(
                    request_id="memory-web-" + uuid.uuid4().hex,
                    origin={"assertion_ref": reference},
                    scope=scope,
                )
                answer = await self._read(name, request)
            self.last = {"at": datetime.now(timezone.utc).isoformat(), "code": "ok"}
            return {
                key: value for key, value in answer.items() if key not in {"scope", "request_id"}
            }
        except Fault as exc:
            self.last = {"at": datetime.now(timezone.utc).isoformat(), "code": exc.code}
            raise

    def _request(self, name, body):
        selected = "role_id" in body or "role_version" in body
        require(not selected or {"role_id", "role_version"} <= set(body), "invalid_input", 400)
        if not selected:
            chosen = self.platform.auth.entries[self.config["entry_id"]]["actor_id"]
            version = next((row["version"] for row in self._directory() if row["id"] == chosen), 0)
        else:
            chosen = _text(body["role_id"])
            version = body["role_version"]
            require(type(version) is int and version >= 0, "invalid_input", 400)
        self.role_choice(chosen, version)
        content = {
            key: value for key, value in body.items() if key not in {"role_id", "role_version"}
        }
        if name == "overview":
            require(content == {}, "invalid_input", 400)
            return {"schema_version": 1}, chosen, version
        if name == "subjects":
            require(set(content) == {"limit", "cursor"}, "invalid_input", 400)
        else:
            require(set(content) == {"subject", "limit", "cursor"}, "invalid_input", 400)
        result = {
            "schema_version": 1,
            "limit": _limit(content["limit"]),
            "cursor": _cursor(content["cursor"]),
        }
        if name == "records":
            subject = content["subject"]
            require(subject is None or isinstance(subject, dict), "invalid_input", 400)
            if subject is not None:
                if subject.get("kind") == "person":
                    require(set(subject) == {"kind", "person_id"}, "invalid_input", 400)
                    subject = {"kind": "person", "person_id": _text(subject["person_id"])}
                elif subject.get("kind") == "group":
                    require(set(subject) == {"kind", "conversation_id"}, "invalid_input", 400)
                    subject = {
                        "kind": "group",
                        "conversation_id": _text(subject["conversation_id"]),
                    }
                else:
                    raise Fault("invalid_input", 400)
            if subject is not None:
                result["subject"] = subject
        return result, chosen, version

    async def _read(self, name, request):
        base = self.config["base_url"].rstrip("/")
        try:
            context = ssl.create_default_context(cafile=self.config.get("ca_file"))
            async with asyncio.timeout(self.config.get("timeout_seconds", 10)):
                async with aiohttp.ClientSession(trust_env=False) as client:
                    async with client.post(
                        base + "/internal/v1/memory/browser/" + name,
                        json=request,
                        headers={"Authorization": "Bearer " + self._credential()},
                        ssl=context,
                        allow_redirects=False,
                    ) as response:
                        raw = bytearray()
                        async for chunk in response.content.iter_chunked(8192):
                            raw.extend(chunk)
                            require(len(raw) <= RESPONSE_LIMIT, "invalid_upstream", 502)
                        try:
                            answer = loads(bytes(raw))
                        except Fault:
                            raise Fault("invalid_upstream", 502) from None
                        require(isinstance(answer, dict), "invalid_upstream", 502)
                        if response.status != 200:
                            require(
                                answer.get("request_id") == request["request_id"],
                                "invalid_upstream",
                                502,
                            )
                            self._error(response.status, answer)
                        require(
                            answer.get("schema_version") == 1
                            and answer.get("request_id") == request["request_id"]
                            and answer.get("scope") == request["scope"]
                            and isinstance(answer.get("verified_at"), str)
                            and type(answer.get("scope_version")) is int,
                            "invalid_upstream",
                            502,
                        )
                        if name == "overview":
                            require(
                                type(answer.get("memory_group_count")) is int
                                and type(answer.get("counts_truncated")) is bool,
                                "invalid_upstream",
                                502,
                            )
                        else:
                            require(
                                isinstance(answer.get("items"), list)
                                and (
                                    answer.get("next_cursor") is None
                                    or isinstance(answer["next_cursor"], str)
                                ),
                                "invalid_upstream",
                                502,
                            )
                            if name == "subjects":
                                require(
                                    all(
                                        isinstance(item, dict)
                                        and type(item.get("group_count_truncated")) is bool
                                        for item in answer["items"]
                                    ),
                                    "invalid_upstream",
                                    502,
                                )
                        return answer
        except (aiohttp.ClientError, OSError, ssl.SSLError):
            raise Fault("dependency_unavailable", 503) from None
        except TimeoutError:
            raise Fault("timeout", 503) from None

    def _error(self, status, answer):
        code = answer.get("code")
        if status == 400 and code == "invalid_input":
            raise Fault("invalid_input", 400)
        if status in {401, 403} and code in {"unauthorized", "forbidden"}:
            raise Fault("upstream_forbidden", 403)
        if status == 409 and code == "scope_changed":
            raise Fault("scope_changed", 409)
        if status == 429 and code == "queue_full":
            raise Fault("upstream_busy", 503)
        if status in {408, 503} and code in {
            "timeout",
            "dependency_unavailable",
            "log_unavailable",
        }:
            raise Fault(code, 503)
        raise Fault("invalid_upstream", 502)

"""Browser's read-only window over Memory's fixed, scoped browser reader.

Platform owns the login and fresh origin; Memory owns the account, actor, scope and source checks.
No browser field can name a service identity, account, actor, origin or upstream URL.
"""

import asyncio
import ssl
import uuid
from datetime import datetime, timezone

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
        return answer

    def _scope(self):
        p, config = self.platform, self.config
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
            require(
                scope["person_id"] is not None and scope["conversation_id"] is not None,
                "memory_identity_not_ready",
                503,
            )
        return header, scope

    def _selection(self):
        header, scope = self._scope()
        origin = self.platform.origins.issue(header, self.config["entry_id"])
        _, current = self._scope()
        require(current == scope, "scope_changed", 409)
        return origin["assertion_ref"], scope

    def _still_issued(self, reference, scope):
        with self.platform.store.connect() as db:
            _, _, context = self.platform.origins.context(
                db, reference, "platform", "memory", "dialogue"
            )
            require(context["allowed_scope"] == scope, "scope_changed", 409)

    async def route(self, path, body, session):
        name = path[len(PREFIX) :] if path.startswith(PREFIX) else ""
        require(name == "state" or name in ROUTES, "not_found", 404)
        require(isinstance(body, dict), "invalid_input", 400)
        if name == "state":
            require(body == {}, "invalid_input", 400)
            return self.state()
        self._prove(session)
        request = self._request(name, body)
        require(self.active < 4, "too_many_requests", 429)
        self.active += 1
        try:
            async with self.slots:
                self._prove(session)
                reference, scope = await self.platform.local_work.run(self._selection)
                self._prove(session)
                request.update(
                    request_id="memory-web-" + uuid.uuid4().hex,
                    origin={"assertion_ref": reference},
                    scope=scope,
                )
                answer = await self._read(name, request)
            self._prove(session)
            _, current = await self.platform.local_work.run(self._scope)
            require(current == scope, "scope_changed", 409)
            await self.platform.local_work.run(self._still_issued, reference, scope)
            self.last = {"at": datetime.now(timezone.utc).isoformat(), "code": "ok"}
            # The verified scope and assertion remain server-side. The browser receives only the
            # read projection needed for a page; the peer's own response is proved before this point.
            return {
                key: value for key, value in answer.items() if key not in {"scope", "request_id"}
            }
        except Fault as exc:
            self.last = {"at": datetime.now(timezone.utc).isoformat(), "code": exc.code}
            raise
        finally:
            self.active -= 1

    def _request(self, name, body):
        if name == "overview":
            require(body == {}, "invalid_input", 400)
            return {"schema_version": 1}
        if name == "subjects":
            require(set(body) == {"limit", "cursor"}, "invalid_input", 400)
        else:
            require(set(body) == {"subject", "limit", "cursor"}, "invalid_input", 400)
        result = {
            "schema_version": 1,
            "limit": _limit(body["limit"]),
            "cursor": _cursor(body["cursor"]),
        }
        if name == "records":
            subject = body["subject"]
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
        return result

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

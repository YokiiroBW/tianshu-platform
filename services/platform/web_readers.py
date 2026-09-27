"""Narrow same-origin read windows over registered Memory and Companion services.

The browser chooses an operation and a bounded, allowlisted subject. It never chooses a URL,
credential, reader identity or upstream operation. The peers retain their own authorization.
"""

import asyncio
import hmac
import re
import ssl
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from .auth import secret
from .contracts import Fault, require

READ_TIMEOUT = 10
RESPONSE_LIMIT = 262144
TOKEN_ENV = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
PROJECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
READ_ROUTES = {
    "knowledge": frozenset({"query", "documents", "document", "notes"}),
    "life": frozenset({"actors", "snapshot", "diaries", "revision"}),
}


def validate_readers(settings, reserved):
    """Fail before service startup on a widened target or borrowed identity."""
    seen = set(reserved)
    for name in ("knowledge", "life", "memory"):
        config = settings.get("web_" + name)
        if config is None:
            continue
        allowed = {"enabled", "base_url", "token_env", "ca_file", "timeout_seconds"}
        if name == "knowledge":
            allowed |= {"projects"}
        if name == "memory":
            allowed |= {"entry_id"}
        require(isinstance(config, dict) and set(config) <= allowed, "invalid_input", 400)
        require(type(config.get("enabled", False)) is bool, "invalid_input", 400)
        # A disabled section is still validated: bad deployment input must not silently appear
        # usable when an operator later flips enabled.
        require({"base_url", "token_env"} <= set(config), "invalid_input", 400)
        require(isinstance(config["base_url"], str), "invalid_input", 400)
        try:
            url = urlsplit(config["base_url"])
            port = url.port
        except ValueError:
            raise Fault("invalid_input", 400) from None
        require(
            url.scheme == "https"
            and bool(url.hostname)
            and port is not None
            and not url.username
            and not url.password
            and url.path in {"", "/"}
            and not url.query
            and not url.fragment,
            "invalid_input",
            400,
        )
        token_env = config["token_env"]
        require(
            isinstance(token_env, str)
            and TOKEN_ENV.fullmatch(token_env) is not None
            and token_env not in seen,
            "invalid_input",
            400,
        )
        current = secret(token_env)
        if current is not None:
            for other in seen:
                value = secret(other)
                require(
                    value is None or not hmac.compare_digest(current, value), "invalid_input", 400
                )
        seen.add(token_env)
        ca = config.get("ca_file")
        require(
            ca is None or (isinstance(ca, str) and Path(ca).is_absolute()), "invalid_input", 400
        )
        timeout = config.get("timeout_seconds", READ_TIMEOUT)
        require(type(timeout) in (int, float) and 0 < timeout <= READ_TIMEOUT, "invalid_input", 400)
        if name == "knowledge":
            projects = config.get("projects")
            require(isinstance(projects, list) and 1 <= len(projects) <= 64, "invalid_input", 400)
            ids = []
            for item in projects:
                require(
                    isinstance(item, dict)
                    and set(item) == {"project_id", "label"}
                    and isinstance(item["project_id"], str)
                    and PROJECT_ID.fullmatch(item["project_id"]) is not None
                    and isinstance(item["label"], str)
                    and 1 <= len(item["label"]) <= 128,
                    "invalid_input",
                    400,
                )
                ids.append(item["project_id"])
            require(len(set(ids)) == len(ids), "invalid_input", 400)
        if name == "memory":
            require(
                isinstance(config.get("entry_id"), str)
                and IDENTIFIER.fullmatch(config["entry_id"]) is not None,
                "invalid_input",
                400,
            )


def _text(value, maximum=128):
    require(isinstance(value, str) and 1 <= len(value) <= maximum, "invalid_input", 400)
    return value


def _limit(value):
    require(type(value) is int and 1 <= value <= 50, "invalid_input", 400)
    return value


def _budget(value):
    require(type(value) is int and 256 <= value <= 32768, "invalid_input", 400)
    return value


def _version(value):
    require(type(value) is int and 1 <= value <= 2**31 - 1, "invalid_input", 400)
    return value


def _cursor(value):
    require(
        value is None or (isinstance(value, str) and 1 <= len(value) <= 2048), "invalid_input", 400
    )
    return value


class WebReader:
    def __init__(self, name, platform, console):
        self.name, self.platform, self.console = name, platform, console
        self.config = platform.settings.get("web_" + name)
        self.last = None
        self.slots = asyncio.Semaphore(4)
        self.active = 0

    def _authorized(self):
        principal = self.platform.auth.principals.get(self.console.config["principal"], {})
        return self.name + ".read" in principal.get("actions", [])

    def code(self):
        if self.config is None or not self.config.get("enabled", False):
            return self.name + "_not_configured"
        if not self._authorized():
            return self.name + "_read_required"
        if secret(self.config["token_env"]) is None:
            return self.name + "_credential_missing"
        return "ready"

    def state(self):
        code = self.code()
        answer = {
            "available": code == "ready",
            "code": code,
            "peer": {
                "configured": self.config is not None and self.config.get("enabled", False),
                "verified_at": self.last["at"] if self.last and self.last["code"] == "ok" else None,
                "code": self.last["code"] if self.last else "unverified",
            },
        }
        if self.name == "knowledge":
            answer["projects"] = (
                self.config["projects"] if self.config and self._authorized() else []
            )
        return answer

    def _prove(self, session):
        require(self.console.session_valid(session), "session_expired", 401)
        code = self.code()
        require(code == "ready", code, 403 if code.endswith("_required") else 503)

    def _credential(self):
        value = secret(self.config["token_env"])
        require(value is not None, self.name + "_credential_missing", 503)
        for other in self.platform.other_credentials:
            if other == self.config["token_env"]:
                continue
            resolved = secret(other)
            require(resolved is None or not hmac.compare_digest(value, resolved), "forbidden", 403)
        return value

    async def route(self, path, body, session):
        prefix = "/api/web/" + self.name + "/"
        name = path[len(prefix) :] if path.startswith(prefix) else ""
        require(name == "state" or name in READ_ROUTES[self.name], "not_found", 404)
        require(isinstance(body, dict), "invalid_input", 400)
        if name == "state":
            require(body == {}, "invalid_input", 400)
            return self.state()
        self._prove(session)
        payload = self._request(name, body)
        require(self.active < 4, "too_many_requests", 429)
        self.active += 1
        try:
            async with self.slots:
                self._prove(session)
                try:
                    result = await self._read(name, payload)
                except Fault as exc:
                    self.last = {"at": datetime.now(timezone.utc).isoformat(), "code": exc.code}
                    raise
        finally:
            self.active -= 1
        self._prove(session)
        self.last = {"at": datetime.now(timezone.utc).isoformat(), "code": "ok"}
        if self.name == "knowledge":
            return {"project_id": body["project_id"], "operation": name, "result": result}
        return result

    def _request(self, name, body):
        if self.name == "knowledge":
            return self._knowledge(name, body)
        return self._life(name, body)

    def _knowledge(self, name, body):
        shapes = {
            "query": {"project_id", "text", "budget_bytes"},
            "notes": {"project_id", "text", "budget_bytes"},
            "documents": {"project_id", "limit", "cursor"},
            "document": {
                "project_id",
                "document_id",
                "expected_version",
                "expected_hash",
                "limit",
                "cursor",
            },
        }
        require(set(body) == shapes[name], "invalid_input", 400)
        project_id = _text(body["project_id"], 64)
        require(PROJECT_ID.fullmatch(project_id) is not None, "invalid_input", 400)
        require(project_id in {p["project_id"] for p in self.config["projects"]}, "forbidden", 403)
        operation = {
            "documents": "document_list",
            "document": "document_read",
            "notes": "note_query",
        }.get(name, name)
        if name in {"query", "notes"}:
            arguments = {
                "text": _text(body["text"], 1024),
                "budget_bytes": _budget(body["budget_bytes"]),
            }
        else:
            arguments = {
                "limit": _limit(body["limit"]),
                "budget_bytes": 32768,
                "cursor": _cursor(body["cursor"]),
            }
            if name == "document":
                arguments.update(
                    document_id=_text(body["document_id"], 128),
                    expected_version=_version(body["expected_version"]),
                    expected_hash=body["expected_hash"],
                )
                require(
                    arguments["expected_hash"] is None
                    or (
                        isinstance(arguments["expected_hash"], str)
                        and re.fullmatch(r"[0-9a-f]{64}", arguments["expected_hash"]) is not None
                    ),
                    "invalid_input",
                    400,
                )
        return {"operation": operation, "project_id": project_id, "arguments": arguments}

    def _life(self, name, body):
        shapes = {
            "actors": {"limit", "after_actor_id"},
            "snapshot": {"actor_id"},
            "diaries": {"actor_id", "limit", "after"},
            "revision": {"actor_id", "diary_id", "revision_id", "expected_diary_version"},
        }
        require(set(body) == shapes[name], "invalid_input", 400)
        result = {"schema_version": 1}
        if name in {"actors", "diaries"}:
            result["limit"] = _limit(body["limit"])
        for key in ("actor_id", "diary_id", "revision_id"):
            if key in body:
                result[key] = _text(body[key])
        if name == "actors":
            require(
                body["after_actor_id"] is None or isinstance(body["after_actor_id"], str),
                "invalid_input",
                400,
            )
            if body["after_actor_id"] is not None:
                result["after_actor_id"] = _text(body["after_actor_id"])
        if name == "diaries":
            after = body["after"]
            require(
                after is None
                or (
                    isinstance(after, dict)
                    and set(after) == {"day", "diary_id"}
                    and isinstance(after["day"], str)
                    and re.fullmatch(r"\d{4}-\d{2}-\d{2}", after["day"]) is not None
                    and isinstance(after["diary_id"], str)
                ),
                "invalid_input",
                400,
            )
            if after is not None:
                result["after"] = {"day": after["day"], "diary_id": _text(after["diary_id"])}
        if name == "revision":
            result["expected_diary_version"] = _version(body["expected_diary_version"])
        return result

    async def _read(self, name, payload):
        base = self.config["base_url"].rstrip("/")
        path = (
            "/local/v1/project-knowledge/action"
            if self.name == "knowledge"
            else "/internal/v1/life-read/" + name
        )
        try:
            context = ssl.create_default_context(cafile=self.config.get("ca_file"))
            async with asyncio.timeout(self.config.get("timeout_seconds", READ_TIMEOUT)):
                async with aiohttp.ClientSession(trust_env=False) as client:
                    async with client.post(
                        base + path,
                        json=payload,
                        headers={"Authorization": "Bearer " + self._credential()},
                        ssl=context,
                        allow_redirects=False,
                    ) as response:
                        raw = bytearray()
                        async for chunk in response.content.iter_chunked(8192):
                            raw.extend(chunk)
                            require(len(raw) <= RESPONSE_LIMIT, "invalid_upstream", 502)
                        from .contracts import loads

                        try:
                            answer = loads(bytes(raw))
                        except Fault:
                            raise Fault("invalid_upstream", 502) from None
                        require(isinstance(answer, dict), "invalid_upstream", 502)
                        if response.status != 200:
                            code = answer.get("code")
                            require(isinstance(code, str), "invalid_upstream", 502)
                            self._peer_error(code, response.status)
                        self._answer(name, payload, answer)
                        return answer
        except (aiohttp.ClientError, OSError, ssl.SSLError):
            raise Fault("dependency_unavailable", 503) from None
        except TimeoutError:
            raise Fault("timeout", 503) from None

    def _peer_error(self, code, status):
        if status in {401, 403, 422} and code in {
            "unauthorized",
            "forbidden",
            "project_unregistered",
        }:
            raise Fault("upstream_forbidden", 403)
        if status in {404, 422} and code == "not_found":
            raise Fault("not_found", 404)
        if code in {"version_conflict", "stale_evidence", "cursor_stale", "invalid_cursor"}:
            raise Fault(code, 409)
        if status in {400, 422} and code in {"invalid_input", "budget_too_small"}:
            raise Fault(code, 400)
        raise Fault("dependency_unavailable", 503)

    def _answer(self, name, payload, answer):
        if self.name == "knowledge":
            require(answer.get("project_id") == payload["project_id"], "invalid_upstream", 502)
            field = {
                "query": "blocks",
                "documents": "items",
                "document": "blocks",
                "notes": "notes",
            }[name]
            require(isinstance(answer.get(field), list), "invalid_upstream", 502)
            if name in {"documents", "document"}:
                require(
                    answer.get("next_cursor") is None or isinstance(answer["next_cursor"], str),
                    "invalid_upstream",
                    502,
                )
            if name == "document":
                require(
                    answer.get("document_id") == payload["arguments"]["document_id"],
                    "invalid_upstream",
                    502,
                )
        else:
            require(
                answer.get("schema_version") == 1 and answer.get("fictional") is True,
                "invalid_upstream",
                502,
            )
            if name in {"actors", "diaries"}:
                require(isinstance(answer.get("items"), list), "invalid_upstream", 502)
            if name in {"snapshot", "diaries", "revision"}:
                require(answer.get("actor_id") == payload["actor_id"], "invalid_upstream", 502)
            if name == "snapshot":
                require(answer.get("state_basis") == "last_persisted", "invalid_upstream", 502)
            if name == "revision":
                require(
                    answer.get("diary_id") == payload["diary_id"]
                    and answer.get("revision_id") == payload["revision_id"]
                    and answer.get("diary_version") == payload["expected_diary_version"]
                    and isinstance(answer.get("content"), str),
                    "invalid_upstream",
                    502,
                )

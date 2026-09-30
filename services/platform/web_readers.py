"""Narrow same-origin read windows over registered Memory and Companion services.

The browser chooses an operation and a bounded, allowlisted subject. It never chooses a URL,
credential, reader identity or upstream operation. The peers retain their own authorization.
"""

import asyncio
import hmac
import re
import secrets
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
    "knowledge": frozenset(
        {
            "query",
            "documents",
            "document",
            "notes",
            "lessons",
            "experiences",
            "continuation",
            "continuation-check",
        }
    ),
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
            allowed |= {"entry_id", "runtime_roles"}
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
                    and set(item) in ({"project_id", "label"}, {"project_id", "label", "checkouts"})
                    and isinstance(item["project_id"], str)
                    and PROJECT_ID.fullmatch(item["project_id"]) is not None
                    and isinstance(item["label"], str)
                    and 1 <= len(item["label"]) <= 128,
                    "invalid_input",
                    400,
                )
                ids.append(item["project_id"])
                checkouts = item.get("checkouts", [])
                require(isinstance(checkouts, list) and len(checkouts) <= 32, "invalid_input", 400)
                checkout_ids = []
                for checkout in checkouts:
                    require(
                        isinstance(checkout, dict)
                        and set(checkout) == {"id", "label"}
                        and isinstance(checkout["id"], str)
                        and IDENTIFIER.fullmatch(checkout["id"]) is not None
                        and isinstance(checkout["label"], str)
                        and 1 <= len(checkout["label"]) <= 128,
                        "invalid_input",
                        400,
                    )
                    checkout_ids.append(checkout["id"])
                require(len(checkout_ids) == len(set(checkout_ids)), "invalid_input", 400)
            require(len(set(ids)) == len(ids), "invalid_input", 400)
        if name == "memory":
            require(
                isinstance(config.get("entry_id"), str)
                and IDENTIFIER.fullmatch(config["entry_id"]) is not None,
                "invalid_input",
                400,
            )
            require(type(config.get("runtime_roles", False)) is bool, "invalid_input", 400)


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
        self.packages = {}

    def forget_session(self, session):
        self.packages = {
            handle: item for handle, item in self.packages.items() if item["owner"] is not session
        }

    def _prune_packages(self):
        now = self.console.clock()
        self.packages = {
            handle: item
            for handle, item in self.packages.items()
            if item["expires"] > now and self.console.session_valid(item["owner"])
        }

    def _package(self, session, project_id, handle):
        require(isinstance(handle, str) and 1 <= len(handle) <= 128, "invalid_input", 400)
        self._prune_packages()
        item = self.packages.get(handle)
        require(
            item is not None and item["owner"] is session and item["project_id"] == project_id,
            "continuation_handle_expired",
            409,
        )
        return item["package"]

    def _hold_package(self, session, project_id, checkout_id, package):
        from .contracts import canonical

        self._prune_packages()
        owned = sum(item["owner"] is session for item in self.packages.values())
        require(owned < 4 and len(self.packages) < 16, "too_many_requests", 429)
        size = len(canonical(package).encode())
        require(
            size <= RESPONSE_LIMIT
            and sum(item["bytes"] for item in self.packages.values()) + size <= 4 * 1024 * 1024,
            "budget_exceeded",
            413,
        )
        handle = secrets.token_urlsafe(32)
        self.packages[handle] = {
            "owner": session,
            "project_id": project_id,
            "checkout_id": checkout_id,
            "package": package,
            "bytes": size,
            "expires": self.console.clock() + 900,
        }
        return handle

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
                [
                    {**project, "checkouts": project.get("checkouts", [])}
                    for project in self.config["projects"]
                ]
                if self.config and self._authorized()
                else []
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
        payload = self._request(name, body, session)
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
            if name == "continuation":
                handle = self._hold_package(
                    session, body["project_id"], body["checkout_id"], result
                )
                return {
                    "project_id": body["project_id"],
                    "operation": name,
                    "result": self._continuation_view(result, handle),
                }
            if name == "continuation-check":
                return {
                    "project_id": body["project_id"],
                    "operation": name,
                    "result": {
                        "valid": result["valid"],
                        "reason": result["reason"],
                        "differences": result["differences"],
                        "observed": result["observed"],
                        "checkout_id": result["worktree"]["id"],
                        "checked_at": result.get("checked_at"),
                    },
                }
            return {"project_id": body["project_id"], "operation": name, "result": result}
        return result

    def _request(self, name, body, session):
        if self.name == "knowledge":
            return self._knowledge(name, body, session)
        return self._life(name, body)

    def _knowledge(self, name, body, session):
        shapes = {
            "query": {"project_id", "text", "budget_bytes"},
            "notes": {"project_id", "text", "budget_bytes"},
            "lessons": {"project_id", "text", "budget_bytes"},
            "experiences": {"project_id", "text", "budget_bytes"},
            "continuation": {"project_id", "checkout_id", "text", "budget_bytes"},
            "continuation-check": {"project_id", "handle"},
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
            "lessons": "lesson_query",
            "experiences": "experience_query",
            "continuation": "continuation_recover",
            "continuation-check": "continuation_check",
        }.get(name, name)
        if name in {"query", "notes", "lessons", "experiences", "continuation"}:
            budget = _budget(body["budget_bytes"])
            if name == "continuation":
                require(budget >= 4096, "invalid_input", 400)
            arguments = {
                "text": _text(body["text"], 1024),
                "budget_bytes": budget,
            }
            if name == "experiences":
                arguments["project_id"] = project_id
            if name == "continuation":
                checkout_id = _text(body["checkout_id"], 128)
                project = next(p for p in self.config["projects"] if p["project_id"] == project_id)
                require(
                    checkout_id in {entry["id"] for entry in project.get("checkouts", [])},
                    "checkout_not_allowed",
                    403,
                )
                arguments["worktree"] = checkout_id
        elif name == "continuation-check":
            arguments = {"package": self._package(session, project_id, body["handle"])}
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

    @staticmethod
    def _continuation_view(package, handle):
        tree = package["worktree"]
        state = package.get("state")
        safe_state = None
        if isinstance(state, dict):
            safe_state = {
                "version": state.get("version"),
                "current": state.get("current"),
                "stale_evidence": state.get("stale_evidence"),
                "goal": state.get("goal"),
                "constraints": state.get("constraints"),
                "unfinished": state.get("unfinished"),
            }
        return {
            "handle": handle,
            "expires_in": 900,
            "status": package["status"],
            "checkout": {
                "id": tree["id"],
                "branch": tree["branch"],
                "head": tree["head"],
                "dirty": tree["dirty"],
                "collected_at": tree["collected_at"],
            },
            "index": {key: package["index"][key] for key in ("total", "listed", "truncated")},
            "state": safe_state,
            "omissions": package["omissions"],
            "budget": {
                key: package["budget"][key] for key in ("limit_bytes", "used_bytes", "over_budget")
            },
            "revision": package["revision"],
        }

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
        if status == 415 and code == "unsupported":
            raise Fault("knowledge_operation_not_enabled", 503)
        if status in {404, 422} and code == "not_found":
            raise Fault("not_found", 404)
        if code in {"version_conflict", "stale_evidence", "cursor_stale", "invalid_cursor"}:
            raise Fault(code, 409)
        if status in {400, 422} and code in {"invalid_input", "budget_too_small"}:
            raise Fault(code, 400)
        raise Fault("dependency_unavailable", 503)

    def _answer(self, name, payload, answer):
        if self.name == "knowledge":
            require(
                ("project_id" not in answer or answer["project_id"] == payload["project_id"])
                if name == "experiences"
                else answer.get("project_id") == payload["project_id"],
                "invalid_upstream",
                502,
            )
            field = {
                "query": "blocks",
                "documents": "items",
                "document": "blocks",
                "notes": "notes",
                "lessons": "lessons",
                "experiences": "entries",
                "continuation": "units",
                "continuation-check": "differences",
            }[name]
            require(isinstance(answer.get(field), list), "invalid_upstream", 502)
            if name == "continuation":
                require(
                    answer.get("status") == "recovered"
                    and isinstance(answer.get("seal"), str)
                    and re.fullmatch(r"[0-9a-f]{64}", answer["seal"]) is not None
                    and isinstance(answer.get("worktree"), dict)
                    and answer["worktree"].get("id") == payload["arguments"]["worktree"]
                    and isinstance(answer.get("index"), dict)
                    and isinstance(answer.get("budget"), dict)
                    and isinstance(answer.get("omissions"), list),
                    "invalid_upstream",
                    502,
                )
            if name == "continuation-check":
                require(
                    type(answer.get("valid")) is bool
                    and isinstance(answer.get("reason"), str)
                    and type(answer.get("observed")) is bool
                    and isinstance(answer.get("worktree"), dict)
                    and answer["worktree"].get("id")
                    == payload["arguments"]["package"]["worktree"]["id"],
                    "invalid_upstream",
                    502,
                )
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

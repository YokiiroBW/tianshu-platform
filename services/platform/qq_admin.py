"""QQ management identity owned by Platform, never inferred from chat text.

The browser is the only writer.  The internal reader resolves a live Platform
origin before looking up a grant, so its request cannot choose an account.
"""

import hashlib
import hmac
import json
import re
import sqlite3
import ssl
import uuid
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from .contracts import Fault, canonical, require
from .auth import secret
from .role_runtime import is_placeholder_actor

WEB_PREFIX = "/api/web/qq-admin/"
CHECK_PATH = "/internal/v1/qq-admin/check"
CAPABILITIES = frozenset({"identity.explain"})


def validate_profiles_configuration(settings, other_credentials):
    used = set(other_credentials)
    for key in ("web_qq_profiles", "qq_alias_memory"):
        c = settings.get(key)
        if c is None:
            continue
        require(
            type(c) is dict
            and set(c) in ({"base_url", "token_env"}, {"base_url", "token_env", "ca_file"}),
            "invalid_input",
            400,
        )
        require(type(c["base_url"]) is str, "invalid_input", 400)
        url = urlsplit(c["base_url"])
        require(
            url.scheme == "https"
            and url.hostname
            and not (url.username or url.password or url.query or url.fragment),
            "invalid_input",
            400,
        )
        require(
            type(c["token_env"]) is str and re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", c["token_env"]),
            "invalid_input",
            400,
        )
        require(c["token_env"] not in used, "invalid_input", 400)
        current = secret(c["token_env"])
        if current:
            require(
                all(
                    not hmac.compare_digest(current, other)
                    for name in used
                    if (other := secret(name)) is not None
                ),
                "invalid_input",
                400,
            )
        used.add(c["token_env"])
        if c.get("ca_file") is not None:
            require(
                type(c["ca_file"]) is str
                and Path(c["ca_file"]).is_absolute()
                and Path(c["ca_file"]).is_file(),
                "invalid_input",
                400,
            )


async def observe_alias(platform, body):
    c = platform.settings.get("qq_alias_memory")
    if c is None or body["schema_version"] != 2 or not (body["nickname"] or body["group_card"]):
        return False
    token = secret(c["token_env"])
    require(token is not None, "dependency_unavailable", 503)
    request_id = "qq-alias:" + uuid.uuid4().hex
    payload = {
        "schema_version": 1,
        "request_id": request_id,
        "account_id": body["account_id"],
        "bot_id": body["self_id"],
        "conversation_id": body["conversation_id"],
        "nickname": body["nickname"],
        "group_card": body["group_card"],
        "event_ref": "bot:"
        + hashlib.sha256(
            canonical([body["connection_id"], body["event_id"], body["account_id"]]).encode()
        ).hexdigest(),
        "observed_at": body["sent_at"],
    }
    try:
        context = ssl.create_default_context(cafile=c.get("ca_file"))
        async with aiohttp.ClientSession(trust_env=False) as client:
            async with client.post(
                c["base_url"].rstrip("/") + "/internal/v1/identity/qq-alias",
                json=payload,
                headers={"Authorization": "Bearer " + token, "Accept-Encoding": "identity"},
                ssl=context,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                raw = await response.content.read(4097)
                require(
                    len(raw) <= 4096
                    and response.headers.get("Content-Encoding", "identity").lower() == "identity",
                    "dependency_unavailable",
                    503,
                )
                answer = json.loads(raw)
                require(
                    response.status == 200
                    and type(answer) is dict
                    and answer.get("request_id") == request_id,
                    "dependency_unavailable",
                    503,
                )
                return True
    except (aiohttp.ClientError, OSError, ValueError, TimeoutError):
        raise Fault("dependency_unavailable", 503) from None


def qq_id(value):
    """One canonical, positive ASCII QQ identifier; bool is never an integer ID."""
    if type(value) is int:
        require(value > 0, "invalid_input", 400)
        return str(value)
    require(
        type(value) is str and re.fullmatch(r"[1-9][0-9]*", value) is not None,
        "invalid_input",
        400,
    )
    return value


def _scope_list(value, prefix):
    require(type(value) is list and len(value) <= 64, "invalid_input", 400)
    require(
        all(type(v) is str and v.startswith(prefix) and len(prefix) < len(v) <= 128 for v in value)
        and len(set(value)) == len(value),
        "invalid_input",
        400,
    )
    return sorted(value)


class QQAdmin:
    def __init__(self, platform):
        self.p = platform
        self.profiles_config = platform.settings.get("web_qq_profiles")
        self.path = str(platform.settings["database_path"]) + ".qq-admin.sqlite"
        with closing(self._db()) as db, db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            require(version in (0, 1), "dependency_unavailable", 503)
            db.execute(
                "CREATE TABLE IF NOT EXISTS head (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL)"
            )
            db.execute("INSERT OR IGNORE INTO head VALUES (1,0)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS grants (qq_id TEXT PRIMARY KEY, body TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS audit (version INTEGER PRIMARY KEY, account_hash TEXT NOT NULL, operation TEXT NOT NULL, principal_hash TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS alias_pending (event_ref TEXT PRIMARY KEY, semantic TEXT NOT NULL, body TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS alias_progress (id INTEGER PRIMARY KEY CHECK(id=1), last_ref TEXT NOT NULL)"
            )
            db.execute("INSERT OR IGNORE INTO alias_progress VALUES (1,'')")
            db.execute("PRAGMA user_version=1")

    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def check_access(self, console, session, action):
        require(console.session_valid(session), "session_expired", 401)
        principal = self.p.auth.principals[console.config["principal"]]
        require(
            principal["kind"] == "operator" and action in principal["actions"], "forbidden", 403
        )

    @staticmethod
    def _version(db):
        return db.execute("SELECT version FROM head WHERE id=1").fetchone()[0]

    def _view(self):
        roles = {
            item["id"]: item["label"]
            for item in (self.p.settings.get("bot_adapter_self_service") or {}).get("actors", [])
            if type(item) is dict
            and type(item.get("id")) is str
            and type(item.get("label")) is str
            and not is_placeholder_actor(item["id"], self.p.role_runtime.get(item["id"]))
        }
        roles.update({item["id"]: item["label"] for item in self.p.role_runtime.active_actors()})
        with closing(self._db()) as db:
            return {
                "schema_version": 1,
                "version": self._version(db),
                "grants": [
                    json.loads(row[0])
                    for row in db.execute("SELECT body FROM grants ORDER BY qq_id")
                ],
                "capabilities": sorted(CAPABILITIES),
                "roles": [{"id": actor, "label": label} for actor, label in sorted(roles.items())],
                "alias_pending": db.execute("SELECT count(*) FROM alias_pending").fetchone()[0],
            }

    def queue_alias(self, body):
        if self.p.settings.get("qq_alias_memory") is None or body["schema_version"] != 2:
            return
        key = hashlib.sha256(
            canonical([body["connection_id"], body["event_id"], body["account_id"]]).encode()
        ).hexdigest()
        projected = {
            key: body[key]
            for key in (
                "schema_version",
                "connection_id",
                "event_id",
                "account_id",
                "self_id",
                "conversation_id",
                "nickname",
                "group_card",
                "sent_at",
            )
        }
        semantic = hashlib.sha256(canonical(projected).encode()).hexdigest()
        with closing(self._db()) as db, db:
            prior = db.execute(
                "SELECT semantic FROM alias_pending WHERE event_ref=?", (key,)
            ).fetchone()
            require(prior is None or prior["semantic"] == semantic, "idempotency_conflict", 409)
            db.execute(
                "INSERT OR IGNORE INTO alias_pending VALUES (?,?,?)",
                (key, semantic, canonical(projected)),
            )

    def pending_aliases(self):
        with closing(self._db()) as db, db:
            cursor = db.execute("SELECT last_ref FROM alias_progress WHERE id=1").fetchone()[0]
            rows = db.execute(
                "SELECT event_ref,body FROM alias_pending WHERE event_ref>? ORDER BY event_ref LIMIT 32",
                (cursor,),
            ).fetchall()
            if not rows:
                rows = db.execute(
                    "SELECT event_ref,body FROM alias_pending ORDER BY event_ref LIMIT 32"
                ).fetchall()
            if rows:
                db.execute(
                    "UPDATE alias_progress SET last_ref=? WHERE id=1", (rows[-1]["event_ref"],)
                )
            return [json.loads(row["body"]) for row in rows]

    def finish_alias(self, body):
        key = hashlib.sha256(
            canonical([body["connection_id"], body["event_id"], body["account_id"]]).encode()
        ).hexdigest()
        with closing(self._db()) as db, db:
            db.execute("DELETE FROM alias_pending WHERE event_ref=?", (key,))

    async def flush_aliases(self):
        for body in await self.p.local_work.run(self.pending_aliases):
            try:
                if await observe_alias(self.p, body):
                    await self.p.local_work.run(self.finish_alias, body)
            except Fault:
                continue

    def _write(self, operation, body, principal):
        require(type(body) is dict, "invalid_input", 400)
        expected = {"qq_id", "expected_version"}
        if operation == "grant":
            expected |= {"note", "actor_ids", "conversations", "capabilities"}
        require(set(body) == expected, "invalid_input", 400)
        account = qq_id(body["qq_id"])
        require(
            type(body["expected_version"]) is int and 0 <= body["expected_version"] < 2**63 - 1,
            "invalid_input",
            400,
        )
        if operation == "grant":
            note = body["note"]
            require(
                type(note) is str and len(note) <= 80 and note.isprintable(), "invalid_input", 400
            )
            actors = _scope_list(body["actor_ids"], "actor:")
            conversations = _scope_list(body["conversations"], "")
            for value in conversations:
                parts = value.split(":", 1)
                require(len(parts) == 2 and parts[0] in {"group", "private"}, "invalid_input", 400)
                qq_id(parts[1])
                require(parts[0] != "private" or parts[1] == account, "invalid_input", 400)
            caps = body["capabilities"]
            require(
                type(caps) is list and all(type(cap) is str for cap in caps),
                "invalid_input",
                400,
            )
            require(len(caps) == len(set(caps)) and set(caps) <= CAPABILITIES, "invalid_input", 400)
            grant = {
                "qq_id": account,
                "note": note,
                "actor_ids": actors,
                "conversations": conversations,
                "capabilities": sorted(caps),
            }
        with closing(self._db()) as db:
            db.execute("BEGIN IMMEDIATE")
            version = self._version(db)
            require(version == body["expected_version"], "version_conflict", 409)
            if operation == "grant":
                db.execute(
                    "INSERT INTO grants VALUES (?,?) ON CONFLICT(qq_id) DO UPDATE SET body=excluded.body",
                    (account, canonical(grant)),
                )
            else:
                db.execute("DELETE FROM grants WHERE qq_id=?", (account,))
            db.execute("UPDATE head SET version=version+1 WHERE id=1")
            db.execute(
                "INSERT INTO audit VALUES (?,?,?,?)",
                (
                    version + 1,
                    hashlib.sha256(account.encode()).hexdigest(),
                    operation,
                    hashlib.sha256(principal.encode()).hexdigest(),
                ),
            )
            db.commit()
        return self._view()

    async def route(self, console, path, body, session):
        operation = path.removeprefix(WEB_PREFIX)
        require(operation in {"view", "profiles", "grant", "revoke"}, "not_found", 404)
        action = "qq.admin.view" if operation in {"view", "profiles"} else "qq.admin.manage"
        self.check_access(console, session, action)
        if operation == "view":
            require(body == {}, "invalid_input", 400)
            answer = await self.p.local_work.run(self._view)
        elif operation == "profiles":
            answer = await self._profiles(body)
        else:
            answer = await self.p.local_work.run(
                self._write, operation, body, console.config["principal"]
            )
        self.check_access(console, session, action)
        return answer

    async def _profiles(self, body):
        c = self.profiles_config
        require(c is not None, "dependency_unavailable", 503)
        require(type(body) is dict and set(body) == {"limit", "after"}, "invalid_input", 400)
        require(type(body["limit"]) is int and 1 <= body["limit"] <= 100, "invalid_input", 400)
        require(
            body["after"] is None or qq_id(body["after"]) == body["after"], "invalid_input", 400
        )
        token = secret(c["token_env"])
        require(token is not None, "dependency_unavailable", 503)
        request_id = "qq-profile:" + uuid.uuid4().hex
        try:
            context = ssl.create_default_context(cafile=c.get("ca_file"))
            async with aiohttp.ClientSession(trust_env=False) as client:
                async with client.post(
                    c["base_url"].rstrip("/") + "/internal/v1/identity/qq-profiles",
                    json={"schema_version": 1, "request_id": request_id, **body},
                    headers={"Authorization": "Bearer " + token, "Accept-Encoding": "identity"},
                    ssl=context,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as response:
                    raw = await response.content.read(65537)
                    require(
                        len(raw) <= 65536
                        and response.headers.get("Content-Encoding", "identity").lower()
                        == "identity",
                        "dependency_unavailable",
                        503,
                    )
                    answer = json.loads(raw)
                    require(
                        response.status == 200
                        and type(answer) is dict
                        and answer.get("request_id") == request_id
                        and answer.get("schema_version") == 1,
                        "dependency_unavailable",
                        503,
                    )
                    require(
                        type(answer.get("items")) is list
                        and (
                            answer.get("next_cursor") is None or type(answer["next_cursor"]) is str
                        ),
                        "dependency_unavailable",
                        503,
                    )
                    for item in answer["items"]:
                        require(
                            type(item) is dict
                            and set(item) == {"qq_id", "person_id", "display_name", "aliases"},
                            "dependency_unavailable",
                            503,
                        )
                        try:
                            qq_id(item["qq_id"])
                        except Fault:
                            raise Fault("dependency_unavailable", 503) from None
                        require(
                            type(item["person_id"]) is str
                            and type(item["display_name"]) is str
                            and type(item["aliases"]) is list,
                            "dependency_unavailable",
                            503,
                        )
                    return {"items": answer["items"], "next_cursor": answer["next_cursor"]}
        except (aiohttp.ClientError, OSError, ValueError, TimeoutError):
            raise Fault("dependency_unavailable", 503) from None

    def check(self, header, body):
        require(
            type(body) is dict
            and set(body)
            == {"schema_version", "request_id", "assertion_ref", "actor_id", "conversation_id"},
            "invalid_input",
            400,
        )
        require(
            body["schema_version"] == 1
            and type(body["request_id"]) is str
            and 1 <= len(body["request_id"]) <= 128,
            "invalid_input",
            400,
        )
        require(
            type(body["assertion_ref"]) is str
            and type(body["actor_id"]) is str
            and type(body["conversation_id"]) is str,
            "invalid_input",
            400,
        )
        with self.p.store.connect() as authority:
            _, principal = self.p.auth.authenticate(header, authority, "qq.admin.check")
            require(
                principal["kind"] == "service" and principal["service"] == "companion",
                "forbidden",
                403,
            )
            _, _, context = self.p.origins.context(
                authority, body["assertion_ref"], "companion", "platform", "dialogue"
            )
            scope = context["allowed_scope"]
            require(
                scope["actor_id"] == body["actor_id"]
                and scope["conversation_id"] == body["conversation_id"],
                "forbidden",
                403,
            )
            account = context["verified_account"]
            require(account["namespace"] == "qq", "forbidden", 403)
            account_id = qq_id(account["immutable_account_id"])
            channel = context["verified_channel"]
            require(channel["namespace"] == "qq", "forbidden", 403)
        with closing(self._db()) as db:
            version = self._version(db)
            row = db.execute("SELECT body FROM grants WHERE qq_id=?", (account_id,)).fetchone()
            grant = json.loads(row[0]) if row else None
        allowed = bool(
            grant
            and "identity.explain" in grant["capabilities"]
            and (not grant["actor_ids"] or body["actor_id"] in grant["actor_ids"])
            and (
                not grant["conversations"]
                or channel["channel_conversation_id"] in grant["conversations"]
            )
        )
        return {
            "schema_version": 1,
            "request_id": body["request_id"],
            "version": version,
            "is_admin": allowed,
            "capabilities": grant["capabilities"] if allowed else [],
        }

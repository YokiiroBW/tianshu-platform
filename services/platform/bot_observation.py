"""Account-scoped bot observation authority, separate from dialogue dispatch."""

import asyncio
import hashlib
import hmac
import re
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime

from .auth import secret
from .contracts import Fault, digest, require, utc
from .transport import core_settings

PREFIX = "/api/web/bot-observation/"
ID = re.compile(r"^[1-9][0-9]{0,19}$")
MODES = {"observe_only", "whitelist", "blacklist"}
MAX_SOURCES = 100000


def _qq(value):
    require(isinstance(value, str) and ID.fullmatch(value) is not None, "invalid_input", 400)
    return value


def _policy(value):
    require(
        isinstance(value, dict) and set(value) == {"observe", "mode", "list", "actor_id"},
        "invalid_input",
        400,
    )
    require(type(value["observe"]) is bool and value["mode"] in MODES, "invalid_input", 400)
    names = value["list"]
    require(
        isinstance(names, list) and len(names) <= 256 and len(set(names)) == len(names),
        "invalid_input",
        400,
    )
    for item in names:
        _qq(item)
    require(
        value["actor_id"] is None
        or isinstance(value["actor_id"], str)
        and (value["actor_id"] is None or 1 <= len(value["actor_id"]) <= 128),
        "invalid_input",
        400,
    )
    return value


def _default():
    return {"observe": True, "mode": "observe_only", "list": [], "actor_id": None}


def decision(policy, target, mentioned, kind):
    """Pure policy: observation, reply permission, and trigger stay distinct."""
    observed = policy["observe"]
    mode = policy["mode"]
    permitted = (mode == "whitelist" and target in policy["list"]) or (
        mode == "blacklist" and target not in policy["list"]
    )
    return {
        "observe": observed,
        "reply_permitted": bool(permitted),
        "reply_triggered": bool(permitted and (kind == "private" or mentioned)),
    }


class BotObservation:
    def __init__(self, platform):
        self.p = platform
        self.adapters = platform.bot_adapters
        self.catalog = self.adapters.catalog
        self.lock = asyncio.Lock()
        self.path = platform.store.path + ".bot-observations.sqlite"
        if self.catalog:
            with closing(self._db()) as db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS sources (
                        source_ref TEXT PRIMARY KEY, connection_id TEXT NOT NULL,
                        instance_id TEXT NOT NULL, self_id TEXT NOT NULL,
                        conversation TEXT NOT NULL, author TEXT NOT NULL,
                        event_id TEXT NOT NULL, digest TEXT NOT NULL,
                        epoch INTEGER NOT NULL, archive_epoch INTEGER NOT NULL,
                        observed_at REAL NOT NULL,
                        UNIQUE(instance_id,self_id,conversation,author,event_id)
                    );
                    CREATE INDEX IF NOT EXISTS sources_page ON sources(connection_id,conversation,observed_at,source_ref);
                    CREATE TABLE IF NOT EXISTS discovered (
                        connection_id TEXT NOT NULL, conversation TEXT NOT NULL,
                        author TEXT NOT NULL, archive_epoch INTEGER NOT NULL,
                        count INTEGER NOT NULL,
                        last_at REAL NOT NULL,
                        PRIMARY KEY(connection_id,conversation,author,archive_epoch)
                    );
                    CREATE INDEX IF NOT EXISTS discovered_page
                        ON discovered(connection_id,archive_epoch,conversation,author);
                    CREATE TABLE IF NOT EXISTS observation_versions (
                        observation_id TEXT NOT NULL, revision INTEGER NOT NULL,
                        group_observe INTEGER NOT NULL, private_observe INTEGER NOT NULL,
                        archive_epoch INTEGER NOT NULL,
                        PRIMARY KEY(observation_id,revision)
                    );
                    CREATE TABLE IF NOT EXISTS reply_connections (
                        connection_id TEXT PRIMARY KEY, observation_id TEXT NOT NULL,
                        conversation TEXT NOT NULL, actor_id TEXT NOT NULL,
                        UNIQUE(observation_id,conversation,actor_id)
                    );
                    CREATE TABLE IF NOT EXISTS reply_authors (
                        connection_id TEXT NOT NULL, author TEXT NOT NULL,
                        PRIMARY KEY(connection_id,author)
                    );
                """)
            with closing(self._db()) as db:
                for reply in db.execute("SELECT * FROM reply_connections"):
                    authors = [
                        r[0]
                        for r in db.execute(
                            "SELECT author FROM reply_authors WHERE connection_id=? ORDER BY author",
                            (reply["connection_id"],),
                        )
                    ]
                    self._install_reply(reply, authors)

    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def _reply_token(self, connection_id):
        return hmac.new(self.catalog.key, connection_id.encode(), hashlib.sha256).hexdigest()

    def _install_reply(self, reply, authors):
        row = self.get(reply["observation_id"])
        require(row is not None, "dependency_unavailable", 503)
        conn = reply["connection_id"]
        slot_id = "observation:" + conn
        binding_id = "binding:" + conn
        kind = reply["conversation"].split(":", 1)[0]
        channel = {
            "namespace": "qq",
            "binding_id": binding_id,
            "channel_conversation_id": reply["conversation"],
            "thread_id": None,
        }
        entry_ids = []
        for author in authors:
            entry_id = conn + ":author:" + author
            actor_entry = entry_id + ":actor"
            account = {"namespace": "qq", "immutable_account_id": author}
            self.p.auth.entries[actor_entry] = {
                "kind": "trusted_application",
                "owner": self.p.bots.config["principal"],
                "account": account,
                "channel": channel,
                "actor_id": reply["actor_id"],
                "audience": "group" if kind == "group" else "self_private",
                "ttl_seconds": 60,
                "routes": [
                    {"caller": "platform", "receiver": "companion", "purpose": "dialogue"},
                    {"caller": "companion", "receiver": "memory", "purpose": "dialogue"},
                    {"caller": "companion", "receiver": "platform", "purpose": "dialogue"},
                ],
            }
            self.p.sources.entries[entry_id] = {
                "owner": self.p.bots.config["principal"],
                "account": account,
                "channel": channel,
                "audience": "group" if kind == "group" else "self_private",
                "ttl_seconds": 60,
                "actor_entries": [actor_entry],
                "default_actor_ids": [reply["actor_id"]],
                "routing_version": 1,
            }
            entry_ids.append(entry_id)
        self.p.bots.slots[slot_id] = {
            "adapter": row["adapter"],
            "platform_id": row["instance_id"],
            "self_id": row["account_id"],
            "input_entry_ids": entry_ids,
            "label": row["name"],
        }

    def reply_connection_active(self, connection_id):
        with closing(self._db()) as db:
            reply = db.execute(
                "SELECT * FROM reply_connections WHERE connection_id=?", (connection_id,)
            ).fetchone()
        if not reply:
            return False
        row = self.get(reply["observation_id"])
        if (
            row is None
            or not row["enabled"]
            or row["pending"]
            or row["state"] not in {"ready", "degraded"}
        ):
            return False
        kind = reply["conversation"].split(":", 1)[0]
        policy = row[kind + "_policy"]
        target = reply["conversation"].split(":", 1)[1]
        expected = self._reply_connection_id(row, reply["conversation"], reply["actor_id"])
        return (
            reply["connection_id"] == expected
            and policy["observe"]
            and decision(policy, target, False, kind)["reply_permitted"]
            and policy["actor_id"] == reply["actor_id"]
        )

    def _reply_connection_id(self, row, conversation, actor_id):
        # A policy revision gets a new delivery scope. Old queued replies cannot
        # become sendable after an allow/deny list or mode change.
        return "bot:" + digest([row["id"], row["host_revision"], conversation, actor_id])[:32]

    async def _ensure_reply_scope(self, row, conversation, author, actor_id):
        conn = self._reply_connection_id(row, conversation, actor_id)
        with closing(self._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            saved = db.execute(
                "SELECT * FROM reply_connections WHERE connection_id=?", (conn,)
            ).fetchone()
            if saved is None:
                db.execute(
                    "INSERT INTO reply_connections VALUES(?,?,?,?)",
                    (conn, row["id"], conversation, actor_id),
                )
            db.execute("INSERT OR IGNORE INTO reply_authors VALUES(?,?)", (conn, author))
            authors = [
                r[0]
                for r in db.execute(
                    "SELECT author FROM reply_authors WHERE connection_id=? ORDER BY author",
                    (conn,),
                )
            ]
            require(len(authors) <= 10000, "queue_full", 429)
        reply = {
            "connection_id": conn,
            "observation_id": row["id"],
            "conversation": conversation,
            "actor_id": actor_id,
        }
        self._install_reply(reply, authors)
        token = self._reply_token(conn)
        with closing(self.p.bots._db()) as db:
            exists = db.execute("SELECT enabled FROM connections WHERE id=?", (conn,)).fetchone()
        if exists is None:
            self.p.bots.create("observation:" + conn, [actor_id], connection_id=conn, token=token)
        current = await self.adapters._core("status", {"connection_id": conn})
        if current.get("found") is not True:
            initial = {
                "request_id": "reply-init:" + conn[4:],
                "connection_id": conn,
                "revision": 1,
                "binding_id": "binding:" + conn,
                "actor_id": actor_id,
                "conversation": {
                    "kind": conversation.split(":", 1)[0],
                    "id": conversation.split(":", 1)[1],
                },
                "enabled": False,
            }
            await self.adapters._core("apply", initial)
            current = await self.adapters._core("status", {"connection_id": conn})
        binding = current.get("binding") or {}
        if binding.get("revision") == 1 and binding.get("enabled") is False:
            enabled = {
                "request_id": "reply-enable:" + conn[4:],
                "connection_id": conn,
                "revision": 2,
                "binding_id": "binding:" + conn,
                "actor_id": actor_id,
                "conversation": {
                    "kind": conversation.split(":", 1)[0],
                    "id": conversation.split(":", 1)[1],
                },
                "enabled": True,
            }
            await self.adapters._core("apply", enabled)
        elif binding.get("revision") != 2 or binding.get("enabled") is not True:
            raise Fault("dependency_unavailable", 503)
        with closing(self.p.bots._db()) as db:
            active = db.execute("SELECT enabled FROM connections WHERE id=?", (conn,)).fetchone()
        if not active or not active[0]:
            self.p.bots.change(conn, "enable")
        return conn, token

    def _disable_invalid_replies(self, row):
        with closing(self._db()) as db:
            replies = db.execute(
                "SELECT * FROM reply_connections WHERE observation_id=?", (row["id"],)
            ).fetchall()
        for reply in replies:
            kind = reply["conversation"].split(":", 1)[0]
            policy = row[kind + "_policy"]
            target = reply["conversation"].split(":", 1)[1]
            if (
                reply["connection_id"]
                != self._reply_connection_id(row, reply["conversation"], reply["actor_id"])
                or not policy["observe"]
                or not decision(policy, target, False, kind)["reply_permitted"]
                or policy["actor_id"] != reply["actor_id"]
            ):
                with closing(self.p.bots._db()) as db:
                    active = db.execute(
                        "SELECT enabled FROM connections WHERE id=?", (reply["connection_id"],)
                    ).fetchone()
                if active and active[0]:
                    self.p.bots.change(reply["connection_id"], "disable")

    def rows(self):
        return (
            [r for r in self.catalog.all() if r.get("kind") == "observation"]
            if self.catalog
            else []
        )

    def get(self, key):
        row = self.catalog.get(key) if self.catalog else None
        return row if row and row.get("kind") == "observation" else None

    def _project(self, row):
        value = {
            key: row[key]
            for key in (
                "id",
                "name",
                "adapter",
                "account_id",
                "instance_id",
                "enabled",
                "revision",
                "state",
                "last_error",
                "last_checked_at",
                "group_policy",
                "private_policy",
                "observation_epoch",
                "archive_epoch",
                "read_enabled",
                "host_revision",
            )
        }
        value["host_pending"] = row.get("host_pending", 0)
        value["host_dropped"] = row.get("host_dropped", 0)
        return value

    def _gate(self, console, session):
        self.adapters._gate(console, session)

    def local_status(self, header):
        with self.p.store.connect(write=True) as db:
            self.p.auth.authenticate(header, db, "bot.manage", operator=True)
        require(self.catalog is not None, "dependency_unavailable", 503)
        return {"connections": [self._project(row) for row in self.rows()]}

    async def local_enroll_default(self, header, body):
        with self.p.store.connect(write=True) as db:
            self.p.auth.authenticate(header, db, "bot.manage", operator=True)
        require(
            isinstance(body, dict)
            and set(body)
            == {
                "adapter",
                "address",
                "access_key",
                "allow_private_http",
                "ca_pem",
                "account_id",
                "name",
            },
            "invalid_input",
            400,
        )
        require(self.catalog is not None, "dependency_unavailable", 503)
        async with self.lock:
            session = {}
            probe = await self.adapters.probe(
                {
                    key: body[key]
                    for key in ("adapter", "address", "access_key", "allow_private_http", "ca_pem")
                },
                session,
            )
            account = _qq(body["account_id"])
            require(any(item["id"] == account for item in probe["accounts"]), "invalid_input", 400)
            existing = [
                row
                for row in self.rows()
                if row["instance_id"] == probe["instance_id"] and row["account_id"] == account
            ]
            if existing:
                require(len(existing) == 1, "scope_changed", 409)
                return {"connection": self._project(existing[0]), "created": False}
            row = await self.create(
                {
                    "draft_id": probe["draft_id"],
                    "account_id": account,
                    "name": body["name"],
                    "client_id": "local-observation:" + uuid.uuid4().hex,
                },
                session,
            )
            return {"connection": self._project(row), "created": True}

    async def route(self, console, path, body, session):
        require(isinstance(body, dict), "invalid_input", 400)
        if path == PREFIX + "view":
            code = self.p.bots._web_code(console, session)
            available = self.catalog is not None and code != "operator_not_authorized"
            return {
                "available": available,
                "unlocked": available and code == "ready",
                "connections": [self._project(row) for row in self.rows()] if available else [],
            }
        self._gate(console, session)
        if path == PREFIX + "create":
            require(
                set(body) == {"draft_id", "account_id", "name", "client_id"}, "invalid_input", 400
            )
            async with self.lock:
                return {"connection": self._project(await self.create(body, session))}
        if path == PREFIX + "policy":
            require(
                set(body)
                == {"id", "expected_revision", "group_policy", "private_policy", "client_id"},
                "invalid_input",
                400,
            )
            async with self.lock:
                return {"connection": self._project(await self.policy(body))}
        if path == PREFIX + "discovered":
            require(set(body) == {"id", "limit", "cursor"}, "invalid_input", 400)
            return await self.discovered(body)
        if path == PREFIX + "history":
            require(
                set(body) == {"id", "expected_revision", "read_enabled", "client_id"},
                "invalid_input",
                400,
            )
            async with self.lock:
                return {"connection": self._project(await self.history(body))}
        if path == PREFIX + "archive":
            require(set(body) == {"id", "conversation_id", "limit", "cursor"}, "invalid_input", 400)
            return await self.archive(body)
        raise Fault("not_found", 404)

    async def _plugin(self, row, operation, request):
        return await self.adapters._plugin(row, "/observation/" + operation, request)

    async def _companion(self, payload):
        settings = self.p.settings["core"]
        core_settings(settings)
        token = secret(settings["token_env"])
        require(token is not None, "dependency_unavailable", 503)
        return await self.adapters._call(
            settings["base_url"],
            token,
            None,
            None,
            "/internal/v2/observations/ingest",
            payload,
            core=True,
            ca_file=settings.get("ca_file"),
        )

    async def _capabilities(self, row):
        result = await self._plugin(row, "capabilities", {})
        require(
            result.get("protocol") == "tianshu.bot-observation/v2"
            and result.get("instance_id") == row["instance_id"]
            and result.get("adapter") == row["adapter"]
            and any(item.get("id") == row["account_id"] for item in result.get("accounts", [])),
            "adapter_incompatible",
            502,
        )

    async def create(self, body, session):
        key = body["client_id"]
        fingerprint = digest(["observation-create", body])
        receipt = self.catalog.receipt(key, fingerprint)
        if receipt:
            return self.get(receipt["id"])
        require(len(self.rows()) < 64, "queue_full", 429)
        draft = self.adapters.drafts.get(body["draft_id"])
        require(
            draft and draft[0] == self.adapters._draft_owner(session) and draft[1] > time.time(),
            "draft_expired",
            409,
        )
        value = self.catalog._open(draft[2], "draft:" + body["draft_id"])
        account_id = _qq(body["account_id"])
        require(any(a["id"] == account_id for a in value["accounts"]), "invalid_input", 400)
        require(
            not any(
                existing.get("kind") != "observation"
                and existing.get("instance_id") == value["instance_id"]
                and existing.get("account_id") == account_id
                for existing in self.catalog.all()
            ),
            "scope_changed",
            409,
        )
        name = body["name"]
        require(
            isinstance(name, str) and 1 <= len(name) <= 64 and name.isprintable(),
            "invalid_input",
            400,
        )
        row = {
            k: value[k]
            for k in (
                "adapter",
                "address",
                "access_key",
                "allow_private_http",
                "ca_pem",
                "pins",
                "instance_id",
            )
        }
        row.update(
            kind="observation",
            id="obs:" + uuid.uuid4().hex,
            name=name,
            account_id=account_id,
            enabled=False,
            revision=1,
            host_revision=1,
            observation_epoch=1,
            archive_epoch=1,
            read_enabled=True,
            state="unknown",
            last_error=None,
            last_checked_at=None,
            host_pending=0,
            host_dropped=0,
            pending={"request_id": key, "enabled": True},
            group_policy=_default(),
            private_policy=_default(),
        )
        await self._capabilities(row)
        self.catalog.begin(row, key, fingerprint)
        self.adapters.drafts.pop(body["draft_id"], None)
        return await self._apply(row)

    async def policy(self, body):
        key = body["client_id"]
        fingerprint = digest(["observation-policy", body])
        receipt = self.catalog.receipt(key, fingerprint)
        if receipt:
            return self.get(receipt["id"])
        row = self.get(body["id"])
        require(row is not None, "not_found", 404)
        require(
            type(body["expected_revision"]) is int and body["expected_revision"] == row["revision"],
            "version_conflict",
            409,
        )
        group, private = _policy(body["group_policy"]), _policy(body["private_policy"])
        actors = {
            a["id"] for a in self.adapters.config["actors"] + self.p.role_runtime.active_actors()
        }
        for policy in (group, private):
            require(policy["actor_id"] is None or policy["actor_id"] in actors, "forbidden", 403)
            require(
                policy["mode"] == "observe_only" or policy["actor_id"] is not None,
                "invalid_input",
                400,
            )
        old_observe = (row["group_policy"]["observe"], row["private_policy"]["observe"])
        new_observe = (group["observe"], private["observe"])
        if old_observe != new_observe:
            row["observation_epoch"] += 1
        row["group_policy"], row["private_policy"] = group, private
        row["revision"] += 1
        row["host_revision"] += 1
        row["state"] = "unknown"
        row["pending"] = {"request_id": key, "enabled": row["read_enabled"] and any(new_observe)}
        self.catalog.begin(row, key, fingerprint)
        self._disable_invalid_replies(row)
        return await self._apply(row)

    async def history(self, body):
        fingerprint = digest(["observation-history", body])
        receipt = self.catalog.receipt(body["client_id"], fingerprint)
        if receipt:
            return self.get(receipt["id"])
        row = self.get(body["id"])
        require(row is not None, "not_found", 404)
        require(
            type(body["expected_revision"]) is int
            and body["expected_revision"] == row["revision"]
            and type(body["read_enabled"]) is bool,
            "version_conflict",
            409,
        )
        if row["read_enabled"] == body["read_enabled"]:
            raise Fault("version_conflict", 409)
        if not body["read_enabled"]:
            row["archive_epoch"] += 1
        row["read_enabled"] = body["read_enabled"]
        row["revision"] += 1
        row["host_revision"] += 1
        row["state"] = "unknown"
        row["pending"] = {
            "request_id": body["client_id"],
            "enabled": body["read_enabled"]
            and any(
                (
                    row["group_policy"]["observe"],
                    row["private_policy"]["observe"],
                )
            ),
        }
        self.catalog.begin(row, body["client_id"], fingerprint)
        self._disable_invalid_replies(row)
        return await self._apply(row)

    async def _apply(self, row):
        # Prepared before the host changes revision, so a crash cannot orphan
        # an event that the host durably accepted under the new policy.
        with closing(self._db()) as db, db:
            db.execute(
                "INSERT OR IGNORE INTO observation_versions VALUES(?,?,?,?,?)",
                (
                    row["id"],
                    row["host_revision"],
                    int(row["group_policy"]["observe"]),
                    int(row["private_policy"]["observe"]),
                    row["archive_epoch"],
                ),
            )
        request = {
            "request_id": row["pending"]["request_id"],
            "account_id": row["account_id"],
            "revision": row["host_revision"],
            "enabled": row["pending"]["enabled"],
            "group_policy": {k: row["group_policy"][k] for k in ("observe", "mode", "list")},
            "private_policy": {k: row["private_policy"][k] for k in ("observe", "mode", "list")},
        }
        try:
            await self._capabilities(row)
            result = await self._plugin(row, "apply", request)
            require(
                result
                == {
                    k: request[k]
                    for k in ("account_id", "revision", "enabled", "group_policy", "private_policy")
                },
                "adapter_incompatible",
                502,
            )
            row["enabled"] = request["enabled"]
            row["state"] = "ready" if row["enabled"] else "disabled"
            row["pending"] = None
            row["last_error"] = None
            row["last_checked_at"] = utc(time.time())
        except Fault as exc:
            row["last_error"] = exc.code
        self.catalog.put(row)
        return row

    def record(self, row, event):
        require(
            event.get("schema_version") == 2
            and event.get("platform_id") == row["instance_id"]
            and event.get("self_id") == row["account_id"]
            and event.get("namespace") == "qq"
            and type(event.get("scope_revision")) is int
            and 1 <= event["scope_revision"] <= row["host_revision"],
            "adapter_incompatible",
            502,
        )
        conversation = event.get("conversation_id", "")
        require(
            re.fullmatch(r"(group|private):[1-9][0-9]*", conversation) is not None,
            "adapter_incompatible",
            502,
        )
        kind = conversation.split(":", 1)[0]
        with closing(self._db()) as db:
            version = db.execute(
                "SELECT * FROM observation_versions WHERE observation_id=? AND revision=?",
                (row["id"], event["scope_revision"]),
            ).fetchone()
        require(version is not None and version[kind + "_observe"], "scope_changed", 409)
        _qq(event.get("account_id"))
        require(
            event["account_id"] != row["account_id"]
            and isinstance(event.get("event_id"), str)
            and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", event["event_id"])
            and isinstance(event.get("text"), str)
            and len(event["text"]) <= 8000
            and event.get("content_state") in {"text", "unsupported"}
            and (event["content_state"] != "unsupported" or event["text"] == "")
            and type(event.get("mentioned")) is bool
            and event.get("revision") == 1
            and isinstance(event.get("sent_at"), str)
            and len(event["sent_at"]) <= 40,
            "adapter_incompatible",
            502,
        )
        if kind == "private":
            require(
                conversation.split(":", 1)[1] == event["account_id"] and not event["mentioned"],
                "adapter_incompatible",
                502,
            )
        try:
            moment = datetime.fromisoformat(event["sent_at"].replace("Z", "+00:00"))
            require(moment.tzinfo is not None, "adapter_incompatible", 502)
        except ValueError:
            raise Fault("adapter_incompatible", 502) from None
        source_ref = (
            "obs:"
            + digest(
                [
                    row["instance_id"],
                    row["account_id"],
                    conversation,
                    event["account_id"],
                    event["event_id"],
                ]
            )[:64]
        )
        value = digest(event)
        with closing(self._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT digest,epoch,archive_epoch FROM sources WHERE source_ref=?", (source_ref,)
            ).fetchone()
            if prior:
                require(prior["digest"] == value, "idempotency_conflict", 409)
            else:
                require(
                    db.execute("SELECT COUNT(*) FROM sources").fetchone()[0] < MAX_SOURCES,
                    "queue_full",
                    429,
                )
                db.execute(
                    "INSERT INTO sources VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        source_ref,
                        row["id"],
                        row["instance_id"],
                        row["account_id"],
                        conversation,
                        event["account_id"],
                        event["event_id"],
                        value,
                        event["scope_revision"],
                        version["archive_epoch"],
                        time.time(),
                    ),
                )
                db.execute(
                    "INSERT INTO discovered VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(connection_id,conversation,author,archive_epoch) DO UPDATE SET "
                    "count=count+1,last_at=excluded.last_at",
                    (
                        row["id"],
                        conversation,
                        event["account_id"],
                        version["archive_epoch"],
                        1,
                        time.time(),
                    ),
                )
        return {
            "source_ref": source_ref,
            "source_digest": value,
            "scope_version": prior["epoch"] if prior else event["scope_revision"],
            "archive_epoch": prior["archive_epoch"] if prior else version["archive_epoch"],
        }

    def verify(self, header, body):
        require(isinstance(body, dict), "invalid_input", 400)
        with self.p.store.connect(write=True) as db:
            _, principal = self.p.auth.authenticate(header, db, "observation.verify")
            require(
                principal["kind"] == "service" and principal["service"] == "memory",
                "forbidden",
                403,
            )
        if body.get("operation") == "scope":
            require(
                set(body) == {"operation", "instance_id", "self_id", "conversation_id"},
                "invalid_input",
                400,
            )
            conversation = body["conversation_id"]
            require(
                re.fullmatch(r"(group|private):[1-9][0-9]*", conversation) is not None,
                "invalid_input",
                400,
            )
            candidates = [
                row
                for row in self.rows()
                if row["instance_id"] == body["instance_id"]
                and row["account_id"] == body["self_id"]
            ]
            require(len(candidates) == 1, "not_found", 404)
            row = candidates[0]
            require(not row["pending"], "dependency_unavailable", 503)
            require(row["read_enabled"], "scope_changed", 409)
            return {"valid": True, "archive_epoch": row["archive_epoch"]}
        require(
            set(body) == {"operation", "source_ref", "source_digest", "scope_version"}
            and body["operation"] in {"ingest", "read"},
            "invalid_input",
            400,
        )
        with closing(self._db()) as db:
            source = db.execute(
                "SELECT * FROM sources WHERE source_ref=?", (body["source_ref"],)
            ).fetchone()
        require(
            source is not None
            and source["digest"] == body["source_digest"]
            and source["epoch"] == body["scope_version"],
            "not_found",
            404,
        )
        row = self.get(source["connection_id"])
        require(row is not None, "not_found", 404)
        require(not row["pending"], "dependency_unavailable", 503)
        require(
            row["read_enabled"] and row["archive_epoch"] == source["archive_epoch"],
            "scope_changed",
            409,
        )
        # A source accepted under an older observation revision may finish
        # archiving after a pause. It never becomes a reply trigger later.
        return {
            "valid": True,
            "source_ref": source["source_ref"],
            "instance_id": source["instance_id"],
            "self_id": source["self_id"],
            "conversation_id": source["conversation"],
            "account_id": source["author"],
            "event_id": source["event_id"],
            "source_digest": source["digest"],
            "scope_version": source["epoch"],
            "archive_epoch": source["archive_epoch"],
        }

    async def discovered(self, body):
        row = self.get(body["id"])
        require(row is not None, "not_found", 404)
        require(row["read_enabled"], "forbidden", 403)
        # Companion owns the archive. This local list is explicitly source intake only.
        limit = body["limit"]
        require(type(limit) is int and 1 <= limit <= 100, "invalid_input", 400)
        cursor = body["cursor"]
        require(
            cursor is None
            or isinstance(cursor, dict)
            and set(cursor) == {"conversation", "author"},
            "invalid_input",
            400,
        )
        if cursor:
            require(
                re.fullmatch(r"(group|private):[1-9][0-9]*", cursor["conversation"])
                and ID.fullmatch(cursor["author"]),
                "invalid_input",
                400,
            )
        with closing(self._db()) as db:
            rows = db.execute(
                "SELECT conversation,author,count,last_at FROM discovered "
                "WHERE connection_id=? AND archive_epoch=? AND (? IS NULL OR conversation>? OR "
                "(conversation=? AND author>?)) ORDER BY conversation,author LIMIT ?",
                (
                    row["id"],
                    row["archive_epoch"],
                    None if cursor is None else cursor["conversation"],
                    "" if cursor is None else cursor["conversation"],
                    "" if cursor is None else cursor["conversation"],
                    "" if cursor is None else cursor["author"],
                    limit + 1,
                ),
            ).fetchall()
        items = [dict(item) for item in rows[:limit]]
        for item in items:
            policy = row[item["conversation"].split(":", 1)[0] + "_policy"]
            item["decision"] = decision(
                policy,
                item["conversation"].split(":", 1)[1],
                False,
                item["conversation"].split(":", 1)[0],
            )
        return {
            "items": items,
            "next_cursor": {
                "conversation": items[-1]["conversation"],
                "author": items[-1]["author"],
            }
            if len(rows) > limit
            else None,
        }

    async def archive(self, body):
        row = self.get(body["id"])
        require(row is not None and row["read_enabled"], "forbidden", 403)
        require(
            isinstance(body["conversation_id"], str)
            and re.fullmatch(r"(group|private):[1-9][0-9]*", body["conversation_id"]),
            "invalid_input",
            400,
        )
        request = {
            "instance_id": row["instance_id"],
            "self_id": row["account_id"],
            "conversation_id": body["conversation_id"],
            "limit": body["limit"],
            "cursor": body["cursor"],
            "archive_epoch": row["archive_epoch"],
        }
        settings = self.p.settings["core"]
        core_settings(settings)
        token = secret(settings["token_env"])
        require(token is not None, "dependency_unavailable", 503)
        page = await self.adapters._call(
            settings["base_url"],
            token,
            None,
            None,
            "/internal/v2/observations/query",
            request,
            core=True,
            ca_file=settings.get("ca_file"),
        )
        async with self.lock:
            current = self.get(body["id"])
            require(
                current is not None
                and current["read_enabled"]
                and current["archive_epoch"] == row["archive_epoch"],
                "scope_changed",
                409,
            )
            return page

    async def _reply_event(self, row, event, reply_claimed):
        conversation = event["conversation_id"]
        kind, target = conversation.split(":", 1)
        policy = row[kind + "_policy"]
        verdict = decision(policy, target, event["mentioned"], kind)
        if (
            not reply_claimed
            or event["scope_revision"] != row["host_revision"]
            or event["content_state"] != "text"
            or not verdict["reply_triggered"]
        ):
            return True
        actor = policy["actor_id"]
        require(actor is not None, "scope_changed", 409)
        managed = self.p.role_runtime.get(actor)
        if managed is not None and not self.p.role_runtime.active(actor):
            return True
        conn, token = await self._ensure_reply_scope(row, conversation, event["account_id"], actor)
        current = self.get(row["id"])
        if (
            current is None
            or current["pending"]
            or current["host_revision"] != row["host_revision"]
            or not self.reply_connection_active(conn)
        ):
            return True
        body = {
            "schema_version": 1,
            "connection_id": conn,
            "platform_id": event["platform_id"],
            "self_id": event["self_id"],
            "event_id": event["event_id"],
            "revision": 1,
            "namespace": "qq",
            "conversation_id": conversation,
            "thread_id": None,
            "account_id": event["account_id"],
            "sent_at": event["sent_at"],
            "text": event["text"],
        }
        outcome = await self.p.bots.event("Bearer " + token, body)
        if outcome["state"] == "accepted":
            return True
        if outcome["state"] == "unknown":
            status = await self.p.local_work.run(
                self.p.bots.event_status,
                "Bearer " + token,
                {
                    "connection_id": conn,
                    "event_id": event["event_id"],
                    "account_id": event["account_id"],
                },
            )
            return status["state"] == "accepted"
        return False

    async def _deliver_replies(self, row):
        with closing(self._db()) as db:
            replies = db.execute(
                "SELECT * FROM reply_connections WHERE observation_id=?", (row["id"],)
            ).fetchall()
        for reply in replies:
            conn = reply["connection_id"]
            if not self.reply_connection_active(conn):
                continue
            token = self._reply_token(conn)
            claimed = await self.p.local_work.run(
                self.p.bots.claim,
                "Bearer " + token,
                {"connection_id": conn, "instance_id": "platform-observation-pump", "limit": 20},
            )
            for delivery in claimed["deliveries"]:
                current = self.get(row["id"])
                if current is None or not self.reply_connection_active(conn):
                    break
                receipt = None
                try:
                    receipt = await self._plugin(
                        current,
                        "messages/send",
                        {
                            "connection_id": conn,
                            "account_id": current["account_id"],
                            "policy_revision": current["host_revision"],
                            "delivery": delivery,
                        },
                    )
                except Fault:
                    try:
                        status = await self._plugin(
                            current,
                            "messages/status",
                            {
                                "connection_id": conn,
                                "reply_id": delivery["reply_id"],
                                "attempt_id": delivery["attempt_id"],
                            },
                        )
                        receipt = status.get("receipt") if status.get("found") else None
                    except Fault:
                        pass
                if receipt is None:
                    receipt = {
                        "reply_id": delivery["reply_id"],
                        "attempt_id": delivery["attempt_id"],
                        "state": "unknown",
                        "channel_message_ids": [],
                    }
                require(
                    receipt.get("reply_id") == delivery["reply_id"]
                    and receipt.get("attempt_id") == delivery["attempt_id"]
                    and receipt.get("state") in {"sent", "failed", "unknown"},
                    "adapter_incompatible",
                    502,
                )
                await self.p.local_work.run(
                    self.p.bots.ack,
                    "Bearer " + token,
                    {
                        "connection_id": conn,
                        "reply_id": delivery["reply_id"],
                        "attempt_id": delivery["attempt_id"],
                        "state": receipt["state"],
                        "channel_message_ids": receipt.get("channel_message_ids", []),
                    },
                )

    async def pump_once(self):
        if not self.catalog:
            return
        for snapshot in self.rows():
            row = self.get(snapshot["id"])
            if row is None:
                continue
            if row["pending"]:
                await self._apply(row)
                row = self.get(row["id"])
            if row["pending"]:
                continue
            try:
                result = await self._plugin(
                    row, "poll", {"account_id": row["account_id"], "limit": 20}
                )
                events = result.get("events")
                require(isinstance(events, list) and len(events) <= 20, "adapter_incompatible", 502)
                acknowledged = []
                for item in events:
                    current = self.get(row["id"])
                    if (
                        current is None
                        or current["revision"] != row["revision"]
                        or current["pending"]
                    ):
                        break
                    event = item["event"]
                    source = self.record(current, event)
                    receipt = await self._companion({**source, "event": event})
                    require(
                        receipt.get("source_ref") == source["source_ref"]
                        and receipt.get("state") in {"accepted", "duplicate"}
                        and receipt.get("archive_state") in {"pending_memory", "archived"},
                        "dependency_unavailable",
                        503,
                    )
                    # The Companion write may have waited while an administrator
                    # tightened policy. Re-evaluate under the local policy lock.
                    async with self.lock:
                        current = self.get(row["id"])
                        if current is None or current["pending"]:
                            break
                        if await self._reply_event(
                            current, event, item.get("reply_claimed") is True
                        ):
                            acknowledged.append(item["id"])
                if acknowledged:
                    ack = await self._plugin(
                        row, "ack", {"account_id": row["account_id"], "event_ids": acknowledged}
                    )
                    require(ack.get("acknowledged") == acknowledged, "adapter_incompatible", 502)
                await self._deliver_replies(row)
                status = await self._plugin(row, "status", {"account_id": row["account_id"]})
                account = status.get("account") or {}
                require(
                    status.get("found") is True
                    and account.get("revision") == row["host_revision"]
                    and type(account.get("pending")) is int
                    and type(account.get("dropped")) is int,
                    "adapter_incompatible",
                    502,
                )
                row["host_pending"] = account["pending"]
                row["host_dropped"] = account["dropped"]
                row["state"] = (
                    "degraded" if account["dropped"] else "ready" if row["enabled"] else "disabled"
                )
                row["last_error"] = "observation_capacity_exceeded" if account["dropped"] else None
                row["last_checked_at"] = utc(time.time())
            except (Fault, ValueError, TypeError, KeyError) as exc:
                row["state"] = "degraded"
                row["last_error"] = exc.code if isinstance(exc, Fault) else "adapter_incompatible"
            self.catalog.observe(row)

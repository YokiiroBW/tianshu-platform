"""Account-scoped bot observation authority, separate from dialogue dispatch."""

import asyncio
import hashlib
import hmac
import re
import sqlite3
from .backends import database as bind_database
import time
import uuid
from contextlib import closing
from datetime import datetime

from .auth import secret
from .contracts import Fault, canonical, digest, loads, require, utc
from .transport import core_settings
from . import group_policy

PREFIX = "/api/web/bot-observation/"
ID = re.compile(r"^[1-9][0-9]*$")
MODES = {"observe_only", "whitelist", "blacklist"}
MAX_SOURCES = 100000
REPLY_SCOPE_BATCH = 8


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


def decision(policy, target, mentioned, kind, text=""):
    """Pure policy: observation, reply permission, and trigger stay distinct."""
    if kind == "group":
        return group_policy.decision(policy, target, mentioned, text)
    observed = policy["observe"]
    mode = policy["mode"]
    permitted = (mode == "whitelist" and target in policy["list"]) or (
        mode == "blacklist" and target not in policy["list"]
    )
    return {
        "observe": observed,
        "reply_permitted": bool(observed and permitted),
        "reply_triggered": bool(observed and permitted and (kind == "private" or mentioned)),
    }


class BotObservation:
    def __init__(self, platform):
        self.p = platform
        self.adapters = platform.bot_adapters
        self.catalog = self.adapters.catalog
        self.lock = asyncio.Lock()
        self._pump_lock = asyncio.Lock()
        self._batches = {}
        self._local_tasks = set()
        self._reply_cursors = {}
        self._closed = False
        self.path = platform.store.path + ".bot-observations.sqlite"
        self.database = bind_database(
            getattr(platform, "storage_runtime", None), "bot_observations", self.path
        )
        from .group_directory import GroupDirectory

        self.groups = GroupDirectory(self)
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
                        conversation TEXT NOT NULL, actor_id TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS observation_group_policies (
                        observation_id TEXT NOT NULL, revision INTEGER NOT NULL,
                        document TEXT NOT NULL, PRIMARY KEY(observation_id,revision)
                    );
                    CREATE TABLE IF NOT EXISTS reply_authors (
                        connection_id TEXT NOT NULL, author TEXT NOT NULL,
                        PRIMARY KEY(connection_id,author)
                    );
                """)
                self._migrate_reply_generations(db)
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
        db = self.database.connect(timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def _migrate_reply_generations(self, db):
        # Preserve every historical native/Core mapping; only connection_id is
        # unique. A new policy generation must coexist with the old receipt.
        if self.database.backend == "sqlite":
            indexes = db.execute("PRAGMA index_list(reply_connections)").fetchall()
            for index in indexes:
                columns = [r[2] for r in db.execute('PRAGMA index_info("' + index[1] + '")')]
                if index[2] and columns == ["observation_id", "conversation", "actor_id"]:
                    with closing(
                        sqlite3.connect(
                            self.path + ".pre-group-modes-" + uuid.uuid4().hex + ".sqlite"
                        )
                    ) as backup:
                        db.backup(backup)
                    with db:
                        db.execute(
                            "ALTER TABLE reply_connections RENAME TO reply_connections_legacy"
                        )
                        db.execute(
                            "CREATE TABLE reply_connections (connection_id TEXT PRIMARY KEY, observation_id TEXT NOT NULL, conversation TEXT NOT NULL, actor_id TEXT NOT NULL)"
                        )
                        db.execute(
                            "INSERT INTO reply_connections SELECT * FROM reply_connections_legacy"
                        )
                        db.execute("DROP TABLE reply_connections_legacy")
                    break
        else:
            rows = db.native(
                "SELECT c.conname,array_agg(a.attname ORDER BY k.ord) FROM pg_constraint c CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY k(num,ord) JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.num WHERE c.conrelid='reply_connections'::regclass AND c.contype='u' GROUP BY c.conname"
            ).fetchall()
            constraints = [
                name
                for name, columns in rows
                if list(columns) == ["observation_id", "conversation", "actor_id"]
            ]
            if constraints:
                from .postgres import quote

                with db:
                    # Native DDL stays autocommit until explicitly begun;
                    # the connection context only commits or rolls back.
                    if not db.in_transaction:
                        db.native("BEGIN")
                    for name in constraints:
                        db.native("ALTER TABLE reply_connections DROP CONSTRAINT " + quote(name))

    async def _local(self, operation, *args):
        # Keep ownership of this operation through cancellation. LocalWork's
        # thread may still be using our database after its HTTP caller stops.
        task = asyncio.create_task(self.p.local_work.run(operation, *args))
        self._local_tasks.add(task)
        task.add_done_callback(self._local_finished)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(task)
            except Exception:
                pass
            raise

    def _local_finished(self, task):
        self._local_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    def _reply_token(self, connection_id):
        return hmac.new(self.catalog.key, connection_id.encode(), hashlib.sha256).hexdigest()

    def _install_reply(self, reply, authors, *, row=None):
        row = row if row is not None else self.get(reply["observation_id"])
        require(row is not None, "dependency_unavailable", 503)
        conn = reply["connection_id"]
        slot_id = "observation:" + conn
        kind = reply["conversation"].split(":", 1)[0]
        channel = self._observation_channel("binding:" + conn, reply["conversation"])
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
            and decision(policy, target, False, kind)["observe"]
            and decision(policy, target, False, kind)["reply_permitted"]
            and self.actor(row, reply["conversation"]) == reply["actor_id"]
        )

    def _reply_connection_id(self, row, conversation, actor_id):
        # A policy revision gets a new delivery scope. Old queued replies cannot
        # become sendable after an allow/deny list or mode change.
        revision = (
            group_policy.revision(row, conversation)
            if conversation.startswith("group:")
            else row["host_revision"]
        )
        return "bot:" + digest([row["id"], revision, conversation, actor_id])[:32]

    @staticmethod
    def actor(row, conversation):
        return (
            group_policy.effective(row["group_policy"], conversation)["actor_id"]
            if conversation.startswith("group:")
            else row["private_policy"]["actor_id"]
        )

    def event_decision(self, row, event):
        kind, target = event["conversation_id"].split(":", 1)
        return decision(row[kind + "_policy"], target, event["mentioned"], kind, event["text"])

    def reply_input_allowed(self, slot, body, *, participation=None, observation=None):
        rows = [
            row
            for row in self.rows()
            if row["instance_id"] == slot["platform_id"] and row["account_id"] == slot["self_id"]
        ]
        if not rows:
            return True
        if len(rows) != 1:
            return False
        row = rows[0]
        conversation = body["conversation_id"]
        if row["pending"] or not row["enabled"] or not row["read_enabled"]:
            return False
        mentioned = body["self_id"] in body.get("message_context", {}).get("mentioned_accounts", [])
        if observation is not None:
            if body != self.reply_body(
                body["connection_id"], observation
            ) or not self.same_capture_policy(row, observation):
                return False
            ref = self._source_ref(
                row["instance_id"],
                row["account_id"],
                conversation,
                body["account_id"],
                body["event_id"],
            )
            with closing(self._db()) as db:
                source = db.execute(
                    "SELECT digest FROM sources WHERE source_ref=? AND connection_id=?",
                    (ref, row["id"]),
                ).fetchone()
            if source is None or source[0] != digest(observation):
                return False
            mentioned = observation["mentioned"]
        verdict = self.event_decision(
            row, dict(conversation_id=conversation, mentioned=mentioned, text=body["text"])
        )
        return verdict["observe"] and (
            verdict["reply_triggered"]
            if participation is None
            else verdict.get("proactive_permitted") is True
        )

    def captured_group_policy(self, row, event):
        with closing(self._db()) as db:
            saved = db.execute(
                "SELECT document FROM observation_group_policies WHERE observation_id=? AND revision=?",
                (row["id"], event["scope_revision"]),
            ).fetchone()
        if saved:
            return loads(saved[0])
        return row["group_policy"] if event["scope_revision"] == row["host_revision"] else None

    def same_capture_policy(self, row, event):
        conversation = event["conversation_id"]
        if conversation.startswith("private:"):
            return event["scope_revision"] == row["host_revision"]
        captured = self.captured_group_policy(row, event)
        return bool(
            captured is not None
            and event["scope_revision"] >= group_policy.revision(row, conversation)
            and group_policy.effective(captured, conversation)
            == group_policy.effective(row["group_policy"], conversation)
        )

    @staticmethod
    def _observation_channel(binding_id, conversation):
        return {
            "namespace": "qq",
            "binding_id": binding_id,
            "channel_conversation_id": conversation,
            "thread_id": None,
        }

    def segment_channel(self, row, conversation, actor_id):
        """Project a read-only frame without installing a reply or admission."""
        frame = digest([row["id"], row["instance_id"], row["account_id"], conversation, actor_id])
        return self._observation_channel("binding:observation:" + frame[:32], conversation)

    def segment_delivery_channel(self, row, conversation, actor_id):
        """Identify the current reply generation without installing or enabling it."""
        return self._observation_channel(
            "binding:" + self._reply_connection_id(row, conversation, actor_id), conversation
        )

    @staticmethod
    def _source_ref(instance_id, self_id, conversation, author, event_id):
        return "obs:" + digest([instance_id, self_id, conversation, author, event_id])[:64]

    def _prepare_reply_scope(self, row, conversation, author, actor_id):
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
        token = self._reply_token(conn)
        current = self.get(row["id"])
        require(current is not None, "dependency_unavailable", 503)
        return conn, token, reply, authors, current

    def _create_reply_scope(self, conn, token, actor_id):
        with closing(self.p.bots._db()) as db:
            exists = db.execute("SELECT enabled FROM connections WHERE id=?", (conn,)).fetchone()
        if exists is None:
            self.p.bots.create("observation:" + conn, [actor_id], connection_id=conn, token=token)

    async def _ensure_reply_scope(self, row, conversation, author, actor_id):
        conn, token, reply, authors, current = await self._local(
            self._prepare_reply_scope, row, conversation, author, actor_id
        )
        # Publish the in-memory source/actor projection without yielding, just
        # as before. Only the complete local SQL operations run off-loop.
        self._install_reply(reply, authors, row=current)
        await self._local(self._create_reply_scope, conn, token, actor_id)
        channel = self.segment_channel(current, conversation, actor_id)
        delivery_channel = self.segment_delivery_channel(current, conversation, actor_id)
        require(delivery_channel["binding_id"] == "binding:" + conn, "scope_changed", 409)
        frame_request = {
            "schema_version": 1,
            "actor_id": actor_id,
            "channel": channel,
            "delivery_channel": delivery_channel,
            "conversation_id": None,
        }
        self.p.contracts.check("memory-extraction#segment_frame_request", frame_request)
        linked = await self.adapters._core("learning-frame", frame_request)
        self.p.contracts.check("memory-extraction#segment_frame_response", linked)
        require(
            linked["valid"]
            and linked["frame"]["actor_id"] == actor_id
            and linked["frame"]["audience"]
            == ("group" if conversation.startswith("group:") else "self_private")
            and linked["channel"] == channel
            and linked["delivery_channel"] == delivery_channel,
            "dependency_unavailable",
            503,
        )
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
        await self._local(self._enable_reply_scope, conn)
        return conn, token

    def _enable_reply_scope(self, conn):
        with closing(self.p.bots._db()) as db:
            active = db.execute("SELECT enabled FROM connections WHERE id=?", (conn,)).fetchone()
        if not active or not active[0]:
            self.p.bots.change(conn, "enable")

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
                or not decision(policy, target, False, kind)["observe"]
                or not decision(policy, target, False, kind)["reply_permitted"]
                or self.actor(row, reply["conversation"]) != reply["actor_id"]
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

    def profile_targets(self, role_id, authors):
        """Resolve profile visibility from existing role-to-conversation bindings."""
        rows = self.rows()
        if not authors or not rows:
            return {}
        targets = {}
        placeholders = ",".join("?" for _ in authors)
        with closing(self._db()) as db:
            for row in rows:
                found = db.execute(
                    "SELECT DISTINCT conversation,author FROM discovered "
                    "WHERE connection_id=? AND archive_epoch=? AND author IN ("
                    + placeholders
                    + ")",
                    (row["id"], row["archive_epoch"], *authors),
                )
                for item in found:
                    if self.actor(row, item["conversation"]) == role_id:
                        targets.setdefault(item["author"], set()).add(
                            (row["account_id"], item["conversation"])
                        )
        return targets

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
        value["group_policy"] = group_policy.copy_policy(row["group_policy"])
        value["group_policy_version"] = row.get("group_policy_version")
        value["ordinary_claim_available"] = row.get("ordinary_claim_available", False)
        value["pending"] = row.get("pending") is not None
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
        if path == PREFIX + "groups":
            return await self._local(self.groups.view, console, session, body)
        if path == PREFIX + "view":
            code = self.p.bots._web_code(console, session)
            available = self.catalog is not None and code != "operator_not_authorized"
            consumer = (
                await self.p.participation.consumer_status() if available and self.rows() else None
            )
            return {
                "available": available,
                "unlocked": available and code == "ready",
                "connections": [self._project(row) for row in self.rows()] if available else [],
                "participation": consumer,
            }
        self._gate(console, session)
        if path == PREFIX + "group-policy":
            async with self.lock:
                row = await self.groups.change(body)
                entries = await self._local(self.groups.entries)
                group = next(
                    item
                    for item in entries
                    if item["owner_id"] == row["id"] and item["group_id"] == body["group_id"]
                )
                return {"connection": self._project(row), "group": group}
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
        if path == PREFIX + "private-access":
            require(
                set(body)
                == {"id", "qq_id", "expected_revision", "enabled", "actor_id", "client_id"},
                "invalid_input",
                400,
            )
            async with self.lock:
                return {"connection": self._project(await self.private_access(body))}
        if path == PREFIX + "discovered":
            require(
                set(body)
                in (
                    {"id", "limit", "cursor"},
                    {"id", "limit", "cursor", "role_id"},
                ),
                "invalid_input",
                400,
            )
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
        row["group_policy_version"] = result.get("group_policy_version")
        row["ordinary_claim_available"] = result.get("ordinary_claim_available") is True
        require(row["group_policy_version"] == 2, "adapter_incompatible", 502)

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
                existing["instance_id"] == value["instance_id"]
                and existing["account_id"] == account_id
                for existing in self.rows()
            ),
            "account_already_added",
            409,
        )
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
            group_policy=group_policy.default(),
            group_default_revision=1,
            group_revisions={},
            private_policy=_default(),
        )
        await self._capabilities(row)
        self.catalog.begin(row, key, fingerprint)
        self.adapters.drafts.pop(body["draft_id"], None)
        return await self._apply(row)

    async def private_access(self, body):
        """Change one discovered private contact without replacing account-wide routing."""
        target = _qq(body["qq_id"])
        require(type(body["enabled"]) is bool, "invalid_input", 400)
        actor = body["actor_id"]
        require(
            actor is None or isinstance(actor, str) and 1 <= len(actor) <= 128,
            "invalid_input",
            400,
        )
        require(
            isinstance(body["client_id"], str) and 1 <= len(body["client_id"]) <= 128,
            "invalid_input",
            400,
        )
        fingerprint = digest(["observation-private-access", body])
        receipt = self.catalog.receipt(body["client_id"], fingerprint)
        if receipt:
            return self.get(receipt["id"])
        row = self.get(body["id"])
        require(row is not None, "not_found", 404)
        require(row["read_enabled"], "forbidden", 403)
        require(
            type(body["expected_revision"]) is int and body["expected_revision"] == row["revision"],
            "version_conflict",
            409,
        )
        with closing(self._db()) as db:
            found = db.execute(
                "SELECT 1 FROM discovered WHERE connection_id=? AND archive_epoch=? "
                "AND conversation=? AND author=?",
                (row["id"], row["archive_epoch"], "private:" + target, target),
            ).fetchone()
        require(found is not None, "private_contact_not_found", 404)
        private = dict(row["private_policy"])
        names = list(private["list"])
        if private["mode"] == "observe_only":
            if body["enabled"]:
                if private["actor_id"] is not None:
                    require(
                        actor is None or actor == private["actor_id"], "reply_role_conflict", 409
                    )
                    actor = private["actor_id"]
                require(actor is not None, "reply_role_required", 400)
                private.update(mode="whitelist", list=[target], actor_id=actor)
        else:
            require(actor is None or actor == private["actor_id"], "reply_role_conflict", 409)
            listed = body["enabled"] == (private["mode"] == "whitelist")
            if listed and target not in names:
                names.append(target)
            elif not listed and target in names:
                names.remove(target)
            private["list"] = names
        return await self.policy(
            {
                "id": row["id"],
                "expected_revision": body["expected_revision"],
                "group_policy": group_policy.normalize(row["group_policy"]),
                "private_policy": private,
                "client_id": body["client_id"],
            },
            intent_fingerprint=fingerprint,
        )

    async def policy(self, body, *, intent_fingerprint=None):
        key = body["client_id"]
        fingerprint = intent_fingerprint or digest(["observation-policy", body])
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
        group, private = (
            group_policy.validate(body["group_policy"], saving=True),
            _policy(body["private_policy"]),
        )
        # A pause does not revoke the owner-attested historical role binding.
        group = group_policy.copy_policy(group)
        previous = group_policy.normalize(row["group_policy"])
        if group["default"]["mode"] == "disabled" and group["default"]["actor_id"] is None:
            group["default"]["actor_id"] = previous["default"]["actor_id"]
        for target, rule in group["groups"].items():
            if rule["mode"] == "disabled" and rule["actor_id"] is None:
                rule["actor_id"] = group_policy.effective(previous, target)["actor_id"]
        actors = {
            a["id"] for a in self.adapters.config["actors"] + self.p.role_runtime.active_actors()
        }
        for policy in [group["default"], *group["groups"].values(), private]:
            require(policy["actor_id"] is None or policy["actor_id"] in actors, "forbidden", 403)
        require(
            private["mode"] == "observe_only" or private["actor_id"] is not None,
            "invalid_input",
            400,
        )
        with closing(self._db()) as db:
            known = [
                r[0].split(":", 1)[1]
                for r in db.execute(
                    "SELECT DISTINCT conversation FROM discovered WHERE connection_id=? AND conversation LIKE 'group:%'",
                    (row["id"],),
                )
            ]
        group_policy.advance(row, row["group_policy"], group, known)
        old_observe = (
            group_policy.observing(row["group_policy"]),
            row["private_policy"]["observe"],
        )
        new_observe = (group_policy.observing(group), private["observe"])
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
        row["group_default_revision"] = row["host_revision"] + 1
        row["group_revisions"] = {
            key: row["host_revision"] + 1
            for key in set(row.get("group_revisions", {}))
            | set(group_policy.normalize(row["group_policy"])["groups"])
        }
        row["revision"] += 1
        row["host_revision"] += 1
        row["state"] = "unknown"
        row["pending"] = {
            "request_id": body["client_id"],
            "enabled": body["read_enabled"]
            and any(
                (
                    group_policy.observing(row["group_policy"]),
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
                    int(group_policy.observing(row["group_policy"])),
                    int(row["private_policy"]["observe"]),
                    row["archive_epoch"],
                ),
            )
            db.execute(
                "INSERT OR IGNORE INTO observation_group_policies VALUES(?,?,?)",
                (
                    row["id"],
                    row["host_revision"],
                    canonical(group_policy.normalize(row["group_policy"])),
                ),
            )
        request = {
            "request_id": row["pending"]["request_id"],
            "account_id": row["account_id"],
            "revision": row["host_revision"],
            "enabled": row["pending"]["enabled"],
            "group_policy": group_policy.normalize(row["group_policy"]),
            "group_revisions": row.get("group_revisions", {}),
            "group_default_revision": row.get("group_default_revision", row["host_revision"]),
            "private_policy": {k: row["private_policy"][k] for k in ("observe", "mode", "list")},
        }
        try:
            await self._capabilities(row)
            result = await self._plugin(row, "apply", request)
            require(
                result
                == {
                    k: request[k]
                    for k in (
                        "account_id",
                        "revision",
                        "enabled",
                        "group_policy",
                        "private_policy",
                        "group_revisions",
                        "group_default_revision",
                    )
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
        base_fields = {
            "schema_version",
            "platform_id",
            "self_id",
            "namespace",
            "conversation_id",
            "account_id",
            "event_id",
            "revision",
            "sent_at",
            "text",
            "content_state",
            "mentioned",
            "scope_revision",
        }
        require(
            type(event) is dict
            and type(event.get("schema_version")) is int
            and event["schema_version"] in (2, 3, 4)
            and set(event)
            == base_fields
            | ({"nickname", "group_card"} if event["schema_version"] >= 3 else set())
            | ({"message_context"} if event["schema_version"] == 4 else set())
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
        if event["schema_version"] >= 3:
            require(kind == "group" or event["group_card"] is None, "adapter_incompatible", 502)
            for name in (event["nickname"], event["group_card"]):
                require(
                    name is None
                    or (
                        type(name) is str
                        and 1 <= len(name.strip()) <= 80
                        and all(
                            ord(char) >= 32
                            and ord(char) != 127
                            and not 0x202A <= ord(char) <= 0x202E
                            and not 0x2066 <= ord(char) <= 0x2069
                            for char in name
                        )
                    ),
                    "adapter_incompatible",
                    502,
                )
        if event["schema_version"] == 4:
            from .bot_message_context import validate

            validate(event["message_context"])
        with closing(self._db()) as db:
            version = db.execute(
                "SELECT * FROM observation_versions WHERE observation_id=? AND revision=?",
                (row["id"], event["scope_revision"]),
            ).fetchone()
        require(version is not None and version[kind + "_observe"], "scope_changed", 409)
        if kind == "group":
            captured = self.captured_group_policy(row, event)
            with closing(self._db()) as db:
                first_v2 = db.execute(
                    "SELECT min(revision) FROM observation_group_policies WHERE observation_id=?",
                    (row["id"],),
                ).fetchone()[0]
            require(
                (captured is None and (first_v2 is None or event["scope_revision"] < first_v2))
                or captured is not None
                and group_policy.effective(captured, conversation)["mode"] != "disabled",
                "scope_changed",
                409,
            )
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
        source_ref = self._source_ref(
            row["instance_id"],
            row["account_id"],
            conversation,
            event["account_id"],
            event["event_id"],
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
                    "count=discovered.count+1,last_at=excluded.last_at",
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
        if body.get("operation") in {"segment", "segment_resolve"}:
            from .observation_segments import check_segment, resolve_segment

            return (
                check_segment(self, body)
                if body["operation"] == "segment"
                else resolve_segment(self, body)
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
        if "role_id" in body:
            require(
                isinstance(body["role_id"], str) and bool(body["role_id"]),
                "invalid_input",
                400,
            )
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
                "WHERE connection_id=? AND archive_epoch=? AND (CAST(? AS TEXT) IS NULL OR conversation>? OR "
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
        page = rows[:limit]
        items = [
            dict(item)
            for item in page
            if "role_id" not in body or self.actor(row, item["conversation"]) == body["role_id"]
        ]
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
                "conversation": page[-1]["conversation"],
                "author": page[-1]["author"],
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
        verdict = decision(policy, target, event["mentioned"], kind, event["text"])
        if (
            not reply_claimed
            or not self.same_capture_policy(row, event)
            or event["content_state"] != "text"
            or not verdict["reply_triggered"]
        ):
            return True
        actor = self.actor(row, conversation)
        require(actor is not None, "scope_changed", 409)
        if not await self._local(self._reply_role_active, actor):
            return True
        conn, token = await self._ensure_reply_scope(row, conversation, event["account_id"], actor)
        current = await self._local(self.get, row["id"])
        if (
            current is None
            or current["pending"]
            or not await self._local(self.reply_connection_active, conn)
        ):
            return True
        body = self.reply_body(conn, event)
        outcome = await self.p.bots.event("Bearer " + token, body, observation=event)
        if outcome["state"] == "accepted":
            return True
        if outcome["state"] == "unknown":
            status = await self._local(
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

    @staticmethod
    def reply_body(conn, event):
        """The same immutable original Bot input for explicit and selected participation."""
        body = {
            "schema_version": 1,
            "connection_id": conn,
            "platform_id": event["platform_id"],
            "self_id": event["self_id"],
            "event_id": event["event_id"],
            "revision": 1,
            "namespace": "qq",
            "conversation_id": event["conversation_id"],
            "thread_id": None,
            "account_id": event["account_id"],
            "sent_at": event["sent_at"],
            "text": event["text"],
        }
        if event["schema_version"] >= 3:
            body.update(
                schema_version=2, nickname=event["nickname"], group_card=event["group_card"]
            )
            if body["nickname"] is None and body["group_card"] is None:
                body = {
                    key: value
                    for key, value in body.items()
                    if key not in {"nickname", "group_card"}
                }
                body["schema_version"] = 1
        if event["schema_version"] == 4:
            body.update(
                schema_version=3,
                nickname=event["nickname"],
                group_card=event["group_card"],
                message_context=event["message_context"],
            )
        return body

    def _reply_role_active(self, actor):
        managed = self.p.role_runtime.get(actor)
        return managed is None or self.p.role_runtime.active(actor)

    def _reply_page(self, row):
        # A fixed, rotating page prevents a large number of empty reply scopes
        # from monopolizing an account. The existing queue owns all deliveries.
        cursor = self._reply_cursors.get(row["id"], "")
        with closing(self._db()) as db:
            replies = db.execute(
                "SELECT * FROM reply_connections WHERE observation_id=? AND connection_id>? "
                "ORDER BY connection_id LIMIT ?",
                (row["id"], cursor, REPLY_SCOPE_BATCH),
            ).fetchall()
            if not replies and cursor:
                replies = db.execute(
                    "SELECT * FROM reply_connections WHERE observation_id=? "
                    "ORDER BY connection_id LIMIT ?",
                    (row["id"], REPLY_SCOPE_BATCH),
                ).fetchall()
        self._reply_cursors[row["id"]] = replies[-1]["connection_id"] if replies else ""
        return replies

    def _reply_enabled(self, conn):
        if not self.reply_connection_active(conn):
            return False
        with closing(self.p.bots._db()) as queue:
            connection = queue.execute(
                "SELECT enabled FROM connections WHERE id=?", (conn,)
            ).fetchone()
        return connection is not None and bool(connection[0])

    async def _deliver_replies(self, row):
        replies = await self._local(self._reply_page, row)
        for reply in replies:
            conn = reply["connection_id"]
            # A reply scope can be recorded before its Core binding is enabled.
            # Retrying that admission owns activation; an unfinished scope must not
            # prevent already enabled conversations from draining their replies.
            if not await self._local(self._reply_enabled, conn):
                continue
            token = self._reply_token(conn)
            participation = getattr(self.p, "participation", None)
            claim_body = {
                "connection_id": conn,
                "instance_id": "platform-observation-pump",
                "limit": 20,
            }
            checked = (
                await participation.delivery.prepare("Bearer " + token, claim_body)
                if participation
                else None
            )
            claimed = await self._local(
                lambda: (
                    self.p.bots.claim("Bearer " + token, claim_body, participation_checked=checked)
                    if participation
                    else self.p.bots.claim("Bearer " + token, claim_body)
                )
            )
            for delivery in claimed["deliveries"]:
                current = await self._local(self.get, row["id"])
                if current is None or not await self._local(self.reply_connection_active, conn):
                    break
                receipt = None
                cancelled = False
                try:
                    if participation is not None:
                        await participation.delivery.before_send(
                            delivery["reply_id"], delivery["attempt_id"]
                        )
                    receipt = await self._plugin(
                        current,
                        "messages/send",
                        {
                            "connection_id": conn,
                            "account_id": current["account_id"],
                            "policy_revision": group_policy.revision(current, reply["conversation"])
                            if reply["conversation"].startswith("group:")
                            else current["host_revision"],
                            "delivery": delivery,
                        },
                    )
                except asyncio.CancelledError:
                    # The SDK write may have started. Settle the existing claim
                    # as unknown before shutting down; never repeat that write.
                    cancelled = True
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
                await self._local(
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
                if cancelled:
                    raise asyncio.CancelledError

    async def _refresh_host_status(self, row):
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
        await self._local(self.catalog.observe, row)

    async def pump_once(self, *, wait=True):
        """Poll even while the account's one bounded business batch is busy.

        Unacknowledged SDK events remain its durable queue. A busy poll renews
        the real host lease but neither replaces the retained batch nor starts
        another dispatch. Standalone callers may await that same batch.
        """
        if not self.catalog or self._closed:
            return
        batches = []
        async with self._pump_lock:
            if self._closed:
                return
            for snapshot in await self._local(self.rows):
                row = await self._local(self.get, snapshot["id"])
                if row is None:
                    continue
                self.groups.schedule(row)
                if row["pending"]:
                    # Management routes use this same policy lock. Do not race
                    # a newer configure/rebind while the pump applies an intent.
                    # Another account's long reply admission may hold it; that
                    # must not delay polls for the remaining active accounts.
                    if self.lock.locked():
                        continue
                    async with self.lock:
                        row = await self._local(self.get, row["id"])
                        if row and row["pending"]:
                            await self._apply(row)
                        row = await self._local(self.get, snapshot["id"])
                if (
                    row is None
                    or row["pending"]
                    or not row["enabled"]
                    or not (
                        group_policy.observing(row["group_policy"])
                        or row["private_policy"]["observe"]
                    )
                ):
                    continue
                try:
                    result = await self._plugin(
                        row, "poll", {"account_id": row["account_id"], "limit": 20}
                    )
                    events = result.get("events")
                    require(
                        isinstance(events, list) and len(events) <= 20, "adapter_incompatible", 502
                    )
                    batch = self._batches.get(row["id"])
                    if batch is not None and batch.done():
                        # Observe an unexpected exception before replacing its
                        # task. Expected peer failures are recorded by the owner.
                        if not batch.cancelled():
                            error = batch.exception()
                            if error is not None:
                                await self._observe_failure(row, error)
                        del self._batches[row["id"]]
                        batch = None
                    if batch is None:
                        batch = asyncio.create_task(self._process_batch(row, events))
                        self._batches[row["id"]] = batch
                    batches.append(batch)
                except (Fault, OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
                    await self._observe_failure(row, exc)
        if wait and batches:
            try:
                await asyncio.gather(*batches)
            finally:
                for key, batch in list(self._batches.items()):
                    if batch.done():
                        del self._batches[key]

    async def _observe_failure(self, row, exc):
        row["state"] = "degraded"
        row["last_error"] = exc.code if isinstance(exc, Fault) else "adapter_incompatible"
        await self._local(self.catalog.observe, row)

    async def _process_batch(self, row, events):
        try:
            await self._refresh_host_status(row)
            await self._deliver_replies(row)
            acknowledged = []
            for index, item in enumerate(events, 1):
                current = await self._local(self.get, row["id"])
                if current is None or current["revision"] != row["revision"] or current["pending"]:
                    break
                event = item["event"]
                # Stop at the entrance, before archive, source preparation or
                # any consumer. ACK old pending items so enabling never replays
                # a message received while this conversation was disabled.
                verdict = self.event_decision(current, event)
                if not verdict["observe"] or not self.same_capture_policy(current, event):
                    acknowledged.append(item["id"])
                    continue
                source = await self._local(self.record, current, event)
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
                    current = await self._local(self.get, row["id"])
                    if current is None or current["pending"]:
                        break
                    if not self.event_decision(current, event)[
                        "observe"
                    ] or not self.same_capture_policy(current, event):
                        acknowledged.append(item["id"])
                        continue
                    if not verdict["reply_triggered"] or await self._reply_event(
                        current, event, item.get("reply_claimed") is True
                    ):
                        acknowledged.append(item["id"])
                participation = getattr(self.p, "participation", None)
                if (
                    participation is not None
                    and verdict.get("proactive_permitted")
                    and not verdict["reply_triggered"]
                    and current.get("ordinary_claim_available") is not False
                ):
                    await participation.observe(current, event, source)
                if index % 4 == 0:
                    await self._deliver_replies(row)
            if acknowledged:
                ack = await self._plugin(
                    row, "ack", {"account_id": row["account_id"], "event_ids": acknowledged}
                )
                require(ack.get("acknowledged") == acknowledged, "adapter_incompatible", 502)
            await self._deliver_replies(row)
            await self._refresh_host_status(row)
        except (Fault, OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            await self._observe_failure(row, exc)

    async def quiesce(self):
        self._closed = True
        async with self._pump_lock:
            await self.groups.close()
            batches = list(self._batches.values())
            for task in batches:
                task.cancel()
            await asyncio.gather(*batches, return_exceptions=True)
            # A second caller cancellation can leave an owned local await
            # alive. Drain only those operations, not the shared worker pool.
            while self._local_tasks:
                tasks = list(self._local_tasks)
                await asyncio.gather(*tasks, return_exceptions=True)
                self._local_tasks.difference_update(tasks)
            self._batches.clear()
            self._reply_cursors.clear()

    async def close(self):
        await self.quiesce()

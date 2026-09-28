"""Narrow bot connections and durable delivery inbox around published dialogue ports."""

import asyncio
import copy
import hmac
import secrets
import sqlite3
import uuid
from contextlib import closing

from .auth import secret
from .contracts import Fault, canonical, digest, epoch, loads, require, utc


def validate_bot_settings(settings, auth, sources, contracts):
    config = settings.get("bot_connections")
    if config is None:
        return {}
    require(
        isinstance(config, dict) and set(config) == {"principal", "slots"}, "invalid_input", 400
    )
    principal = auth.principals.get(config["principal"])
    require(
        principal is not None
        and principal["kind"] == "service"
        and principal["service"] == "platform"
        and {"source.register", "source.dispatch", "mapping.prepare"} <= set(principal["actions"]),
        "invalid_input",
        400,
    )
    slots = config["slots"]
    require(isinstance(slots, dict) and len(slots) <= 64, "invalid_input", 400)
    for slot_id, slot in slots.items():
        contracts.check("common#id", slot_id)
        require(
            isinstance(slot, dict)
            and set(slot) == {"adapter", "platform_id", "self_id", "input_entry_ids", "label"}
            and slot["adapter"] in {"nonebot", "astrbot"}
            and isinstance(slot["platform_id"], str)
            and 1 <= len(slot["platform_id"]) <= 128
            and isinstance(slot["self_id"], str)
            and 1 <= len(slot["self_id"]) <= 128
            and isinstance(slot["input_entry_ids"], list)
            and 1 <= len(slot["input_entry_ids"]) <= 64
            and len(set(slot["input_entry_ids"])) == len(slot["input_entry_ids"])
            and isinstance(slot["label"], str)
            and 1 <= len(slot["label"]) <= 64,
            "invalid_input",
            400,
        )
        channel = None
        authors = set()
        for entry_id in slot["input_entry_ids"]:
            entry = sources.entries.get(entry_id)
            require(
                entry is not None and entry["owner"] == config["principal"], "invalid_input", 400
            )
            require(entry["channel"]["namespace"] in {"qq", "tg"}, "invalid_input", 400)
            require(entry["audience"] in {"group", "self_private"}, "invalid_input", 400)
            require(channel is None or channel == entry["channel"], "invalid_input", 400)
            channel = entry["channel"]
            author = canonical(entry["account"])
            require(author not in authors, "invalid_input", 400)
            authors.add(author)
            for actor_id in entry["default_actor_ids"]:
                require(
                    any(
                        auth.entries[item]["actor_id"] == actor_id
                        for item in entry["actor_entries"]
                    ),
                    "invalid_input",
                    400,
                )
            for actor_entry in entry["actor_entries"]:
                actor = auth.entries[actor_entry]
                for caller, receiver in (
                    ("platform", "companion"),
                    ("companion", "memory"),
                    ("companion", "platform"),
                ):
                    auth.route(actor, caller, receiver, "dialogue")
    return slots


class Bots:
    def __init__(self, platform):
        self.p = platform
        config = platform.settings.get("bot_connections")
        self.config = config
        self.slots = copy.deepcopy(
            validate_bot_settings(
                platform.settings, platform.auth, platform.sources, platform.contracts
            )
        )
        self.path = platform.store.path + ".bots.sqlite"
        if config is None:
            return
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            require(
                db.execute("PRAGMA user_version").fetchone()[0] in (0, 1),
                "dependency_unavailable",
                503,
            )
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS connections (
                    id TEXT PRIMARY KEY, slot_id TEXT NOT NULL UNIQUE,
                    actor_ids TEXT NOT NULL, token_hash TEXT NOT NULL,
                    enabled INTEGER NOT NULL, created_at REAL NOT NULL,
                    last_seen_at REAL, last_event_at REAL, last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    connection_id TEXT NOT NULL, event_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    semantic TEXT NOT NULL, message_id TEXT NOT NULL,
                    state TEXT NOT NULL, result TEXT,
                    PRIMARY KEY(connection_id,event_id,account_id)
                );
                CREATE TABLE IF NOT EXISTS replies (
                    reply_id TEXT PRIMARY KEY, command_key TEXT NOT NULL UNIQUE,
                    semantic TEXT NOT NULL, scope TEXT NOT NULL,
                    connection_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL, actor_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL, segment_sequence INTEGER NOT NULL,
                    request TEXT NOT NULL, state TEXT NOT NULL,
                    attempt_id TEXT NOT NULL, channel_message_ids TEXT NOT NULL,
                    claimed_at REAL, observed_at REAL NOT NULL,
                    unknown_origin TEXT,
                    UNIQUE(conversation_id,actor_id,turn_id,segment_sequence)
                );
                PRAGMA user_version=1;
            """)

    def _db(self):
        require(self.config is not None, "dependency_unavailable", 503)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        return db

    def _slot(self, row):
        slot = self.slots.get(row["slot_id"])
        require(slot is not None, "scope_changed", 409)
        entries = [self.p.sources.entries[item] for item in slot["input_entry_ids"]]
        return slot, entries

    def _physical_target(self, row):
        slot, entries = self._slot(row)
        channel = entries[0]["channel"]
        return (
            channel["namespace"],
            slot["self_id"],
            channel["channel_conversation_id"],
            channel["thread_id"],
        )

    def _require_unique_owner(self, db, row):
        target = self._physical_target(row)
        actors = set(loads(row["actor_ids"]))
        for other in db.execute(
            "SELECT * FROM connections WHERE enabled=1 AND id<>?", (row["id"],)
        ):
            require(
                self._physical_target(other) != target
                or not actors.intersection(loads(other["actor_ids"])),
                "idempotency_conflict",
                409,
            )

    def _connection(self, db, connection_id, *, enabled=True, authority=None):
        row = db.execute("SELECT * FROM connections WHERE id=?", (connection_id,)).fetchone()
        require(row is not None, "not_found", 404)
        require(not enabled or row["enabled"], "forbidden", 403)
        if row["enabled"]:
            self._require_unique_owner(db, row)
        slot, entries = self._slot(row)

        def check(current):
            for entry_id in slot["input_entry_ids"]:
                self.p.sources._entry(current, entry_id)
            for actor_id in loads(row["actor_ids"]):
                for entry in entries:
                    actor_entry = next(
                        (
                            item
                            for item in entry["actor_entries"]
                            if self.p.auth.entries[item]["actor_id"] == actor_id
                        ),
                        None,
                    )
                    require(actor_entry is not None, "scope_changed", 409)
                    self.p.auth.entry(current, actor_entry)

        if authority is None:
            with self.p.store.connect(write=True) as current:
                check(current)
        else:
            check(authority)
        return row, slot, entries

    def _authenticate(self, db, header, connection_id, *, settlement=False):
        row = db.execute("SELECT * FROM connections WHERE id=?", (connection_id,)).fetchone()
        require(row is not None, "unauthorized", 401)
        require(isinstance(header, str) and header.startswith("Bearer "), "unauthorized", 401)
        token = header[7:]
        require(
            token.isascii()
            and len(token) >= 32
            and hmac.compare_digest(digest(token), row["token_hash"]),
            "unauthorized",
            401,
        )
        return (row, *self._slot(row)) if settlement else self._connection(db, connection_id)

    def _platform_header(self):
        principal = self.p.auth.principals[self.config["principal"]]
        return "Bearer " + (secret(principal["token_env"]) or "")

    def _web_code(self, console, session=None):
        if self.config is None:
            return "management_disabled"
        principal = self.p.auth.principals[console.config["principal"]]
        if "bot.manage" not in principal["actions"]:
            return "operator_not_authorized"
        if session is not None and session.get("bot_management", 0) <= console.clock():
            return "management_required"
        return "ready"

    async def web_route(self, console, path, body, session):
        if path == "/api/web/bots/view":
            require(body == {}, "invalid_input", 400)
            code = self._web_code(console, session)
            result = (
                await self.p.local_work.run(self.view)
                if code != "operator_not_authorized"
                else {"available": False, "slots": [], "connections": []}
            )
            result["management"] = {"code": code, "unlocked": code == "ready"}
            return result
        if path == "/api/web/bots/unlock":
            require(set(body) == {"password"}, "invalid_input", 400)
            require(self._web_code(console) == "ready", "operator_not_authorized", 403)
            password = body["password"]
            require(isinstance(password, str) and 12 <= len(password) <= 256, "unauthorized", 401)
            async with console.login_lock:
                console.failures = [t for t in console.failures if t > console.clock() - 60]
                require(len(console.failures) < 5, "too_many_requests", 429)
                if not await asyncio.to_thread(console.verify_password, password):
                    console.failures.append(console.clock())
                    raise Fault("unauthorized", 401)
            session["bot_management"] = console.clock() + 900
            return {"unlocked": True, "expires_in": 900}
        require(self._web_code(console, session) == "ready", "management_required", 403)
        if path == "/api/web/bots/create":
            require(set(body) == {"slot_id", "actor_ids"}, "invalid_input", 400)
            require(body["slot_id"] in self.config["slots"], "not_found", 404)
            return await self.p.local_work.run(self.create, body["slot_id"], body["actor_ids"])
        operation = path.rsplit("/", 1)[-1]
        require(operation in {"enable", "disable", "rotate"}, "not_found", 404)
        require(set(body) == {"connection_id"}, "invalid_input", 400)
        managed = getattr(self.p, "bot_adapters", None)
        require(
            managed is None
            or managed.catalog is None
            or managed.catalog.get(body["connection_id"]) is None,
            "not_found",
            404,
        )
        return await self.p.local_work.run(self.change, body["connection_id"], operation)

    def view(self):
        if self.config is None:
            return {"available": False, "slots": [], "connections": []}
        with closing(self._db()) as db:
            rows = {row["slot_id"]: row for row in db.execute("SELECT * FROM connections")}
            slots = []
            connections = []
            for slot_id, slot in self.slots.items():
                if slot_id not in self.config["slots"]:
                    continue
                entries = [self.p.sources.entries[item] for item in slot["input_entry_ids"]]
                entry = entries[0]
                slots.append(
                    {
                        "slot_id": slot_id,
                        "label": slot["label"],
                        "adapter": slot["adapter"],
                        "platform_id": slot["platform_id"],
                        "self_id": slot["self_id"],
                        "namespace": entry["channel"]["namespace"],
                        "conversation_id": entry["channel"]["channel_conversation_id"],
                        "actor_ids": [
                            self.p.auth.entries[item]["actor_id"] for item in entry["actor_entries"]
                        ],
                        "registered_authors": len(entries),
                        "created": slot_id in rows,
                    }
                )
                row = rows.get(slot_id)
                if row:
                    current = True
                    if row["enabled"]:
                        try:
                            self._connection(db, row["id"])
                        except Fault:
                            current = False
                    delivery = {
                        state: db.execute(
                            "SELECT count(*) FROM replies WHERE connection_id=? AND state=?",
                            (row["id"], state),
                        ).fetchone()[0]
                        for state in ("pending", "claimed", "sent", "failed", "unknown")
                    }
                    online = (
                        current
                        and row["enabled"]
                        and row["last_seen_at"] is not None
                        and self.p.origins.clock() - row["last_seen_at"] <= 90
                    )
                    connections.append(
                        {
                            "connection_id": row["id"],
                            "slot_id": slot_id,
                            "adapter": slot["adapter"],
                            "actor_ids": loads(row["actor_ids"]),
                            "enabled": bool(row["enabled"]),
                            "state": "failed"
                            if row["enabled"] and not current
                            else "online"
                            if online
                            else "pending"
                            if row["enabled"]
                            else "disabled",
                            "last_seen_at": utc(row["last_seen_at"])
                            if row["last_seen_at"]
                            else None,
                            "last_event_at": utc(row["last_event_at"])
                            if row["last_event_at"]
                            else None,
                            "last_error": "authorization_revoked"
                            if not current
                            else row["last_error"],
                            "credential": "configured" if row["token_hash"] else "missing",
                            "delivery": delivery,
                        }
                    )
        return {"available": True, "slots": slots, "connections": connections}

    def create(self, slot_id, actor_ids, *, connection_id=None, token=None):
        require(slot_id in self.slots, "not_found", 404)
        require(
            isinstance(actor_ids, list)
            and 1 <= len(actor_ids) <= 32
            and len(actor_ids) == len(set(actor_ids)),
            "invalid_input",
            400,
        )
        slot = self.slots[slot_id]
        entry = self.p.sources.entries[slot["input_entry_ids"][0]]
        allowed = {self.p.auth.entries[item]["actor_id"] for item in entry["actor_entries"]}
        for entry_id in slot["input_entry_ids"][1:]:
            entry = self.p.sources.entries[entry_id]
            allowed &= {self.p.auth.entries[item]["actor_id"] for item in entry["actor_entries"]}
        require(set(actor_ids) <= allowed, "forbidden", 403)
        token = token or secrets.token_urlsafe(48)
        connection_id = connection_id or "bot:" + uuid.uuid4().hex
        with closing(self._db()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO connections VALUES(?,?,?,?,0,?,NULL,NULL,NULL)",
                (
                    connection_id,
                    slot_id,
                    canonical(sorted(actor_ids)),
                    digest(token),
                    self.p.origins.clock(),
                ),
            )
            db.commit()
        return {"connection_id": connection_id, "token": token, "enabled": False}

    def change(self, connection_id, operation):
        require(operation in {"enable", "disable", "rotate"}, "invalid_input", 400)
        with closing(self._db()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM connections WHERE id=?", (connection_id,)).fetchone()
            require(row is not None, "not_found", 404)
            if operation == "rotate":
                token = secrets.token_urlsafe(48)
                db.execute(
                    "UPDATE connections SET token_hash=?,last_seen_at=NULL WHERE id=?",
                    (digest(token), connection_id),
                )
            elif operation == "enable":
                self._connection(db, connection_id, enabled=False)
                self._require_unique_owner(db, row)
                db.execute(
                    "UPDATE connections SET enabled=1,last_error=NULL WHERE id=?", (connection_id,)
                )
                token = None
            else:
                db.execute(
                    "UPDATE connections SET enabled=0,last_seen_at=NULL WHERE id=?",
                    (connection_id,),
                )
                db.execute(
                    "UPDATE replies SET state='unknown',unknown_origin='disable',observed_at=? WHERE connection_id=? AND state IN ('pending','claimed')",
                    (self.p.origins.clock(), connection_id),
                )
                token = None
            db.commit()
        result = {
            "connection_id": connection_id,
            "enabled": operation == "enable" or (operation == "rotate" and bool(row["enabled"])),
        }
        if token:
            result["token"] = token
        return result

    def heartbeat(self, header, body):
        require(set(body) == {"connection_id", "instance_id"}, "invalid_input", 400)
        require(
            isinstance(body["instance_id"], str) and 1 <= len(body["instance_id"]) <= 128,
            "invalid_input",
            400,
        )
        with closing(self._db()) as db:
            db.execute("BEGIN IMMEDIATE")
            self._authenticate(db, header, body["connection_id"])
            at = self.p.origins.clock()
            db.execute(
                "UPDATE connections SET last_seen_at=? WHERE id=?", (at, body["connection_id"])
            )
            db.commit()
        return {"state": "online", "observed_at": utc(at)}

    async def event(self, header, body):
        require(
            set(body)
            == {
                "schema_version",
                "connection_id",
                "platform_id",
                "self_id",
                "event_id",
                "revision",
                "namespace",
                "conversation_id",
                "thread_id",
                "account_id",
                "sent_at",
                "text",
            },
            "invalid_input",
            400,
        )
        require(
            type(body["schema_version"]) is int
            and body["schema_version"] == 1
            and type(body["revision"]) is int
            and body["revision"] == 1,
            "invalid_input",
            400,
        )
        for key in ("event_id", "account_id", "conversation_id", "platform_id", "self_id"):
            require(isinstance(body[key], str) and 1 <= len(body[key]) <= 256, "invalid_input", 400)
        require(
            isinstance(body["text"], str)
            and 1 <= len(body["text"].strip()) <= 8000
            and len(body["text"]) <= 8000,
            "invalid_input",
            400,
        )
        require(isinstance(body["sent_at"], str), "invalid_input", 400)
        epoch(body["sent_at"])
        semantic = digest(body)
        admission = await self.p.local_work.run(self._admit_event, header, body, semantic)
        if "prior" in admission:
            return admission["prior"]
        connection_id = admission["connection_id"]
        message_id = admission["message_id"]
        entry_id = admission["entry_id"]
        entry = admission["entry"]
        channel = entry["channel"]
        data = {
            "message_key": {"channel": channel, "message_id": message_id, "revision": 1},
            "author": entry["account"],
            "sent_at": body["sent_at"],
            "kind": "message",
            "parts": [{"kind": "text", "text": body["text"]}],
            "reply_refs": [],
            "mentioned_accounts": [],
        }
        result, state = [], "unknown"
        try:
            platform_header = self._platform_header()
            origin = await self.p.local_work.run(
                self.p.sources.register_input, platform_header, entry_id, data
            )
            ingest = {
                "schema_version": 1,
                "command": {
                    "schema_version": 1,
                    "request_id": "request:" + uuid.uuid4().hex,
                    "idempotency_key": "bot:"
                    + digest([connection_id, body["event_id"], body["account_id"]]),
                    "origin": {"assertion_ref": origin["assertion_ref"]},
                    "deadline_at": utc(self.p.origins.clock() + 30),
                },
                "input": data,
                "target_actor_ids": admission["actor_ids"],
            }
            answer = await self.p.sources.dispatch(platform_header, ingest)
            result = [
                {"actor_id": item["actor_id"], "state": item["state"]}
                for item in answer["outcomes"]
            ]
            state = (
                "accepted"
                if any(item["state"] in {"accepted", "duplicate"} for item in result)
                else "not_started"
            )
        except (Fault, OSError, sqlite3.Error):
            pass
        await self.p.local_work.run(
            self._finish_event, connection_id, body["event_id"], body["account_id"], state, result
        )
        return {
            "event_id": body["event_id"],
            "message_id": message_id,
            "state": state,
            "outcomes": result,
        }

    def _admit_event(self, header, body, semantic):
        with closing(self._db()) as db:
            db.execute("BEGIN IMMEDIATE")
            row, slot, entries = self._authenticate(db, header, body["connection_id"])
            require(
                body["platform_id"] == slot["platform_id"] and body["self_id"] == slot["self_id"],
                "forbidden",
                403,
            )
            entry_id = next(
                (
                    item
                    for item in slot["input_entry_ids"]
                    if self.p.sources.entries[item]["account"]["immutable_account_id"]
                    == body["account_id"]
                ),
                None,
            )
            require(entry_id is not None, "forbidden", 403)
            entry = self.p.sources.entries[entry_id]
            channel = entry["channel"]
            require(
                body["namespace"] == channel["namespace"]
                and body["conversation_id"] == channel["channel_conversation_id"]
                and body["thread_id"] == channel["thread_id"]
                and body["account_id"] == entry["account"]["immutable_account_id"],
                "forbidden",
                403,
            )
            old = db.execute(
                "SELECT * FROM events WHERE connection_id=? AND event_id=? AND account_id=?",
                (row["id"], body["event_id"], body["account_id"]),
            ).fetchone()
            if old:
                require(old["semantic"] == semantic, "idempotency_conflict", 409)
                return {
                    "prior": {
                        "event_id": body["event_id"],
                        "message_id": old["message_id"],
                        "state": old["state"],
                        "outcomes": loads(old["result"]) if old["result"] else [],
                    }
                }
            message_id = "message:" + uuid.uuid4().hex
            db.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,NULL)",
                (row["id"], body["event_id"], body["account_id"], semantic, message_id, "unknown"),
            )
            db.commit()
        return {
            "connection_id": row["id"],
            "message_id": message_id,
            "entry_id": entry_id,
            "entry": entry,
            "actor_ids": loads(row["actor_ids"]),
        }

    def _finish_event(self, connection_id, event_id, account_id, state, result):
        with closing(self._db()) as db:
            db.execute(
                "UPDATE events SET state=?,result=? WHERE connection_id=? AND event_id=? AND account_id=?",
                (state, canonical(result), connection_id, event_id, account_id),
            )
            if state == "accepted":
                db.execute(
                    "UPDATE connections SET last_event_at=?,last_error=NULL WHERE id=?",
                    (self.p.origins.clock(), connection_id),
                )
            elif state == "unknown":
                db.execute(
                    "UPDATE connections SET last_error='admission_unknown' WHERE id=?",
                    (connection_id,),
                )
            db.commit()

    def event_status(self, header, body):
        require(set(body) == {"connection_id", "event_id", "account_id"}, "invalid_input", 400)
        with closing(self._db()) as db:
            self._authenticate(db, header, body["connection_id"])
            row = db.execute(
                "SELECT * FROM events WHERE connection_id=? AND event_id=? AND account_id=?",
                (body["connection_id"], body["event_id"], body["account_id"]),
            ).fetchone()
            return {
                "found": row is not None,
                "state": row["state"] if row else None,
                "message_id": row["message_id"] if row else None,
                "outcomes": loads(row["result"]) if row and row["result"] else [],
            }

    def _receipt(self, row, request_id):
        receipt = {
            "schema_version": 1,
            "request_id": request_id,
            "reply_id": row["reply_id"],
            "segment_sequence": row["segment_sequence"],
            "attempt_id": row["attempt_id"],
            "state": row["state"] if row["state"] in {"sent", "failed"} else "unknown",
            "channel_message_ids": loads(row["channel_message_ids"]),
            "observed_at": utc(row["observed_at"]),
            "retry_safe": False,
        }
        self.p.contracts.check("conversation#send_receipt", receipt)
        return receipt

    def send(self, header, request):
        p = self.p
        p.contracts.check("conversation#send_request", request)
        require(len(canonical(request).encode()) <= 65536, "budget_exceeded", 413)
        command = request["command"]
        require(epoch(command["deadline_at"]) > p.origins.clock(), "timeout", 408)
        require(
            request["segment_sequence"] <= request["segment_count"] <= 100, "invalid_input", 400
        )
        with p.store.connect(write=True) as authority:
            principal_id, principal = p.auth.authenticate(header, authority, "dialogue.send")
            require(principal["kind"] == "service" and principal["service"] == "companion")
            actor_entry_id, actor, context = p.origins.context(
                authority, command["origin"]["assertion_ref"], "companion", "platform", "dialogue"
            )
            scope = context["allowed_scope"]
            require(
                request["destination"] == actor["channel"]
                and request["actor_id"] == scope["actor_id"]
                and request["conversation_id"] == scope["conversation_id"]
                and scope["person_id"] is not None
                and request["destination"]["namespace"] in {"qq", "tg"},
                "forbidden",
                403,
            )
            semantic = digest(
                {
                    "delivery": {key: value for key, value in request.items() if key != "command"},
                    "scope": scope,
                }
            )
            command_key = canonical([principal_id, command["idempotency_key"]])
        with closing(self._db()) as db:
            db.execute("BEGIN IMMEDIATE")
            candidates = []
            for row in db.execute("SELECT * FROM connections WHERE enabled=1"):
                slot, input_entries = self._slot(row)
                if any(
                    input_entry["channel"] == actor["channel"]
                    and actor_entry_id in input_entry["actor_entries"]
                    for input_entry in input_entries
                ) and request["actor_id"] in loads(row["actor_ids"]):
                    candidates.append(row)
            require(len(candidates) == 1, "forbidden", 403)
            connection = candidates[0]
            self._require_unique_owner(db, connection)
            prior_command = db.execute(
                "SELECT reply_id FROM replies WHERE command_key=?", (command_key,)
            ).fetchone()
            require(
                prior_command is None or prior_command["reply_id"] == request["reply_id"],
                "idempotency_conflict",
                409,
            )
            prior = db.execute(
                "SELECT * FROM replies WHERE reply_id=?", (request["reply_id"],)
            ).fetchone()
            if prior:
                require(
                    prior["semantic"] == semantic and prior["command_key"] == command_key,
                    "idempotency_conflict",
                    409,
                )
                return self._receipt(prior, command["request_id"])
            require(
                not db.execute(
                    "SELECT 1 FROM replies WHERE conversation_id=? AND actor_id=? AND turn_id=? AND segment_sequence=?",
                    (
                        request["conversation_id"],
                        request["actor_id"],
                        request["turn_id"],
                        request["segment_sequence"],
                    ),
                ).fetchone(),
                "idempotency_conflict",
                409,
            )
            now = p.origins.clock()
            db.execute(
                "INSERT INTO replies VALUES(?,?,?,?,?,?,?,?,?,?,'pending',?, '[]',NULL,?,NULL)",
                (
                    request["reply_id"],
                    command_key,
                    semantic,
                    canonical(scope),
                    connection["id"],
                    request["conversation_id"],
                    request["actor_id"],
                    request["turn_id"],
                    request["segment_sequence"],
                    canonical(request),
                    "attempt:" + uuid.uuid4().hex,
                    now,
                ),
            )
            db.commit()
            row = db.execute(
                "SELECT * FROM replies WHERE reply_id=?", (request["reply_id"],)
            ).fetchone()
            return self._receipt(row, command["request_id"])

    def reply_status(self, header, request):
        self.p.contracts.check("conversation#send_request", request)
        with self.p.store.connect(write=True) as authority:
            principal_id, principal = self.p.auth.authenticate(header, authority, "dialogue.send")
            require(principal["kind"] == "service" and principal["service"] == "companion")
            actor_entry_id, actor, context = self.p.origins.context(
                authority,
                request["command"]["origin"]["assertion_ref"],
                "companion",
                "platform",
                "dialogue",
            )
            scope = context["allowed_scope"]
            require(
                actor_entry_id in self.p.auth.entries
                and request["destination"] == actor["channel"]
                and request["actor_id"] == scope["actor_id"]
                and request["conversation_id"] == scope["conversation_id"],
                "forbidden",
                403,
            )
        with closing(self._db()) as db:
            row = db.execute(
                "SELECT * FROM replies WHERE reply_id=?", (request["reply_id"],)
            ).fetchone()
            if row is None:
                return {"receipt": None}
            semantic = digest(
                {
                    "delivery": {key: value for key, value in request.items() if key != "command"},
                    "scope": scope,
                }
            )
            require(
                row["semantic"] == semantic
                and row["command_key"]
                == canonical([principal_id, request["command"]["idempotency_key"]]),
                "idempotency_conflict",
                409,
            )
            return {
                "receipt": self._receipt(row, request["command"]["request_id"])
                if row["state"] in {"sent", "failed", "unknown"}
                else None
            }

    def claim(self, header, body):
        require(set(body) == {"connection_id", "instance_id", "limit"}, "invalid_input", 400)
        require(
            isinstance(body["instance_id"], str)
            and 1 <= len(body["instance_id"]) <= 128
            and type(body["limit"]) is int
            and 1 <= body["limit"] <= 20,
            "invalid_input",
            400,
        )
        with closing(self._db()) as db:
            db.execute("BEGIN IMMEDIATE")
            row, slot, entries = self._authenticate(db, header, body["connection_id"])
            now = self.p.origins.clock()
            db.execute(
                "UPDATE replies SET state='unknown',unknown_origin='lease',observed_at=? WHERE connection_id=? AND state='claimed' AND claimed_at<?",
                (now, row["id"], now - 60),
            )
            pending = list(
                db.execute(
                    "SELECT * FROM replies WHERE connection_id=? AND state='pending' ORDER BY observed_at,reply_id LIMIT ?",
                    (row["id"], body["limit"]),
                )
            )
            deliveries = []
            for reply in pending:
                db.execute(
                    "UPDATE replies SET state='claimed',claimed_at=? WHERE reply_id=? AND state='pending'",
                    (now, reply["reply_id"]),
                )
                request = loads(reply["request"])
                channel = request["destination"]
                deliveries.append(
                    {
                        "reply_id": reply["reply_id"],
                        "attempt_id": reply["attempt_id"],
                        "namespace": channel["namespace"],
                        "conversation_id": channel["channel_conversation_id"],
                        "thread_id": channel["thread_id"],
                        "text": request["text"],
                        "turn_id": request["turn_id"],
                        "segment_sequence": request["segment_sequence"],
                    }
                )
            db.execute("UPDATE connections SET last_seen_at=? WHERE id=?", (now, row["id"]))
            db.commit()
        return {"deliveries": deliveries}

    def claim_status(self, header, body):
        require(set(body) == {"connection_id", "reply_id", "attempt_id"}, "invalid_input", 400)
        with closing(self._db()) as db:
            self._authenticate(db, header, body["connection_id"], settlement=True)
            row = db.execute(
                "SELECT * FROM replies WHERE connection_id=? AND reply_id=?",
                (body["connection_id"], body["reply_id"]),
            ).fetchone()
            require(row is not None, "not_found", 404)
            require(row["attempt_id"] == body["attempt_id"], "forbidden", 403)
            expired = (
                row["state"] == "claimed"
                and row["claimed_at"] is not None
                and row["claimed_at"] < self.p.origins.clock() - 60
            )
            return {"state": "unknown" if expired else row["state"]}

    def ack(self, header, body):
        require(
            set(body)
            == {"connection_id", "reply_id", "attempt_id", "state", "channel_message_ids"},
            "invalid_input",
            400,
        )
        require(body["state"] in {"sent", "failed", "unknown"}, "invalid_input", 400)
        ids = body["channel_message_ids"]
        require(
            isinstance(ids, list)
            and len(ids) <= 20
            and all(isinstance(item, str) and 1 <= len(item) <= 256 for item in ids),
            "invalid_input",
            400,
        )
        require((body["state"] == "sent") == bool(ids), "invalid_input", 400)
        with closing(self._db()) as db:
            db.execute("BEGIN IMMEDIATE")
            self._authenticate(db, header, body["connection_id"], settlement=True)
            row = db.execute(
                "SELECT * FROM replies WHERE reply_id=? AND connection_id=?",
                (body["reply_id"], body["connection_id"]),
            ).fetchone()
            require(row is not None, "not_found", 404)
            require(row["attempt_id"] == body["attempt_id"], "forbidden", 403)
            if row["state"] in {"sent", "failed"} or (
                row["state"] == "unknown" and row["unknown_origin"] == "sdk"
            ):
                require(
                    row["state"] == body["state"] and loads(row["channel_message_ids"]) == ids,
                    "idempotency_conflict",
                    409,
                )
            else:
                require(
                    row["state"] == "claimed"
                    or (
                        row["state"] == "unknown" and row["unknown_origin"] in {"lease", "disable"}
                    ),
                    "forbidden",
                    403,
                )
                db.execute(
                    "UPDATE replies SET state=?,channel_message_ids=?,observed_at=?,unknown_origin=? WHERE reply_id=?",
                    (
                        body["state"],
                        canonical(ids),
                        self.p.origins.clock(),
                        "sdk" if body["state"] == "unknown" else None,
                        body["reply_id"],
                    ),
                )
            db.commit()
        return {"reply_id": body["reply_id"], "state": body["state"]}

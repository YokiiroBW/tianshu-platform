"""Durable web delivery through the published, service-authenticated sender port."""

import sqlite3
import uuid
from contextlib import closing

from .contracts import Fault, canonical, digest, epoch, loads, require, utc


class WebSender:
    def __init__(self, platform):
        self.platform = platform
        # Dedicated additive sidecar: no migration of the authority store.
        self.path = platform.store.path + ".web-replies.sqlite"
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            require(
                db.execute("PRAGMA user_version").fetchone()[0] in (0, 1),
                "dependency_unavailable",
                503,
            )
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS replies (
                    reply_id TEXT PRIMARY KEY, semantic TEXT NOT NULL,
                    account TEXT NOT NULL, channel TEXT NOT NULL, actor_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                    segment_sequence INTEGER NOT NULL, request TEXT NOT NULL,
                    receipt TEXT NOT NULL,
                    command_key TEXT NOT NULL UNIQUE,
                    UNIQUE(conversation_id, actor_id, turn_id, segment_sequence)
                );
                PRAGMA user_version=1;
            """)

    def send(self, header, request):
        p = self.platform
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
            actor_entry_id, entry, context = p.origins.context(
                authority, command["origin"]["assertion_ref"], "companion", "platform", "dialogue"
            )
            scope = context["allowed_scope"]
            require(entry["channel"]["namespace"] == "web" and entry["audience"] == "self_private")
            require(
                request["destination"] == entry["channel"]
                and request["actor_id"] == scope["actor_id"]
                and request["conversation_id"] == scope["conversation_id"]
                and scope["person_id"] is not None
            )
            c = p.settings.get("web")
            require(c is not None and entry["owner"] == c["principal"])
            allowed = False
            for entry_id in c["input_entries"]:
                if entry_id not in p.sources.entries:
                    continue
                try:
                    candidate = p.sources._entry(authority, entry_id)
                except Fault as exc:
                    if exc.status != 403:
                        raise
                    continue
                if (
                    candidate["account"] == entry["account"]
                    and candidate["channel"] == entry["channel"]
                    and actor_entry_id in candidate["actor_entries"]
                ):
                    allowed = True
            require(allowed)
            # Request correlation/deadline/origin may refresh; delivery semantics may not.
            semantic = digest(
                {
                    "delivery": {k: v for k, v in request.items() if k != "command"},
                    "account": entry["account"],
                    "scope": scope,
                }
            )
            command_key = canonical([principal_id, command["idempotency_key"]])
            with closing(sqlite3.connect(self.path, timeout=5)) as db:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                require(epoch(command["deadline_at"]) > p.origins.clock(), "timeout", 408)
                p.origins.context(
                    authority,
                    command["origin"]["assertion_ref"],
                    "companion",
                    "platform",
                    "dialogue",
                )
                same_command = db.execute(
                    "SELECT reply_id FROM replies WHERE command_key=?", (command_key,)
                ).fetchone()
                require(
                    same_command is None or same_command[0] == request["reply_id"],
                    "idempotency_conflict",
                    409,
                )
                prior = db.execute(
                    "SELECT semantic,receipt FROM replies WHERE reply_id=?", (request["reply_id"],)
                ).fetchone()
                if prior:
                    require(prior[0] == semantic, "idempotency_conflict", 409)
                    receipt = loads(prior[1])
                    receipt["request_id"] = command["request_id"]
                    return receipt
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
                receipt = {
                    "schema_version": 1,
                    "request_id": command["request_id"],
                    "reply_id": request["reply_id"],
                    "segment_sequence": request["segment_sequence"],
                    "attempt_id": "attempt:" + uuid.uuid4().hex,
                    "state": "sent",
                    "channel_message_ids": ["web:" + uuid.uuid4().hex],
                    "observed_at": utc(p.origins.clock()),
                    "retry_safe": False,
                }
                p.contracts.check("conversation#send_receipt", receipt)
                db.execute(
                    "INSERT INTO replies VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        request["reply_id"],
                        semantic,
                        canonical(entry["account"]),
                        canonical(entry["channel"]),
                        request["actor_id"],
                        request["conversation_id"],
                        request["turn_id"],
                        request["segment_sequence"],
                        canonical(request),
                        canonical(receipt),
                        command_key,
                    ),
                )
                db.commit()
                return receipt

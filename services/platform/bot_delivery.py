"""Incremental expressions over Bots' existing durable adapter queue.

Expression rows describe append/closure and ownership. The replies table remains the only
execution/receipt ledger; responses, Direct and proactive share its claim/ack path.
"""

import base64
import binascii
import hashlib
import uuid
from contextlib import closing

from .contracts import Fault, canonical, digest, loads, require, utc, epoch

PREFIX = "/internal/v2/bot-delivery/"
ROUTES = {PREFIX + name for name in ("send", "query", "cancel", "finalize", "context")}


class Delivery:
    def __init__(self, bots):
        self.bots, self.p = bots, bots.p
        self.p.contracts.load_runtime("bot-delivery/v2.1")
        with closing(bots._db()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS expressions (
                    id TEXT PRIMARY KEY, principal TEXT NOT NULL, origin TEXT NOT NULL,
                    scope TEXT NOT NULL, channel TEXT NOT NULL, entry_id TEXT NOT NULL,
                    connection_id TEXT NOT NULL, final INTEGER NOT NULL DEFAULT 0,
                    cancelled INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS expression_segments (
                    expression_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                    segment_id TEXT NOT NULL, reply_id TEXT NOT NULL UNIQUE,
                    semantic TEXT NOT NULL,
                    PRIMARY KEY(expression_id,sequence), UNIQUE(expression_id,segment_id)
                );
                CREATE TABLE IF NOT EXISTS expression_operations (
                    principal TEXT NOT NULL, request_id TEXT NOT NULL,
                    expression_id TEXT NOT NULL, operation TEXT NOT NULL,
                    semantic TEXT NOT NULL, PRIMARY KEY(principal,request_id)
                );
                PRAGMA user_version=2;
            """)

    def _caller(self, db, header):
        identity, principal = self.p.auth.authenticate(header, db, "dialogue.send")
        require(principal["kind"] == "service" and principal["service"] == "companion")
        return identity

    def context(self, header, request):
        """Revalidate a recipient and continue its lease without admitting user input."""
        self.p.contracts.check("bot-delivery#context_request", request)
        with (
            self.bots._managed_guard(),
            self.p.store.connect(write=True) as authority,
            closing(self.bots._db()) as queue,
        ):
            principal = self._caller(authority, header)
            entry_id, entry = self._entry(
                authority,
                request["origin"],
                request["scope"],
                request["channel"],
                allow_expired=True,
            )
            self.p.auth.route(entry, "companion", "memory", "dialogue")
            self._connection(authority, queue, entry_id, entry, request["scope"])
            if request["origin"]["kind"] in {"response", "direct"}:
                ref = request["origin"]["origin"]["assertion_ref"]
                now = self.p.origins.clock()
                expires = min(
                    now + entry["ttl_seconds"],
                    epoch(entry["expires_at"]) if "expires_at" in entry else float("inf"),
                )
                require(expires > now, "forbidden", 403)
                authority.execute(
                    "UPDATE origins SET expires_at=MAX(expires_at,?) WHERE ref=?", (expires, ref)
                )
                authority.execute(
                    "INSERT INTO audit(principal,operation,object_id,observed_at) VALUES(?,?,?,?)",
                    (principal, "delivery.origin.continue", digest(ref), now),
                )
                issued = {
                    "assertion_ref": ref,
                    "expires_at": utc(
                        authority.execute(
                            "SELECT expires_at FROM origins WHERE ref=?", (ref,)
                        ).fetchone()[0]
                    ),
                }
            else:
                issued = self.p.origins.issue_registered(authority, principal, entry_id, entry)
            result = {
                "schema_version": 2,
                "request_id": request["request_id"],
                "origin": {"assertion_ref": issued["assertion_ref"]},
                "expires_at": issued["expires_at"],
                "scope": request["scope"],
                "channel": entry["channel"],
            }
            self.p.contracts.check("bot-delivery#context_response", result)
            return result

    def _entry(self, db, origin, scope, channel, entry_id=None, *, allow_expired=False):
        require(
            scope["person_id"] is not None
            and scope["conversation_id"] is not None
            and origin["actor_id"] == scope["actor_id"],
            "forbidden",
            403,
        )
        if origin["kind"] in {"response", "direct"}:
            if allow_expired:
                row = db.execute(
                    "SELECT * FROM origins WHERE ref=?", (origin["origin"]["assertion_ref"],)
                ).fetchone()
                require(row is not None and not row["revoked"], "forbidden", 403)
                selected = row["entry_id"]
                entry = self.p.auth.entry(db, selected, row["entry_digest"])
                self.p.auth.route(entry, "companion", "platform", "dialogue")
                current_scope = self.p.origins.scope(db, entry)
            else:
                selected, entry, context = self.p.origins.context(
                    db, origin["origin"]["assertion_ref"], "companion", "platform", "dialogue"
                )
                current_scope = context["allowed_scope"]
            require(current_scope == scope and entry["channel"] == channel)
            require(entry_id is None or selected == entry_id, "scope_changed", 409)
            return selected, entry
        candidates = []
        for key in [entry_id] if entry_id else self.p.auth.entries:
            try:
                entry = self.p.auth.entry(db, key)
                self.p.auth.route(entry, "companion", "platform", "dialogue")
            except Fault as exc:
                if exc.status == 403:
                    continue
                raise
            if entry["channel"] == channel and self.p.origins.scope(db, entry) == scope:
                candidates.append((key, entry))
        require(len(candidates) == 1, "forbidden", 403)
        return candidates[0]

    def _connection(self, authority, queue, entry_id, entry, scope):
        if entry["channel"]["namespace"] == "web":
            config = self.p.settings.get("web") or {}
            require(
                entry["owner"] == config.get("principal") and entry["audience"] == "self_private"
            )
            candidates = [
                self.p.sources._entry(authority, key)
                for key, source in self.p.sources.entries.items()
                if source["owner"] == config.get("principal")
            ]
            require(
                any(
                    candidate["account"] == entry["account"]
                    and candidate["channel"] == entry["channel"]
                    and entry_id in candidate["actor_entries"]
                    for candidate in candidates
                )
            )
            return "web:" + digest([entry["owner"], entry["channel"], scope["actor_id"]])
        rows = []
        for row in queue.execute("SELECT * FROM connections WHERE enabled=1"):
            _, inputs = self.bots._slot(row)
            if (
                scope["actor_id"] in loads(row["actor_ids"])
                and self.bots._managed_active(row)
                and any(
                    item["channel"] == entry["channel"] and entry_id in item["actor_entries"]
                    for item in inputs
                )
            ):
                self.bots._connection(queue, row["id"], authority=authority)
                rows.append(row)
        require(len(rows) == 1, "forbidden", 403)
        return rows[0]["id"]

    def _owned(self, authority, queue, header, expression_id, origin=None):
        principal = self._caller(authority, header)
        row = queue.execute("SELECT * FROM expressions WHERE id=?", (expression_id,)).fetchone()
        if row is None:
            return principal, None
        require(row["principal"] == principal, "not_found", 404)
        stored = loads(row["origin"])
        if origin is not None:
            require(canonical(origin) == row["origin"], "idempotency_conflict", 409)
            entry_id, entry = self._entry(
                authority, origin, loads(row["scope"]), loads(row["channel"]), row["entry_id"]
            )
        else:
            # Receipt reads do not revive expired origins. The current registered scope and
            # adapter permission still have to agree with the originally admitted expression.
            entry_id = row["entry_id"]
            entry = self.p.auth.entry(authority, entry_id)
            self.p.auth.route(entry, "companion", "platform", "dialogue")
            require(
                self.p.origins.scope(authority, entry) == loads(row["scope"]), "scope_changed", 409
            )
            require(
                entry["channel"] == loads(row["channel"])
                and stored["actor_id"] == entry["actor_id"]
            )
        connection = self._connection(authority, queue, entry_id, entry, loads(row["scope"]))
        require(connection == row["connection_id"], "scope_changed", 409)
        return principal, row

    def _operation(self, queue, principal, operation, request):
        fingerprint = digest({key: value for key, value in request.items() if key != "request_id"})
        previous = queue.execute(
            "SELECT * FROM expression_operations WHERE principal=? AND request_id=?",
            (principal, request["request_id"]),
        ).fetchone()
        if previous:
            require(
                previous["expression_id"] == request["expression_id"]
                and previous["operation"] == operation
                and previous["semantic"] == fingerprint,
                "idempotency_conflict",
                409,
            )
            return True
        queue.execute(
            "INSERT INTO expression_operations VALUES(?,?,?,?,?)",
            (
                principal,
                request["request_id"],
                request["expression_id"],
                operation,
                fingerprint,
            ),
        )
        return False

    def _receipt(self, db, row, request_id):
        parts = []
        for item in db.execute(
            "SELECT s.*,r.state,r.attempt_id,r.channel_message_ids,r.observed_at FROM expression_segments s JOIN replies r ON r.reply_id=s.reply_id WHERE s.expression_id=? ORDER BY s.sequence",
            (row["id"],),
        ):
            state = {"pending": "queued", "claimed": "sending"}.get(item["state"], item["state"])
            parts.append(
                {
                    "segment_id": item["segment_id"],
                    "reply_id": item["reply_id"],
                    "segment_sequence": item["sequence"],
                    "state": state,
                    "receipt_id": item["attempt_id"]
                    if state in {"sent", "failed", "unknown", "cancelled"}
                    else None,
                    "retry_safe": False,
                    "channel_message_ids": loads(item["channel_message_ids"]),
                }
            )
        states = {item["state"] for item in parts}
        if "unknown" in states:
            state = "unknown"
        elif "sending" in states:
            state = "sending"
        elif "queued" in states:
            state = "sending" if "sent" in states else "queued"
        elif "sent" in states:
            state = "partial" if states != {"sent"} else "sent" if row["final"] else "sending"
        elif states == {"failed"}:
            state = "failed"
        elif row["cancelled"] or states == {"cancelled"}:
            state = "cancelled"
        else:
            state = "sending"
        result = {
            "schema_version": 2,
            "request_id": request_id,
            "expression_id": row["id"],
            "final": bool(row["final"]),
            "state": state,
            "segments": parts,
            "observed_at": utc(self.p.origins.clock()),
        }
        self.p.contracts.check("bot-delivery#send_receipt", result)
        return result

    def send(self, header, request):
        self.p.contracts.check("bot-delivery#send_request", request)
        require(len(canonical(request).encode()) <= 45 * 1024 * 1024, "budget_exceeded", 413)
        with (
            self.bots._managed_guard(),
            self.p.store.connect(write=True) as authority,
            closing(self.bots._db()) as db,
        ):
            db.execute("BEGIN IMMEDIATE")
            principal, row = self._owned(
                authority, db, header, request["expression_id"], request["origin"]
            )
            if row is None:
                entry_id, entry = self._entry(
                    authority, request["origin"], request["scope"], request["channel"]
                )
                connection = self._connection(authority, db, entry_id, entry, request["scope"])
                db.execute(
                    "INSERT INTO expressions VALUES(?,?,?,?,?,?,?,0,0,?)",
                    (
                        request["expression_id"],
                        principal,
                        canonical(request["origin"]),
                        canonical(request["scope"]),
                        canonical(request["channel"]),
                        entry_id,
                        connection,
                        self.p.origins.clock(),
                    ),
                )
                row = db.execute(
                    "SELECT * FROM expressions WHERE id=?", (request["expression_id"],)
                ).fetchone()
            require(
                row["scope"] == canonical(request["scope"])
                and row["channel"] == canonical(request["channel"]),
                "idempotency_conflict",
                409,
            )
            replay = self._operation(db, principal, "send", request)
            media_count, media_size = 0, 0
            for stored in db.execute(
                "SELECT r.request FROM expression_segments s JOIN replies r ON r.reply_id=s.reply_id WHERE s.expression_id=?",
                (row["id"],),
            ):
                for media in loads(stored[0]).get("media", []):
                    media_count += 1
                    media_size += len(base64.b64decode(media["data"], validate=True))
            for segment in request["segments"]:
                previous = db.execute(
                    "SELECT * FROM expression_segments WHERE expression_id=? AND sequence=?",
                    (row["id"], segment["segment_sequence"]),
                ).fetchone()
                semantic = digest(segment)
                if previous:
                    require(previous["semantic"] == semantic, "idempotency_conflict", 409)
                    continue
                require(
                    not replay and not row["final"] and not row["cancelled"],
                    "idempotency_conflict",
                    409,
                )
                count = db.execute(
                    "SELECT COUNT(*) FROM expression_segments WHERE expression_id=?", (row["id"],)
                ).fetchone()[0]
                require(
                    count < 64 and segment["segment_sequence"] == count + 1,
                    "idempotency_conflict",
                    409,
                )
                require(
                    not db.execute(
                        "SELECT 1 FROM replies WHERE reply_id=?", (segment["reply_id"],)
                    ).fetchone(),
                    "idempotency_conflict",
                    409,
                )
                require(
                    not db.execute(
                        "SELECT 1 FROM expression_segments WHERE expression_id=? AND segment_id=?",
                        (row["id"], segment["segment_id"]),
                    ).fetchone(),
                    "idempotency_conflict",
                    409,
                )
                materialized = segment.get("media", [])
                for media in materialized:
                    reference = media["content_ref"]
                    require(reference in segment["content_refs"], "invalid_input", 400)
                    expected_kind = (
                        "image"
                        if media["media_type"].startswith("image/")
                        else "audio"
                        if media["media_type"].startswith("audio/")
                        else "video"
                    )
                    require(
                        reference["kind"] == expected_kind
                        and reference["sha256"] == media["sha256"],
                        "invalid_input",
                        400,
                    )
                    try:
                        raw = base64.b64decode(media["data"], validate=True)
                    except (ValueError, binascii.Error):
                        raise Fault("invalid_input", 400) from None
                    require(
                        raw and hashlib.sha256(raw).hexdigest() == media["sha256"],
                        "invalid_input",
                        400,
                    )
                    media_count += 1
                    media_size += len(raw)
                    require(
                        media_count <= 4 and media_size <= 32 * 1024 * 1024, "budget_exceeded", 413
                    )
                require(
                    all(
                        ref["kind"] not in {"image", "audio", "video"}
                        or any(item["content_ref"] == ref for item in materialized)
                        for ref in segment["content_refs"]
                    ),
                    "invalid_input",
                    400,
                )
                sequence = db.execute(
                    "SELECT COALESCE(MAX(segment_sequence),0)+1 FROM replies WHERE conversation_id=? AND actor_id=?",
                    (request["scope"]["conversation_id"], request["scope"]["actor_id"]),
                ).fetchone()[0]
                wire = {
                    "destination": request["channel"],
                    "text": segment["text"],
                    "turn_id": request["origin"].get("turn_id", row["id"]),
                    "segment_sequence": sequence,
                    "content_refs": segment["content_refs"],
                    "media": materialized,
                }
                web = request["channel"]["namespace"] == "web"
                now = self.p.origins.clock()
                db.execute(
                    "INSERT INTO replies VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        segment["reply_id"],
                        canonical([principal, row["id"], segment["segment_id"]]),
                        semantic,
                        row["scope"],
                        row["connection_id"],
                        request["scope"]["conversation_id"],
                        request["scope"]["actor_id"],
                        wire["turn_id"],
                        sequence,
                        canonical(wire),
                        "sent" if web else "pending",
                        "attempt:" + uuid.uuid4().hex,
                        canonical(["web:" + uuid.uuid4().hex]) if web else "[]",
                        None,
                        now,
                        None,
                    ),
                )
                db.execute(
                    "INSERT INTO expression_segments VALUES(?,?,?,?,?)",
                    (
                        row["id"],
                        segment["segment_sequence"],
                        segment["segment_id"],
                        segment["reply_id"],
                        semantic,
                    ),
                )
            if request["final"]:
                db.execute("UPDATE expressions SET final=1 WHERE id=?", (row["id"],))
            row = db.execute("SELECT * FROM expressions WHERE id=?", (row["id"],)).fetchone()
            result = self._receipt(db, row, request["request_id"])
            db.commit()
            return result

    def control(self, header, request, operation):
        self.p.contracts.check(
            "bot-delivery#finalize_request"
            if operation == "finalize"
            else "bot-delivery#lookup_request",
            request,
        )
        with (
            self.bots._managed_guard(),
            self.p.store.connect(write=True) as authority,
            closing(self.bots._db()) as db,
        ):
            db.execute("BEGIN IMMEDIATE")
            principal, row = self._owned(
                authority, db, header, request["expression_id"], request.get("origin")
            )
            if row is not None and operation != "query":
                self._operation(db, principal, operation, request)
                if operation == "cancel":
                    # Only pending jobs are proven not dispatched; claimed jobs may have
                    # reached the adapter and must retain their current/unknown outcome.
                    db.execute(
                        "UPDATE replies SET state='cancelled',observed_at=? WHERE state='pending' AND reply_id IN (SELECT reply_id FROM expression_segments WHERE expression_id=?)",
                        (self.p.origins.clock(), row["id"]),
                    )
                    db.execute(
                        "UPDATE expressions SET final=1,cancelled=1 WHERE id=?", (row["id"],)
                    )
                else:
                    db.execute("UPDATE expressions SET final=1 WHERE id=?", (row["id"],))
                row = db.execute("SELECT * FROM expressions WHERE id=?", (row["id"],)).fetchone()
            result = {
                "schema_version": 2,
                "request_id": request["request_id"],
                "expression_id": request["expression_id"],
                "receipt": self._receipt(db, row, request["request_id"]) if row else None,
            }
            self.p.contracts.check("bot-delivery#lookup_response", result)
            db.commit()
            return result

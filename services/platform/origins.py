"""Authenticated rehearsal issuance and receiver-specific delegation, with live checks."""

import uuid

from .contracts import canonical, digest, epoch, loads, require, utc


def channel_key(entry):
    # The core owns a channel's conversation, independently of the speaker/actor.
    return canonical(entry["channel"])


def source_key(message_key):
    return canonical({k: v for k, v in message_key.items() if k != "revision"})


class Origins:
    def __init__(self, store, auth, contracts, clock):
        self.store, self.auth, self.contracts, self.clock = store, auth, contracts, clock

    def issue(self, header, entry_id):
        with self.store.connect(write=True) as db:
            identity, _ = self.auth.authenticate(header, db, "origin.issue")
            entry = self.auth.entry(db, entry_id)
            require(entry["owner"] == identity)
            ref = "origin:" + uuid.uuid4().hex
            expires = min(
                self.clock() + entry["ttl_seconds"],
                epoch(entry["expires_at"]) if "expires_at" in entry else float("inf"),
            )
            db.execute(
                "INSERT INTO origins(ref,entry_id,entry_digest,expires_at) VALUES(?,?,?,?)",
                (ref, entry_id, digest(entry), expires),
            )
            db.execute(
                "INSERT INTO audit(principal,operation,object_id,observed_at) VALUES(?,?,?,?)",
                (identity, "origin.issue", ref, self.clock()),
            )
            return {"assertion_ref": ref, "expires_at": utc(expires), "mode": self.auth.mode}

    def scope(self, db, entry):
        identity = db.execute(
            "SELECT person_id FROM identities WHERE account=?", (canonical(entry["account"]),)
        ).fetchone()
        channel = db.execute(
            "SELECT conversation_id FROM channels WHERE channel=?", (channel_key(entry),)
        ).fetchone()
        return {
            "actor_id": entry["actor_id"],
            "audience": entry["audience"],
            "person_id": identity[0] if identity else None,
            "conversation_id": channel[0] if channel else None,
        }

    def context(self, db, ref, caller, receiver, purpose):
        row = db.execute("SELECT * FROM origins WHERE ref=?", (ref,)).fetchone()
        require(row is not None and not row["revoked"])
        require(row["expires_at"] > self.clock())
        entry = self.auth.entry(db, row["entry_id"], row["entry_digest"])
        self.auth.route(entry, caller, receiver, purpose)
        owner = self.auth.principals[entry["owner"]]
        context = {
            "issuer": "platform",
            "authenticated_service": caller,
            "audience_service": receiver,
            "assertion_ref": ref,
            "verified_account": entry["account"],
            "principal_id": entry["owner"] if owner["kind"] == "operator" else None,
            "allowed_scope": self.scope(db, entry),
            "verified_channel": entry["channel"],
            "expires_at": utc(row["expires_at"]),
            "revoked": False,
        }
        self.contracts.check("common#trusted_context", context)
        return row["entry_id"], entry, context

    def resolve(self, header, request):
        self.contracts.check("common#origin_resolve_request", request)
        with self.store.connect() as db:
            _, caller = self.auth.authenticate(header, db, "origin.resolve")
            resolver = caller.get("resolver")
            require(resolver is not None)
            _, _, context = self.context(
                db,
                request["assertion_ref"],
                resolver["caller"],
                caller["service"],
                resolver["purpose"],
            )
            result = {"schema_version": 1, "request_id": request["request_id"], "context": context}
            self.contracts.check("common#origin_resolve_response", result)
            return result

    def revoke(self, header, kind, object_id):
        # Only fixed operations/table names; no arbitrary table or SQL from input.
        table, action = {
            "origin": ("origins", "origin.revoke"),
            "entry": ("revoked_entries", "entry.revoke"),
            "principal": ("revoked_principals", "principal.revoke"),
        }[kind]
        self.contracts.check("common#id", object_id)
        with self.store.connect(write=True) as db:
            identity, _ = self.auth.authenticate(header, db, action, operator=True)
            if kind == "origin":
                source = db.execute(
                    "SELECT 1 FROM source_inputs WHERE ref=?", (object_id,)
                ).fetchone()
                if source:
                    db.execute("UPDATE source_inputs SET revoked=1 WHERE ref=?", (object_id,))
                    return
                require(
                    db.execute("SELECT 1 FROM origins WHERE ref=?", (object_id,)).fetchone(),
                    "not_found",
                    404,
                )
                db.execute("UPDATE origins SET revoked=1 WHERE ref=?", (object_id,))
            else:
                known = (
                    {**self.auth.entries, **getattr(self, "input_entries", {})}
                    if kind == "entry"
                    else self.auth.principals
                )
                require(object_id in known, "not_found", 404)
                db.execute(f"INSERT OR IGNORE INTO {table}(id) VALUES(?)", (object_id,))
            db.execute(
                "INSERT INTO audit(principal,operation,object_id,observed_at) VALUES(?,?,?,?)",
                (identity, action, object_id, self.clock()),
            )

    def prepare_mapping(self, header, kind, request):
        """Internal outgoing-call port. Keep only correlation/targets, never message text."""
        family = {
            "identity": "identity-memory#register_request",
            "identity_resolve": "identity-memory#resolve_request",
            "ingest": "conversation#ingest_request",
        }[kind]
        self.contracts.check(family, request)
        envelope = request["query"] if kind == "identity_resolve" else request["command"]
        with self.store.connect(write=True) as db:
            preparer, principal = self.auth.authenticate(header, db, "mapping.prepare")
            receiver = "companion" if kind == "ingest" else "memory"
            entry_id, entry, context = self.context(
                db, envelope["origin"]["assertion_ref"], principal["service"], receiver, "dialogue"
            )
            deadline = epoch(envelope.get("deadline_at", context["expires_at"]))
            require(deadline > self.clock(), "timeout", 408)
            expected = {
                "request_id": envelope["request_id"],
                "account": entry["account"],
                "channel": entry["channel"],
            }
            if kind != "ingest":
                require(request["account"] == entry["account"])
            else:
                require(
                    request["author"] == entry["account"]
                    and request["message_key"]["channel"] == entry["channel"]
                )
                require(request["target_actor_ids"] == [entry["actor_id"]])
            ticket = "exchange:" + uuid.uuid4().hex
            db.execute(
                "INSERT INTO exchanges(ticket,entry_id,entry_digest,prepared_by,kind,request,deadline) VALUES(?,?,?,?,?,?,?)",
                (
                    ticket,
                    entry_id,
                    digest(entry),
                    preparer,
                    kind,
                    canonical(expected),
                    min(deadline, self.clock() + 60),
                ),
            )
            return ticket

    def confirm_mapping(self, header, ticket, response):
        """Internal authenticated upstream-response port. Never exposed as a public HTTP wire."""
        with self.store.connect(write=True) as db:
            _, principal = self.auth.authenticate(header, db, "mapping.confirm")
            row = db.execute("SELECT * FROM exchanges WHERE ticket=?", (ticket,)).fetchone()
            require(row is not None, "not_found", 404)
            kind = row["kind"]
            require(
                not db.execute(
                    "SELECT 1 FROM revoked_principals WHERE id=?", (row["prepared_by"],)
                ).fetchone()
            )
            require(principal["service"] == ("companion" if kind == "ingest" else "memory"))
            entry = self.auth.entry(db, row["entry_id"], row["entry_digest"])
            self.contracts.check(
                {
                    "identity": "identity-memory#identity_response",
                    "identity_resolve": "identity-memory#resolve_response",
                    "ingest": "conversation#ingest_response",
                }[kind],
                response,
            )
            expected = loads(row["request"])
            require(response["request_id"] == expected["request_id"])
            stable = canonical(response)
            if row["response"] is not None:
                require(row["response"] == stable, "idempotency_conflict", 409)
                return
            require(row["deadline"] > self.clock(), "timeout", 408)
            if kind != "ingest":
                account = canonical(expected["account"])
                attested = db.execute(
                    "SELECT person_id FROM ingest_subjects WHERE account=?", (account,)
                ).fetchone()
                current = db.execute(
                    "SELECT * FROM identities WHERE account=?", (account,)
                ).fetchone()
                if kind == "identity_resolve" and response["state"] == "unregistered":
                    require(current is None and attested is None, "version_conflict", 409)
                    db.execute("UPDATE exchanges SET response=? WHERE ticket=?", (stable, ticket))
                    return
                require(attested is None or attested[0] == response["person_id"])
                if current:
                    require(
                        current["person_id"] == response["person_id"]
                        and current["version"] == response["binding_version"],
                        "version_conflict",
                        409,
                    )
                else:
                    db.execute(
                        "INSERT INTO identities VALUES(?,?,?)",
                        (account, response["person_id"], response["binding_version"]),
                    )
            else:
                require(
                    response["collection_key"]
                    == {"channel": expected["channel"], "author": expected["account"]}
                )
                person = self.scope(db, entry)["person_id"]
                require(person is None or person == response["person_id"])
                account = canonical(expected["account"])
                attested = db.execute(
                    "SELECT person_id FROM ingest_subjects WHERE account=?", (account,)
                ).fetchone()
                require(attested is None or attested[0] == response["person_id"])
                if attested is None:
                    db.execute(
                        "INSERT INTO ingest_subjects VALUES(?,?)", (account, response["person_id"])
                    )
                current = db.execute(
                    "SELECT conversation_id FROM channels WHERE channel=?", (channel_key(entry),)
                ).fetchone()
                require(
                    current is None or current[0] == response["conversation_id"],
                    "version_conflict",
                    409,
                )
                if current is None:
                    db.execute(
                        "INSERT INTO channels VALUES(?,?)",
                        (channel_key(entry), response["conversation_id"]),
                    )
            db.execute("UPDATE exchanges SET response=? WHERE ticket=?", (stable, ticket))

    def observe_source(self, header, entry_id, message_key, *, tombstone=False):
        """Trusted rehearsal event adapter only; records revisions, never archive success."""
        self.contracts.check("common#message_key", message_key)
        require(type(tombstone) is bool, "invalid_input", 400)
        with self.store.connect(write=True) as db:
            identity, _ = self.auth.authenticate(header, db, "source.observe")
            entry = self.auth.entry(db, entry_id)
            require(entry["owner"] == identity and message_key["channel"] == entry["channel"])
            key, revision = source_key(message_key), message_key["revision"]
            current = db.execute(
                "SELECT * FROM sources WHERE source_key=? AND entry_id=?", (key, entry_id)
            ).fetchone()
            if current:
                require(revision >= current["revision"], "version_conflict", 409)
                require(not current["tombstone"] or tombstone, "scope_changed", 409)
                if revision == current["revision"]:
                    require(bool(current["tombstone"]) == tombstone, "version_conflict", 409)
                    return
            db.execute(
                "INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?)",
                (key, entry_id, digest(entry), revision, tombstone),
            )

    def verify_current_sources(self, header, event):
        """Partial current-source check; memory still owns scope versions and turn validation.

        Uses authenticated publisher + current entry/mapping/revisions, not an expired user ref.
        Archive receipt verification is deliberately unavailable in this adapter.
        """
        self.contracts.check("conversation#committed_event", event)
        with self.store.connect() as db:
            _, publisher = self.auth.authenticate(header, db, "source.verify")
            require(publisher["service"] == event["owner"] == "companion")
            require(event["conversation_id"] == event["scope"]["conversation_id"])
            require(
                len({source_key(s["message_key"]) for s in event["sources"]})
                == len(event["sources"]),
                "invalid_input",
                400,
            )
            for source in event["sources"]:
                require(source["archive_state"] != "archived", "dependency_unavailable", 503)
                rows = db.execute(
                    "SELECT * FROM sources WHERE source_key=?", (source_key(source["message_key"]),)
                ).fetchall()
                require(bool(rows), "dependency_unavailable", 503)
                matching = []
                for row in rows:
                    entry = self.auth.entry(db, row["entry_id"], row["entry_digest"])
                    if self.scope(db, entry) == event["scope"]:
                        self.auth.route(entry, publisher["service"], "memory", "dialogue")
                        matching.append(row)
                require(len(matching) == 1)
                row = matching[0]
                require(
                    not row["tombstone"] and row["revision"] == source["message_key"]["revision"],
                    "scope_changed",
                    409,
                )
            return {
                "current_sources": True,
                "archive_verified": False,
                "scope_version_verified": False,
            }

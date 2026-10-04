"""Platform's real user-source and exact operation bindings for Memory.

Companion interprets the current user's explicit action. This owner attests the actual
source/author/scope, never a model's authority claim or the Memory target's contents.
"""

import sqlite3
import uuid
from contextlib import closing

from .contracts import canonical, digest, epoch, loads, require, utc
from .origins import source_key

ISSUE = "/internal/v1/memory-context/proof/issue"
VERIFY = "/internal/v1/memory-context/proof/verify"


def operation_digest(request):
    # C1's semantic_payload/fingerprint contract: retain nulls, text and array order.
    return digest(
        {
            key: value
            for key, value in request.items()
            if key not in {"query", "command", "proof_ref"}
        }
    )


class MemoryProofs:
    def __init__(self, platform):
        self.p = platform
        platform.contracts.load_runtime("memory-context/v1")
        with platform.store.connect(write=True) as db:
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='memory_operation_proofs'"
            ).fetchone():
                # Backup with a read connection: the Store's writer already owns a transaction.
                with (
                    closing(sqlite3.connect(platform.store.path)) as source,
                    closing(
                        sqlite3.connect(
                            platform.store.path
                            + ".pre-memory-context-"
                            + uuid.uuid4().hex
                            + ".sqlite"
                        )
                    ) as backup,
                ):
                    source.backup(backup)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS memory_operation_proofs (
                    ref TEXT PRIMARY KEY, purpose TEXT NOT NULL, semantic TEXT NOT NULL,
                    principal TEXT NOT NULL, accounts TEXT NOT NULL, scopes TEXT NOT NULL,
                    entries TEXT NOT NULL, source_key TEXT, source_revision INTEGER,
                    source_digest TEXT, expires_at REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
                    command_key TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS memory_link_challenges (
                    id TEXT PRIMARY KEY, nonce_digest TEXT NOT NULL UNIQUE,
                    source_entry TEXT NOT NULL, source_account TEXT NOT NULL,
                    source_scope TEXT NOT NULL, target_account TEXT NOT NULL,
                    target_entry TEXT, consent_key TEXT, consent_revision INTEGER,
                    consent_digest TEXT, expires_at REAL NOT NULL,
                    request_id TEXT NOT NULL UNIQUE, operation_id TEXT NOT NULL,
                    association TEXT, state TEXT NOT NULL
                );
            """)

    def issue(self, header, request):
        p = self.p
        p.contracts.check("memory-context#issue_request", request)
        proposal, source = request["proposal"], request["source"]
        require(proposal["command"]["origin"] == request["origin"], "forbidden", 403)
        require(epoch(proposal["command"]["deadline_at"]) > p.origins.clock(), "timeout", 408)
        require(source["kind"] != "retract", "forbidden", 403)
        require(
            any(item["message_key"] == source["message_key"] for item in proposal["evidence_refs"]),
            "forbidden",
            403,
        )
        with p.store.connect(write=True) as db:
            caller, principal = p.auth.authenticate(header, db, "source.input")
            require(principal["kind"] == "service" and principal["service"] == "companion")
            entry_id, entry, context = p.origins.context(
                db, request["origin"]["assertion_ref"], "companion", "memory", "dialogue"
            )
            require(
                source["author"] == entry["account"]
                and source["message_key"]["channel"] == entry["channel"]
                and proposal["scope"] == context["allowed_scope"]
                and proposal["scope"]["person_id"] is not None,
                "forbidden",
                403,
            )
            admitted = db.execute(
                "SELECT * FROM actor_origins WHERE ref=?", (request["origin"]["assertion_ref"],)
            ).fetchone()
            source_digest = digest(source)
            require(
                admitted is not None
                and admitted["input_digest"] == source_digest
                and admitted["entry_document"] == canonical(entry),
                "forbidden",
                403,
            )
            p.sources._entry(db, admitted["input_entry_id"])
            key = source_key(source["message_key"])
            current = db.execute(
                "SELECT * FROM input_observations WHERE physical_key=?", (key,)
            ).fetchone()
            require(
                current is not None
                and current["input_digest"] == source_digest
                and current["revision"] == source["message_key"]["revision"]
                and current["author"] == canonical(entry["account"])
                and current["kind"] != "retract",
                "scope_changed",
                409,
            )
            semantic = operation_digest(proposal)
            command_key = canonical([caller, proposal["command"]["idempotency_key"], key])
            row = db.execute(
                "SELECT * FROM memory_operation_proofs WHERE command_key=?", (command_key,)
            ).fetchone()
            if row:
                require(
                    row["semantic"] == semantic and row["source_digest"] == source_digest,
                    "idempotency_conflict",
                    409,
                )
                require(
                    not row["revoked"] and row["expires_at"] > p.origins.clock(), "forbidden", 403
                )
            else:
                reference = "memory-proof:" + uuid.uuid4().hex
                expires = min(
                    epoch(context["expires_at"]), epoch(proposal["command"]["deadline_at"])
                )
                db.execute(
                    "INSERT INTO memory_operation_proofs VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?)",
                    (
                        reference,
                        "revision",
                        semantic,
                        entry["owner"],
                        canonical([entry["account"]]),
                        canonical([proposal["scope"]]),
                        canonical([entry_id]),
                        key,
                        source["message_key"]["revision"],
                        source_digest,
                        expires,
                        command_key,
                    ),
                )
                row = db.execute(
                    "SELECT * FROM memory_operation_proofs WHERE ref=?", (reference,)
                ).fetchone()
            result = {
                "schema_version": 1,
                "request_id": request["request_id"],
                "proof_ref": row["ref"],
                "operation_digest": semantic,
                "expires_at": utc(row["expires_at"]),
            }
            p.contracts.check("memory-context#issue_response", result)
            return result

    def verify(self, header, request):
        p = self.p
        p.contracts.check("memory-context#proof_request", request)
        with p.store.connect() as db:
            _, principal = p.auth.authenticate(header, db, "source.current")
            require(principal["kind"] == "service" and principal["service"] == "memory")
            _, entry, context = p.origins.context(
                db, request["origin"]["assertion_ref"], "companion", "memory", "dialogue"
            )
            row = db.execute(
                "SELECT * FROM memory_operation_proofs WHERE ref=?", (request["proof_ref"],)
            ).fetchone()
            require(
                row is not None and not row["revoked"] and row["expires_at"] > p.origins.clock(),
                "forbidden",
                403,
            )
            accounts, scopes = loads(row["accounts"]), loads(row["scopes"])
            require(
                row["purpose"] == request["purpose"]
                and row["semantic"] == request["operation_digest"]
                and entry["account"] in accounts
                and context["allowed_scope"] in scopes,
                "forbidden",
                403,
            )
            for entry_id, account, scope in zip(
                loads(row["entries"]), accounts, scopes, strict=True
            ):
                current = p.auth.entry(db, entry_id)
                require(
                    current["account"] == account and p.origins.scope(db, current) == scope,
                    "scope_changed",
                    409,
                )
                p.auth.route(current, "companion", "memory", "dialogue")
            if row["source_key"] is not None:
                current = db.execute(
                    "SELECT * FROM input_observations WHERE physical_key=?", (row["source_key"],)
                ).fetchone()
                require(
                    current is not None
                    and current["input_digest"] == row["source_digest"]
                    and current["revision"] == row["source_revision"]
                    and current["kind"] != "retract",
                    "scope_changed",
                    409,
                )
            result = {
                "schema_version": 1,
                "request_id": request["request_id"],
                "proof_ref": row["ref"],
                "purpose": row["purpose"],
                "operation_digest": row["semantic"],
                "valid": True,
                "principal_id": row["principal"],
                "accounts": accounts,
                "scopes": scopes,
                "expires_at": utc(row["expires_at"]),
            }
            p.contracts.check("memory-context#proof_response", result)
            return result

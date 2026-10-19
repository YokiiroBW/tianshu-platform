"""Durable claims in the existing infrastructure backup, shared by platform instances."""

import json
import sqlite3
from contextlib import closing, contextmanager

from ..contracts import Fault, canonical, digest, require, utc

MUTATIONS = {"action", "execute"}


class ControlRepository:
    def __init__(self, database):
        self.database = database
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS control_operations (
                operation_id TEXT PRIMARY KEY, client_id TEXT UNIQUE NOT NULL,
                node_id TEXT NOT NULL, revision INTEGER NOT NULL, kind TEXT NOT NULL,
                stack_id TEXT, container_id TEXT, lock_key TEXT NOT NULL,
                semantics TEXT NOT NULL, payload TEXT NOT NULL, document TEXT NOT NULL,
                state TEXT NOT NULL, settled INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                owner TEXT, lease_until REAL NOT NULL DEFAULT 0,
                sent INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0)""")
            db.execute("""CREATE INDEX IF NOT EXISTS control_pending
                ON control_operations(settled,next_attempt,created_at,operation_id)""")
            db.execute("""CREATE TABLE IF NOT EXISTS control_plan_uses (
                node_id TEXT NOT NULL, plan_id TEXT NOT NULL, operation_id TEXT NOT NULL,
                PRIMARY KEY(node_id,plan_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS control_plans (
                node_id TEXT NOT NULL, plan_id TEXT NOT NULL,
                operation_id TEXT UNIQUE NOT NULL, document TEXT NOT NULL,
                PRIMARY KEY(node_id,plan_id))""")

    @contextmanager
    def _db(self, *, write=True):
        try:
            with closing(sqlite3.connect(self.database, timeout=2)) as db:
                db.row_factory = sqlite3.Row
                if write:
                    db.execute("PRAGMA synchronous=FULL")
                    db.execute("BEGIN IMMEDIATE")
                with db:
                    yield db
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
            raise Fault("infrastructure_store_unavailable", 503) from None

    @staticmethod
    def _row(row):
        if row is None:
            return None
        return {
            **dict(row),
            "document": json.loads(row["document"]),
            "payload": json.loads(row["payload"]),
        }

    def get(self, operation_id=None, *, client_id=None):
        with self._db(write=False) as db:
            column, value = (
                ("client_id", client_id) if client_id else ("operation_id", operation_id)
            )
            return self._row(
                db.execute(
                    f"SELECT * FROM control_operations WHERE {column}=?", (value,)
                ).fetchone()
            )

    def latest_read(self, node_id, revision, *, stack_id=None, container_id=None):
        with self._db(write=False) as db:
            return self._row(
                db.execute(
                    """SELECT * FROM control_operations WHERE node_id=? AND revision=?
                   AND kind IN ('inventory','detail') AND stack_id IS ? AND container_id IS ?
                   ORDER BY created_at DESC,operation_id DESC LIMIT 1""",
                    (node_id, revision, stack_id, container_id),
                ).fetchone()
            )

    def accept(self, document, payload, lock_key, now, *, deduplicate=False):
        semantic = digest({"kind": document["kind"], "payload": payload})
        with self._db() as db:
            old = db.execute(
                "SELECT * FROM control_operations WHERE client_id=?", (document["client_id"],)
            ).fetchone()
            if old:
                require(old["semantics"] == semantic, "idempotency_conflict", 409)
                return self._row(old)
            if deduplicate:
                old = db.execute(
                    """SELECT * FROM control_operations WHERE node_id=? AND revision=?
                       AND kind=? AND lock_key=? AND settled=0 ORDER BY created_at LIMIT 1""",
                    (
                        document["node_id"],
                        document["connection_revision"],
                        document["kind"],
                        lock_key,
                    ),
                ).fetchone()
                if old:
                    return self._row(old)
            require(
                db.execute("SELECT count(*) FROM control_operations WHERE settled=0").fetchone()[0]
                < 256,
                "too_many_requests",
                429,
            )
            if document["kind"] == "execute":
                plan_id = payload["params"]["target"]["plan_id"]
                previous = db.execute(
                    "SELECT operation_id FROM control_plan_uses WHERE node_id=? AND plan_id=?",
                    (document["node_id"], plan_id),
                ).fetchone()
                require(previous is None, "infrastructure_plan_already_executed", 409)
                db.execute(
                    "INSERT INTO control_plan_uses VALUES(?,?,?)",
                    (document["node_id"], plan_id, document["operation_id"]),
                )
            db.execute(
                """INSERT INTO control_operations
                   (operation_id,client_id,node_id,revision,kind,stack_id,container_id,lock_key,
                    semantics,payload,document,state,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    document["operation_id"],
                    document["client_id"],
                    document["node_id"],
                    document["connection_revision"],
                    document["kind"],
                    document["stack_id"],
                    document["container_id"],
                    lock_key,
                    semantic,
                    canonical(payload),
                    canonical(document),
                    document["state"],
                    now,
                    now,
                ),
            )
            return self._row(
                db.execute(
                    "SELECT * FROM control_operations WHERE operation_id=?",
                    (document["operation_id"],),
                ).fetchone()
            )

    def claim(self, owner, now, lease):
        with self._db() as db:
            rows = db.execute(
                """SELECT * FROM control_operations WHERE settled=0 AND next_attempt<=?
                   AND (owner IS NULL OR lease_until<?) ORDER BY created_at,operation_id LIMIT 256""",
                (now, now),
            ).fetchall()
            for row in rows:
                if row["kind"] in MUTATIONS:
                    blocked = db.execute(
                        """SELECT 1 FROM control_operations WHERE node_id=?
                           AND (lock_key=? OR lock_key='node:'||node_id OR ?='node:'||node_id)
                           AND kind IN ('action','execute') AND settled=0 AND operation_id!=?
                           AND (sent=1 OR owner IS NOT NULL OR (created_at,operation_id)<(?,?))
                           LIMIT 1""",
                        (
                            row["node_id"],
                            row["lock_key"],
                            row["lock_key"],
                            row["operation_id"],
                            row["created_at"],
                            row["operation_id"],
                        ),
                    ).fetchone()
                    if blocked:
                        continue
                document = json.loads(row["document"])
                document.update(
                    state="running",
                    stage="reconciling" if row["sent"] else "reading",
                    updated_at=utc(now),
                    settled=False,
                    finished_at=None,
                )
                db.execute(
                    """UPDATE control_operations SET owner=?,lease_until=?,document=?,state='running',
                       updated_at=? WHERE operation_id=?""",
                    (owner, now + lease, canonical(document), now, row["operation_id"]),
                )
                found = self._row(row)
                found.update(owner=owner, lease_until=now + lease, document=document)
                return found
        return None

    def sending(self, operation_id, owner, now):
        """Commit before invoking transport. An abandoned claim can only be reconciled."""
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM control_operations WHERE operation_id=? AND owner=? AND lease_until>=?",
                (operation_id, owner, now),
            ).fetchone()
            if not row:
                return False
            document = json.loads(row["document"])
            document.update(stage="submitting", acceptance="unknown", updated_at=utc(now))
            db.execute(
                "UPDATE control_operations SET sent=1,document=?,updated_at=? WHERE operation_id=?",
                (canonical(document), now, operation_id),
            )
            return True

    def renew(self, operation_id, owner, now, lease):
        with self._db() as db:
            return bool(
                db.execute(
                    """UPDATE control_operations SET lease_until=?
                   WHERE operation_id=? AND owner=? AND lease_until>=?""",
                    (now + lease, operation_id, owner, now),
                ).rowcount
            )

    def finish(self, operation_id, owner, document, now, *, retry_after=0):
        with self._db() as db:
            changed = bool(
                db.execute(
                    """UPDATE control_operations SET document=?,state=?,settled=?,updated_at=?,
                   owner=NULL,lease_until=0,next_attempt=? WHERE operation_id=? AND owner=?""",
                    (
                        canonical(document),
                        document["state"],
                        int(document["settled"]),
                        now,
                        now + retry_after,
                        operation_id,
                        owner,
                    ),
                ).rowcount
            )
            if (
                changed
                and document["kind"] == "plan"
                and document["state"] in {"observed", "partial"}
            ):
                plan = document["result"]
                if plan is not None:
                    db.execute(
                        "INSERT OR REPLACE INTO control_plans VALUES(?,?,?,?)",
                        (document["node_id"], plan["plan_id"], operation_id, canonical(plan)),
                    )
            return changed

    def refresh(self, operation_id, now):
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM control_operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            require(row is not None, "infrastructure_operation_not_found", 404)
            document = json.loads(row["document"])
            require(document["can_refresh"], "invalid_input", 400)
            document.update(settled=False, finished_at=None)
            db.execute(
                "UPDATE control_operations SET document=?,settled=0,next_attempt=0 WHERE operation_id=?",
                (canonical(document), operation_id),
            )
            return document

    def plan(self, node_id, plan_id):
        with self._db(write=False) as db:
            row = db.execute(
                "SELECT document FROM control_plans WHERE node_id=? AND plan_id=?",
                (node_id, plan_id),
            ).fetchone()
            if row:
                return json.loads(row[0])
        return None

    def page(self, *, after=None, limit=20, states=None, node_id=None, stack_id=None, kinds=None):
        filters, args = [], []
        for field, value in (("node_id", node_id), ("stack_id", stack_id)):
            if value is not None:
                filters.append(field + "=?")
                args.append(value)
        for field, values in (("kind", kinds), ("state", states)):
            if values is not None:
                if not values:
                    return [], False
                filters.append(field + " IN (" + ",".join("?" for _ in values) + ")")
                args.extend(sorted(values))
        if after:
            filters.append("(CAST(created_at AS INTEGER),operation_id)<(?,?)")
            args.extend(after)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        with self._db(write=False) as db:
            rows = db.execute(
                "SELECT * FROM control_operations"
                + where
                + " ORDER BY CAST(created_at AS INTEGER) DESC,operation_id DESC LIMIT ?",
                (*args, limit + 1),
            ).fetchall()
            return [self._row(row) for row in rows[:limit]], len(rows) > limit

    def count(self, *, kinds=None):
        with self._db(write=False) as db:
            if kinds:
                return db.execute(
                    "SELECT count(*) FROM control_operations WHERE kind IN ("
                    + ",".join("?" for _ in kinds)
                    + ")",
                    tuple(sorted(kinds)),
                ).fetchone()[0]
            return db.execute("SELECT count(*) FROM control_operations").fetchone()[0]

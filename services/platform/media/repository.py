"""Durable subscriptions, source membership and CAS-owned download jobs.

Only this module opens the media database. Transactions contain no network or file work.
"""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from ..contracts import Fault, digest, canonical, require

TERMINAL = frozenset(
    {
        "completed",
        "published",
        "cancelled",
        "failed",
        "unknown",
        "auth_required",
        "waiting_metadata",
    }
)
OVERVIEW_STATES = {
    "published": ("published", "completed"),
    "processing": (
        "downloading",
        "validating",
        "metadata_ready",
        "publishing",
        "asset_indexed",
        "library_verifying",
    ),
    "queued": ("queued", "retry_wait"),
    "attention": ("failed", "unknown", "auth_required", "waiting_metadata"),
    "cancelled": ("cancelled",),
}


class Repository:
    def __init__(self, database, *, clock=time.time):
        self.database = Path(database)
        self.clock = clock
        self.database.parent.mkdir(parents=True, exist_ok=True)
        if self.database.exists():
            with sqlite3.connect(self.database) as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                require(version in (0, 1), "media_store_unavailable", 503)
                if (
                    version == 0
                    and db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone()
                ):
                    backup = self.database.with_name(
                        self.database.name + ".pre-media-" + uuid.uuid4().hex
                    )
                    with sqlite3.connect(backup) as target:
                        db.backup(target)
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS documents(kind TEXT NOT NULL,id TEXT NOT NULL,revision INTEGER NOT NULL,body TEXT NOT NULL,PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS members(subscription_id TEXT NOT NULL,bvid TEXT NOT NULL,PRIMARY KEY(subscription_id,bvid));
                CREATE TABLE IF NOT EXISTS member_parts(subscription_id TEXT NOT NULL,bvid TEXT NOT NULL,cid TEXT NOT NULL,PRIMARY KEY(subscription_id,bvid,cid));
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,dedupe TEXT UNIQUE NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,generation INTEGER NOT NULL DEFAULT 0,owner TEXT,lease REAL NOT NULL DEFAULT 0,body TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(state,lease,created);
                CREATE INDEX IF NOT EXISTS jobs_order ON jobs(CAST(created AS INTEGER) DESC,id DESC);
                CREATE TABLE IF NOT EXISTS events(job_id TEXT NOT NULL,state TEXT NOT NULL,code TEXT NOT NULL,at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS episodes(bvid TEXT NOT NULL,cid TEXT NOT NULL,number INTEGER NOT NULL,PRIMARY KEY(bvid,cid),UNIQUE(bvid,number));
                CREATE TABLE IF NOT EXISTS scan_leases(id TEXT PRIMARY KEY,owner TEXT NOT NULL,generation INTEGER NOT NULL,expires REAL NOT NULL);
                PRAGMA user_version=1;
            """)

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _document(self, db, kind, identity):
        row = db.execute(
            "SELECT body FROM documents WHERE kind=? AND id=?", (kind, identity)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def get(self, kind, identity):
        with self.transaction() as db:
            return self._document(db, kind, identity)

    def list(self, kind):
        with self.transaction() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT body FROM documents WHERE kind=? ORDER BY id", (kind,)
                )
            ]

    def put(self, kind, identity, body, expected=None, *, db=None):
        if db is None:
            with self.transaction() as transaction:
                return self.put(kind, identity, body, expected, db=transaction)
        current = self._document(db, kind, identity)
        revision = current.get("revision", 0) if current else 0
        require(expected is None or revision == expected, "version_conflict", 409)
        result = {**body, "revision": revision + 1}
        db.execute(
            "INSERT INTO documents VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET revision=excluded.revision,body=excluded.body",
            (kind, identity, result["revision"], json.dumps(result, ensure_ascii=False)),
        )
        return result

    def receipt(self, client_id, request, operation):
        self.client_id(client_id)
        fingerprint = digest(canonical(request))
        with self.transaction() as db:
            row = db.execute(
                "SELECT fingerprint,body FROM receipts WHERE id=?", (client_id,)
            ).fetchone()
            if row:
                require(row[0] == fingerprint, "idempotency_conflict", 409)
                return {**json.loads(row[1]), "replayed": True}
            result = operation(db)
            db.execute(
                "INSERT INTO receipts VALUES(?,?,?)",
                (client_id, fingerprint, json.dumps(result, ensure_ascii=False)),
            )
            return {**result, "replayed": False}

    def client_id(self, client_id):
        require(isinstance(client_id, str), "invalid_input", 400)
        try:
            require(str(uuid.UUID(client_id)) == client_id, "invalid_input", 400)
        except ValueError:
            raise Fault("invalid_input", 400) from None

    def replay(self, client_id, request):
        self.client_id(client_id)
        fingerprint = digest(canonical(request))
        with self.transaction() as db:
            row = db.execute(
                "SELECT fingerprint,body FROM receipts WHERE id=?", (client_id,)
            ).fetchone()
            if row:
                require(row[0] == fingerprint, "idempotency_conflict", 409)
                return {**json.loads(row[1]), "replayed": True}
            return None

    def fact(self, kind, identity, changes, expected):
        with self.transaction() as db:
            current = self._document(db, kind, identity)
            require(current and current["revision"] == expected, "version_conflict", 409)
            body = {**current, **changes}
            db.execute(
                "UPDATE documents SET body=? WHERE kind=? AND id=?",
                (json.dumps(body, ensure_ascii=False), kind, identity),
            )
            return body

    def enqueue(self, payload, *, db=None):
        if db is None:
            with self.transaction() as transaction:
                return self.enqueue(payload, db=transaction)
        # The job is one complete selected-P package, with per-CID membership retained inside it.
        for existing in db.execute(
            "SELECT body FROM jobs WHERE state NOT IN ('cancelled','failed')"
        ):
            prior = json.loads(existing[0])
            if (
                prior["bvid"] == payload["bvid"]
                and prior["target_id"] == payload["target_id"]
                and prior["quality"] == payload["quality"]
                and prior.get("layout") == payload.get("layout")
                and set(payload["selected_cids"]) <= set(prior["selected_cids"])
            ):
                return prior
        dedupe = digest(
            canonical(
                {
                    "bvid": payload["bvid"],
                    "selected_cids": sorted(payload["selected_cids"]),
                    "target_id": payload["target_id"],
                    "quality": payload["quality"],
                    "layout": payload.get("layout"),
                    "edition": payload.get("edition", 0),
                }
            )
        )
        row = db.execute("SELECT body FROM jobs WHERE dedupe=?", (dedupe,)).fetchone()
        if row:
            return json.loads(row[0])
        now = self.clock()
        body = {
            **payload,
            "job_id": str(uuid.uuid4()),
            "state": "queued",
            "stage": "queued",
            "code": "queued",
            "progress": None,
            "created_at": now,
            "updated_at": now,
            "actual_quality": None,
            "cancel_requested": False,
            "asset_receipt": None,
            "library_results": [],
            "generation": 0,
            "revision": 1,
        }
        db.execute(
            "INSERT INTO jobs(id,dedupe,state,created,updated,body) VALUES(?,?,?,?,?,?)",
            (body["job_id"], dedupe, "queued", now, now, json.dumps(body, ensure_ascii=False)),
        )
        db.execute("INSERT INTO events VALUES(?,?,?,?)", (body["job_id"], "queued", "queued", now))
        return body

    def job(self, identity):
        with self.transaction() as db:
            row = db.execute("SELECT body FROM jobs WHERE id=?", (identity,)).fetchone()
            return json.loads(row[0]) if row else None

    def jobs(self, *, limit=100, after=None, states=None):
        where, parameters = [], []
        if states is not None:
            if not states:
                return [], False
            where.append("state IN (" + ",".join("?" for _ in states) + ")")
            parameters.extend(sorted(states))
        if after is not None:
            identity = after[1].removeprefix("media:")
            where.append("(CAST(created AS INTEGER)<? OR (CAST(created AS INTEGER)=? AND id<?))")
            parameters.extend((after[0], after[0], identity))
        sql = (
            "SELECT body FROM jobs"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY CAST(created AS INTEGER) DESC,id DESC LIMIT ?"
        )
        parameters.append(limit + 1)
        with self.transaction() as db:
            rows = db.execute(sql, parameters).fetchall()
        found = [json.loads(row[0]) for row in rows]
        return found[:limit], len(found) > limit

    def job_count(self):
        with self.transaction() as db:
            return db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    def job_overview(self):
        """Count the whole ledger in SQL without loading any job documents."""
        with self.transaction() as db:
            counts = dict(db.execute("SELECT state, COUNT(*) FROM jobs GROUP BY state"))
        return {
            "total": sum(counts.values()),
            **{
                category: sum(counts.get(state, 0) for state in states)
                for category, states in OVERVIEW_STATES.items()
            },
        }

    def events(self, identity):
        with self.transaction() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT state,code,at FROM events WHERE job_id=? ORDER BY at LIMIT 500",
                    (identity,),
                )
            ]

    def claim(self, owner, lease_seconds=30):
        now = self.clock()
        with self.transaction() as db:
            rows = db.execute(
                "SELECT id,state,generation,body FROM jobs WHERE (state='queued' OR (state='retry_wait' AND lease<?) OR (owner IS NOT NULL AND lease<?)) AND state NOT IN ('completed','published','cancelled','failed','unknown','auth_required','waiting_metadata') ORDER BY created LIMIT 500",
                (now, now),
            ).fetchall()
            occupied = [
                json.loads(value[0])
                for value in db.execute(
                    "SELECT body FROM jobs WHERE owner IS NOT NULL AND lease>=?", (now,)
                )
            ]
            row = next(
                (
                    value
                    for value in rows
                    if not any(
                        peer["job_id"] != value["id"]
                        and peer["bvid"] == json.loads(value["body"])["bvid"]
                        and peer["target_id"] == json.loads(value["body"])["target_id"]
                        for peer in occupied
                    )
                ),
                None,
            )
            if row is None:
                return None
            body = json.loads(row["body"])
            body["generation"] = row["generation"] + 1
            # A lost publication response is reconciled through its stable publisher key.
            body["state"] = "publishing" if body.get("package") else "downloading"
            body["stage"] = body["state"]
            body["updated_at"] = now
            db.execute(
                "UPDATE jobs SET state=?,generation=?,owner=?,lease=?,updated=?,body=? WHERE id=?",
                (
                    body["state"],
                    body["generation"],
                    owner,
                    now + lease_seconds,
                    now,
                    json.dumps(body, ensure_ascii=False),
                    body["job_id"],
                ),
            )
            return body

    def update_job(self, identity, changes, *, owner=None, generation=None, db=None):
        if db is None:
            with self.transaction() as transaction:
                return self.update_job(
                    identity, changes, owner=owner, generation=generation, db=transaction
                )
        row = db.execute(
            "SELECT body,owner,generation FROM jobs WHERE id=?", (identity,)
        ).fetchone()
        require(row is not None, "not_found", 404)
        if owner is not None:
            require(
                row["owner"] == owner and row["generation"] == generation, "lease_conflict", 409
            )
        elif generation is not None:
            require(row["generation"] == generation, "lease_conflict", 409)
        previous = json.loads(row["body"])
        if owner is not None and changes.get("state") not in {"cancelled", "unknown"}:
            require(not previous["cancel_requested"], "job_cancelled", 409)
        body = {
            **previous,
            **changes,
            "updated_at": self.clock(),
            "revision": previous.get("revision", 1) + 1,
        }
        ended = body["state"] in TERMINAL
        db.execute(
            "UPDATE jobs SET state=?,updated=?,body=?,owner=?,lease=? WHERE id=?",
            (
                body["state"],
                body["updated_at"],
                json.dumps(body, ensure_ascii=False),
                None if ended or body["state"] in {"retry_wait", "queued"} else row["owner"],
                0
                if ended
                else self.clock()
                + (body.get("retry_delay", 30) if body["state"] == "retry_wait" else 30),
                identity,
            ),
        )
        if "state" in changes:
            db.execute(
                "INSERT INTO events VALUES(?,?,?,?)",
                (identity, body["state"], body["code"], body["updated_at"]),
            )
        if changes.get("state") == "asset_indexed" and body.get("asset_receipt"):
            self.put(
                "target_head",
                body["target_id"] + ":" + body["bvid"],
                {"job_id": identity, "operation_id": body["asset_receipt"]["operation_id"]},
                db=db,
            )
        return body

    def heartbeat(self, identity, owner, generation):
        with self.transaction() as db:
            row = db.execute(
                "SELECT body FROM jobs WHERE id=? AND owner=? AND generation=?",
                (identity, owner, generation),
            ).fetchone()
            require(row is not None, "lease_conflict", 409)
            body = json.loads(row[0])
            require(not body["cancel_requested"], "job_cancelled", 409)
            db.execute("UPDATE jobs SET lease=? WHERE id=?", (self.clock() + 30, identity))

    def scan_claim(self, identity, owner):
        with self.transaction() as db:
            row = db.execute(
                "SELECT generation,expires FROM scan_leases WHERE id=?", (identity,)
            ).fetchone()
            require(row is None or row["expires"] < self.clock(), "scan_in_progress", 409)
            generation = row["generation"] + 1 if row else 1
            db.execute(
                "INSERT INTO scan_leases VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,generation=excluded.generation,expires=excluded.expires",
                (identity, owner, generation, self.clock() + 30),
            )
            return generation

    def scan_heartbeat(self, identity, owner, generation):
        with self.transaction() as db:
            changed = db.execute(
                "UPDATE scan_leases SET expires=? WHERE id=? AND owner=? AND generation=?",
                (self.clock() + 30, identity, owner, generation),
            ).rowcount
            require(changed == 1, "lease_conflict", 409)

    def scan_release(self, identity, owner, generation):
        with self.transaction() as db:
            db.execute(
                "DELETE FROM scan_leases WHERE id=? AND owner=? AND generation=?",
                (identity, owner, generation),
            )

    def complete_scan(
        self, subscription, members, jobs, owner=None, generation=None, part_members=None
    ):
        with self.transaction() as db:
            if owner is not None:
                lease = db.execute(
                    "SELECT 1 FROM scan_leases WHERE id=? AND owner=? AND generation=? AND expires>=?",
                    (subscription["subscription_id"], owner, generation, self.clock()),
                ).fetchone()
                require(lease is not None, "lease_conflict", 409)
            current = self._document(db, "subscription", subscription["subscription_id"])
            require(
                current is not None
                and current["revision"] == subscription["revision"]
                and current["state"] == "active",
                "version_conflict",
                409,
            )
            # Membership is accumulated: deleting and later re-adding a member does not download twice.
            db.executemany(
                "INSERT OR IGNORE INTO members VALUES(?,?)",
                ((subscription["subscription_id"], bvid) for bvid in members),
            )
            before = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            for job in jobs:
                self.enqueue(job, db=db)
            queued = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] - before
            if part_members:
                db.executemany(
                    "INSERT OR IGNORE INTO member_parts VALUES(?,?,?)",
                    (
                        (subscription["subscription_id"], bvid, cid)
                        for bvid, cids in part_members.items()
                        for cid in cids
                    ),
                )
            count = db.execute(
                "SELECT COUNT(*) FROM members WHERE subscription_id=?",
                (subscription["subscription_id"],),
            ).fetchone()[0]
            body = {
                **current,
                "baseline_ready": True,
                "last_scan_at": self.clock(),
                "next_scan_at": self.clock() + current["interval_seconds"],
                "code": "scan_complete",
                "counts": {"members": count, "queued": current["counts"]["queued"] + queued},
                "quality_reassessment": False,
            }
            # Scanning facts do not invalidate the user's configuration revision.
            db.execute(
                "UPDATE documents SET body=? WHERE kind='subscription' AND id=?",
                (json.dumps(body, ensure_ascii=False), subscription["subscription_id"]),
            )
            return body, queued

    def members(self, identity):
        with self.transaction() as db:
            return {
                row[0]
                for row in db.execute(
                    "SELECT bvid FROM members WHERE subscription_id=?", (identity,)
                )
            }

    def member_parts(self, identity):
        result = {}
        with self.transaction() as db:
            for bvid, cid in db.execute(
                "SELECT bvid,cid FROM member_parts WHERE subscription_id=?", (identity,)
            ):
                result.setdefault(bvid, set()).add(cid)
        return result

    def episode_numbers(self, bvid, cids):
        with self.transaction() as db:
            maximum = db.execute(
                "SELECT COALESCE(MAX(number),0) FROM episodes WHERE bvid=?", (bvid,)
            ).fetchone()[0]
            for cid in cids:
                if (
                    db.execute(
                        "SELECT 1 FROM episodes WHERE bvid=? AND cid=?", (bvid, cid)
                    ).fetchone()
                    is None
                ):
                    maximum += 1
                    db.execute("INSERT INTO episodes VALUES(?,?,?)", (bvid, cid, maximum))
            return {
                row[0]: row[1]
                for row in db.execute("SELECT cid,number FROM episodes WHERE bvid=?", (bvid,))
            }

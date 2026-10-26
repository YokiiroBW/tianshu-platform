"""Participation correlations in the existing Bot ledger, never a send queue."""

from contextlib import closing

from ..contracts import canonical, loads, require


class Repository:
    def __init__(self, bots):
        self.bots = bots
        with closing(bots._db()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS participation_candidates (
                    id TEXT PRIMARY KEY, observation_id TEXT NOT NULL,
                    document TEXT NOT NULL, event TEXT NOT NULL,
                    decision TEXT, claim_request TEXT, claim_result TEXT,
                    created_at REAL NOT NULL, expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS participation_candidate_page
                    ON participation_candidates(created_at,id);
                CREATE TABLE IF NOT EXISTS participation_events (
                    event_key TEXT PRIMARY KEY, candidate_id TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS participation_registrations (
                    id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL UNIQUE,
                    actor_ref TEXT NOT NULL UNIQUE, document TEXT NOT NULL,
                    ingest TEXT NOT NULL, host_registered INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS participation_bindings (
                    reply_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL,
                    registration_id TEXT NOT NULL, document TEXT NOT NULL
                );
            """)

    def get(self, candidate_id, *, db=None):
        if db is None:
            with closing(self.bots._db()) as connection:
                return self.get(candidate_id, db=connection)
        row = db.execute(
            "SELECT * FROM participation_candidates WHERE id=?", (candidate_id,)
        ).fetchone()
        require(row is not None, "not_found", 404)
        return {
            **dict(row),
            **{
                key: loads(row[key]) if row[key] else None
                for key in ("document", "event", "decision", "claim_request", "claim_result")
            },
        }

    def capture(self, observation_id, candidate, event, now, expires):
        with closing(self.bots._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT document,event FROM participation_candidates WHERE id=?",
                (candidate["candidate_id"],),
            ).fetchone()
            if old:
                require(old["event"] == canonical(event), "idempotency_conflict", 409)
                return loads(old["document"])
            require(
                db.execute(
                    "SELECT COUNT(*) FROM participation_candidates WHERE expires_at>?", (now,)
                ).fetchone()[0]
                < 2000,
                "queue_full",
                429,
            )
            # Unregistered expired candidates are disposable observation projections.
            db.execute(
                "DELETE FROM participation_candidates WHERE expires_at<? AND id NOT IN "
                "(SELECT candidate_id FROM participation_events)",
                (now - 86400,),
            )
            db.execute(
                "INSERT INTO participation_candidates(id,observation_id,document,event,created_at,expires_at) VALUES(?,?,?,?,?,?)",
                (
                    candidate["candidate_id"],
                    observation_id,
                    canonical(candidate),
                    canonical(event),
                    now,
                    expires,
                ),
            )
            return candidate

    def save_decision(self, candidate_id, decision):
        with closing(self.bots._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = self.get(candidate_id, db=db)
            prior = row["decision"]
            if prior:
                if (
                    prior["decision_id"] == decision["decision_id"]
                    and prior["revision"] == decision["revision"]
                ):
                    require(prior == decision, "idempotency_conflict", 409)
                    return prior
                require(
                    row["claim_request"] is None
                    and prior["outcome"] == "defer"
                    and decision["revision"] == prior["revision"] + 1,
                    "idempotency_conflict",
                    409,
                )
            db.execute(
                "UPDATE participation_candidates SET decision=? WHERE id=?",
                (canonical(decision), candidate_id),
            )
            return decision

    def claim_request(self, candidate_id, request):
        with closing(self.bots._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = self.get(candidate_id, db=db)
            if row["claim_request"]:
                return row["claim_request"]
            db.execute(
                "UPDATE participation_candidates SET claim_request=? WHERE id=?",
                (canonical(request), candidate_id),
            )
            return request

    def claim_result(self, candidate_id, result):
        with closing(self.bots._db()) as db, db:
            db.execute(
                "UPDATE participation_candidates SET claim_result=? WHERE id=?",
                (canonical(result), candidate_id),
            )

    def registration(self, candidate_id, *, db=None):
        if db is None:
            with closing(self.bots._db()) as connection:
                return self.registration(candidate_id, db=connection)
        row = db.execute(
            "SELECT * FROM participation_registrations WHERE candidate_id=?", (candidate_id,)
        ).fetchone()
        return (
            None
            if row is None
            else {**dict(row), "document": loads(row["document"]), "ingest": loads(row["ingest"])}
        )

    def register(self, candidate_id, document, ingest):
        with closing(self.bots._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            prior = self.registration(candidate_id, db=db)
            if prior:
                require(
                    prior["document"] == document and prior["ingest"] == ingest,
                    "idempotency_conflict",
                    409,
                )
                return prior
            db.execute(
                "INSERT INTO participation_registrations(id,candidate_id,actor_ref,document,ingest) VALUES(?,?,?,?,?)",
                (
                    document["registration_id"],
                    candidate_id,
                    document["admission"]["actor_assertion_ref"],
                    canonical(document),
                    canonical(ingest),
                ),
            )
            return self.registration(candidate_id, db=db)

    def host_registered(self, candidate_id):
        with closing(self.bots._db()) as db, db:
            db.execute(
                "UPDATE participation_registrations SET host_registered=1 WHERE candidate_id=?",
                (candidate_id,),
            )

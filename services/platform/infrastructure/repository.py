"""Latest attempts and the last usable observation, never a second connection/credential store."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from ..contracts import Fault, canonical


class Repository:
    def __init__(self, database):
        self.database = str(database)
        Path(database).parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("""CREATE TABLE IF NOT EXISTS observations (
                node_id TEXT NOT NULL, operation TEXT NOT NULL, revision INTEGER NOT NULL,
                latest TEXT NOT NULL, sample TEXT, PRIMARY KEY(node_id,operation))""")

    def record(self, node_id, operation, revision, result):
        try:
            with closing(sqlite3.connect(self.database, timeout=2)) as db, db:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                old = db.execute(
                    "SELECT revision,sample FROM observations WHERE node_id=? AND operation=?",
                    (node_id, operation),
                ).fetchone()
                # LocalWork owns running SQLite work after its caller is cancelled.
                # A retired connection's late commit cannot replace a newer revision.
                if old and old[0] > revision:
                    return
                sample = old[1] if old and old[0] == revision else None
                if result["status"] in {"ok", "partial"} and result.get("data") is not None:
                    sample = canonical(result)
                db.execute(
                    "INSERT OR REPLACE INTO observations VALUES(?,?,?,?,?)",
                    (node_id, operation, revision, canonical(result), sample),
                )
        except (sqlite3.Error, OSError):
            raise Fault("infrastructure_store_unavailable", 503) from None

    def read(self, node_id, operation, revision):
        try:
            with closing(sqlite3.connect(self.database, timeout=2)) as db:
                row = db.execute(
                    "SELECT revision,latest,sample FROM observations WHERE node_id=? AND operation=?",
                    (node_id, operation),
                ).fetchone()
            if row is None or row[0] != revision:
                return None
            return {"latest": json.loads(row[1]), "sample": json.loads(row[2]) if row[2] else None}
        except (sqlite3.Error, OSError, ValueError):
            raise Fault("infrastructure_store_unavailable", 503) from None

    def keys(self, node_id, prefix, revision):
        try:
            with closing(sqlite3.connect(self.database, timeout=2)) as db:
                return [
                    row[0]
                    for row in db.execute(
                        "SELECT operation FROM observations WHERE node_id=? AND revision=? AND operation LIKE ?",
                        (node_id, revision, prefix + "%"),
                    )
                ]
        except (sqlite3.Error, OSError):
            raise Fault("infrastructure_store_unavailable", 503) from None

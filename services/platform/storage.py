"""SQLite is an explicitly selected local rehearsal store, not PostgreSQL."""

import sqlite3
import uuid
from contextlib import closing, contextmanager
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise ValueError("unsupported platform store version")
            if (
                version == 0
                and db.execute("SELECT 1 FROM sqlite_master WHERE name='origins'").fetchone()
            ):
                # SQLite backup includes WAL; retain a unique pre-migration copy.
                with closing(
                    sqlite3.connect(self.path + ".pre-source-" + uuid.uuid4().hex + ".sqlite")
                ) as backup:
                    db.backup(backup)
            if version == 1:
                # TS-015 native configs join the schema; keep a unique pre-migration copy.
                with closing(
                    sqlite3.connect(self.path + ".pre-native-" + uuid.uuid4().hex + ".sqlite")
                ) as backup:
                    db.backup(backup)
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS origins (
                    ref TEXT PRIMARY KEY, entry_id TEXT NOT NULL, entry_digest TEXT NOT NULL,
                    expires_at REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS revoked_principals (id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS revoked_entries (id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS identities (
                    account TEXT PRIMARY KEY, person_id TEXT NOT NULL, version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS channels (
                    channel TEXT PRIMARY KEY, conversation_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ingest_subjects (
                    account TEXT PRIMARY KEY, person_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS exchanges (
                    ticket TEXT PRIMARY KEY, entry_id TEXT NOT NULL, entry_digest TEXT NOT NULL,
                    prepared_by TEXT NOT NULL, kind TEXT NOT NULL, request TEXT NOT NULL, response TEXT,
                    deadline REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS configs (
                    version INTEGER PRIMARY KEY, document TEXT NOT NULL,
                    digest TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS native_configs (
                    version INTEGER PRIMARY KEY, document TEXT NOT NULL,
                    digest TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, principal TEXT NOT NULL,
                    operation TEXT NOT NULL, object_id TEXT NOT NULL, observed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sources (
                    source_key TEXT NOT NULL, entry_id TEXT NOT NULL, entry_digest TEXT NOT NULL,
                    revision INTEGER NOT NULL, tombstone INTEGER NOT NULL,
                    PRIMARY KEY(source_key, entry_id)
                );
                CREATE TABLE IF NOT EXISTS projections (
                    owner TEXT NOT NULL, job_id TEXT NOT NULL, version INTEGER NOT NULL,
                    document TEXT NOT NULL, PRIMARY KEY(owner, job_id)
                );
                CREATE TABLE IF NOT EXISTS authority_head (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    generation TEXT NOT NULL, sequence INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS authority_policy (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), document TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS input_observations (
                    physical_key TEXT PRIMARY KEY, author TEXT NOT NULL,
                    revision INTEGER NOT NULL, input_digest TEXT NOT NULL, kind TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_inputs (
                    ref TEXT PRIMARY KEY, entry_id TEXT NOT NULL, entry_digest TEXT NOT NULL,
                    ingress TEXT NOT NULL, input_digest TEXT NOT NULL,
                    input_metadata TEXT NOT NULL, expires_at REAL NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS fanout_routes (
                    command_key TEXT PRIMARY KEY, semantic_digest TEXT NOT NULL,
                    effective TEXT NOT NULL, defaults TEXT NOT NULL, routing_version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS actor_origins (
                    ref TEXT PRIMARY KEY REFERENCES origins(ref), input_digest TEXT NOT NULL,
                    entry_document TEXT NOT NULL, issued_at REAL NOT NULL, ingress TEXT NOT NULL,
                    input_entry_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fanout_exchanges (
                    ticket TEXT PRIMARY KEY, command_key TEXT NOT NULL, prepared_by TEXT NOT NULL,
                    request TEXT NOT NULL, access_request TEXT NOT NULL, authority TEXT NOT NULL,
                    response TEXT
                );
                CREATE TABLE IF NOT EXISTS admission_history (
                    receipt_id TEXT PRIMARY KEY, admission TEXT NOT NULL,
                    entry_id TEXT NOT NULL, entry_document TEXT NOT NULL, account TEXT NOT NULL,
                    selector_key TEXT NOT NULL, revision INTEGER NOT NULL,
                    UNIQUE(selector_key, revision)
                );
                PRAGMA user_version=2;
                COMMIT;
            """)
            db.execute(
                "INSERT OR IGNORE INTO authority_head VALUES(1,?,0)",
                ("platform:" + uuid.uuid4().hex,),
            )
            for table in (
                "origins",
                "revoked_entries",
                "revoked_principals",
                "identities",
                "channels",
                "ingest_subjects",
                "authority_policy",
                "input_observations",
                "source_inputs",
                "fanout_routes",
                "admission_history",
            ):
                for event in ("INSERT", "UPDATE", "DELETE"):
                    db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS head_{table}_{event} AFTER {event} ON {table} BEGIN UPDATE authority_head SET sequence=sequence+1 WHERE singleton=1; END"
                    )
            db.commit()

    @contextmanager
    def connect(self, *, write=False, timeout=5):
        db = sqlite3.connect(self.path, timeout=timeout)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

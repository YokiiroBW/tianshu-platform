"""SQLite is an explicitly selected local rehearsal store, not PostgreSQL."""

import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
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
            """)

    @contextmanager
    def connect(self, *, write=False):
        db = sqlite3.connect(self.path, timeout=5)
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

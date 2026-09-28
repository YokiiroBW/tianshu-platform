"""Private encrypted owner of self-service bot adapter connections.

The key and SQLite file are one backup unit. All row data, including endpoints and credentials,
is authenticated and encrypted. An incomplete or damaged owner fails closed at startup.
"""

import ipaddress
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .contracts import Fault, canonical, require


def validate_configuration(config, settings):
    if config is None:
        return
    require(
        isinstance(config, dict) and set(config) == {"directory", "allowed_cidrs", "actors"},
        "invalid_input",
        400,
    )
    require(isinstance(config["directory"], str), "invalid_input", 400)
    root = Path(config["directory"])
    require(root.is_absolute() and not root.is_symlink(), "invalid_input", 400)
    ranges = config["allowed_cidrs"]
    require(isinstance(ranges, list) and 1 <= len(ranges) <= 16, "invalid_input", 400)
    try:
        networks = [ipaddress.ip_network(item, strict=True) for item in ranges]
    except (ValueError, TypeError):
        raise Fault("invalid_input", 400) from None
    require(
        all(n.is_private and not n.is_link_local and not n.is_multicast for n in networks),
        "invalid_input",
        400,
    )
    actors = config["actors"]
    require(isinstance(actors, list) and 1 <= len(actors) <= 32, "invalid_input", 400)
    ids = set()
    for item in actors:
        require(isinstance(item, dict) and set(item) == {"id", "label"}, "invalid_input", 400)
        actor_id, label = item["id"], item["label"]
        require(
            isinstance(actor_id, str) and 1 <= len(actor_id) <= 128 and actor_id not in ids,
            "invalid_input",
            400,
        )
        require(
            isinstance(label, str) and 1 <= len(label) <= 64 and label.isprintable(),
            "invalid_input",
            400,
        )
        ids.add(actor_id)
    bot = settings.get("bot_connections")
    require(isinstance(bot, dict) and "principal" in bot, "invalid_input", 400)
    require(
        settings.get("core") is not None and settings.get("web") is not None, "invalid_input", 400
    )


class BotAdapterCatalog:
    def __init__(self, directory):
        self.root = Path(directory)
        self.database = self.root / "bot-adapters.sqlite"
        self.key_file = self.root / "bot-adapters.key"
        try:
            require(not self.root.is_symlink(), "bot_adapter_store_unavailable", 503)
            if not self.root.exists() or (self.root.is_dir() and not any(self.root.iterdir())):
                if not self.root.exists():
                    self.root.mkdir(mode=0o700, parents=False)
                else:
                    os.chmod(self.root, 0o700)
                with self.key_file.open("xb") as output:
                    os.chmod(self.key_file, 0o600)
                    output.write(AESGCM.generate_key(bit_length=256))
                    output.flush()
                    os.fsync(output.fileno())
                with closing(sqlite3.connect(self.database)) as db:
                    os.chmod(self.database, 0o600)
                    db.executescript("""
                        PRAGMA synchronous=FULL;
                        CREATE TABLE rows (id TEXT PRIMARY KEY, document BLOB NOT NULL);
                        CREATE TABLE receipts (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, document BLOB NOT NULL);
                        PRAGMA user_version=1;
                    """)
            require(
                self.root.is_dir()
                and not self.root.is_symlink()
                and self.database.is_file()
                and not self.database.is_symlink()
                and self.key_file.is_file()
                and not self.key_file.is_symlink(),
                "bot_adapter_store_unavailable",
                503,
            )
            self.key = self.key_file.read_bytes()
            self.cipher = AESGCM(self.key)
            with closing(self._db()) as db:
                require(
                    db.execute("PRAGMA user_version").fetchone()[0] == 1,
                    "bot_adapter_store_unavailable",
                    503,
                )
                for key, blob in db.execute("SELECT id,document FROM rows"):
                    require(
                        self._open(blob, "row:" + key)["id"] == key,
                        "bot_adapter_store_unavailable",
                        503,
                    )
                for key, _, blob in db.execute("SELECT id,fingerprint,document FROM receipts"):
                    self._open(blob, "receipt:" + key)
        except Fault:
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            raise Fault("bot_adapter_store_unavailable", 503) from None

    def _seal(self, value, scope):
        nonce = os.urandom(12)
        return nonce + self.cipher.encrypt(nonce, canonical(value).encode(), scope.encode())

    def _open(self, blob, scope):
        try:
            return json.loads(self.cipher.decrypt(blob[:12], blob[12:], scope.encode()))
        except Exception:
            raise Fault("bot_adapter_store_unavailable", 503) from None

    def _db(self):
        require(
            self.key_file.is_file()
            and not self.key_file.is_symlink()
            and self.key_file.read_bytes() == self.key,
            "bot_adapter_store_unavailable",
            503,
        )
        db = sqlite3.connect(self.database, timeout=5)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def all(self):
        with closing(self._db()) as db:
            return [
                self._open(blob, "row:" + key)
                for key, blob in db.execute("SELECT id,document FROM rows ORDER BY id")
            ]

    def get(self, key):
        with closing(self._db()) as db:
            row = db.execute("SELECT document FROM rows WHERE id=?", (key,)).fetchone()
            return self._open(row[0], "row:" + key) if row else None

    def put(self, item):
        key = item["id"]
        with closing(self._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO rows VALUES(?,?) ON CONFLICT(id) DO UPDATE SET document=excluded.document",
                (key, self._seal(item, "row:" + key)),
            )

    def receipt(self, client_id, fingerprint):
        with closing(self._db()) as db:
            row = db.execute(
                "SELECT fingerprint,document FROM receipts WHERE id=?", (client_id,)
            ).fetchone()
            if row:
                require(row[0] == fingerprint, "idempotency_conflict", 409)
                return self._open(row[1], "receipt:" + client_id)
            return None

    def record(self, client_id, fingerprint, result):
        with closing(self._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT fingerprint FROM receipts WHERE id=?", (client_id,)
            ).fetchone()
            require(previous is None or previous[0] == fingerprint, "idempotency_conflict", 409)
            db.execute(
                "INSERT OR IGNORE INTO receipts VALUES(?,?,?)",
                (client_id, fingerprint, self._seal(result, "receipt:" + client_id)),
            )

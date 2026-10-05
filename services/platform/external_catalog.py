"""Explicitly initialized encrypted owner for administrator supplied peer connections.

The directory, database and key are one backup unit. Missing pieces fail closed; serving a
request never creates or repairs them. SQLite CAS serializes saves across platform processes.
"""

import hashlib
import hmac
import ipaddress
import json
import os
import sqlite3
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .contracts import Fault, canonical, require


def supported_kind(kind):
    return kind in {"assets", "home", "weather"} or (
        isinstance(kind, str)
        and (
            (kind.startswith("images:") and 1 <= len(kind[7:]) <= 128)
            or (kind.startswith("skills:") and 1 <= len(kind[7:]) <= 128)
        )
    )


def validate_configuration(config, settings, *, existing=True):
    if config is None:
        return
    require(
        isinstance(config, dict)
        and set(config) == {"directory", "allowed_cidrs", "assets_connection_id"},
        "invalid_input",
        400,
    )
    require(
        isinstance(config["directory"], str) and Path(config["directory"]).is_absolute(),
        "invalid_input",
        400,
    )
    ranges = config["allowed_cidrs"]
    require(isinstance(ranges, list) and 1 <= len(ranges) <= 16, "invalid_input", 400)
    try:
        networks = [ipaddress.ip_network(item, strict=True) for item in ranges]
    except (ValueError, TypeError):
        raise Fault("invalid_input", 400) from None
    require(
        all(
            network.is_private and not network.is_multicast and not network.is_link_local
            for network in networks
        ),
        "invalid_input",
        400,
    )
    connection = config["assets_connection_id"]
    require(
        isinstance(connection, str)
        and settings.get("asset_connections", {}).get(connection) == {"managed_external": True},
        "invalid_input",
        400,
    )
    page = settings.get("web_assets", {})
    principal = settings.get("principals", {}).get(page.get("principal"), {})
    require(
        page.get("enabled") is True
        and connection in page.get("allowed_connections", [])
        and connection in principal.get("asset_connections", [])
        and "asset.read" in principal.get("actions", []),
        "invalid_input",
        400,
    )
    require(settings.get("home") is None, "invalid_input", 400)
    if existing:
        ExternalCatalog.verify_existing(config["directory"])


class ExternalCatalog:
    """Secret-bearing revisions and last-test receipts; never returns secrets in view()."""

    @staticmethod
    def verify_existing(directory):
        catalog = ExternalCatalog(directory)
        catalog.snapshot()

    def __init__(self, directory, *, create=False):
        self.root = Path(directory).absolute()
        self.database = self.root / "external.sqlite"
        self.key_file = self.root / "external.key"
        try:
            if create:
                self.root.mkdir(mode=0o700, parents=False, exist_ok=False)
                key = AESGCM.generate_key(bit_length=256)
                with self.key_file.open("xb") as output:
                    os.chmod(self.key_file, 0o600)
                    output.write(key)
                    output.flush()
                    os.fsync(output.fileno())
                cipher = AESGCM(key)
                nonce = os.urandom(12)
                seal = nonce + cipher.encrypt(nonce, b"external-catalog-v1:0", b"seal")
                with closing(sqlite3.connect(self.database)) as db, db:
                    os.chmod(self.database, 0o600)
                    db.executescript("""
                        PRAGMA synchronous=FULL;
                        CREATE TABLE metadata (id INTEGER PRIMARY KEY CHECK(id=1),
                          revision INTEGER NOT NULL, seal BLOB NOT NULL);
                        CREATE TABLE connections (kind TEXT PRIMARY KEY,
                          document TEXT NOT NULL, credential BLOB, ca BLOB, test TEXT,
                          mac TEXT NOT NULL);
                        CREATE TABLE receipts (id TEXT PRIMARY KEY,
                          fingerprint TEXT NOT NULL, result TEXT NOT NULL);
                    """)
                    db.execute("INSERT INTO metadata VALUES(1,0,?)", (seal,))
            require(
                self.root.is_dir()
                and not self.root.is_symlink()
                and self.database.is_file()
                and not self.database.is_symlink()
                and self.key_file.is_file()
                and not self.key_file.is_symlink(),
                "external_store_unavailable",
                503,
            )
            self.key = self.key_file.read_bytes()
            self.cipher = AESGCM(self.key)
            with self._read() as db:
                self._revision(db)
                for kind, document, credential, ca, test, mac in db.execute(
                    "SELECT * FROM connections"
                ):
                    require(supported_kind(kind), "external_store_unavailable", 503)
                    require(
                        hmac.compare_digest(mac, self._mac(kind, document, credential, ca, test)),
                        "external_store_unavailable",
                        503,
                    )
                    if credential is not None:
                        self._decrypt(credential, kind + ":credential")
                    if ca is not None:
                        self._decrypt(ca, kind + ":ca")
        except Fault:
            raise
        except Exception:
            raise Fault("external_store_unavailable", 503) from None

    def _encrypt(self, value, identity):
        nonce = os.urandom(12)
        return nonce + self.cipher.encrypt(nonce, value.encode(), identity.encode())

    def _decrypt(self, value, identity):
        try:
            return self.cipher.decrypt(value[:12], value[12:], identity.encode()).decode()
        except Exception:
            raise Fault("external_store_unavailable", 503) from None

    def _mac(self, kind, document, credential, ca, test):
        return hmac.new(
            self.key,
            canonical(
                [
                    kind,
                    document,
                    credential.hex() if credential else None,
                    ca.hex() if ca else None,
                    test,
                ]
            ).encode(),
            hashlib.sha256,
        ).hexdigest()

    def _revision(self, db):
        row = db.execute("SELECT revision,seal FROM metadata WHERE id=1").fetchone()
        require(
            row is not None
            and type(row[0]) is int
            and row[0] >= 0
            and self._decrypt(row[1], "seal") == f"external-catalog-v1:{row[0]}",
            "external_store_unavailable",
            503,
        )
        return row[0]

    @contextmanager
    def _transaction(self):
        db = None
        try:
            require(
                self.key_file.is_file()
                and not self.key_file.is_symlink()
                and hmac.compare_digest(self.key_file.read_bytes(), self.key),
                "external_store_unavailable",
                503,
            )
            db = sqlite3.connect(self.database.as_uri() + "?mode=rw", uri=True, timeout=5)
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except (sqlite3.Error, OSError):
            raise Fault("external_store_unavailable", 503) from None
        finally:
            if db is not None:
                db.close()

    @contextmanager
    def _read(self):
        db = None
        try:
            require(
                self.key_file.is_file()
                and not self.key_file.is_symlink()
                and hmac.compare_digest(self.key_file.read_bytes(), self.key),
                "external_store_unavailable",
                503,
            )
            db = sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True, timeout=5)
            yield db
        except (sqlite3.Error, OSError):
            raise Fault("external_store_unavailable", 503) from None
        finally:
            if db is not None:
                db.close()

    def snapshot(self):
        with self._read() as db:
            revision = self._revision(db)
            rows = {}
            for kind, document, credential, ca, test, mac in db.execute(
                "SELECT * FROM connections"
            ):
                require(
                    hmac.compare_digest(mac, self._mac(kind, document, credential, ca, test)),
                    "external_store_unavailable",
                    503,
                )
                rows[kind] = {
                    "value": json.loads(document),
                    "credential": self._decrypt(credential, kind + ":credential")
                    if credential
                    else None,
                    "ca_pem": self._decrypt(ca, kind + ":ca") if ca else None,
                    "last_test": json.loads(test) if test else None,
                }
            return revision, rows

    def save(self, kind, value, credential, ca, expected_revision, client_id):
        require(supported_kind(kind), "invalid_input", 400)
        require(type(expected_revision) is int and expected_revision >= 0, "invalid_input", 400)
        try:
            require(str(uuid.UUID(client_id)) == client_id, "invalid_input", 400)
        except (ValueError, TypeError):
            raise Fault("invalid_input", 400) from None
        fingerprint = hmac.new(
            self.key,
            canonical([kind, value, credential, ca, expected_revision]).encode(),
            hashlib.sha256,
        ).hexdigest()
        with self._transaction() as db:
            previous = db.execute(
                "SELECT fingerprint,result FROM receipts WHERE id=?", (client_id,)
            ).fetchone()
            if previous:
                require(hmac.compare_digest(previous[0], fingerprint), "idempotency_conflict", 409)
                return json.loads(previous[1])
            revision = self._revision(db)
            require(revision == expected_revision, "revision_conflict", 409)
            row = db.execute(
                "SELECT credential,ca FROM connections WHERE kind=?", (kind,)
            ).fetchone()

            def selected(spec, index, identity):
                action = spec["action"]
                if action == "keep":
                    return row[index] if row else None
                if action == "clear":
                    return None
                return self._encrypt(spec["value"], identity)

            chosen_credential = selected(credential, 0, kind + ":credential")
            chosen_ca = selected(ca, 1, kind + ":ca")
            revision += 1
            document = canonical({**value, "connection_revision": revision})
            db.execute(
                "INSERT OR REPLACE INTO connections VALUES(?,?,?,?,NULL,?)",
                (
                    kind,
                    document,
                    chosen_credential,
                    chosen_ca,
                    self._mac(kind, document, chosen_credential, chosen_ca, None),
                ),
            )
            db.execute(
                "UPDATE metadata SET revision=?,seal=? WHERE id=1",
                (revision, self._encrypt(f"external-catalog-v1:{revision}", "seal")),
            )
            # The catalog commit and the live adapter swap are separate steps. Only the web
            # route may claim applied after its sync succeeds.
            result = {
                "revision": revision,
                "state": "saved_unverified",
                "applied": False,
                "kind": kind,
            }
            db.execute(
                "INSERT INTO receipts VALUES(?,?,?)", (client_id, fingerprint, canonical(result))
            )
            return result

    def record_test(self, kind, revision, result):
        with self._transaction() as db:
            current = self._revision(db)
            require(current == revision, "revision_conflict", 409)
            row = db.execute(
                "SELECT document,credential,ca FROM connections WHERE kind=?", (kind,)
            ).fetchone()
            require(row is not None, "external_not_configured", 503)
            test = canonical(result)
            db.execute(
                "UPDATE connections SET test=?,mac=? WHERE kind=?",
                (test, self._mac(kind, row[0], row[1], row[2], test), kind),
            )


def main():
    """Installation-only init/check; the serving path never invokes init."""
    import argparse

    parser = argparse.ArgumentParser(description="Private external connection catalog")
    parser.add_argument("operation", choices=("init", "check"))
    parser.add_argument("directory")
    args = parser.parse_args()
    if args.operation == "init":
        ExternalCatalog(args.directory, create=True)
    else:
        ExternalCatalog.verify_existing(args.directory)
    print("external_catalog_ready")


if __name__ == "__main__":
    main()

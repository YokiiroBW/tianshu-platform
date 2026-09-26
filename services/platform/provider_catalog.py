"""Private, persistent provider owner. No transport, credentials, or network policy is inferred.

The caller MUST authenticate and authorize administrators before invoking mutations. This
module is deliberately not registered with HTTP or Models. An executor may receive a secret
only through execution_context after trusted authorization. The entire private directory is
one offline backup unit; never restore just the SQLite database or just its encryption key.
"""

import hashlib
import hmac
import json
import os
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .contracts import Fault, canonical, require


PROTOCOL = "openai-chat-completions"
TEST_CODES = frozenset(
    {
        "succeeded",
        "authentication_failed",
        "endpoint_failed",
        "model_not_found",
        "timed_out",
        "cancelled",
        "connection_failed",
        "unknown",
    }
)


@dataclass(frozen=True)
class ProviderExecutionContext:
    """Ephemeral private DTO; repr excludes endpoint/model and credential values."""

    provider_id: str
    revision: int
    protocol: str
    base_url: str = field(repr=False)
    model_id: str = field(repr=False)
    api_key: str = field(repr=False)


def _text(value, maximum):
    require(isinstance(value, str), "invalid_input", 400)
    value = value.strip()
    require(0 < len(value) <= maximum and not any(ord(c) < 32 for c in value), "invalid_input", 400)
    return value


def normalize_base_url(value):
    """Syntax only. Executor still has to enforce DNS, TLS, target and redirect policy."""
    value = _text(value, 2048)
    require(
        not any(ord(c) <= 32 or ord(c) > 126 for c in value)
        and "\\" not in value
        and "%" not in value,
        "invalid_input",
        400,
    )
    try:
        url = urlsplit(value)
        require(
            url.scheme == "https"
            and url.hostname
            and not url.username
            and not url.password
            and not url.query
            and not url.fragment,
            "invalid_input",
            400,
        )
        port = url.port
        require(port is None or 0 < port <= 65535, "invalid_input", 400)
        host = url.hostname.lower()
        require(not any(part in {".", ".."} for part in url.path.split("/")), "invalid_input", 400)
        authority = f"[{host}]" if ":" in host else host
        if port is not None and port != 443:
            authority += f":{port}"
        return urlunsplit(("https", authority, url.path.rstrip("/"), "", ""))
    except ValueError:
        raise Fault("invalid_input", 400) from None


class ProviderCatalog:
    """SQLite serializes writers across processes; encrypted keys commit with public revisions.

    create=True is an explicit first-install operation, not an automatic missing-file recovery.
    Existing directories are never overwritten, including an interrupted initialization.
    On POSIX the directory is 0700, files 0600. Windows deployments must provision an
    administrator/service-only ACL on the parent; chmod does not establish a Windows ACL.
    """

    @staticmethod
    def verify_existing(directory):
        """Read-only preflight of the complete backup unit, including every ciphertext."""
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            root = Path(directory).absolute()
            database, key_file = root / "providers.sqlite", root / "providers.key"
            require(
                root.is_dir()
                and not root.is_symlink()
                and database.is_file()
                and not database.is_symlink()
                and key_file.is_file()
                and not key_file.is_symlink(),
                "provider_store_unavailable",
                503,
            )
            cipher = AESGCM(key_file.read_bytes())
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
                seal = db.execute("SELECT seal FROM metadata WHERE id=1").fetchone()
                require(seal is not None, "provider_store_unavailable", 503)
                value = seal[0]
                require(
                    cipher.decrypt(value[:12], value[12:], b"seal") == b"provider-catalog-v1",
                    "provider_store_unavailable",
                    503,
                )
                for identity, encrypted in db.execute("SELECT id,secret FROM providers"):
                    if encrypted is not None:
                        cipher.decrypt(encrypted[:12], encrypted[12:], identity.encode())
                for table in (
                    "receipts",
                    "test_attempts",
                    "provider_publications",
                    "provider_turn_grants",
                ):
                    require(
                        db.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                        ).fetchone()
                        is not None,
                        "provider_store_unavailable",
                        503,
                    )
        except Fault:
            raise
        except ImportError:
            raise Fault("provider_crypto_unavailable", 503) from None
        except Exception:
            raise Fault("provider_store_unavailable", 503) from None

    def __init__(self, directory, *, create=False, clock=time.time):
        self.directory = Path(directory).absolute()
        self.database = self.directory / "providers.sqlite"
        self.key_file = self.directory / "providers.key"
        self.clock = clock
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            raise Fault("provider_crypto_unavailable", 503) from None
        try:
            if create:
                self.directory.mkdir(mode=0o700, parents=False, exist_ok=False)
                key = AESGCM.generate_key(bit_length=256)
                with self.key_file.open("xb") as handle:
                    os.chmod(self.key_file, 0o600)
                    handle.write(key)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._cipher = AESGCM(key)
                self._key = key
                with closing(sqlite3.connect(self.database)) as db, db:
                    os.chmod(self.database, 0o600)
                    db.executescript("""
                        PRAGMA synchronous=FULL;
                        CREATE TABLE metadata (id INTEGER PRIMARY KEY CHECK(id=1),
                          seal BLOB NOT NULL, default_id TEXT, default_revision INTEGER NOT NULL);
                        CREATE TABLE providers (id TEXT PRIMARY KEY, document TEXT NOT NULL,
                          secret BLOB);
                        CREATE TABLE receipts (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                          result TEXT NOT NULL);
                        CREATE TABLE test_attempts (id TEXT PRIMARY KEY,
                          fingerprint TEXT NOT NULL, state TEXT NOT NULL, result TEXT);
                        CREATE TABLE provider_publications (version INTEGER PRIMARY KEY,
                          provider_id TEXT NOT NULL, provider_revision INTEGER NOT NULL,
                          usable_until REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
                        CREATE TABLE provider_turn_grants (turn_id TEXT PRIMARY KEY,
                          scope_digest TEXT NOT NULL, version INTEGER NOT NULL,
                          caller_service TEXT NOT NULL, workload TEXT NOT NULL);
                    """)
                    db.execute(
                        "INSERT INTO metadata VALUES (1, ?, NULL, 0)",
                        (self._encrypt("provider-catalog-v1", "seal"),),
                    )
            require(
                self.directory.is_dir()
                and not self.directory.is_symlink()
                and self.database.is_file()
                and not self.database.is_symlink()
                and self.key_file.is_file()
                and not self.key_file.is_symlink(),
                "provider_store_unavailable",
                503,
            )
            self._key = self.key_file.read_bytes()
            self._cipher = AESGCM(self._key)
            with self._transaction() as db:
                row = db.execute("SELECT seal FROM metadata WHERE id=1").fetchone()
                require(
                    row is not None and self._decrypt(row[0], "seal") == "provider-catalog-v1",
                    "provider_store_unavailable",
                    503,
                )
                for row in db.execute("SELECT id, document, secret FROM providers"):
                    document = json.loads(row[1])
                    require(
                        document["provider_id"] == row[0]
                        and document["has_key"] == (row[2] is not None),
                        "provider_store_unavailable",
                        503,
                    )
                    if row[2] is not None:
                        self._decrypt(row[2], row[0])
                for table in ("test_attempts", "provider_publications", "provider_turn_grants"):
                    require(
                        db.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                        ).fetchone()
                        is not None,
                        "provider_store_unavailable",
                        503,
                    )
        except Fault:
            raise
        except Exception:
            raise Fault("provider_store_unavailable", 503) from None

    def _encrypt(self, plaintext, identity):
        nonce = os.urandom(12)
        return nonce + self._cipher.encrypt(nonce, plaintext.encode(), identity.encode())

    def _decrypt(self, ciphertext, identity):
        try:
            return self._cipher.decrypt(
                ciphertext[:12], ciphertext[12:], identity.encode()
            ).decode()
        except Exception:
            raise Fault("provider_store_unavailable", 503) from None

    @contextmanager
    def _transaction(self):
        db = None
        try:
            require(
                self.key_file.is_file()
                and not self.key_file.is_symlink()
                and hmac.compare_digest(self.key_file.read_bytes(), self._key),
                "provider_store_unavailable",
                503,
            )
            # mode=rw forbids silently creating a replacement if the live DB disappears.
            db = sqlite3.connect(self.database.as_uri() + "?mode=rw", uri=True, timeout=5)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except (sqlite3.Error, OSError):
            raise Fault("provider_store_unavailable", 503) from None
        finally:
            if db is not None:
                db.close()

    def _mutate(self, client_id, operation, arguments, action):
        client_id = _text(client_id, 128)
        # A keyed fingerprint protects low-entropy API keys from offline receipt guessing.
        fingerprint = hmac.new(
            self._key, canonical([operation, arguments]).encode(), hashlib.sha256
        ).hexdigest()
        with self._transaction() as db:
            receipt = db.execute(
                "SELECT fingerprint, result FROM receipts WHERE id=?", (client_id,)
            ).fetchone()
            if receipt:
                require(hmac.compare_digest(receipt[0], fingerprint), "idempotency_conflict", 409)
                return json.loads(receipt[1])
            result = action(db)
            db.execute(
                "INSERT INTO receipts VALUES (?, ?, ?)", (client_id, fingerprint, canonical(result))
            )
            return result

    @staticmethod
    def _row(db, provider_id, expected_revision):
        require(
            isinstance(provider_id, str)
            and bool(provider_id)
            and type(expected_revision) is int
            and expected_revision > 0,
            "invalid_input",
            400,
        )
        row = db.execute(
            "SELECT document, secret FROM providers WHERE id=?", (provider_id,)
        ).fetchone()
        require(row is not None, "provider_not_found", 404)
        document = json.loads(row[0])
        require(document["revision"] == expected_revision, "revision_conflict", 409)
        return document, row[1]

    @staticmethod
    def _clear_default(db, provider_id):
        db.execute(
            "UPDATE metadata SET default_id=NULL, default_revision=default_revision+1 "
            "WHERE id=1 AND default_id=?",
            (provider_id,),
        )

    def save(
        self,
        *,
        client_id,
        name,
        base_url,
        model_id="",
        protocol=PROTOCOL,
        enabled=True,
        api_key="",
        provider_id=None,
        expected_revision=None,
    ):
        name = _text(name, 120)
        require(
            isinstance(model_id, str)
            and len(model_id) <= 256
            and not any(ord(c) < 32 for c in model_id),
            "invalid_input",
            400,
        )
        model_id = model_id.strip()
        base_url = normalize_base_url(base_url)
        require(
            protocol == PROTOCOL
            and type(enabled) is bool
            and isinstance(api_key, str)
            and len(api_key) <= 8192,
            "invalid_input",
            400,
        )
        api_key = api_key.strip()
        require(not any(ord(c) < 32 for c in api_key), "invalid_input", 400)
        require(
            (provider_id is None and expected_revision is None)
            or (
                isinstance(provider_id, str) and bool(provider_id) and expected_revision is not None
            ),
            "invalid_input",
            400,
        )
        arguments = [
            name,
            base_url,
            model_id,
            protocol,
            enabled,
            api_key,
            provider_id,
            expected_revision,
        ]

        def action(db):
            identity = provider_id or "provider-" + uuid.uuid4().hex
            previous, encrypted = (
                self._row(db, identity, expected_revision) if provider_id else (None, None)
            )
            if api_key:
                encrypted = self._encrypt(api_key, identity)
            document = dict(
                provider_id=identity,
                name=name,
                protocol=protocol,
                base_url=base_url,
                model_id=model_id,
                enabled=enabled,
                revision=previous["revision"] + 1 if previous else 1,
                has_key=encrypted is not None,
                test=None,
            )
            db.execute(
                "INSERT INTO providers VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "document=excluded.document, secret=excluded.secret",
                (identity, canonical(document), encrypted),
            )
            # Every changed revision invalidates the selected tested revision conservatively.
            self._clear_default(db, identity)
            return document

        return self._mutate(client_id, "save", arguments, action)

    def clear_key(self, *, client_id, provider_id, expected_revision):
        def action(db):
            document, _ = self._row(db, provider_id, expected_revision)
            document.update(has_key=False, revision=document["revision"] + 1, test=None)
            db.execute(
                "UPDATE providers SET document=?, secret=NULL WHERE id=?",
                (canonical(document), provider_id),
            )
            self._clear_default(db, provider_id)
            return document

        return self._mutate(client_id, "clear_key", [provider_id, expected_revision], action)

    def delete(self, *, client_id, provider_id, expected_revision):
        def action(db):
            self._row(db, provider_id, expected_revision)
            db.execute("DELETE FROM providers WHERE id=?", (provider_id,))
            self._clear_default(db, provider_id)
            return {"provider_id": provider_id, "deleted": True}

        return self._mutate(client_id, "delete", [provider_id, expected_revision], action)

    def record_test(self, *, client_id, provider_id, expected_revision, outcome):
        """Trusted executor completion only. No response body, exception or model list accepted."""
        require(isinstance(outcome, str) and outcome in TEST_CODES, "invalid_input", 400)

        def action(db):
            return self._record_test(db, provider_id, expected_revision, outcome)

        return self._mutate(client_id, "test", [provider_id, expected_revision, outcome], action)

    def _record_test(self, db, provider_id, expected_revision, outcome):
        document, _ = self._row(db, provider_id, expected_revision)
        require(
            document["enabled"] and document["has_key"] and document["model_id"],
            "provider_unavailable",
            409,
        )
        document["test"] = {
            "revision": expected_revision,
            "outcome": outcome,
            "tested_at": self.clock(),
        }
        db.execute("UPDATE providers SET document=? WHERE id=?", (canonical(document), provider_id))
        if outcome != "succeeded":
            self._clear_default(db, provider_id)
        return document

    def claim_test(self, *, client_id, provider_id, expected_revision):
        """Durably claim one paid test before network IO; an unknown retry never reexecutes."""
        client_id = _text(client_id, 128)
        fingerprint = hmac.new(
            self._key, canonical([provider_id, expected_revision]).encode(), hashlib.sha256
        ).hexdigest()
        with self._transaction() as db:
            existing = db.execute(
                "SELECT fingerprint,state,result FROM test_attempts WHERE id=?", (client_id,)
            ).fetchone()
            if existing:
                require(hmac.compare_digest(existing[0], fingerprint), "idempotency_conflict", 409)
                return {
                    "state": existing[1],
                    "result": json.loads(existing[2]) if existing[2] else None,
                }
            document, _ = self._row(db, provider_id, expected_revision)
            require(
                document["enabled"] and document["has_key"] and document["model_id"],
                "provider_unavailable",
                409,
            )
            db.execute(
                "INSERT INTO test_attempts VALUES (?,?,?,NULL)", (client_id, fingerprint, "pending")
            )
            return {"state": "new", "result": None}

    def finish_test(
        self, *, client_id, provider_id, expected_revision, outcome, error=None, status=None
    ):
        """Commit catalog verdict and replay receipt together after one claimed call."""
        require(
            outcome in TEST_CODES
            and (
                (error is None and status is None and outcome == "succeeded")
                or (
                    error
                    in {
                        "authentication_failed",
                        "endpoint_failed",
                        "model_not_found",
                        "enumeration_unsupported",
                        "connection_failed",
                        "timed_out",
                        "upstream_invalid",
                        "upstream_rejected",
                        "dependency_unavailable",
                    }
                    and type(status) is int
                    and 400 <= status <= 504
                )
            ),
            "invalid_input",
            400,
        )
        with self._transaction() as db:
            row = db.execute(
                "SELECT fingerprint,state,result FROM test_attempts WHERE id=?", (client_id,)
            ).fetchone()
            require(row is not None, "invalid_input", 400)
            fingerprint = hmac.new(
                self._key, canonical([provider_id, expected_revision]).encode(), hashlib.sha256
            ).hexdigest()
            require(hmac.compare_digest(row[0], fingerprint), "idempotency_conflict", 409)
            if row[1] == "settled":
                return json.loads(row[2])
            document = self._record_test(db, provider_id, expected_revision, outcome)
            result = (
                {"provider_id": provider_id, "revision": expected_revision, **document["test"]}
                if error is None
                else {"error": error, "status": status}
            )
            db.execute(
                "UPDATE test_attempts SET state='settled',result=? WHERE id=?",
                (canonical(result), client_id),
            )
            return result

    def set_default(self, *, client_id, provider_id, expected_revision, expected_default_revision):
        def action(db):
            document, _ = self._row(db, provider_id, expected_revision)
            revision = db.execute("SELECT default_revision FROM metadata WHERE id=1").fetchone()[0]
            require(
                type(expected_default_revision) is int and revision == expected_default_revision,
                "default_revision_conflict",
                409,
            )
            require(self._selectable(document), "provider_not_tested", 409)
            db.execute(
                "UPDATE metadata SET default_id=?, default_revision=default_revision+1 WHERE id=1",
                (provider_id,),
            )
            return {
                "provider_id": provider_id,
                "provider_revision": expected_revision,
                "revision": revision + 1,
            }

        return self._mutate(
            client_id,
            "default",
            [provider_id, expected_revision, expected_default_revision],
            action,
        )

    @staticmethod
    def _selectable(document):
        test = document["test"]
        return bool(
            document["enabled"]
            and document["has_key"]
            and test
            and test["revision"] == document["revision"]
            and test["outcome"] == "succeeded"
        )

    def view(self):
        with self._transaction() as db:
            documents = [
                json.loads(row[0])
                for row in db.execute("SELECT document FROM providers ORDER BY id")
            ]
            identity, revision = db.execute(
                "SELECT default_id, default_revision FROM metadata WHERE id=1"
            ).fetchone()
            selected = next((d for d in documents if d["provider_id"] == identity), None)
            return {
                "providers": documents,
                "default": {
                    "provider_id": identity,
                    "revision": revision,
                    "provider_revision": selected["revision"] if selected else None,
                    "configured": bool(selected and self._selectable(selected)),
                },
            }

    def execution_context(self, provider_id, expected_revision):
        """Only trusted internal callers; this is NOT a public projection or a live grant."""
        with self._transaction() as db:
            document, encrypted = self._row(db, provider_id, expected_revision)
            require(document["enabled"] and encrypted is not None, "provider_unavailable", 409)
            return ProviderExecutionContext(
                provider_id,
                document["revision"],
                document["protocol"],
                document["base_url"],
                document["model_id"],
                self._decrypt(encrypted, provider_id),
            )

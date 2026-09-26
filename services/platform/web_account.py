"""One household login, persisted separately from deployment-owned service authority.

Construction and reads never create a database. Only an explicit setup/claim writes.
The seal survives loss of the account database and prevents reopening registration.
Both files belong to the same persistent deployment volume and backup set.
"""

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path

from .auth import secret
from .contracts import Fault, require


def password_hash(password, salt=None):
    require(isinstance(password, str) and 12 <= len(password) <= 256, "invalid_input", 400)
    salt = salt or secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return "scrypt-v1$" + salt.hex() + "$" + key.hex()


def validate_hash(value):
    require(
        isinstance(value, str)
        and re.fullmatch(r"scrypt-v1\$[0-9a-f]{32}\$[0-9a-f]{64}", value) is not None,
        "invalid_input",
        400,
    )


def validate_username(value):
    require(
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and value == value.strip()
        and all(ord(c) >= 32 and ord(c) != 127 for c in value),
        "invalid_input",
        400,
    )


def verify_password(password, verifier):
    require(isinstance(password, str) and 12 <= len(password) <= 256, "unauthorized", 401)
    return hmac.compare_digest(
        password_hash(password, bytes.fromhex(verifier.split("$")[1])), verifier
    )


def inspect_account(database_path):
    """Read the account at its fixed deployment path, including its loss-detection seal."""
    path = Path(str(database_path) + ".web-account.sqlite")
    seal = Path(str(path) + ".sealed")
    try:
        require(not path.is_symlink() and not seal.is_symlink(), "account_unavailable", 503)
        if not path.exists():
            require(not seal.exists(), "account_unavailable", 503)
            return None
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.25)
        ) as db:
            db.execute("PRAGMA query_only=ON")
            return WebAccount.read_record(db, seal)
    except (OSError, sqlite3.Error, ValueError, TypeError):
        raise Fault("account_unavailable", 503) from None


class WebAccount:
    def __init__(self, settings):
        self.config = settings.get("web_account")
        self.database_path = settings.get("database_path", "")
        self.path = Path(str(settings.get("database_path", "")) + ".web-account.sqlite")
        self.seal = Path(str(self.path) + ".sealed")
        if self.config is None:
            # Removing the opt-in setting is not a password reset or a rollback instruction.
            # Existing account state must never reactivate a deployment's superseded hash.
            self.read()
            return
        c = self.config
        require(isinstance(c, dict) and c.get("mode") in {"create", "claim"}, "invalid_input", 400)
        expected = {"mode", "setup_token_env"} if c["mode"] == "create" else {"mode"}
        require(set(c) == expected and isinstance(settings.get("web"), dict), "invalid_input", 400)
        web = settings["web"]
        if c["mode"] == "create":
            require(
                isinstance(c["setup_token_env"], str)
                and re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", c["setup_token_env"])
                and "username" not in web
                and "password_hash" not in web,
                "invalid_input",
                400,
            )
        else:
            validate_username(web.get("username"))
            validate_hash(web.get("password_hash"))

    def read(self):
        if self.config is None:
            require(
                not any(p.exists() or p.is_symlink() for p in (self.path, self.seal)),
                "account_unavailable",
                503,
            )
            return None
        return inspect_account(self.database_path)

    @staticmethod
    def read_record(db, seal):
        require(db.execute("PRAGMA user_version").fetchone()[0] == 1, "account_unavailable", 503)
        rows = db.execute(
            "SELECT singleton, username, password_hash, version FROM administrator"
        ).fetchall()
        require(len(rows) == 1, "account_unavailable", 503)
        singleton, username, verifier, version = rows[0]
        require(singleton == 1 and version == 1 and seal.is_file(), "account_unavailable", 503)
        require(seal.read_bytes() == b"web-account-v1\n", "account_unavailable", 503)
        try:
            validate_username(username)
            validate_hash(verifier)
        except Fault:
            raise Fault("account_unavailable", 503) from None
        return {"username": username, "password_hash": verifier, "version": version}

    def setup_secret(self):
        value = secret(self.config["setup_token_env"])
        require(value is not None, "setup_unavailable", 503)
        return value

    def check_setup_token(self, supplied, reserved=()):
        require(self.config is not None and self.config["mode"] == "create", "setup_closed", 409)
        require(self.read() is None, "setup_closed", 409)
        expected = self.setup_secret()
        require(
            all(
                secret(name) != expected
                for name in reserved
                if name != self.config["setup_token_env"]
            ),
            "setup_unavailable",
            503,
        )
        require(
            isinstance(supplied, str)
            and len(supplied) <= 4096
            and hmac.compare_digest(supplied.encode(), expected.encode()),
            "unauthorized",
            401,
        )

    def _seal(self):
        try:
            descriptor = os.open(self.seal, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            require(self.seal.read_bytes() == b"web-account-v1\n", "account_unavailable", 503)
            return
        with os.fdopen(descriptor, "wb") as output:
            output.write(b"web-account-v1\n")
            output.flush()
            os.fsync(output.fileno())
        if os.name != "nt":
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    def create(self, username, verifier):
        """Only called after setup-token or current-account authentication by the console.

        A database transaction, not the console's in-process lock, chooses the sole winner.
        The durable seal precedes committing the account. An interruption cannot acknowledge
        an account whose guard was never persisted.
        """
        validate_username(username)
        validate_hash(verifier)
        require(self.config is not None, "setup_closed", 409)
        require(self.read() is None, "setup_closed", 409)
        try:
            # Exclusive creation avoids accidentally creating a replacement for a lost account.
            try:
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(descriptor)
            except FileExistsError:
                pass
            with closing(
                sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)
            ) as db:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                try:
                    version = db.execute("PRAGMA user_version").fetchone()[0]
                    if version == 0:
                        require(not self.seal.exists(), "account_unavailable", 503)
                        require(
                            not db.execute("SELECT name FROM sqlite_master").fetchall(),
                            "account_unavailable",
                            503,
                        )
                        db.execute(
                            "CREATE TABLE administrator (singleton INTEGER PRIMARY KEY CHECK(singleton=1), username TEXT NOT NULL, password_hash TEXT NOT NULL, version INTEGER NOT NULL CHECK(version=1))"
                        )
                        db.execute("PRAGMA user_version=1")
                    else:
                        require(self.read_record(db, self.seal) is None, "setup_closed", 409)
                    self._seal()
                    db.execute(
                        "INSERT INTO administrator VALUES (1, ?, ?, 1)", (username, verifier)
                    )
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
        except (OSError, sqlite3.Error):
            raise Fault("account_unavailable", 503) from None
        return self.read()

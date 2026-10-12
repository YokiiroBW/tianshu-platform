"""Shared provider connections and their model entries in the existing private catalogue.

The historical provider_id is the stable model-entry ID used by roles and runtime
grants. Keeping it avoids rebinding existing roles when connections are separated.
"""

import json
import os
import sqlite3
import uuid
from contextlib import closing

from .contracts import canonical, require
from .provider_values import PROTOCOL, ProviderExecutionContext, text as _text, normalize_base_url

MODEL_KINDS = frozenset({"text", "embed", "rerank"})
DERIVED = frozenset(
    {"base_url", "protocol", "has_key", "source_name", "source_revision", "source_enabled"}
)


class ProviderSources:
    def __init__(self, catalog):
        self.catalog = catalog

    def initialize(self, db, *, create):
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='provider_sources'").fetchone():
            for row in db.execute("SELECT id,document,secret FROM provider_sources"):
                value = json.loads(row["document"])
                require(
                    value["source_id"] == row["id"]
                    and value["has_key"] == (row["secret"] is not None),
                    "provider_store_unavailable",
                    503,
                )
                if row["secret"] is not None:
                    self.catalog._decrypt(row["secret"], row["id"])
            return
        if not create:
            backup = self.catalog.directory / (
                "providers.pre-sources-" + uuid.uuid4().hex + ".sqlite"
            )
            with closing(
                sqlite3.connect(self.catalog.database.as_uri() + "?mode=ro", uri=True)
            ) as source:
                with closing(sqlite3.connect(backup)) as destination:
                    os.chmod(backup, 0o600)
                    source.backup(destination)
        db.execute(
            "CREATE TABLE provider_sources (id TEXT PRIMARY KEY, document TEXT NOT NULL, secret BLOB)"
        )
        for row in db.execute("SELECT id,document,secret FROM providers").fetchall():
            model = json.loads(row["document"])
            identity = "source-" + uuid.uuid4().hex
            source = dict(
                source_id=identity,
                name=model["name"],
                base_url=model["base_url"],
                protocol=model["protocol"],
                enabled=True,
                has_key=model["has_key"],
                revision=1,
            )
            encrypted = (
                self.catalog._encrypt(self.catalog._decrypt(row["secret"], row["id"]), identity)
                if row["secret"] is not None
                else None
            )
            db.execute(
                "INSERT INTO provider_sources VALUES (?,?,?)",
                (identity, canonical(source), encrypted),
            )
            model.update(source_id=identity, kind="text")
            self.store_model(db, model)

    def row(self, db, source_id, expected_revision=...):
        require(isinstance(source_id, str) and bool(source_id), "invalid_input", 400)
        row = db.execute(
            "SELECT document,secret FROM provider_sources WHERE id=?", (source_id,)
        ).fetchone()
        require(row is not None, "provider_not_found", 404)
        source = json.loads(row["document"])
        if expected_revision is not Ellipsis:
            require(type(expected_revision) is int and expected_revision > 0, "invalid_input", 400)
            require(source["revision"] == expected_revision, "revision_conflict", 409)
        return source, row["secret"]

    def project(self, db, model):
        source, encrypted = self.row(db, model["source_id"])
        return {
            **model,
            **{key: source[key] for key in ("base_url", "protocol", "has_key")},
            "source_name": source["name"],
            "source_revision": source["revision"],
            "source_enabled": source["enabled"],
        }, encrypted

    @staticmethod
    def store_model(db, model):
        stored = {key: value for key, value in model.items() if key not in DERIVED}
        db.execute(
            "INSERT INTO providers VALUES (?,?,NULL) ON CONFLICT(id) DO UPDATE SET document=excluded.document,secret=NULL",
            (model["provider_id"], canonical(stored)),
        )

    @staticmethod
    def view(db):
        return [
            json.loads(row[0])
            for row in db.execute("SELECT document FROM provider_sources ORDER BY rowid")
        ]

    def _save(
        self,
        db,
        *,
        name,
        base_url,
        protocol,
        enabled,
        api_key,
        source_id=None,
        expected_revision=None,
    ):
        identity = source_id or "source-" + uuid.uuid4().hex
        previous, encrypted = (
            self.row(db, identity, expected_revision) if source_id else (None, None)
        )
        connection_changed = previous is not None and (
            any(
                previous[key] != value
                for key, value in (
                    ("base_url", base_url),
                    ("protocol", protocol),
                    ("enabled", enabled),
                )
            )
            or bool(
                api_key
                and (encrypted is None or self.catalog._decrypt(encrypted, identity) != api_key)
            )
        )
        if api_key:
            encrypted = self.catalog._encrypt(api_key, identity)
        source = dict(
            source_id=identity,
            name=name,
            base_url=base_url,
            protocol=protocol,
            enabled=enabled,
            has_key=encrypted is not None,
            revision=previous["revision"] + 1 if previous else 1,
        )
        db.execute(
            "INSERT INTO provider_sources VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET document=excluded.document,secret=excluded.secret",
            (identity, canonical(source), encrypted),
        )
        if connection_changed:
            self.invalidate(db, identity)
        return source

    def invalidate(self, db, source_id):
        for row in db.execute(
            "SELECT document FROM providers WHERE json_extract(document,'$.source_id')=?",
            (source_id,),
        ).fetchall():
            model = json.loads(row[0])
            model.update(revision=model["revision"] + 1, test=None)
            self.store_model(db, model)
            self.catalog._clear_default(db, model["provider_id"])

    def save(
        self,
        *,
        client_id,
        name,
        base_url,
        api_key="",
        protocol=PROTOCOL,
        enabled=True,
        source_id=None,
        expected_revision=None,
    ):
        name, base_url = _text(name, 120), normalize_base_url(base_url)
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
            (source_id is None and expected_revision is None)
            or (
                isinstance(source_id, str)
                and bool(source_id)
                and type(expected_revision) is int
                and expected_revision > 0
            ),
            "invalid_input",
            400,
        )
        args = dict(
            name=name,
            base_url=base_url,
            api_key=api_key,
            protocol=protocol,
            enabled=enabled,
            source_id=source_id,
            expected_revision=expected_revision,
        )
        return self.catalog._mutate(
            client_id, "source-save", args, lambda db: self._save(db, **args)
        )

    def save_legacy(
        self,
        db,
        *,
        name,
        base_url,
        model_id,
        protocol,
        enabled,
        api_key,
        provider_id,
        expected_revision,
    ):
        """Older clients and fixtures still save a single model; storage stays shared."""
        previous, _ = (
            self.catalog._row(db, provider_id, expected_revision) if provider_id else (None, None)
        )
        source = self._save(
            db,
            name=name,
            base_url=base_url,
            protocol=protocol,
            enabled=previous["source_enabled"] if previous else True,
            api_key=api_key,
            source_id=previous["source_id"] if previous else None,
            expected_revision=previous["source_revision"] if previous else None,
        )
        model = dict(
            provider_id=provider_id or "provider-" + uuid.uuid4().hex,
            source_id=source["source_id"],
            name=name,
            model_id=model_id,
            kind=previous["kind"] if previous else "text",
            enabled=enabled,
            revision=previous["revision"] + 1 if previous else 1,
            test=None,
        )
        self.store_model(db, model)
        self.catalog._clear_default(db, model["provider_id"])
        return self.project(db, model)[0]

    def add_models(self, *, client_id, source_id, expected_revision, models, kind="text"):
        require(
            isinstance(kind, str)
            and kind in MODEL_KINDS
            and isinstance(models, list)
            and 0 < len(models) <= 256,
            "invalid_input",
            400,
        )
        models = list(dict.fromkeys(_text(item, 256) for item in models))

        def action(db):
            self.row(db, source_id, expected_revision)
            result = []
            for identity in models:
                existing = db.execute(
                    "SELECT id FROM providers WHERE json_extract(document,'$.source_id')=? AND json_extract(document,'$.model_id')=? AND json_extract(document,'$.kind')=?",
                    (source_id, identity, kind),
                ).fetchone()
                if existing:
                    result.append(existing[0])
                    continue
                model = dict(
                    provider_id="provider-" + uuid.uuid4().hex,
                    source_id=source_id,
                    name=identity[:120],
                    model_id=identity,
                    kind=kind,
                    enabled=True,
                    revision=1,
                    test=None,
                )
                self.store_model(db, model)
                result.append(model["provider_id"])
            return {"provider_ids": result}

        return self.catalog._mutate(
            client_id, "model-add", [source_id, expected_revision, models, kind], action
        )

    def save_model(
        self, *, client_id, provider_id, expected_revision, name, model_id, kind, enabled
    ):
        name, model_id = _text(name, 120), _text(model_id, 256)
        require(
            isinstance(kind, str) and kind in MODEL_KINDS and type(enabled) is bool,
            "invalid_input",
            400,
        )

        def action(db):
            model, _ = self.catalog._row(db, provider_id, expected_revision)
            duplicate = db.execute(
                "SELECT id FROM providers WHERE id<>? AND json_extract(document,'$.source_id')=? AND json_extract(document,'$.model_id')=? AND json_extract(document,'$.kind')=?",
                (provider_id, model["source_id"], model_id, kind),
            ).fetchone()
            require(duplicate is None, "model_already_added", 409)
            changed = any(
                model[key] != value
                for key, value in (("model_id", model_id), ("kind", kind), ("enabled", enabled))
            )
            model.update(name=name, model_id=model_id, kind=kind, enabled=enabled)
            if changed:
                model.update(revision=model["revision"] + 1, test=None)
                self.catalog._clear_default(db, provider_id)
            self.store_model(db, model)
            return model

        return self.catalog._mutate(
            client_id,
            "model-save",
            [provider_id, expected_revision, name, model_id, kind, enabled],
            action,
        )

    def clear_key(self, *, client_id, source_id, expected_revision):
        return self.catalog._mutate(
            client_id,
            "source-clear-key",
            [source_id, expected_revision],
            lambda db: self._clear_key(db, source_id, expected_revision),
        )

    def _clear_key(self, db, source_id, expected_revision):
        source, _ = self.row(db, source_id, expected_revision)
        source.update(has_key=False, revision=source["revision"] + 1)
        db.execute(
            "UPDATE provider_sources SET document=?,secret=NULL WHERE id=?",
            (canonical(source), source_id),
        )
        self.invalidate(db, source_id)
        return source

    def delete(self, *, client_id, source_id, expected_revision):
        def action(db):
            self.row(db, source_id, expected_revision)
            for row in db.execute(
                "SELECT id FROM providers WHERE json_extract(document,'$.source_id')=?",
                (source_id,),
            ).fetchall():
                self.catalog._clear_default(db, row[0])
            db.execute(
                "DELETE FROM providers WHERE json_extract(document,'$.source_id')=?", (source_id,)
            )
            db.execute("DELETE FROM provider_sources WHERE id=?", (source_id,))
            return {"source_id": source_id, "deleted": True}

        return self.catalog._mutate(
            client_id, "source-delete", [source_id, expected_revision], action
        )

    def execution_context(self, source_id, expected_revision):
        with self.catalog._transaction() as db:
            source, encrypted = self.row(db, source_id, expected_revision)
            require(source["enabled"] and encrypted is not None, "provider_unavailable", 409)
            return ProviderExecutionContext(
                source_id,
                source["revision"],
                source["protocol"],
                source["base_url"],
                "",
                self.catalog._decrypt(encrypted, source_id),
            )

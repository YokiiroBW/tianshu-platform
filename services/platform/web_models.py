"""Explicit local-operator model management behind the same-origin web console.

The browser only ever sends a registered template identifier, the version it last read and
a client id. Provider addresses, credential references, namespaces, capabilities and the
validity window are resolved from deployment settings on the server, so no URL or
credential can be injected from a page. Configuration authority stays with
`Platform.Models`: this module performs no writes of its own except the publish replay
ledger, and reads only redacted projections.
"""

import asyncio
import math
import sqlite3
import uuid
from contextlib import closing

from .auth import secret
from .contracts import Fault, canonical, digest, epoch, loads, require, utc

# Fixed target registry: schema shape, protocol pin and workload per published contract.
TARGETS = {
    "chat": {
        "label": "Chat 兼容配置",
        "detail": "text-dialogue/v1 · companion.text",
        "protocol": "openai-chat-completions",
        "workload": "companion.text",
        "table": "configs",
        "version_key": "config_version",
    },
    "native": {
        "label": "原生 Responses 配置",
        "detail": "model-protocol/v1 · native.responses",
        "protocol": "openai-responses",
        "workload": "native.responses",
        "table": "native_configs",
        "version_key": "native_config_version",
    },
}
TEMPLATE_KEYS = {
    "template_id",
    "label",
    "target",
    "provider_id",
    "model_id",
    "verified_capabilities",
    "lifetime_seconds",
    "timeout_ms",
}
MANAGEMENT_ACTIONS = {"config.publish", "config.revoke", "config.view"}
VERSION_HISTORY = 20
# Two durable states: prepared means "claimed, outcome unknown", committed means "receipt held".
PREPARED = "prepared"
COMMITTED = "committed"
INTENT_COLUMNS = (
    "client_id",
    "semantic",
    "target",
    "version",
    "expected_version",
    "digest",
    "published_at",
    "usable_until",
    "state",
    "result",
    "prepared_at",
)
INTENT_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS publication_intents ("
    "client_id TEXT PRIMARY KEY, semantic TEXT NOT NULL, target TEXT NOT NULL, "
    "version INTEGER NOT NULL, expected_version INTEGER, digest TEXT NOT NULL, "
    "published_at TEXT NOT NULL, usable_until TEXT NOT NULL, state TEXT NOT NULL, "
    "result TEXT, prepared_at REAL NOT NULL, settled_at REAL)"
)
REQUIRED_REGISTRATION = {
    "base_url",
    "credential_ref",
    "credential_namespace",
    "capability_verification",
    "verified_capabilities",
    "model_ids",
}


class WebModels:
    """Console-side management authority; writes are delegated to the Models owner."""

    def __init__(self, platform, console):
        self.p = platform
        self.console = console
        self.config = platform.settings.get("web_models")
        self.enabled = False
        self.templates = {}
        self.unlock_ttl = 900
        self.ledger_path = platform.store.path + ".web-models.sqlite"
        if self.config is not None:
            self._configure()

    def _configure(self):
        c = self.config
        require(isinstance(c, dict), "invalid_input", 400)
        require(set(c) <= {"enabled", "unlock_ttl_seconds", "templates"}, "invalid_input", 400)
        require(type(c.get("enabled", False)) is bool, "invalid_input", 400)
        ttl = c.get("unlock_ttl_seconds", 900)
        require(type(ttl) is int and 60 <= ttl <= 3600, "invalid_input", 400)
        items = c.get("templates", [])
        require(isinstance(items, list) and 1 <= len(items) <= 32, "invalid_input", 400)
        for item in items:
            self._template(item)
        require(len(self.templates) == len(items), "invalid_input", 400)
        self.unlock_ttl = ttl
        self.enabled = c.get("enabled", False)
        if self.enabled:
            with closing(sqlite3.connect(self.ledger_path, timeout=5)) as db:
                db.execute("PRAGMA journal_mode=WAL")
                # Prepared before the authoritative write, settled after: the recovery record.
                db.execute(INTENT_SCHEMA)
                db.commit()

    def _template(self, item):
        """Templates are reviewed deployment input; every provider field is checked here."""
        require(isinstance(item, dict), "invalid_input", 400)
        require(
            set(item) <= TEMPLATE_KEYS
            and {"template_id", "label", "target", "provider_id", "verified_capabilities"}
            <= set(item),
            "invalid_input",
            400,
        )
        self.p.contracts.check("common#id", item["template_id"])
        require(item["template_id"] not in self.templates, "invalid_input", 400)
        require(
            isinstance(item["label"], str) and 1 <= len(item["label"]) <= 64, "invalid_input", 400
        )
        require(item["target"] in TARGETS, "invalid_input", 400)
        target = TARGETS[item["target"]]
        registration = self.p.models.registrations.get(item["provider_id"])
        require(registration is not None, "dependency_unavailable", 503)
        require(REQUIRED_REGISTRATION <= set(registration), "invalid_input", 400)
        # The registry protocol pin decides the target; one provider never serves both.
        if item["target"] == "native":
            require(registration.get("protocol") == target["protocol"], "invalid_input", 400)
        else:
            require(
                registration.get("protocol") in (None, target["protocol"]), "invalid_input", 400
            )
        require(
            isinstance(registration["model_ids"], list) and registration["model_ids"],
            "invalid_input",
            400,
        )
        model_id = item.get("model_id", registration["model_ids"][0])
        require(model_id in registration["model_ids"], "invalid_input", 400)
        capabilities = item["verified_capabilities"]
        require(
            isinstance(capabilities, list)
            and len(set(capabilities)) == len(capabilities)
            and set(capabilities) <= set(registration["verified_capabilities"]),
            "invalid_input",
            400,
        )
        lifetime = item.get("lifetime_seconds", 300)
        require(
            type(lifetime) is int and 1 <= lifetime <= self.p.models.max_lifetime,
            "invalid_input",
            400,
        )
        timeout = item.get("timeout_ms", 30000)
        require(type(timeout) is int and 1 <= timeout <= 120000, "invalid_input", 400)
        self.templates[item["template_id"]] = {
            "template_id": item["template_id"],
            "label": item["label"],
            "target": item["target"],
            "provider_id": item["provider_id"],
            "model_id": model_id,
            "verified_capabilities": list(capabilities),
            "lifetime_seconds": lifetime,
            "timeout_ms": timeout,
        }

    def code(self, session=None):
        """One honest state word per request; never a success for a disabled capability."""
        if not self.enabled:
            return "management_disabled"
        principal = self.p.auth.principals.get(self.console.config["principal"], {})
        if not MANAGEMENT_ACTIONS <= set(principal.get("actions", [])):
            return "operator_not_authorized"
        if session is not None and session.get("management", 0) <= self.console.clock():
            return "management_required"
        return "ready"

    def _gate(self, session):
        code = self.code(session)
        require(code == "ready", code, 403)

    def operator_header(self):
        principal = self.p.auth.principals[self.console.config["principal"]]
        return "Bearer " + (secret(principal["token_env"]) or "")

    async def route(self, path, body, session):
        if path == "/api/web/models/view":
            require(body == {}, "invalid_input", 400)
            return self.view(session)
        if path == "/api/web/models/unlock":
            return await self.unlock(body, session)
        if path == "/api/web/models/lock":
            require(body == {}, "invalid_input", 400)
            session.pop("management", None)
            return {"unlocked": False, "code": self.code(session)}
        if path == "/api/web/models/preview":
            return self.preview(body, session)
        if path == "/api/web/models/publish":
            return self.publish(body, session)
        if path == "/api/web/models/revoke":
            return self.revoke(body, session)
        raise Fault("not_found", 404)

    async def unlock(self, body, session):
        """Re-authentication step: an ordinary chat login never carries management rights."""
        require(set(body) == {"password"}, "invalid_input", 400)
        self._require_available()
        password = body["password"]
        require(isinstance(password, str) and 12 <= len(password) <= 256, "unauthorized", 401)
        console = self.console
        # Serialize with the login path and share its single failure budget.
        async with console.login_lock:
            console.failures = [t for t in console.failures if t > console.clock() - 60]
            require(len(console.failures) < 5, "too_many_requests", 429)
            valid = await asyncio.to_thread(console.verify_password, password)
            if not valid:
                console.failures.append(console.clock())
                raise Fault("unauthorized", 401)
        session["management"] = console.clock() + self.unlock_ttl
        return {
            "unlocked": True,
            "code": self.code(session),
            "expires_in": self.unlock_ttl,
        }

    def _require_available(self):
        code = self.code()
        require(code == "ready", code, 403)

    def view(self, session):
        """Redacted management projection: identifiers and lifecycles, never endpoints."""
        code = self.code(session)
        available = code in {"ready", "management_required"}
        return {
            "management": {
                "available": available,
                "code": code,
                "unlocked": code == "ready",
                "unlock_ttl_seconds": self.unlock_ttl,
                "templates": [self._template_view(t) for t in self.templates.values()]
                if available
                else [],
            },
            "targets": [self._target_view(name) for name in TARGETS],
        }

    def preview(self, body, session):
        self._gate(session)
        require(set(body) == {"template_id"}, "invalid_input", 400)
        template = self._template_of(body["template_id"])
        expected = self._current(template["target"])
        published = math.floor(self.p.models.clock())
        document = self._document(
            template,
            (expected or 0) + 1,
            utc(published),
            utc(published + template["lifetime_seconds"]),
        )
        version_key = TARGETS[template["target"]]["version_key"]
        return {
            "template": self._template_view(template),
            "target": template["target"],
            "expected_version": expected,
            "version": document[version_key],
            "published_at": document["published_at"],
            "usable_until": document["usable_until"],
            "providers": [self._provider_view(p) for p in document["providers"]],
            "bindings": [self._binding_view(b) for b in document["bindings"]],
        }

    def publish(self, body, session):
        """Publish one registered template at the version the operator last read.

        The authoritative configuration store and the console ledger are separate databases,
        so the outcome is made recoverable instead of assumed: a durable intent is written
        *before* the authoritative write, and every answer is reconciled against the
        authoritative table. A request whose publication committed but whose receipt was lost
        (I/O failure or process death between the two writes) therefore recovers as the same
        result instead of a false conflict, and a failure is only reported when the
        authoritative state really is unchanged.
        """
        self._gate(session)
        require(set(body) == {"template_id", "expected_version", "client_id"}, "invalid_input", 400)
        template = self._template_of(body["template_id"])
        expected = body["expected_version"]
        require(expected is None or type(expected) is int and expected > 0, "invalid_input", 400)
        client_id = body["client_id"]
        require(isinstance(client_id, str) and len(client_id) == 36, "invalid_input", 400)
        try:
            uuid.UUID(client_id)
        except ValueError:
            raise Fault("invalid_input", 400) from None
        semantic = digest(
            {
                "template_id": template["template_id"],
                "target": template["target"],
                "expected_version": expected,
                "principal": self.console.config["principal"],
            }
        )
        intent = self._intent(client_id)
        if intent is None:
            intent = self._prepare(client_id, semantic, template, expected)
        # One client id is one reviewed intent: different content never reuses the receipt.
        require(intent["semantic"] == semantic, "idempotency_conflict", 409)
        return self._settle(template, intent)

    def _settle(self, template, intent):
        """Answer from the authoritative table first; publish only if nothing is committed."""
        target = intent["target"]
        row = self._published(intent)
        if row is not None:
            # The intended document, at the intended version, is what the authority holds.
            result = self._result(intent)
            self._receipt(intent, result)
            return result
        if intent["state"] == COMMITTED:
            # A receipt without its authoritative publication is never reported as success.
            raise Fault("publication_unverified", 503)
        require(self._current(target) == intent["expected_version"], "version_conflict", 409)
        if (
            not epoch(intent["published_at"])
            <= self.p.models.clock()
            < epoch(intent["usable_until"])
        ):
            # The prepared window is immutable; an expired one needs a fresh preview.
            raise Fault("version_conflict", 409)
        document = self._document(
            template, intent["version"], intent["published_at"], intent["usable_until"]
        )
        require(digest(self._stable(document)) == intent["digest"], "version_conflict", 409)
        header = self.operator_header()
        if target == "native":
            outcome = self.p.models.native_publish(header, document)
        else:
            outcome = self.p.models.publish(header, document)
        result = {
            "target": target,
            "version": outcome[TARGETS[target]["version_key"]],
            "expected_version": intent["expected_version"],
            "state": "published",
            "deduplicated": outcome["deduplicated"],
            "usable_until": document["usable_until"],
        }
        self._receipt(intent, result)
        return result

    def revoke(self, body, session):
        self._gate(session)
        require(set(body) == {"target", "version"}, "invalid_input", 400)
        require(body["target"] in TARGETS and type(body["version"]) is int, "invalid_input", 400)
        version = body["version"]
        header = self.operator_header()
        if body["target"] == "native":
            self.p.models.native_revoke(header, version)
        else:
            self.p.models.revoke(header, version)
        return {"target": body["target"], "version": version, "revoked": True}

    def _template_of(self, template_id):
        require(isinstance(template_id, str), "invalid_input", 400)
        template = self.templates.get(template_id)
        require(template is not None, "not_found", 404)
        return template

    def _stable(self, document):
        """The correlation-only request_id is excluded, exactly as the Models owner stores it."""
        return {k: v for k, v in document.items() if k != "request_id"}

    def _document(self, template, version, published_at, usable_until):
        """Build the published shape from reviewed registration data and a fixed window.

        The window is anchored to the whole second the request was served in: a raw clock
        reading is rounded by the contract's date-time encoding, and a rounded-up reading
        can land just after the write transaction's own clock check. Whole seconds
        round-trip exactly, so the window stays exactly `lifetime_seconds` long and the
        publication is never rejected for a timestamp it created itself.
        """
        registration = self.p.models.registrations[template["provider_id"]]
        target = TARGETS[template["target"]]
        provider = {
            key: registration[key]
            for key in (
                "base_url",
                "credential_ref",
                "credential_namespace",
                "capability_verification",
            )
        }
        provider.update(
            provider_id=template["provider_id"],
            protocol=target["protocol"],
            model_id=template["model_id"],
            verified_capabilities=list(template["verified_capabilities"]),
            model_policy={"mode": "preserve_client", "fields": {}},
            reasoning_policy={"mode": "preserve_client", "fields": {}},
        )
        if template["target"] == "native":
            provider["state_references"] = "reject"
        document = {
            "schema_version": 1,
            "request_id": "web-models:" + uuid.uuid4().hex,
            target["version_key"]: version,
            "status": "published",
            "published_at": published_at,
            "usable_until": usable_until,
            "providers": [provider],
            "bindings": [
                {
                    "workload": target["workload"],
                    "provider_id": template["provider_id"],
                    "model_id": template["model_id"],
                    "timeout_ms": template["timeout_ms"],
                    "fallback": "disabled",
                }
            ],
        }
        if template["target"] == "native":
            document["contract"] = "model-protocol/v1"
        return document

    def _rows(self, target):
        table = TARGETS[target]["table"]
        with self.p.store.connect() as db:
            return db.execute(
                f"SELECT version,document,digest,revoked FROM {table} ORDER BY version DESC LIMIT 50"
            ).fetchall()

    def _current(self, target):
        rows = self._rows(target)
        return rows[0]["version"] if rows else None

    def _target_view(self, name):
        target = TARGETS[name]
        now = self.p.models.clock()
        versions = []
        for row in self._rows(name):
            document = loads(row["document"])
            # Stored-content integrity is checked exactly as the snapshot port does it.
            require(
                document[target["version_key"]] == row["version"]
                and digest(document) == row["digest"],
                "dependency_unavailable",
                503,
            )
            versions.append(
                {
                    "version": row["version"],
                    "revoked": bool(row["revoked"]),
                    "availability": "revoked"
                    if row["revoked"]
                    else "expired"
                    if epoch(document["usable_until"]) <= now
                    else "available",
                    "published_at": document["published_at"],
                    "usable_until": document["usable_until"],
                    "providers": [self._provider_view(p) for p in document["providers"]],
                    "bindings": [self._binding_view(b) for b in document["bindings"]],
                }
            )
        current = versions[0] if versions else None
        return {
            "target": name,
            "label": target["label"],
            "detail": target["detail"],
            "protocol": target["protocol"],
            "workload": target["workload"],
            "current_version": current["version"] if current else None,
            "availability": current["availability"] if current else "unconfigured",
            "usable_until": current["usable_until"] if current else None,
            "versions": versions[:VERSION_HISTORY],
        }

    def _template_view(self, template):
        return {key: template[key] for key in sorted(template)}

    def _provider_view(self, provider):
        return {
            key: provider[key]
            for key in (
                "provider_id",
                "protocol",
                "model_id",
                "capability_verification",
                "verified_capabilities",
            )
        }

    def _binding_view(self, binding):
        return {
            key: binding[key]
            for key in ("workload", "provider_id", "model_id", "timeout_ms", "fallback")
        }

    def _prepare(self, client_id, semantic, template, expected):
        """Durably claim one intent before the authoritative store is touched.

        The row carries the reviewed window and the digest of the exact document this client
        id stands for, so any later attempt can prove whether that document is the one the
        authority holds. It stores no endpoint or credential reference.
        """
        # Claiming is the last point where the caller's expectation is known to hold; after
        # that the intent is the record of truth for this client id.
        require(self._current(template["target"]) == expected, "version_conflict", 409)
        version = (expected or 0) + 1
        published = math.floor(self.p.models.clock())
        published_at = utc(published)
        usable_until = utc(published + template["lifetime_seconds"])
        document = self._document(template, version, published_at, usable_until)
        record = (
            client_id,
            semantic,
            template["target"],
            version,
            expected,
            digest(self._stable(document)),
            published_at,
            usable_until,
            PREPARED,
            None,
            self.p.models.clock(),
        )
        with closing(sqlite3.connect(self.ledger_path, timeout=5)) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "INSERT INTO publication_intents ("
                    + ",".join(INTENT_COLUMNS)
                    + ") VALUES("
                    + ",".join("?" * len(INTENT_COLUMNS))
                    + ")",
                    record,
                )
                db.commit()
            except sqlite3.IntegrityError:
                # Another attempt claimed this client id first; its intent wins.
                db.rollback()
        prepared = self._intent(client_id)
        require(prepared is not None, "dependency_unavailable", 503)
        return prepared

    def _intent(self, client_id):
        with closing(sqlite3.connect(self.ledger_path, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM publication_intents WHERE client_id=?", (client_id,)
            ).fetchone()
        return dict(row) if row else None

    def _published(self, intent):
        """The authoritative row for this intent's version, if any (revocation is not deletion)."""
        table = TARGETS[intent["target"]]["table"]
        with self.p.store.connect() as db:
            row = db.execute(
                f"SELECT digest FROM {table} WHERE version=?", (intent["version"],)
            ).fetchone()
        if row is None or row["digest"] != intent["digest"]:
            return None
        return row

    def _result(self, intent):
        """The recorded outcome, or the shape a committed attempt of this intent produces."""
        if intent["result"] is not None:
            return {**loads(intent["result"]), "state": "replayed", "deduplicated": True}
        return {
            "target": intent["target"],
            "version": intent["version"],
            "expected_version": intent["expected_version"],
            "state": "replayed",
            "deduplicated": True,
            "usable_until": intent["usable_until"],
        }

    def _record(self, intent, result):
        """Write the durable receipt for an already verified publication."""
        with closing(sqlite3.connect(self.ledger_path, timeout=5)) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE publication_intents SET state=?,result=?,settled_at=? WHERE client_id=?",
                (COMMITTED, canonical(result), self.p.models.clock(), intent["client_id"]),
            )
            db.commit()

    def _receipt(self, intent, result):
        """Best-effort receipt write after the authoritative outcome is already verified.

        This is the write the earlier design performed as the only record of success; it is
        now an optimisation. When it fails the intent stays `prepared` and the next attempt
        reconciles from the authoritative digest, so a lost receipt never turns a committed
        publication into a reported failure and never becomes a later conflict.
        """
        try:
            self._record(intent, result)
        except (sqlite3.Error, OSError):
            pass

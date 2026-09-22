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
from .storage import is_ledger_key

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
# The task centre reads the platform's own operation record (`audit`) plus this module's claim
# ledger. These name those records in the merged view and add no second store.
VERSION_PREFIX = "model-version:"
INTENT_PREFIX = "model-intent:"
TASK_KIND = "model.publish"
PUBLISH_OPERATIONS = {"chat": "config.publish", "native": "native_config.publish"}
REVOKE_OPERATIONS = {"chat": "config.revoke", "native": "native_config.revoke"}
# Module words the task centre normalizes: a claim is never reported as a publication.
PUBLISHED = "published"
AUDIT_WITHOUT_AUTHORITY = "audit_without_authority"
CORRUPT = "corrupt"
INTENT_UNRESOLVED = "intent_unresolved"
PUBLICATION_STATES = {PUBLISHED, AUDIT_WITHOUT_AUTHORITY, CORRUPT, INTENT_UNRESOLVED}
PUBLICATION_LIMIT = 50
# The states an audit row can carry; the authority's integrity decides which one it is.
OPERATION_STATES = {PUBLISHED, AUDIT_WITHOUT_AUTHORITY, CORRUPT}


def position(record):
    """The immutable merged position of a record: whole recorded second, then identifier.

    The SQL keyset compares exactly this pair, so the page a stream returns and the page the
    caller merges are the same sequence and neither can skip a record.
    """
    return (int(record["created_at"]), record["task_id"])


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
            return await self.p.local_work.run(self.view, session)
        if path == "/api/web/models/unlock":
            return await self.unlock(body, session)
        if path == "/api/web/models/lock":
            require(body == {}, "invalid_input", 400)
            session.pop("management", None)
            return {"unlocked": False, "code": self.code(session)}
        if path == "/api/web/models/preview":
            return await self.p.local_work.run(self._guarded, self.preview, body, session)
        if path == "/api/web/models/publish":
            return await self.p.local_work.run(self._guarded, self.publish, body, session)
        if path == "/api/web/models/revoke":
            return await self.p.local_work.run(self._guarded, self.revoke, body, session)
        raise Fault("not_found", 404)

    def _guarded(self, operation, body, session):
        # Queue waiting must not preserve a login that has since ended.
        from .models import WRITE_GUARD

        require(self.console.session_valid(session), "session_expired", 401)

        def guard():
            require(self.console.session_live(session), "session_expired", 401)
            self._gate(session)

        token = WRITE_GUARD.set(guard)
        try:
            return operation(body, session)
        finally:
            WRITE_GUARD.reset(token)

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

    # ------------------------------------------------------------ task centre
    #
    # Read-only windows over ledgers that already exist: the platform's own operation record
    # (`audit`, written by the Models owner on every publication) and this module's claim
    # ledger. A version row is reported as a real publication only when the authoritative
    # table still holds it intact; a claim whose receipt never became authoritative is a
    # different fact and stays a different record. Nothing is copied, and no second store
    # exists. The ordering key is the immutable moment the operation was recorded plus the
    # record's identifier, so a record never moves across a cursor while its state changes.

    def ledger_state(self):
        """Whether the publication ledgers behind the task centre can be read at all."""
        return (
            {"state": "available", "code": "ok"}
            if self.ledger_count() is not None
            else {"state": "unreadable", "code": "dependency_unavailable"}
        )

    def ledger_count(self):
        """Publication operations plus unresolved claims, or `None` if unreadable."""
        published = self._read(
            "SELECT COUNT(*) AS total FROM audit WHERE operation IN (?, ?)",
            tuple(sorted(PUBLISH_OPERATIONS.values())),
        )
        if published is None:
            return None
        unresolved = self._unresolved_count()
        if unresolved is None:
            return None
        return published[0]["total"] + unresolved

    def publications(self, *, after=None, limit=20, states=None):
        """One merged page of publication records, newest first, keyset by immutable key.

        `states` filters on this module's own words. Returns `(records, has_more)`; a `None`
        record list means one of the ledgers could not be read, which the caller must report
        as an incomplete source rather than as an empty list.
        """
        require(type(limit) is int and 1 <= limit <= PUBLICATION_LIMIT, "invalid_input", 400)
        if after is not None:
            require(is_ledger_key(after), "invalid_input", 400)
        if states is None:
            names = sorted(PUBLICATION_STATES)
        else:
            names = sorted(states)
            require(names and set(names) <= PUBLICATION_STATES, "invalid_input", 400)
        operations = sorted(set(names) & OPERATION_STATES)
        claims = [INTENT_UNRESOLVED] if INTENT_UNRESOLVED in names else []
        pages, readable = [], True
        for target in sorted(TARGETS):
            # Two streams per target: the platform's publication operations and the claims
            # whose receipt never became authoritative. Both are paged in the same order.
            for wanted, fetch in ((operations, self._operation_page), (claims, self._claims_page)):
                if not wanted:
                    continue
                page = fetch(target, after, limit, wanted)
                if page is None:
                    readable = False
                    continue
                pages.append(page)
        if not readable:
            return None
        # Each stream returned its own next `limit + 1` records in exactly this order, so the
        # merged head is the true next page; a record's key never changes, so it cannot be
        # skipped while a later page is taken, and it cannot appear twice.
        merged = sorted((record for page in pages for record in page), key=position, reverse=True)
        return merged[:limit], len(merged) > limit

    def version_record(self, target, version):
        """One publication operation re-read live, or `None` when this ledger has no such row."""
        require(target in TARGETS and type(version) is int and version > 0, "invalid_input", 400)
        rows = self._read(
            "SELECT a.sequence AS sequence, a.principal AS principal, a.operation AS operation,"
            " a.object_id AS object_id, a.observed_at AS observed_at,"
            " t.version AS stored_version, t.document AS stored_document,"
            " t.digest AS stored_digest, t.revoked AS stored_revoked"
            " FROM audit a LEFT JOIN " + TARGETS[target]["table"] + " t"
            " ON t.version = CAST(a.object_id AS INTEGER)"
            " WHERE a.operation=? AND a.object_id=? ORDER BY a.sequence DESC LIMIT 1",
            (PUBLISH_OPERATIONS[target], str(version)),
        )
        if not rows:
            return None
        return self._operation_record(target, rows[0])

    def intent_record(self, client_id):
        """One claim re-read live, or `None` when this ledger has no such row."""
        rows = self._ledger("SELECT * FROM publication_intents WHERE client_id=?", (client_id,))
        if not rows:
            return None
        intent = dict(rows[0])
        return self._intent_record(intent, self._authority_for(intent))

    def _read(self, statement, params=()):
        """Read the authoritative store: `audit` and the version tables live there."""
        try:
            with self.p.store.connect() as db:
                return db.execute(statement, params).fetchall()
        except (sqlite3.Error, OSError):
            return None

    def _ledger(self, statement, params=()):
        """Read this console's own claim ledger, which is its own database file."""
        try:
            with closing(sqlite3.connect(self.ledger_path, timeout=5)) as db:
                db.row_factory = sqlite3.Row
                return db.execute(statement, params).fetchall()
        except (sqlite3.Error, OSError):
            return None

    def _versions(self, target):
        """Version to stored digest of the authoritative table, or `None` if unreadable.

        The claim ledger and the authoritative store are different databases, so "did this
        claim become the authoritative version" is decided in one place and in plain Python:
        no query ever joins across the two files.
        """
        rows = self._read("SELECT version, digest FROM " + TARGETS[target]["table"])
        return None if rows is None else {row["version"]: row["digest"] for row in rows}

    def _unresolved_count(self):
        """Claims whose receipt never became the authoritative version, over both targets."""
        total = 0
        for target in sorted(TARGETS):
            versions = self._versions(target)
            rows = self._ledger(
                "SELECT version, digest FROM publication_intents WHERE target=?", (target,)
            )
            if versions is None or rows is None:
                # Without the console ledger there are no claims at all; a ledger that exists
                # but cannot be read is reported, never silently counted as zero.
                return None if self.enabled else 0
            total += sum(1 for row in rows if versions.get(row["version"]) != row["digest"])
        return total

    def _operation_page(self, target, after, limit, states):
        """The next `limit + 1` publication operations of this target in the wanted states.

        A row whose stored version is corrupt is still a row here: the state filter is applied
        to the built records, and the loop keeps reading strictly older rows until the page is
        full, so a filtered-out row can never shift a real record out of the page.
        """
        table = TARGETS[target]["table"]
        wanted, collected, position_after = set(states), [], after
        while len(collected) <= limit:
            where, params = ["a.operation=?"], [PUBLISH_OPERATIONS[target]]
            if position_after is not None:
                where.append(
                    "(CAST(a.observed_at AS INTEGER), ? || ? || ':' || a.object_id) < (?, ?)"
                )
                params += [VERSION_PREFIX, target, position_after[0], position_after[1]]
            if wanted == {AUDIT_WITHOUT_AUTHORITY}:
                where.append("t.version IS NULL")
            elif not wanted & {PUBLISHED, CORRUPT}:
                return collected[:limit]
            elif AUDIT_WITHOUT_AUTHORITY not in wanted:
                where.append("t.version IS NOT NULL")
            rows = self._read(
                "SELECT a.sequence AS sequence, a.principal AS principal,"
                " a.operation AS operation, a.object_id AS object_id,"
                " a.observed_at AS observed_at, t.version AS stored_version,"
                " t.document AS stored_document, t.digest AS stored_digest,"
                " t.revoked AS stored_revoked"
                " FROM audit a LEFT JOIN "
                + table
                + " t ON t.version = CAST(a.object_id AS INTEGER)"
                " WHERE " + " AND ".join(where) + " ORDER BY CAST(a.observed_at AS INTEGER) DESC,"
                " ? || ? || ':' || a.object_id DESC LIMIT ?",
                (*params, VERSION_PREFIX, target, limit + 1),
            )
            if rows is None:
                return None
            if not rows:
                break
            for row in rows:
                position_after = (
                    int(row["observed_at"]),
                    VERSION_PREFIX + target + ":" + str(row["object_id"]),
                )
                record = self._operation_record(target, row)
                if record is not None and record["state"] in wanted:
                    collected.append(record)
            if len(rows) < limit + 1:
                break
        return collected[: limit + 1]

    def _claims_page(self, target, after, limit, states):
        """The next `limit + 1` unresolved claims of this target, newest first.

        "Unresolved" is decided against the authoritative table in Python, so a claim that was
        filtered out never shifts a real record out of the page: the loop keeps reading strictly
        older rows until it has a full page or the ledger ends.
        """
        versions = self._versions(target)
        if versions is None:
            return None
        collected, position_after = [], after
        while len(collected) <= limit:
            where, params = ["i.target=?"], [target]
            if position_after is not None:
                where.append("(CAST(i.prepared_at AS INTEGER), ? || i.client_id) < (?, ?)")
                params += [INTENT_PREFIX, position_after[0], position_after[1]]
            rows = self._ledger(
                "SELECT * FROM publication_intents i WHERE "
                + " AND ".join(where)
                + " ORDER BY CAST(i.prepared_at AS INTEGER) DESC, ? || i.client_id DESC LIMIT ?",
                (*params, INTENT_PREFIX, limit + 1),
            )
            if rows is None:
                # Without the console ledger there are no claims; a broken one is not read
                # as an empty list.
                return [] if not self.enabled else None
            if not rows:
                break
            for row in rows:
                intent = dict(row)
                position_after = (int(intent["prepared_at"]), INTENT_PREFIX + intent["client_id"])
                if versions.get(intent["version"]) == intent["digest"]:
                    # This claim did become the authoritative version: it is that record, and
                    # it is not reported a second time as an unresolved claim.
                    continue
                record = self._intent_record(intent, self._authority_for(intent))
                if record is not None and record["state"] in set(states):
                    collected.append(record)
            if len(rows) < limit + 1:
                break
        return collected[: limit + 1]

    def _authority_for(self, intent):
        """The authoritative row for a claim: same version and the very digest it claimed."""
        if intent["target"] not in TARGETS:
            return None
        rows = self._read(
            "SELECT version, document, digest, revoked FROM "
            + TARGETS[intent["target"]]["table"]
            + " WHERE version=?",
            (intent["version"],),
        )
        if not rows or rows[0]["digest"] != intent["digest"]:
            return None
        return rows[0]

    def _operation_record(self, target, row):
        """One publication operation: the audit fact first, the stored version as evidence."""
        operation, observed_at = row["operation"], row["observed_at"]
        try:
            version = int(row["object_id"])
            require(version > 0)
        except (TypeError, ValueError, Fault):
            return None
        document, integrity = None, "missing"
        if row["stored_version"] is not None:
            try:
                document = loads(row["stored_document"])
                require(document[TARGETS[target]["version_key"]] == row["stored_version"])
                integrity = "intact" if digest(document) == row["stored_digest"] else "mismatch"
            except (Fault, KeyError, TypeError):
                document, integrity = None, "mismatch"
        if integrity == "intact":
            state = PUBLISHED
        elif integrity == "mismatch":
            state = CORRUPT
        else:
            state = AUDIT_WITHOUT_AUTHORITY
        intent = self._matching_intent(target, version, row["stored_digest"])
        revoke = self._revocation(target, version)
        return {
            "task_id": VERSION_PREFIX + target + ":" + str(version),
            "created_at": float(observed_at),
            "updated_at": float(revoke["observed_at"]) if revoke else float(observed_at),
            "state": state,
            "target": target,
            "origin": "authoritative" if integrity == "intact" else "unverified",
            "document": self._operation_view(
                target, version, operation, row, document, integrity, intent, revoke
            ),
        }

    def _operation_view(self, target, version, operation, row, document, integrity, intent, revoke):
        view = {
            "target": target,
            "target_label": TARGETS[target]["label"],
            "version": version,
            "version_key": TARGETS[target]["version_key"],
            "operation": operation,
            "actor": row["principal"],
            "recorded_at": utc(float(row["observed_at"])),
            "integrity": integrity,
            "availability": "unknown",
            "revoked": False,
            "revoked_at": None,
            "revoked_by": None,
            "published_at": None,
            "usable_until": None,
            "providers": [],
            "bindings": [],
            "claim": None,
        }
        if revoke is not None:
            view.update(
                revoked=True,
                revoked_at=utc(float(revoke["observed_at"])),
                revoked_by=revoke["principal"],
            )
        if document is not None:
            view.update(
                published_at=document.get("published_at"),
                usable_until=document.get("usable_until"),
                providers=self._views(self._provider_view, document.get("providers")),
                bindings=self._views(self._binding_view, document.get("bindings")),
            )
            if isinstance(view["usable_until"], str):
                try:
                    expired = epoch(view["usable_until"]) <= self.p.models.clock()
                except ValueError:
                    expired = True
                view["availability"] = (
                    "revoked" if view["revoked"] else "expired" if expired else "available"
                )
        if intent is not None:
            view["claim"] = {
                "client_id": intent["client_id"],
                "state": intent["state"],
                "prepared_at": utc(intent["prepared_at"]),
                "settled_at": utc(intent["settled_at"]) if intent["settled_at"] else None,
                "recorded": intent["state"] == COMMITTED,
            }
        return view

    def _intent_record(self, intent, authority):
        """One claim: an intent the platform recorded, with the authority's answer as evidence."""
        if intent["target"] not in TARGETS or intent["state"] not in {PREPARED, COMMITTED}:
            return None
        try:
            prepared_at = float(intent["prepared_at"])
            settled_at = float(intent["settled_at"]) if intent["settled_at"] else None
            open_window = epoch(intent["usable_until"]) > self.p.models.clock()
        except (TypeError, ValueError):
            return None
        return {
            "task_id": INTENT_PREFIX + intent["client_id"],
            "created_at": prepared_at,
            "updated_at": settled_at or prepared_at,
            "state": INTENT_UNRESOLVED,
            "target": intent["target"],
            "origin": "authoritative" if authority is not None else "unverified",
            "document": {
                "target": intent["target"],
                "target_label": TARGETS[intent["target"]]["label"],
                "version": intent["version"],
                "version_key": TARGETS[intent["target"]]["version_key"],
                "expected_version": intent["expected_version"],
                "client_id": intent["client_id"],
                "receipt": intent["state"],
                "receipt_recorded": intent["state"] == COMMITTED,
                "code": "publication_unverified"
                if intent["state"] == COMMITTED
                else "receipt_missing",
                "prepared_at": utc(prepared_at),
                "settled_at": utc(settled_at) if settled_at else None,
                "window": "open" if open_window else "expired",
                "published_at": intent["published_at"],
                "usable_until": intent["usable_until"],
                "authoritative": self._authority_word(intent, authority),
            },
        }

    def _authority_word(self, intent, authority):
        """What the authoritative table says about exactly this claim, never a guess."""
        if authority is not None:
            return "digest_match"
        exists = self._version_exists(intent)
        if exists is None:
            return "unknown"
        return "digest_mismatch" if exists else "absent"

    def _views(self, builder, items):
        """Redacted provider/binding views; a malformed stored entry is skipped, not invented."""
        result = []
        for item in items if isinstance(items, list) else []:
            try:
                result.append(builder(item))
            except (KeyError, TypeError):
                continue
        return result

    def _version_exists(self, intent):
        """Whether the claimed version number exists at all, whatever its digest is."""
        rows = self._read(
            "SELECT digest FROM " + TARGETS[intent["target"]]["table"] + " WHERE version=?",
            (intent["version"],),
        )
        if rows is None:
            return None
        return bool(rows)

    def _matching_intent(self, target, version, stored_digest):
        """The console claim this operation is, when one exists for exactly this digest."""
        if stored_digest is None:
            return None
        rows = self._ledger(
            "SELECT * FROM publication_intents WHERE target=? AND version=? AND digest=?"
            " ORDER BY prepared_at DESC LIMIT 1",
            (target, version, stored_digest),
        )
        if not rows:
            return None
        intent = dict(rows[0])
        return intent if intent["state"] in {PREPARED, COMMITTED} else None

    def _revocation(self, target, version):
        """The latest revocation the platform recorded for this version, if any."""
        rows = self._read(
            "SELECT principal, observed_at FROM audit WHERE operation=? AND object_id=?"
            " ORDER BY sequence DESC LIMIT 1",
            (REVOKE_OPERATIONS[target], str(version)),
        )
        return rows[0] if rows else None

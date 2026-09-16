"""Home Assistant connector: registered entities, honest readings, limited light/switch control.

The browser only ever names a registered action template, the reading revision it actually saw
and a client id. The HA address, the environment variable holding its long-lived token, every
entity and every service pair come from deployment settings, so no page can inject a target, a
credential or an arbitrary HA service. Locks, doors, alarms, scenes and scripts are absent from
the vocabulary on purpose: only explicit target-state `light`/`switch` services exist.

Reading and controlling are separate operations on purpose (A10). A service-call receipt is an
*acceptance*, never proof that the device executed anything; only a later `GET /api/states`
observation is reported as observed. An outcome that cannot be established stays `unknown` and
is never re-sent automatically.

Exactly one command is ever transmitted per reviewed intent. Execution ownership is durable, not
in-memory: the ledger row itself moves from `observing` (claimed, nothing transmitted) to
`sending` (transmitted or about to be) under a compare-and-set that SQLite serializes across
processes, so a second request for the same client id finds an owner instead of sending again.
Because the device not being at the target does not prove that an earlier command was not sent,
a `sending` intent is only ever concluded by observation: at the target it becomes `observed`,
otherwise it becomes `unknown` (`control_unverified`) and waits for the operator to decide again.
"""

import asyncio
import ipaddress
import math
import re
import sqlite3
import uuid
from contextlib import closing
from urllib.parse import urlsplit

import aiohttp

from .auth import secret
from .contracts import Fault, canonical, digest, epoch, loads, require, utc
from .storage import is_ledger_key

KINDS = {"light", "switch", "sensor"}
CONTROLLABLE = {"light", "switch"}
# Explicit target-state services only: a repeated request converges instead of toggling.
SERVICES = {"turn_on": "on", "turn_off": "off"}
CONTROL_ACTION = "device.control"
REDIRECTS = {301, 302, 303, 307, 308}
REJECTED_STATUS = {400, 404, 405, 409, 410, 422}
CONTROL_STATUS = {
    "device_timeout": 504,
    "device_unavailable": 503,
    "device_credential_missing": 503,
    "device_redirect": 502,
    "device_unauthorized": 502,
    "device_rejected": 502,
    "device_failed": 502,
    "device_invalid_response": 502,
    "device_missing": 502,
    "control_unverified": 503,
    "control_in_progress": 409,
}
SETTING_KEYS = {
    "enabled",
    "base_url",
    "reviewed_addresses",
    "allow_private_http",
    "token_env",
    "unlock_ttl_seconds",
    "timeout_seconds",
    "status_max_age_seconds",
    "entities",
    "templates",
}
ENTITY_KEYS = {"entity_id", "label", "kind", "unit"}
TEMPLATE_KEYS = {"template_id", "label", "entity_id", "service"}
ENTITY_PATTERN = r"(?:light|switch|sensor)\.[a-z0-9_]{1,64}"
MAX_ENTITIES = 32
MAX_TEMPLATES = 32
RESPONSE_BUDGET = 1_048_576
HISTORY = 8
# The task centre reads the same ledger through the port below; these name this module's own
# records in that merged view and never introduce a second execution store.
TASK_PREFIX = "home-control:"
TASK_KIND = "device.control"
HISTORY_LIMIT = 50
# Durable intent lifecycle. `observing` and `sending` are owned executions: `observing` means no
# command has been transmitted yet, `sending` means one has left (or may have left) this process.
PREPARED, OBSERVING, SENDING, ACCEPTED, OBSERVED, UNKNOWN, REJECTED = (
    "prepared",
    "observing",
    "sending",
    "accepted",
    "observed",
    "unknown",
    "rejected",
)
ACTIVE_STATES = {PREPARED, OBSERVING, SENDING}
# Every word this module can leave in the ledger, for the task centre's own filter check.
LEDGER_STATES = ACTIVE_STATES | {ACCEPTED, OBSERVED, UNKNOWN, REJECTED}
# A recorded outcome is only ever improved: a late writer with weaker evidence cannot downgrade
# an observation that already confirmed the requested state.
OUTCOME_RANKS = {
    PREPARED: 0,
    OBSERVING: 0,
    SENDING: 0,
    REJECTED: 1,
    UNKNOWN: 1,
    ACCEPTED: 2,
    OBSERVED: 3,
}
STAGE_CODES = {PREPARED: "claimed", OBSERVING: "observing", SENDING: "executing"}
# A released or expired claim may be taken over a few times; beyond that the request reports that
# the intent is still owned instead of looping.
CLAIM_ATTEMPTS = 4
RETRY = object()
CONTROL_COLUMNS = (
    "client_id",
    "semantic",
    "template_id",
    "entity_id",
    "service",
    "label",
    "expected_revision",
    "prepared_at",
    "state",
    "code",
    "target_reported",
    "settled_at",
    "owner",
    "lease_expires_at",
)
CONTROL_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS control_intents ("
    "client_id TEXT PRIMARY KEY, semantic TEXT NOT NULL, template_id TEXT NOT NULL, "
    "entity_id TEXT NOT NULL, service TEXT NOT NULL, label TEXT NOT NULL, "
    "expected_revision INTEGER NOT NULL, prepared_at REAL NOT NULL, state TEXT NOT NULL, "
    "code TEXT, target_reported INTEGER, settled_at REAL, owner TEXT, lease_expires_at REAL)"
)
READING_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS readings ("
    "entity_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, observed_at TEXT, code TEXT NOT NULL, "
    "state TEXT, value TEXT, unit TEXT, attempt_at TEXT NOT NULL, attempt_code TEXT NOT NULL)"
)
LEDGER_SCHEMA = (CONTROL_SCHEMA, READING_SCHEMA)


class Home:
    """One registered HA connection shared by the page and by any future caller."""

    def __init__(self, platform, console):
        self.p = platform
        self.console = console
        self.config = platform.settings.get("home")
        self.enabled = False
        self.entities = {}
        self.templates = {}
        self.by_entity = {}
        self.unlock_ttl = 900
        self.timeout = 4
        self.max_age = 120
        self.base_url = None
        self.token_env = None
        self.clock = platform.models.clock
        self.ledger_path = platform.store.path + ".home-controls.sqlite"
        if self.config is not None:
            self._configure()

    def _configure(self):
        c = self.config
        require(
            isinstance(c, dict)
            and set(c) <= SETTING_KEYS
            and {"base_url", "token_env", "entities"} <= set(c),
            "invalid_input",
            400,
        )
        require(type(c.get("enabled", False)) is bool, "invalid_input", 400)
        require(type(c.get("allow_private_http", False)) is bool, "invalid_input", 400)
        reviewed = c.get("reviewed_addresses", [])
        require(isinstance(reviewed, list) and 1 <= len(reviewed) <= 8, "invalid_input", 400)
        self._address(c["base_url"], reviewed, c.get("allow_private_http", False))
        require(
            isinstance(c["token_env"], str)
            and re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", c["token_env"]) is not None,
            "invalid_input",
            400,
        )
        self.token_env = c["token_env"]
        ttl = c.get("unlock_ttl_seconds", 900)
        require(type(ttl) is int and 60 <= ttl <= 3600, "invalid_input", 400)
        timeout = c.get("timeout_seconds", 4)
        require(type(timeout) is int and 1 <= timeout <= 15, "invalid_input", 400)
        age = c.get("status_max_age_seconds", 120)
        require(type(age) is int and 10 <= age <= 3600, "invalid_input", 400)
        items = c["entities"]
        require(isinstance(items, list) and 1 <= len(items) <= MAX_ENTITIES, "invalid_input", 400)
        for item in items:
            self._entity(item)
        require(len(self.entities) == len(items), "invalid_input", 400)
        templates = c.get("templates", [])
        require(
            isinstance(templates, list) and len(templates) <= MAX_TEMPLATES, "invalid_input", 400
        )
        for item in templates:
            self._template(item)
        require(len(self.templates) == len(templates), "invalid_input", 400)
        self.unlock_ttl = ttl
        self.timeout = timeout
        self.max_age = age
        self.enabled = c.get("enabled", False)
        with closing(sqlite3.connect(self.ledger_path, timeout=5)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(READING_SCHEMA)
            self._migrate(db)
            db.commit()

    def _migrate(self, db):
        """Create or extend the intent ledger without ever making a legacy claim re-send.

        The previous schema could not record whether a command had been transmitted, so a legacy
        `prepared` row is treated as possibly sent: it is reconciled by observation and never
        transmitted again on its own.
        """
        columns = {row[1] for row in db.execute("PRAGMA table_info(control_intents)")}
        if not columns:
            db.execute(CONTROL_SCHEMA)
            return
        added = False
        for name, kind in (("owner", "TEXT"), ("lease_expires_at", "REAL")):
            if name not in columns:
                db.execute(f"ALTER TABLE control_intents ADD COLUMN {name} {kind}")
                added = True
        if added:
            db.execute(
                "UPDATE control_intents SET state=?, owner=NULL, lease_expires_at=0 WHERE state=?",
                (SENDING, PREPARED),
            )

    def _address(self, base, reviewed, allow_private_http):
        """The same reviewed-address policy the model registrations already use."""
        require(isinstance(base, str) and base, "invalid_input", 400)
        url = urlsplit(base)
        require(
            url.scheme in {"http", "https"}
            and url.hostname
            and not url.username
            and not url.password
            and url.path in {"", "/"}
            and not url.query
            and not url.fragment,
            "invalid_input",
            400,
        )
        require(
            not base.endswith("/")
            and not any(ord(c) <= 32 or ord(c) > 126 for c in base)
            and "%" not in base
            and "\\" not in base,
            "invalid_input",
            400,
        )
        try:
            addresses = [ipaddress.ip_address(a) for a in reviewed]
            require(bool(addresses), "invalid_input", 400)
            if url.scheme == "http":
                require(
                    allow_private_http is True and all(a.is_private for a in addresses),
                    "invalid_input",
                    400,
                )
                # This local-only platform cannot approve a new cleartext production boundary.
                require(all(a.is_loopback for a in addresses), "invalid_input", 400)
            try:
                literal = ipaddress.ip_address(url.hostname)
            except ValueError:
                literal = None
            require(literal is None or addresses == [literal], "invalid_input", 400)
            _ = url.port
        except ValueError:
            require(False, "invalid_input", 400)
        self.base_url = base

    def _entity(self, item):
        require(isinstance(item, dict) and set(item) <= ENTITY_KEYS, "invalid_input", 400)
        require({"entity_id", "label", "kind"} <= set(item), "invalid_input", 400)
        entity_id = item["entity_id"]
        require(
            isinstance(entity_id, str) and re.fullmatch(ENTITY_PATTERN, entity_id) is not None,
            "invalid_input",
            400,
        )
        require(entity_id not in self.entities, "invalid_input", 400)
        require(
            item["kind"] in KINDS and entity_id.split(".")[0] == item["kind"], "invalid_input", 400
        )
        require(
            isinstance(item["label"], str) and 1 <= len(item["label"]) <= 64, "invalid_input", 400
        )
        unit = item.get("unit")
        require(
            unit is None or (isinstance(unit, str) and 1 <= len(unit) <= 16 and unit.isprintable()),
            "invalid_input",
            400,
        )
        self.entities[entity_id] = {
            "entity_id": entity_id,
            "label": item["label"],
            "kind": item["kind"],
            "unit": unit,
        }
        self.by_entity[entity_id] = []

    def _template(self, item):
        require(isinstance(item, dict) and set(item) <= TEMPLATE_KEYS, "invalid_input", 400)
        require({"template_id", "label", "entity_id", "service"} <= set(item), "invalid_input", 400)
        self.p.contracts.check("common#id", item["template_id"])
        require(item["template_id"] not in self.templates, "invalid_input", 400)
        require(
            isinstance(item["label"], str) and 1 <= len(item["label"]) <= 64, "invalid_input", 400
        )
        entity = self.entities.get(item["entity_id"])
        require(entity is not None, "invalid_input", 400)
        # A read-only sensor has no action, and no service outside the explicit pair exists.
        require(entity["kind"] in CONTROLLABLE, "invalid_input", 400)
        require(item["service"] in SERVICES, "invalid_input", 400)
        template = {
            "template_id": item["template_id"],
            "label": item["label"],
            "entity_id": entity["entity_id"],
            "service": item["service"],
        }
        self.templates[item["template_id"]] = template
        self.by_entity[entity["entity_id"]].append(template)

    @property
    def token(self):
        return secret(self.token_env) if self.token_env else None

    def code(self, session=None):
        """One honest state word per request; never a control port without real authority."""
        if self.config is None:
            return "home_disabled"
        if not self.enabled:
            return "control_disabled"
        principal = self.p.auth.principals.get(self.console.config["principal"], {})
        if CONTROL_ACTION not in set(principal.get("actions", [])):
            return "operator_not_authorized"
        if session is not None and session.get("devices", 0) <= self.console.clock():
            return "control_required"
        return "ready"

    def _gate(self, session):
        code = self.code(session)
        require(code == "ready", code, 403)

    def _fault(self, code):
        return Fault(code, CONTROL_STATUS.get(code, 502))

    async def route(self, path, body, session):
        require(self.config is not None, "home_disabled", 403)
        if path == "/api/web/home/view":
            require(body == {}, "invalid_input", 400)
            return self.view(session)
        if path == "/api/web/home/refresh":
            require(body == {}, "invalid_input", 400)
            return await self.refresh(session)
        if path == "/api/web/home/unlock":
            return await self.unlock(body, session)
        if path == "/api/web/home/lock":
            require(body == {}, "invalid_input", 400)
            session.pop("devices", None)
            return {"unlocked": False, "code": self.code(session)}
        if path == "/api/web/home/control":
            return await self.control(body, session)
        raise Fault("not_found", 404)

    async def unlock(self, body, session):
        """Re-authentication step: an ordinary chat login never carries device authority."""
        require(set(body) == {"password"}, "invalid_input", 400)
        require(self.code() == "ready", self.code(), 403)
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
        session["devices"] = console.clock() + self.unlock_ttl
        return {"unlocked": True, "code": self.code(session), "expires_in": self.unlock_ttl}

    # ---------------------------------------------------------------- reading

    def _client(self):
        return aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout), trust_env=False
        )

    async def _call(self, client, method, path, payload=None):
        """One bounded HA request; redirects are refused instead of followed."""
        token = self.token
        require(token is not None, "device_credential_missing", 503)
        try:
            async with client.request(
                method,
                self.base_url + path,
                json=payload,
                headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
                allow_redirects=False,
            ) as response:
                require(response.status not in REDIRECTS, "device_redirect", 502)
                chunks, total = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    total += len(chunk)
                    require(total <= RESPONSE_BUDGET, "device_invalid_response", 502)
                    chunks.append(chunk)
                return response.status, b"".join(chunks)
        except Fault:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            raise Fault("device_timeout", 504) from None
        except (aiohttp.ClientError, OSError, ValueError):
            raise Fault("device_unavailable", 503) from None

    def _decoded(self, raw):
        try:
            return loads(raw)
        except Fault:
            raise Fault("device_invalid_response", 502) from None

    def _normalize(self, entity, document):
        """Trust the registered entity and a bounded value; never guess a missing state."""
        state = document.get("state")
        reading = {"code": "ok", "state": None, "value": None, "unit": None}
        if not isinstance(state, str) or len(state) > 64 or not state.isprintable():
            return {**reading, "code": "device_malformed_state"}
        if state == "unavailable":
            return {**reading, "code": "unavailable"}
        if state == "unknown":
            return {**reading, "code": "unknown"}
        if entity["kind"] in CONTROLLABLE:
            if state not in {"on", "off"}:
                # An unrecognized switch/light state is reported as unreadable, never as off.
                return {**reading, "code": "device_malformed_state"}
            return {**reading, "state": state}
        try:
            number = float(state)
        except ValueError:
            # Text readings are passed through as bounded text, never as a number.
            return {**reading, "code": "text", "value": state[:64]}
        if not math.isfinite(number):
            return {**reading, "code": "device_malformed_state"}
        return {**reading, "value": number}

    async def _observe(self, client, entity_id):
        entity = self.entities[entity_id]
        status, raw = await self._call(client, "GET", "/api/states/" + entity_id)
        if status in {401, 403}:
            raise Fault("device_unauthorized", 502)
        if status == 404:
            raise Fault("device_missing", 502)
        require(status == 200, "device_invalid_response", 502)
        document = self._decoded(raw)
        # A payload for another entity is never accepted as this entity's reading.
        require(
            isinstance(document, dict)
            and document.get("entity_id") == entity_id
            and isinstance(document.get("attributes", {}), dict),
            "device_invalid_response",
            502,
        )
        reading = self._normalize(entity, document)
        unit = document["attributes"].get("unit_of_measurement")
        if isinstance(unit, str) and 1 <= len(unit) <= 16 and unit.isprintable():
            reading["unit"] = unit
        # The registered unit is the reviewed one; HA may only confirm it.
        reading["unit"] = entity["unit"] or reading["unit"]
        self._store_reading(entity_id, reading)
        return reading

    async def _read_one(self, client, entity_id):
        try:
            await self._observe(client, entity_id)
        except Fault as exc:
            # One unreachable entity never hides the others, and never becomes a fake reading.
            self._store_attempt(entity_id, exc.code)

    async def refresh(self, session):
        """A real observation round: one bounded GET per registered entity."""
        require(self.token is not None, "device_credential_missing", 503)
        async with self._client() as client:
            await asyncio.gather(*(self._read_one(client, key) for key in self.entities))
        return self.view(session)

    def _availability(self, row):
        """Current, expired, offline or unknown: never a silent zero and never a fake "off"."""
        if row is None:
            return "never_read", "never_read"
        if row["attempt_code"] != "ok":
            return "offline", row["attempt_code"]
        if row["code"] in {"unavailable", "unknown"}:
            return row["code"], row["code"]
        if row["code"] not in {"ok", "text"}:
            return "unknown", row["code"]
        if row["observed_at"] is None:
            return "never_read", "never_read"
        if self.clock() - epoch(row["observed_at"]) > self.max_age:
            return "stale", "ok"
        return "current", "ok"

    # -------------------------------------------------------------- control

    def _template_of(self, template_id):
        require(isinstance(template_id, str), "invalid_input", 400)
        template = self.templates.get(template_id)
        require(template is not None, "not_found", 404)
        return template

    async def control(self, body, session):
        """Run one registered template for the revision the operator actually read."""
        self._gate(session)
        require(
            set(body) == {"template_id", "expected_revision", "client_id"}, "invalid_input", 400
        )
        template = self._template_of(body["template_id"])
        expected = body["expected_revision"]
        require(type(expected) is int and expected >= 0, "invalid_input", 400)
        client_id = body["client_id"]
        require(isinstance(client_id, str) and len(client_id) == 36, "invalid_input", 400)
        try:
            uuid.UUID(client_id)
        except ValueError:
            raise Fault("invalid_input", 400) from None
        require(self.token is not None, "device_credential_missing", 503)
        semantic = digest(
            {
                "template_id": template["template_id"],
                "entity_id": template["entity_id"],
                "service": template["service"],
                "expected_revision": expected,
                "principal": self.console.config["principal"],
            }
        )
        intent = self._intent(client_id)
        if intent is None:
            intent = self._prepare(client_id, semantic, template, expected)
        # One client id is one reviewed intent: different content never reuses the outcome.
        require(intent["semantic"] == semantic, "idempotency_conflict", 409)
        return await self._settle(template, intent, session)

    def _prepare(self, client_id, semantic, template, expected):
        """Durably record one intent before HA is contacted at all."""
        require(self._revision(template["entity_id"]) == expected, "state_conflict", 409)
        record = (
            client_id,
            semantic,
            template["template_id"],
            template["entity_id"],
            template["service"],
            template["label"],
            expected,
            self.clock(),
            PREPARED,
            None,
            None,
            None,
            None,
            None,
        )
        with closing(sqlite3.connect(self.ledger_path, timeout=5)) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "INSERT INTO control_intents ("
                    + ",".join(CONTROL_COLUMNS)
                    + ") VALUES("
                    + ",".join("?" * len(CONTROL_COLUMNS))
                    + ")",
                    record,
                )
                db.commit()
            except sqlite3.IntegrityError:
                # Another attempt recorded this client id first; its intent wins.
                db.rollback()
        prepared = self._intent(client_id)
        require(prepared is not None, "dependency_unavailable", 503)
        return prepared

    def _lease(self):
        """Long enough for one observation plus one service call, and nothing longer."""
        return 2 * self.timeout + 2

    def _claim(self, intent):
        """Become the single durable executor of this intent, or find that someone else is.

        This is a compare-and-set inside `BEGIN IMMEDIATE`, so SQLite serializes it across
        processes too: with several instances sharing the ledger exactly one of them wins, and
        no in-memory lock is involved. A claim whose owner died is taken over after its lease
        expires; takeover never transmits anything by itself.
        """
        owner = "owner:" + uuid.uuid4().hex
        now = self.clock()
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state,lease_expires_at FROM control_intents WHERE client_id=?",
                (intent["client_id"],),
            ).fetchone()
            if row is None or row["state"] not in ACTIVE_STATES:
                db.rollback()
                return None
            if row["state"] != PREPARED and (row["lease_expires_at"] or 0) > now:
                # A live owner is executing this very intent.
                db.rollback()
                return None
            sending = row["state"] == SENDING
            cursor = db.execute(
                "UPDATE control_intents SET state=?,owner=?,lease_expires_at=? WHERE client_id=?"
                " AND (state=? OR (state IN (?,?) AND lease_expires_at <= ?))",
                (
                    SENDING if sending else OBSERVING,
                    owner,
                    now + self._lease(),
                    intent["client_id"],
                    PREPARED,
                    OBSERVING,
                    SENDING,
                    now,
                ),
            )
            won = cursor.rowcount == 1
            if won:
                db.commit()
            else:
                db.rollback()
        return (owner, sending) if won else None

    def _release(self, intent, owner):
        """Give back a claim that never transmitted anything, so a later request may run it."""
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "UPDATE control_intents SET state=?,owner=NULL,lease_expires_at=NULL"
                    " WHERE client_id=? AND owner=? AND state=?",
                    (PREPARED, intent["client_id"], owner, OBSERVING),
                )
                db.commit()
        except (sqlite3.Error, OSError):
            # The lease still expires, and an `observing` takeover transmits at most once.
            pass

    def _transmit(self, intent, owner):
        """The durable boundary: after this row the command may be on its way to the device.

        Only the owner that still holds an `observing` claim crosses it, and only once.
        """
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                "UPDATE control_intents SET state=?,lease_expires_at=? WHERE client_id=?"
                " AND owner=? AND state=?",
                (SENDING, self.clock() + self._lease(), intent["client_id"], owner, OBSERVING),
            )
            claimed = cursor.rowcount == 1
            db.commit()
        return claimed

    def _authorize(self, intent, session):
        """Authority and the reading the operator acted on, checked before anything is sent.

        Revision 0 means the operator acted without any reading of this entity: there is no
        earlier view that could have gone stale, so the connector's own pre-send observation
        becomes the first one. Any real revision must still be the one the connector holds.
        """
        code = self.code(session)
        require(code == "ready", code, 403)
        require(
            intent["expected_revision"] == 0
            or self._revision(intent["entity_id"]) == intent["expected_revision"],
            "state_conflict",
            409,
        )

    async def _settle(self, template, intent, session):
        """At most one transmitted command per reviewed intent, however many requests arrive."""
        for _ in range(CLAIM_ATTEMPTS):
            if intent["state"] not in ACTIVE_STATES:
                return self._replay(intent)
            claim = self._claim(intent)
            if claim is None:
                outcome = await self._await_owner(intent)
                if outcome is not RETRY:
                    return outcome
                intent = self._intent(intent["client_id"])
                require(intent is not None, "dependency_unavailable", 503)
                continue
            owner, sending = claim
            return await self._execute(template, intent, session, owner, sending)
        raise self._fault("control_in_progress")

    async def _await_owner(self, intent):
        """Another owner holds this intent: wait for its outcome instead of sending again."""
        deadline = self.clock() + self._lease() + 1
        while self.clock() < deadline:
            row = self._intent(intent["client_id"])
            require(row is not None, "dependency_unavailable", 503)
            if row["state"] not in ACTIVE_STATES:
                return self._replay(row)
            if row["state"] == PREPARED or (row["lease_expires_at"] or 0) <= self.clock():
                # The owner gave the claim back, or died: taking over is safe from here.
                return RETRY
            await asyncio.sleep(0.05)
        raise self._fault("control_in_progress")

    async def _execute(self, template, intent, session, owner, sending):
        """The owned critical section: observe, re-check, transmit once, record the receipt."""
        async with self._client() as client:
            if sending:
                # An earlier attempt may already be on its way, and the device still showing the
                # old state proves nothing. Only an observation may conclude this intent, and
                # nothing is ever transmitted again for it.
                reading = await self._observe(client, template["entity_id"])
                if reading["state"] == SERVICES[template["service"]]:
                    return self._answer(intent, OBSERVED, "recovered_at_target", True)
                self._record(intent, UNKNOWN, "control_unverified", False)
                raise self._fault("control_unverified")
            try:
                self._authorize(intent, session)
                reading = await self._observe(client, template["entity_id"])
                # The observation is asynchronous: the operator's authority and the reading they
                # acted on are checked again before anything leaves this process.
                self._authorize(intent, session)
                if reading["state"] == SERVICES[template["service"]]:
                    return self._answer(intent, OBSERVED, "recovered_at_target", True)
                claimed = self._transmit(intent, owner)
            except Fault:
                self._release(intent, owner)
                raise
            if not claimed:
                raise self._fault("control_in_progress")
            try:
                state, code, reported = await self._invoke(client, template)
            except Fault as exc:
                # A redirect means HA never served the service; anything else (timeout, lost
                # connection, unusable reply) leaves the outcome unknown, never a failure claim.
                if exc.code == "device_redirect":
                    state, code, reported = REJECTED, "device_redirect", False
                else:
                    state, code, reported = UNKNOWN, exc.code, False
        if state not in {ACCEPTED, OBSERVED}:
            self._record(intent, state, code, reported)
            raise self._fault(code)
        return self._answer(intent, state, code, reported)

    async def _invoke(self, client, template):
        """Call the entity's own domain service; a receipt is an acceptance, not an execution."""
        entity = self.entities[template["entity_id"]]
        status, raw = await self._call(
            client,
            "POST",
            "/api/services/" + entity["kind"] + "/" + template["service"],
            {"entity_id": entity["entity_id"]},
        )
        if status in {401, 403}:
            return REJECTED, "device_unauthorized", False
        if status in REJECTED_STATUS:
            return REJECTED, "device_rejected", False
        if status != 200:
            # A 5xx (or anything unexpected) may still have had an effect: unknown, not failed.
            return UNKNOWN, "device_failed", False
        try:
            document = loads(raw)
        except Fault:
            return UNKNOWN, "receipt_unreadable", False
        if not isinstance(document, list):
            return UNKNOWN, "receipt_unreadable", False
        target = SERVICES[template["service"]]
        reported = any(
            isinstance(item, dict)
            and item.get("entity_id") == entity["entity_id"]
            and item.get("state") == target
            for item in document
        )
        return ACCEPTED, "ok", reported

    def _answer(self, intent, state, code, reported):
        """Verify the outcome durably, then answer without ever inventing an execution."""
        durable = self._record(intent, state, code, reported)
        return {
            **self._document(intent, state, code, reported, self.clock()),
            "deduplicated": False,
            "durable": durable,
        }

    def _replay(self, intent):
        if intent["state"] not in {ACCEPTED, OBSERVED}:
            # An unknown or rejected outcome is replayed as that outcome, never as success.
            raise self._fault(intent["code"])
        return {
            **self._document(
                intent,
                intent["state"],
                intent["code"],
                bool(intent["target_reported"]),
                intent["settled_at"],
            ),
            "deduplicated": True,
            "durable": True,
        }

    def _document(self, intent, state, code, reported, settled_at):
        requested = SERVICES[intent["service"]]
        row = self._readings().get(intent["entity_id"])
        return {
            "control_id": intent["client_id"],
            "template_id": intent["template_id"],
            "label": intent["label"],
            "entity_id": intent["entity_id"],
            "service": intent["service"],
            "requested_state": requested,
            "acceptance": {
                PREPARED: "pending",
                OBSERVING: "executing",
                SENDING: "executing",
                ACCEPTED: "accepted",
                OBSERVED: "observed",
                UNKNOWN: "unknown",
                REJECTED: "rejected",
            }[state],
            "code": code,
            "target_reported": bool(reported),
            "recorded_at": utc(settled_at) if settled_at else None,
            "observation": self._observation(intent, state, requested, row, settled_at),
            "revision": row["revision"] if row else 0,
        }

    def _observation(self, intent, state, requested, row, settled_at):
        """Acceptance and later observation are separate evidence and stay separate facts."""
        if state == REJECTED:
            return {"status": "not_sent", "state": None, "observed_at": None}
        if state == UNKNOWN:
            return {"status": "unknown", "state": None, "observed_at": None}
        if state == OBSERVED:
            observed = row["observed_at"] if row else None
            return {"status": "confirmed", "state": requested, "observed_at": observed}
        if row is not None and row["attempt_code"] != "ok" and row["attempt_at"]:
            if settled_at is not None and epoch(row["attempt_at"]) > settled_at:
                return {"status": "unreadable", "state": None, "observed_at": row["attempt_at"]}
        if row is not None and row["observed_at"] and settled_at is not None:
            if epoch(row["observed_at"]) > settled_at:
                if row["code"] in {"unavailable", "unknown"}:
                    status = "unavailable"
                elif row["state"] == requested:
                    status = "confirmed"
                elif row["state"] is None:
                    status = "unknown"
                else:
                    status = "contradicted"
                return {"status": status, "state": row["state"], "observed_at": row["observed_at"]}
        return {"status": "pending", "state": None, "observed_at": None}

    # ---------------------------------------------------------------- ledger

    def _connect(self):
        return closing(sqlite3.connect(self.ledger_path, timeout=5))

    def _readings(self):
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM readings").fetchall()
        return {
            row["entity_id"]: {**dict(row), "value": loads(row["value"]) if row["value"] else None}
            for row in rows
        }

    def _revision(self, entity_id):
        row = self._readings().get(entity_id)
        return row["revision"] if row else 0

    def _store_attempt(self, entity_id, code):
        now = utc(self.clock())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT revision FROM readings WHERE entity_id=?", (entity_id,)
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO readings VALUES(?,0,NULL,?,NULL,NULL,NULL,?,?)",
                    (entity_id, "never_read", now, code),
                )
            else:
                db.execute(
                    "UPDATE readings SET attempt_at=?,attempt_code=? WHERE entity_id=?",
                    (now, code, entity_id),
                )
            db.commit()

    def _store_reading(self, entity_id, reading):
        """Persist one observation; the revision moves only when the reading really changed."""
        now = utc(self.clock())
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM readings WHERE entity_id=?", (entity_id,)).fetchone()
            same = row is not None and (
                row["code"],
                row["state"],
                loads(row["value"]) if row["value"] else None,
                row["unit"],
            ) == (reading["code"], reading["state"], reading["value"], reading["unit"])
            revision = row["revision"] if same else (row["revision"] + 1 if row else 1)
            db.execute(
                "INSERT OR REPLACE INTO readings VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    entity_id,
                    revision,
                    now,
                    reading["code"],
                    reading["state"],
                    canonical(reading["value"]) if reading["value"] is not None else None,
                    reading["unit"],
                    now,
                    "ok",
                ),
            )
            db.commit()

    def _intent(self, client_id):
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM control_intents WHERE client_id=?", (client_id,)
            ).fetchone()
        return dict(row) if row else None

    def _intents(self):
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM control_intents ORDER BY prepared_at DESC LIMIT ?", (HISTORY,)
            ).fetchall()
        return [dict(row) for row in rows]

    def _record(self, intent, state, code, reported):
        """Best-effort durable outcome: a lost write never turns an accepted command into a failure.

        The intent then keeps its durable state (`observing` or `sending`), so the next request
        for the same client id reconciles by observation instead of issuing the command again. A
        recorded outcome is only ever improved: a late writer holding weaker evidence cannot
        downgrade an observation that already confirmed the requested state.
        """
        try:
            with self._connect() as db:
                db.row_factory = sqlite3.Row
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT state FROM control_intents WHERE client_id=?", (intent["client_id"],)
                ).fetchone()
                if row is not None and OUTCOME_RANKS.get(row["state"], 0) > OUTCOME_RANKS.get(
                    state, 0
                ):
                    db.commit()
                    return True
                db.execute(
                    "UPDATE control_intents SET state=?,code=?,target_reported=?,settled_at=?,"
                    "owner=NULL,lease_expires_at=NULL WHERE client_id=?",
                    (state, code, 1 if reported else 0, self.clock(), intent["client_id"]),
                )
                db.commit()
            return True
        except (sqlite3.Error, OSError):
            return False

    # ------------------------------------------------------------ projection

    def view(self, session):
        """Redacted projection: registered labels, readings and recent controls."""
        require(self.config is not None, "home_disabled", 403)
        code = self.code(session)
        readings = self._readings()
        return {
            "connector": {
                "available": True,
                "code": code,
                "enabled": self.enabled,
                "control_available": code in {"ready", "control_required"},
                "unlocked": code == "ready",
                "unlock_ttl_seconds": self.unlock_ttl,
                "stale_after_seconds": self.max_age,
                "timeout_seconds": self.timeout,
                "entities": len(self.entities),
                "templates": len(self.templates),
                "readable": self.token is not None,
            },
            "entities": [
                self._entity_view(entity, readings.get(entity_id))
                for entity_id, entity in self.entities.items()
            ],
            "controls": [self._control_view(intent, readings) for intent in self._intents()],
        }

    def _entity_view(self, entity, row):
        availability, code = self._availability(row)
        return {
            "entity_id": entity["entity_id"],
            "label": entity["label"],
            "kind": entity["kind"],
            "unit": row["unit"] if row and row["unit"] else entity["unit"],
            "availability": availability,
            "code": code,
            "state": row["state"] if row else None,
            "value": row["value"] if row else None,
            "observed_at": row["observed_at"] if row else None,
            "attempt_at": row["attempt_at"] if row else None,
            "revision": row["revision"] if row else 0,
            "control": entity["kind"] in CONTROLLABLE,
            "templates": [
                {
                    "template_id": item["template_id"],
                    "label": item["label"],
                    "service": item["service"],
                }
                for item in self.by_entity[entity["entity_id"]]
            ],
        }

    def _control_view(self, intent, readings):
        document = self._document(
            intent,
            intent["state"],
            intent["code"] or STAGE_CODES.get(intent["state"], "attempting"),
            bool(intent["target_reported"]),
            intent["settled_at"],
        )
        return {key: document[key] for key in sorted(document) if key != "durable"}

    # ------------------------------------------------------------ task centre
    #
    # A read-only window over this module's own durable ledger (A10/A12). Nothing is copied
    # into a second execution table: the task centre reads these rows and normalizes the words
    # this module already publishes. The ordering key is the immutable second the intent was
    # recorded in plus its identifier, so a record can never move across a cursor while its
    # own state changes underneath. `None` from any read below means "this ledger cannot be
    # read at all", which the caller must report as a missing source and never as an empty list.

    def source_state(self):
        """This connector's own state word for the task centre; unknown is never healthy."""
        if self.config is None:
            return {"state": "unconfigured", "code": "home_disabled"}
        if not self.enabled:
            return {"state": "disabled", "code": "control_disabled"}
        if self.token is None:
            return {"state": "credential_missing", "code": "device_credential_missing"}
        try:
            readings = self._readings()
        except (sqlite3.Error, OSError):
            return {"state": "unreadable", "code": "dependency_unavailable"}
        if not readings:
            return {"state": "never_read", "code": "never_read"}
        failed = next((row for row in readings.values() if row["attempt_code"] != "ok"), None)
        if failed is not None:
            # One unreachable entity is one real failure of the source, with its own reason.
            return {"state": "offline", "code": failed["attempt_code"]}
        return {"state": "online", "code": "ok"}

    def history(self, *, after=None, limit=20, states=None, prefix=TASK_PREFIX):
        """One page of the durable control ledger, newest first, keyset by immutable key.

        `states` filters on this module's own words. `prefix` is the identifier space the
        caller publishes these rows under, so the cursor of the caller's merged order compares
        against exactly the same key it was built from. Returns `(records, has_more)` or `None`
        when the ledger is unreadable.
        """
        require(
            type(limit) is int
            and 1 <= limit <= HISTORY_LIMIT
            and isinstance(prefix, str)
            and prefix,
            "invalid_input",
            400,
        )
        where, params = [], []
        if after is not None:
            require(is_ledger_key(after), "invalid_input", 400)
            where.append("(CAST(prepared_at AS INTEGER), ? || client_id) < (?, ?)")
            params += [prefix, after[0], after[1]]
        if states is not None:
            names = sorted(states)
            require(names and set(names) <= LEDGER_STATES, "invalid_input", 400)
            where.append("state IN (" + ",".join("?" * len(names)) + ")")
            params += names
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self._rows(
            "SELECT * FROM control_intents"
            + clause
            + " ORDER BY CAST(prepared_at AS INTEGER) DESC, ? || client_id DESC LIMIT ?",
            (*params, prefix, limit + 1),
        )
        if rows is None:
            return None
        readings = self._reading_rows()
        records = [self._history_record(dict(row), readings, prefix) for row in rows[:limit]]
        return records, len(rows) > limit

    def history_count(self):
        """How many durable control intents this ledger holds, or `None` if unreadable."""
        rows = self._rows("SELECT COUNT(*) AS total FROM control_intents")
        return rows[0]["total"] if rows else None

    def control_record(self, client_id, prefix=TASK_PREFIX):
        """One intent re-read live for a detail view; `None` when this ledger has no such row."""
        rows = self._rows("SELECT * FROM control_intents WHERE client_id=?", (client_id,))
        if not rows:
            return None
        return self._history_record(dict(rows[0]), self._reading_rows(), prefix)

    def _reading_rows(self):
        """Readings for evidence; an unreadable reading table never hides a control record."""
        try:
            return self._readings()
        except (sqlite3.Error, OSError):
            return {}

    def _rows(self, statement, params=()):
        try:
            with self._connect() as db:
                db.row_factory = sqlite3.Row
                return db.execute(statement, params).fetchall()
        except (sqlite3.Error, OSError):
            return None

    def _history_record(self, intent, readings, prefix):
        return {
            "task_id": prefix + intent["client_id"],
            "created_at": intent["prepared_at"],
            "updated_at": intent["settled_at"] or intent["prepared_at"],
            "state": intent["state"],
            # The origin is the registered entity and action template the record was made for:
            # a record whose origin left the registration stays visible and says so.
            "origin": "registered"
            if intent["template_id"] in self.templates and intent["entity_id"] in self.entities
            else "unregistered",
            "document": self._control_view(intent, readings),
        }

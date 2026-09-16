"""Read-only task centre: one honest projection over the platform's existing ledgers.

This module adds no task database and no execution path. The platform already persists what it
really did, and the task centre reads exactly those records through the ports their own modules
expose:

* the household connector's durable control ledger (`<db>.home-controls.sqlite`), where every
  reviewed device command moves through `prepared / observing / sending / accepted / observed /
  unknown / rejected`;
* the Models owner's operation record (`audit`), where every published configuration version is
  written together with the operator that published it;
* the console's own claim ledger (`<db>.web-models.sqlite`), where a publication attempt whose
  receipt never became the authoritative version is still recorded.

Reading and executing stay separate (A10/A12): an acceptance is not an execution, an observation
is evidence, and an outcome that cannot be established stays unknown. Accepted, running, observed,
unknown and failed are therefore five distinct statuses carrying the evidence that produced each
of them, and they are never collapsed into one success mark. A source that is not connected, that
was removed, or whose ledger cannot be read says so instead of showing an empty list.

Cancellation is not offered here. Only the executing module may cancel, this round has no such
capability, and an already transmitted home command is never re-sent from this page, so every
record carries `cancel.supported = false` with the code the operator needs to understand why.

Records are ordered by the immutable whole second they were recorded in together with their
identifier. That key never changes, so a cursor cannot skip a record whose state changed while a
later page was being read and cannot show one twice; records written meanwhile are newer than the
cursor and arrive on the next poll.
"""

import base64
import uuid

from . import home as home_module
from . import web_models as models_module
from .contracts import Fault, canonical, digest, loads, require, utc
from .storage import is_ledger_key

# Five separate statuses, each naming exactly what is known; they are never merged.
STATUSES = ("accepted", "in_progress", "observed", "unknown", "failed")
SORT = "recorded_desc"
CURSOR_VERSION = 1
DEFAULT_PAGE = 10
MAX_PAGE = 50

# This module's words for the module words, taken from each module's own vocabulary.
HOME_STATUS = {
    home_module.PREPARED: ("accepted", "pending"),
    home_module.OBSERVING: ("in_progress", "observing"),
    home_module.SENDING: ("in_progress", "executing"),
    home_module.ACCEPTED: ("accepted", "accepted"),
    home_module.OBSERVED: ("observed", "observed"),
    home_module.UNKNOWN: ("unknown", "unknown"),
    home_module.REJECTED: ("failed", "rejected"),
}
MODEL_STATUS = {
    models_module.PUBLISHED: ("observed", "published"),
    models_module.AUDIT_WITHOUT_AUTHORITY: ("unknown", "unverified"),
    models_module.CORRUPT: ("unknown", "corrupt"),
    models_module.INTENT_UNRESOLVED: ("unknown", "unverified"),
}


def _by_status(table):
    """One normalized status to the module states it is made of, for exact SQL filtering."""
    result = {status: frozenset() for status in STATUSES}
    for state, (status, _) in table.items():
        result[status] = result[status] | {state}
    return result


MODULES = {
    "platform.home": {
        "kind": home_module.TASK_KIND,
        "prefixes": (home_module.TASK_PREFIX,),
        "states": frozenset(HOME_STATUS),
        "by_status": _by_status(HOME_STATUS),
        "cancel_code": "executor_does_not_cancel",
    },
    "platform.models": {
        "kind": models_module.TASK_KIND,
        "prefixes": (models_module.VERSION_PREFIX, models_module.INTENT_PREFIX),
        "states": frozenset(MODEL_STATUS),
        "by_status": _by_status(MODEL_STATUS),
        "cancel_code": "recorded_fact",
    },
}
# Declared, never invented: no owner has a contracted task vocabulary for these this round, and
# this product does not read another product's database to guess one.
PENDING_MODULES = (
    {"id": "companion.core", "state": "not_connected", "code": "no_task_contract"},
    {"id": "assets.remote", "state": "not_connected", "code": "no_task_contract"},
    {"id": "resources.download", "state": "not_connected", "code": "no_task_contract"},
    {"id": "platform.dialogue", "state": "not_connected", "code": "no_operation_ledger"},
)
SOURCE_IDS = tuple(MODULES) + tuple(item["id"] for item in PENDING_MODULES)
SUMMARY_FIELDS = (
    "task_id",
    "source",
    "kind",
    "title",
    "target",
    "status",
    "stage",
    "code",
    "created_at",
    "updated_at",
    "settled",
    "pending",
    "attention",
    "source_state",
    "cancel",
    "evidence",
    "module",
)
MODULE_PAGES = {"platform.home": "#/home", "platform.models": "#/settings/2"}


class Tasks:
    """Console-side read-only aggregation; every write belongs to another module."""

    def __init__(self, platform, console):
        self.p = platform
        self.console = console

    # ------------------------------------------------------------------ routes

    def route(self, path, body, session):
        require(isinstance(body, dict), "invalid_input", 400)
        if path == "/api/web/tasks/view":
            return self.view(body, session)
        if path == "/api/web/tasks/detail":
            return self.detail(body, session)
        raise Fault("not_found", 404)

    # -------------------------------------------------------------------- page

    def view(self, body, session):
        """One page of platform operation records, newest first, with each source's own state."""
        require(set(body) <= {"status", "source", "cursor", "page_size"}, "invalid_input", 400)
        status, source, size = self._filters(body)
        cursor, at = self._cursor(body.get("cursor"), status, source)
        records, more, readable = [], False, {}
        for name, module in MODULES.items():
            if source is not None and source != name:
                continue
            wanted = module["states"] if status is None else module["by_status"][status]
            if not wanted:
                continue
            page = self._page(name, cursor.get(name), size, wanted)
            readable[name] = page is not None
            if page is None:
                # One unreadable ledger never erases the records the other one still holds.
                continue
            found, stream_more = page
            records += found
            more = more or stream_more
        # Both streams returned their own next page in exactly this order, so the merged head is
        # the true next page of the requested order. A record written after the cursor was made
        # is newer than everything it already delivered: it belongs to a fresh first page, never
        # in the middle of this walk, so the walk stays strictly ordered and never repeats.
        records.sort(key=self._position, reverse=True)
        if at is not None:
            records = [record for record in records if self._position(record) < at]
        page_records = records[:size]
        page_more = more or len(records) > size
        # The cursor keeps every stream at the position of the last record it contributed, so
        # the next page continues each of them exactly where this one stopped.
        positions = dict(cursor)
        for record in page_records:
            positions[self._source_of(record["task_id"])] = self._position(record)
        items = [self._task(record) for record in page_records]
        watermark = self._position(page_records[-1]) if page_records else None
        return {
            "generated_at": utc(self.p.models.clock()),
            "filters": {"status": status, "source": source, "page_size": size},
            "statuses": list(STATUSES),
            "sources": self._sources(readable),
            "items": [self._summary(item) for item in items],
            "page": {
                "size": size,
                "returned": len(page_records),
                "has_more": bool(page_more),
                "sort": SORT,
                "next_cursor": (
                    self._encode(positions, status, source, watermark)
                    if page_more and watermark is not None
                    else None
                ),
            },
        }

    def detail(self, body, session):
        """One record re-read live: a page is a snapshot, a detail is the record as it is now."""
        require(set(body) == {"task_id"}, "invalid_input", 400)
        record = self._record(body["task_id"])
        require(record is not None, "not_found", 404)
        return {
            "generated_at": utc(self.p.models.clock()),
            "task": self._task(record),
            "sources": self._sources({}),
        }

    # ----------------------------------------------------------------- streams

    def _page(self, name, after, size, states):
        """One module's next page, or `None` when that module's ledger cannot be read."""
        if name == "platform.home":
            found = self.console.home.history(after=after, limit=size, states=states)
            return None if found is None else (found[0], found[1])
        found = self.console.models.publications(after=after, limit=size, states=states)
        return None if found is None else (found[0], found[1])

    def _record(self, task_id):
        """One record by identifier; an unknown identifier is a real 404, not an empty record."""
        require(isinstance(task_id, str) and 0 < len(task_id) <= 128, "invalid_input", 400)
        if task_id.startswith(home_module.TASK_PREFIX):
            client_id = self._identifier(task_id, home_module.TASK_PREFIX)
            return self.console.home.control_record(client_id, prefix=home_module.TASK_PREFIX)
        if task_id.startswith(models_module.VERSION_PREFIX):
            rest = task_id[len(models_module.VERSION_PREFIX) :]
            target, _, version = rest.partition(":")
            require(
                target in models_module.TARGETS and version.isdigit() and int(version) > 0,
                "invalid_input",
                400,
            )
            return self.console.models.version_record(target, int(version))
        if task_id.startswith(models_module.INTENT_PREFIX):
            client_id = self._identifier(task_id, models_module.INTENT_PREFIX)
            return self.console.models.intent_record(client_id)
        raise Fault("not_found", 404)

    def _identifier(self, task_id, prefix):
        """The identifier part of a record id: canonical uuid text, or nothing at all."""
        value = task_id[len(prefix) :]
        require(len(value) == 36, "invalid_input", 400)
        try:
            require(str(uuid.UUID(value)) == value, "invalid_input", 400)
        except ValueError:
            raise Fault("invalid_input", 400) from None
        return value

    def _source_of(self, task_id):
        """Which responsible module a record identifier belongs to."""
        for name, module in MODULES.items():
            for prefix in module["prefixes"]:
                if task_id.startswith(prefix):
                    return name
        raise Fault("invalid_input", 400)

    def _position(self, record):
        """The immutable position: whole recorded second, then the record identifier."""
        return (int(record["created_at"]), record["task_id"])

    # ----------------------------------------------------------------- records

    def _task(self, record):
        """One normalized record together with the evidence its status was read from."""
        source = self._source_of(record["task_id"])
        module = MODULES[source]
        document = record["document"]
        if source == "platform.home":
            status, stage = HOME_STATUS[record["state"]]
            built = self._home_task(record, document, status)
        else:
            status, stage = MODEL_STATUS[record["state"]]
            built = self._model_task(record, document, status)
        built.update(
            task_id=record["task_id"],
            source=source,
            kind=module["kind"],
            status=status,
            stage=stage,
            created_at=utc(int(record["created_at"])),
            updated_at=utc(int(record["updated_at"])),
            pending=status in {"accepted", "in_progress"},
            cancel={"supported": False, "code": module["cancel_code"]},
            module={"page": MODULE_PAGES[source]},
        )
        built["attention"] = bool(built["attention"]) or built["source_state"] != "current"
        return built

    def _home_task(self, record, document, status):
        """A device command: the receipt and the later observation are separate evidence."""
        observation = document["observation"]
        if self.console.home.config is None:
            # The whole registration is gone: the history stays visible and says so.
            source_state = "source_missing"
        elif record["origin"] == "registered":
            source_state = "current"
        else:
            source_state = "origin_missing"
        return {
            "title": document["label"],
            "target": document["entity_id"],
            "code": document["code"],
            "settled": record["state"] not in home_module.ACTIVE_STATES,
            "source_state": source_state,
            "attention": status in {"unknown", "failed"} or observation["status"] == "contradicted",
            "evidence": {
                "acceptance": document["acceptance"],
                "receipt": bool(document["target_reported"]),
                "observation": observation["status"],
                "observation_state": observation["state"],
                "observed_at": observation["observed_at"],
                "revision": document["revision"],
            },
            "request": {
                "template_id": document["template_id"],
                "entity_id": document["entity_id"],
                "service": document["service"],
                "requested_state": document["requested_state"],
            },
            "timeline": {
                "recorded_at": document["recorded_at"],
                "observed_at": observation["observed_at"],
            },
        }

    def _model_task(self, record, document, status):
        """A publication: the authoritative version and any claim about it are separate facts."""
        base = {
            "title": document["target_label"],
            "target": document["target"],
            "settled": True,
            "source_state": "current",
            "request": {"target": document["target"], "version": document["version"]},
        }
        if "operation" in document:
            # A publication operation the platform itself recorded, with the stored version.
            base.update(
                code=document["integrity"],
                attention=status == "unknown" or document["revoked"],
                evidence={
                    "operation": document["operation"],
                    "actor": document["actor"],
                    "integrity": document["integrity"],
                    "availability": document["availability"],
                    "revoked": document["revoked"],
                    "claim": document["claim"] is not None,
                },
                timeline={
                    "recorded_at": document["recorded_at"],
                    "published_at": document["published_at"],
                    "usable_until": document["usable_until"],
                    "revoked_at": document["revoked_at"],
                },
            )
        else:
            # A claim whose receipt never became the authoritative version: never a publication.
            base.update(
                code=document["code"],
                settled=bool(document["receipt_recorded"]),
                attention=True,
                evidence={
                    "receipt": document["receipt"],
                    "receipt_recorded": bool(document["receipt_recorded"]),
                    "authoritative": document["authoritative"],
                    "window": document["window"],
                },
                timeline={
                    "recorded_at": document["prepared_at"],
                    "settled_at": document["settled_at"],
                    "usable_until": document["usable_until"],
                },
            )
        return base

    def _summary(self, task):
        """The list projection: the same record, without the fields only a detail needs."""
        return {key: task[key] for key in SUMMARY_FIELDS if key in task}

    # ---------------------------------------------------------------- sources

    def _sources(self, readable):
        """Every responsible module's own state, including the ones with nothing to show."""
        result = []
        for name, module in MODULES.items():
            if name == "platform.home":
                state = self.console.home.source_state()
                records = self.console.home.history_count()
                if state["state"] == "unconfigured" and records:
                    # A registration that was removed leaves its real history behind.
                    state = {"state": "missing", "code": "home_removed"}
            else:
                state = self.console.models.ledger_state()
                records = self.console.models.ledger_count()
                if state["state"] == "available" and not records:
                    state = {"state": "empty", "code": "no_records"}
            if readable.get(name) is False:
                # A ledger that could not be read for this page says so, whatever its own
                # state probe answered.
                state = {"state": "unreadable", "code": "dependency_unavailable"}
            result.append(
                {
                    "id": name,
                    "kind": module["kind"],
                    "connected": True,
                    "state": state["state"],
                    "code": state["code"],
                    "records": records,
                    "cancel_code": module["cancel_code"],
                }
            )
        result += [
            {
                "id": item["id"],
                "kind": None,
                "connected": False,
                "state": item["state"],
                "code": item["code"],
                "records": None,
                "cancel_code": None,
            }
            for item in PENDING_MODULES
        ]
        return result

    # ----------------------------------------------------------------- cursor

    def _filters(self, body):
        status = body.get("status")
        require(status is None or status in STATUSES, "invalid_input", 400)
        source = body.get("source")
        require(source is None or source in SOURCE_IDS, "invalid_input", 400)
        size = body.get("page_size", DEFAULT_PAGE)
        require(type(size) is int and 1 <= size <= MAX_PAGE, "invalid_input", 400)
        return status, source, size

    def _filter_words(self, status, source):
        return {"status": status, "source": source, "sort": SORT}

    def _encode(self, positions, status, source, at):
        """An opaque continuation: the filter it belongs to, each stream's position, the watermark."""
        document = {
            "v": CURSOR_VERSION,
            "filter": digest(self._filter_words(status, source)),
            "streams": {
                name: list(position) for name, position in sorted(positions.items()) if position
            },
            "at": list(at),
        }
        return base64.urlsafe_b64encode(canonical(document).encode()).decode("ascii")

    def _cursor(self, value, status, source):
        """Decode a continuation, bound to the very sorting and filtering it was made for."""
        if value is None:
            return {}, None
        require(isinstance(value, str) and 0 < len(value) <= 1024, "invalid_input", 400)
        try:
            raw = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
        except (ValueError, UnicodeError):
            raise Fault("invalid_input", 400) from None
        document = loads(raw)
        require(
            isinstance(document, dict)
            and set(document) == {"v", "filter", "streams", "at"}
            and document["v"] == CURSOR_VERSION
            and isinstance(document["filter"], str)
            and isinstance(document["streams"], dict),
            "invalid_input",
            400,
        )
        require(
            document["filter"] == digest(self._filter_words(status, source)),
            "cursor_conflict",
            409,
        )
        positions = {}
        for name, position in document["streams"].items():
            require(name in MODULES and isinstance(position, list), "invalid_input", 400)
            require(is_ledger_key(tuple(position)), "invalid_input", 400)
            require(self._source_of(position[1]) == name, "invalid_input", 400)
            positions[name] = (position[0], position[1])
        at = document["at"]
        require(is_ledger_key(tuple(at)) if isinstance(at, list) else False, "invalid_input", 400)
        self._source_of(at[1])
        return positions, (at[0], at[1])

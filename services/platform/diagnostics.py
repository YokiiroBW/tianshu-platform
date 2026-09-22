"""The platform's own safe runtime event log: one frozen record per line, and nothing else.

This is a leaf adapter. It knows the frozen `contracts/diagnostics/v1` field set, the event
vocabulary this product registers in *this* file, and one durable file sink. It deliberately
imports no business module and never accepts a free-form mapping, so a request path, a persona
body, a credential or a third-party exception string has no route into a log line: the only
things a caller can supply are registered names and bounded scalars.

Two rules shape the whole design.

*The log is not a second business database.* Business outcomes still come from the original
domain ledgers. A failed write therefore never raises into a request, never causes a retry and
never rewrites an `unknown` result into something more comfortable; it only stops *new* work from
being admitted and turns `ready` red.

*Full means full.* Every registered event and level is written; nothing is sampled, filtered by
success or silently de-duplicated. The directory cap is enforced by refusing new work and keeping
every sealed segment, never by deleting what a collector has not confirmed.
"""

import contextvars
import errno
import json
import os
import re
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone

SCHEMA_VERSION = "1.0.0"
SERVICE = "platform"
# The published `service` enum. This service writes exactly one value of it; the rest are listed so
# the assertion is against the contract rather than against this product's own choice.
SERVICE_NAMES = ("platform", "companion", "memory", "memory-knowledge", "gateway")
# The frozen field set and its order. `additionalProperties: false` in the published schema makes
# any extra key a contract violation, so the record is assembled from exactly these names.
FIELDS = (
    "schema_version",
    "timestamp",
    "service",
    "instance_id",
    "sequence",
    "event_id",
    "level",
    "event",
    "outcome",
    "correlation_id",
    "duration_ms",
    "error_code",
)
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
OUTCOMES = ("started", "succeeded", "failed", "cancelled", "unknown", "rejected", "degraded")

# Event names and the levels each one may carry. A user string that happens to match the schema
# pattern is still not an event: only a name registered here can ever be written.
EVENTS = {
    # process lifecycle
    "runtime.starting": ("INFO",),
    "runtime.started": ("INFO",),
    "runtime.startup_failed": ("ERROR", "CRITICAL"),
    "runtime.stopping": ("INFO",),
    "runtime.stopped": ("INFO",),
    # configuration and contract loading
    "config.loaded": ("INFO",),
    "config.load_failed": ("ERROR", "CRITICAL"),
    "contract.loaded": ("INFO",),
    "contract.verify_failed": ("ERROR", "CRITICAL"),
    # inbound HTTP: start, authentication result, terminal state
    "http.request.started": ("INFO",),
    "http.auth.succeeded": ("INFO",),
    "http.auth.rejected": ("WARNING",),
    "http.request.finished": ("INFO", "WARNING", "ERROR"),
    "http.request.unknown_path": ("WARNING",),
    # outbound calls: queueing, start, each terminal state
    "outbound.call.queued": ("DEBUG", "INFO"),
    "outbound.call.started": ("INFO",),
    "outbound.call.succeeded": ("INFO",),
    "outbound.call.failed": ("WARNING", "ERROR"),
    "outbound.call.timed_out": ("WARNING",),
    "outbound.call.cancelled": ("INFO",),
    # the log's own durable sink
    "logging.segment_sealed": ("INFO",),
    "logging.unavailable": ("ERROR",),
    "logging.capacity_exhausted": ("ERROR",),
    "logging.recovered": ("INFO", "WARNING"),
    # explicit local adapter actions, never the web console
    "cli.action.started": ("INFO",),
    "cli.action.finished": ("INFO", "ERROR"),
}
# The only error codes the platform may write. A vendor code, an exception message or a SQL
# string never becomes one: anything unrecognised collapses to `internal_error`.
ERROR_CODES = frozenset(
    {
        "internal_error",
        "invalid_input",
        "unauthorized",
        "forbidden",
        "not_found",
        "timeout",
        "budget_exceeded",
        "dependency_unavailable",
        "invalid_upstream",
        "queue_full",
        "result_unknown",
        "deadline_exceeded",
        "too_many_requests",
        "version_conflict",
        "idempotency_conflict",
        "state_conflict",
        "cursor_conflict",
        "session_expired",
        "web_not_configured",
        "core_web_not_connected",
        "persona_read_required",
        "publication_unverified",
        "control_in_progress",
        "control_unverified",
        "log_write_failed",
        "log_capacity_exhausted",
        "log_flush_timeout",
        "log_directory_unusable",
        "log_queue_full",
    }
)

MAX_LINE_BYTES = 4096
SEGMENT_BYTES = 64 * 1024 * 1024
DEFAULT_DIRECTORY_BYTES = 1024 * 1024 * 1024
MIN_DIRECTORY_BYTES = 32 * 1024 * 1024
MAX_DIRECTORY_BYTES = 64 * 1024 * 1024 * 1024
QUEUE_LIMIT = 8192
ADMIT_TIMEOUT = 0.25

# durable | non_durable | unavailable. Only `durable` may back a production `ready`.
DURABLE = "durable"
NON_DURABLE = "non_durable"
UNAVAILABLE = "unavailable"

CORRELATION_PATTERN = re.compile(r"[a-f0-9]{32}\Z")
# The frozen schema pins both identifiers to `format: uuid`; asserting it here keeps a malformed
# identifier from becoming a record no collector would accept.
UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z", re.IGNORECASE
)
HEADER = "X-Tianshu-Correlation-Id"

# One correlation per request, carried in a context variable so async children and
# `asyncio.to_thread` workers inherit exactly the request that created them and never another's.
CORRELATION = contextvars.ContextVar("tianshu_correlation_id", default=None)


def new_correlation():
    """A fresh random correlation id: never derived from a user, account or session."""
    return uuid.uuid4().hex


def valid_correlation(value):
    return isinstance(value, str) and CORRELATION_PATTERN.match(value) is not None


def adopt_correlation(value):
    """The id a request will use: the caller's only when it is already legal, otherwise a new one.

    An illegal value is never echoed back, so a header cannot be used to plant an arbitrary
    string in the log or in the response.
    """
    return value if valid_correlation(value) else new_correlation()


def current_correlation():
    """The correlation of the request in flight in this task, if it carries one."""
    return CORRELATION.get()


def use_correlation(value):
    """Bind an already-legal correlation to this task; returns the token to reset with."""
    return CORRELATION.set(value)


def reset_correlation(token):
    CORRELATION.reset(token)


def _now_iso(wall):
    return (
        datetime.fromtimestamp(wall, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class RecordError(ValueError):
    """A caller asked for something the frozen contract does not allow."""


def build(
    event,
    level,
    outcome,
    *,
    sequence,
    instance_id,
    event_id=None,
    correlation_id=None,
    duration_ms=None,
    error_code=None,
    service=SERVICE,
    wall=None,
):
    """Assemble exactly the frozen field set, validating every value before it is encoded."""
    if event not in EVENTS:
        raise RecordError("unregistered event")
    if level not in EVENTS[event]:
        raise RecordError("level not registered for event")
    if outcome not in OUTCOMES:
        raise RecordError("unknown outcome")
    if type(sequence) is not int or sequence < 1:
        raise RecordError("sequence must be a positive integer")
    if not UUID_PATTERN.fullmatch(str(instance_id)):
        raise RecordError("instance_id must be a uuid")
    if event_id is not None and not UUID_PATTERN.fullmatch(str(event_id)):
        raise RecordError("event_id must be a uuid")
    if service not in SERVICE_NAMES:
        raise RecordError("service is not in the published enum")
    if error_code is not None and error_code not in ERROR_CODES:
        raise RecordError("unregistered error code")
    if correlation_id is not None and not valid_correlation(correlation_id):
        raise RecordError("correlation id must be 32 lowercase hex characters")
    if duration_ms is not None:
        if type(duration_ms) not in (int, float) or duration_ms != duration_ms:
            raise RecordError("duration must be a finite number")
        if duration_ms in (float("inf"), float("-inf")) or duration_ms < 0:
            raise RecordError("duration must be finite and non-negative")
        duration_ms = round(float(duration_ms), 3)
    return {
        "schema_version": SCHEMA_VERSION,
        "timestamp": _now_iso(time.time() if wall is None else wall),
        "service": service,
        "instance_id": instance_id,
        "sequence": sequence,
        "event_id": event_id or str(uuid.uuid4()),
        "level": level,
        "event": event,
        "outcome": outcome,
        "correlation_id": correlation_id,
        "duration_ms": duration_ms,
        "error_code": error_code,
    }


def encode(record):
    """One UTF-8 JSON object plus LF, inside the contract's byte budget.

    `allow_nan=False` is what makes NaN and Infinity a hard error rather than invalid JSON on
    the wire. Because every value above is a registered enum, an identifier or a bounded number,
    the budget holds structurally; the check is an assertion, not a filter that could drop a line.
    """
    if tuple(record) != FIELDS:
        raise RecordError("record must carry exactly the frozen fields, in order")
    line = (
        json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if len(line) > MAX_LINE_BYTES:
        raise RecordError("event exceeds the 4096 byte line budget")
    if b"\n" in line[:-1] or b"\r" in line:
        raise RecordError("event must be a single line")
    return line


class _Sink:
    """One durable JSONL file sink: sealed segments, a directory cap, and no deletions."""

    def __init__(self, directory, instance_id, service, segment_bytes, directory_bytes):
        self.directory = directory
        self.service = service
        self.instance_id = instance_id
        self.segment_bytes = segment_bytes
        self.directory_bytes = directory_bytes
        self.index = 0
        self.size = 0
        self.used = 0
        self.fd = None
        self.sealed = []
        self.error = None
        self._open_next()

    def _path(self, index):
        return os.path.join(self.directory, f"{self.service}-{self.instance_id}-{index:04d}.jsonl")

    def _open_next(self):
        # The cap counts what is already on disk as well as what this process writes, so a
        # restart cannot quietly widen the budget.
        self.used = _directory_bytes(self.directory)
        path = self._path(self.index)
        # `O_BINARY` is what keeps the contract's LF-only JSONL from becoming CRLF on Windows: the
        # default text mode would translate every line ending this sink writes.
        self.fd = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o640
        )
        self.size = os.fstat(self.fd).st_size
        self.used += self.size

    def write(self, line):
        """Append one line. Returns False when the directory cap refuses it; never deletes."""
        if self.size + len(line) > self.segment_bytes:
            self.seal()
            self._open_next()
        if self.used + len(line) > self.directory_bytes:
            self.error = "log_capacity_exhausted"
            return False
        written = os.write(self.fd, line)
        if written != len(line):
            raise OSError(errno.EIO, "short write")
        self.size += written
        self.used += written
        return True

    def seal(self):
        if self.fd is not None:
            os.fsync(self.fd)
            os.close(self.fd)
            self.sealed.append(self._path(self.index))
            self.fd = None
            self.index += 1

    def sync(self):
        if self.fd is not None:
            os.fsync(self.fd)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def _directory_bytes(directory):
    total = 0
    with os.scandir(directory) as entries:
        for entry in entries:
            try:
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total


class Diagnostics:
    """The process-wide event sink. Assembled by the entry point, never by a domain module.

    Callers only ever hand it registered names and bounded scalars, so the adapter needs no
    knowledge of the business to be safe. Everything that can fail about durability is turned
    into one in-memory state plus, at most, one fixed line on stderr.
    """

    def __init__(
        self,
        directory=None,
        *,
        service=SERVICE,
        instance_id=None,
        segment_bytes=SEGMENT_BYTES,
        directory_bytes=None,
        queue_limit=QUEUE_LIMIT,
        admit_timeout=ADMIT_TIMEOUT,
        stderr=None,
        clock=time.monotonic,
    ):
        self.service = service
        self.instance_id = instance_id or str(uuid.uuid4())
        self.state = NON_DURABLE
        self.error = None
        self.queue_limit = queue_limit
        self.admit_timeout = admit_timeout
        self.clock = clock
        self._stderr = sys.stderr if stderr is None else stderr
        self._warned = False
        self._sequence = 0
        self._synced = 0
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._queue = deque()
        self._stop = False
        self._sink = None
        self._thread = None
        if directory is not None:
            self._start(directory, segment_bytes, directory_bytes)

    # -- assembly ---------------------------------------------------------------------------

    def _start(self, directory, segment_bytes, directory_bytes):
        limit = DEFAULT_DIRECTORY_BYTES if directory_bytes is None else directory_bytes
        if not MIN_DIRECTORY_BYTES <= limit <= MAX_DIRECTORY_BYTES:
            self.state = UNAVAILABLE
            self.error = "log_directory_unusable"
            self._warn()
            return
        # A segment must comfortably hold more than the announcement of its own sealing. Below that
        # floor the announcement would seal the segment it was just written into, and each new
        # announcement would seal the next one: a roll that feeds itself instead of a bounded log.
        segment_bytes = max(int(segment_bytes), MAX_LINE_BYTES * 2)
        try:
            os.makedirs(directory, exist_ok=True)
            self._sink = _Sink(directory, self.instance_id, self.service, segment_bytes, int(limit))
        except OSError:
            # A missing, read-only or otherwise unusable directory is a deployment fault, not a
            # reason to fall back to a weaker mode. New work is refused until it is fixed.
            self._sink = None
            self.state = UNAVAILABLE
            self.error = "log_directory_unusable"
            self._warn()
            return
        self.state = DURABLE
        self._thread = threading.Thread(target=self._drain, name="tianshu-diagnostics", daemon=True)
        self._thread.start()

    def _warn(self):
        """One fixed, secret-free line per outage; repeated failures must not flood stderr."""
        if self._warned:
            return
        self._warned = True
        try:
            self._stderr.write(f"tianshu-diagnostics: log output unavailable ({self.error})\n")
            self._stderr.flush()
        except Exception:
            pass

    # -- callers ----------------------------------------------------------------------------

    def admit(self):
        """May new business start? False means answer 503 and let nothing else happen.

        Explicit development without a configured directory is allowed to keep working - it is
        an operator's deliberate choice, and `ready` stays red so a deployment gate cannot pass
        it. A real durability failure stops admission.
        """
        return self.state in (DURABLE, NON_DURABLE)

    def emit(
        self,
        event,
        level,
        outcome,
        *,
        correlation_id=None,
        duration_ms=None,
        error_code=None,
    ):
        """Accept one event for writing without waiting. Returns its sequence, or None.

        A refused event is never a reason to change the caller's business result: the return
        value exists so an admission path can react, not so a domain path can undo work.
        """
        try:
            _, sequence = self._encode(
                event, level, outcome, correlation_id, duration_ms, error_code
            )
        except RecordError:
            raise
        # A development sink has no persistent bytes to wait for, but the event is still accepted
        # and its sequence is still its identity.
        return sequence

    def emit_durable(self, *args, **kwargs):
        """Emit an admission event and wait, bounded, until its bytes are on the platter.

        The contract requires an accepting event to be flushed and fsynced *before* the new
        business side effect. Waiting has a hard ceiling: a stuck sink stops admission instead
        of blocking the event loop without limit.
        """
        sequence = self.emit(*args, **kwargs)
        if sequence is None:
            return None
        deadline = self.clock() + self.admit_timeout
        timed_out = False
        with self._condition:
            while self._synced < sequence:
                remaining = deadline - self.clock()
                if remaining <= 0:
                    timed_out = True
                    break
                self._condition.wait(remaining)
        if timed_out:
            # `_fail` takes the same condition this thread has just released: calling it while
            # holding the lock would deadlock the caller against its own writer.
            self._fail("log_flush_timeout")
            return None
        return sequence

    def retransmit(self, record):
        """Re-send a previously emitted record unchanged.

        A gap must stay reconcilable: a retransmission keeps the original `event_id` and
        `sequence` so a collector sees one event twice, not two events.
        """
        line = encode(record)
        with self._condition:
            if self.state not in (DURABLE, NON_DURABLE):
                return False
            if self.state == NON_DURABLE:
                return self._write_stderr_line(line)
            self._queue.append((line, int(record["sequence"])))
            self._condition.notify_all()
        return True

    def recover(self):
        """Explicit maintenance: prove the sink works again by really writing an event.

        Probes never call this. Recovery is only ever a successful persistent write, because a
        readable directory, a writable bit or a stat result is not evidence that bytes landed.
        The gap itself is then recorded, so the outage stays reconcilable in the log's own terms.
        """
        if self.state == DURABLE:
            return True
        if self._sink is None:
            return False
        with self._condition:
            pending = self.error
            self.error = None
            self._warned = False
            self.state = DURABLE
            self._condition.notify_all()
        # Held events are retried ahead of this one, so a sink that is still broken keeps the
        # state unavailable and this probe times out rather than claiming a false recovery.
        if self.emit_durable("logging.recovered", "WARNING", "succeeded") is None:
            return False
        if pending in ("log_capacity_exhausted", "log_directory_unusable"):
            self.emit("logging.capacity_exhausted", "ERROR", "degraded", error_code=pending)
        elif pending is not None:
            self.emit("logging.unavailable", "ERROR", "failed", error_code=pending)
        return True

    def flush(self, timeout=2.0):
        """Bounded drain: wait for the queue to empty, then fsync."""
        deadline = self.clock() + timeout
        with self._condition:
            while self._queue and self.clock() < deadline:
                self._condition.wait(deadline - self.clock())
        if self._sink is not None and self.state == DURABLE:
            try:
                self._sink.sync()
            except OSError:
                self._fail("log_write_failed")
                return False
        return True

    def close(self, timeout=2.0):
        """Stop the writer after a bounded flush; a stuck sink must not hang shutdown."""
        self.flush(timeout)
        with self._condition:
            self._stop = True
            self._condition.notify_all()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)
        if self._sink is not None:
            try:
                self._sink.seal()
            except OSError:
                self._sink.close()
            self._sink.close()

    def sealed_segments(self):
        return tuple(self._sink.sealed) if self._sink is not None else ()

    # -- internals --------------------------------------------------------------------------

    def _encode(self, event, level, outcome, correlation_id, duration_ms, error_code):
        with self._condition:
            if self.state == UNAVAILABLE:
                return None, None
            if len(self._queue) >= self.queue_limit:
                overflow = True
            else:
                overflow = False
                self._sequence += 1
                sequence = self._sequence
                record = build(
                    event,
                    level,
                    outcome,
                    sequence=sequence,
                    instance_id=self.instance_id,
                    correlation_id=correlation_id,
                    duration_ms=duration_ms,
                    error_code=error_code,
                    service=self.service,
                )
                line = encode(record)
                if self.state == NON_DURABLE:
                    if not self._write_stderr_line(line):
                        return None, None
                    self._synced = sequence
                    self._condition.notify_all()
                    return None, sequence
                self._queue.append((line, sequence))
                self._condition.notify_all()
                return line, sequence
        # Outside the lock: `_fail` takes the same condition this thread has just released, and
        # calling it while holding the lock would deadlock the emitter against itself.
        if overflow:
            self._fail("log_queue_full")
        return None, None

    def _write_stderr_line(self, line):
        """Development mode writes the same JSONL record to stderr, and nothing more."""
        try:
            buffer = getattr(self._stderr, "buffer", None)
            if buffer is not None:
                buffer.write(line)
                buffer.flush()
            else:
                self._stderr.write(line.decode("utf-8"))
                self._stderr.flush()
        except Exception:
            return False
        return True

    def _fail(self, code):
        with self._condition:
            if self.state == UNAVAILABLE:
                return
            self.state = UNAVAILABLE
            self.error = code
            self._condition.notify_all()
        self._warn()

    def _drain(self):
        while True:
            with self._condition:
                while not self._queue and not self._stop:
                    self._condition.wait(0.2)
                if self._stop:
                    return
                batch = list(self._queue)
                self._queue.clear()
            sealed = len(self._sink.sealed)
            written = 0
            last = 0
            failed = False
            for line, sequence in batch:
                try:
                    if self._sink.write(line) is False:
                        failed = True
                        break
                except OSError:
                    failed = True
                    break
                written += 1
                last = sequence
            if not failed:
                try:
                    self._sink.sync()
                except OSError:
                    failed = True
            if failed:
                # Nothing accepted is dropped: what could not be written goes back to the front of
                # the queue in its original order, and admission stops instead. The failure is
                # never logged recursively, and this is not an unbounded error loop - the writer
                # parks until an explicit recovery or shutdown wakes it.
                with self._condition:
                    self._queue.extendleft(reversed(batch[written:]))
                    self._condition.notify_all()
                self._fail(self._sink.error or "log_write_failed")
                with self._condition:
                    while self.state == UNAVAILABLE and not self._stop:
                        self._condition.wait(0.2)
                continue
            if last:
                # The promise an admitting call waits on is `fsync`, so the acknowledgement is
                # published after the sync and not merely after the write syscall returned.
                with self._condition:
                    self._synced = max(self._synced, last)
                    self._condition.notify_all()
            if len(self._sink.sealed) > sealed:
                # A rolled segment is a durable fact about the log's own shape, recorded after the
                # seal it describes and in place of no event that filled it.
                self.emit("logging.segment_sealed", "INFO", "succeeded")


# The process-wide instance. Modules reach it through the functions below, which are inert until
# the entry point assembles one, so importing a business module never writes anything by itself.
_ACTIVE = None
_ACTIVE_LOCK = threading.Lock()


def activate(diagnostics):
    """Install the process-wide sink. Called only by the entry point."""
    global _ACTIVE
    with _ACTIVE_LOCK:
        _ACTIVE = diagnostics
    return diagnostics


def active():
    return _ACTIVE


def reset():
    """Drop the process-wide sink (used by isolated tests)."""
    global _ACTIVE
    with _ACTIVE_LOCK:
        _ACTIVE = None


def admittable():
    return _ACTIVE is None or _ACTIVE.admit()


def safe_code(value):
    """A registered error code, or the one fixed code for anything this product does not own.

    A vendor string, a status text or an exception class name is never written: it collapses to
    `internal_error`, which is what keeps third-party text out of the log entirely.
    """
    return value if value in ERROR_CODES else "internal_error"


def event(event_name, level, outcome, *, correlation_id=None, duration_ms=None, error_code=None):
    """Emit through the process-wide sink; a missing sink is simply no output."""
    sink = _ACTIVE
    if sink is None:
        return None
    try:
        return sink.emit(
            event_name,
            level,
            outcome,
            correlation_id=correlation_id,
            duration_ms=duration_ms,
            error_code=error_code,
        )
    except RecordError:
        # A programming error must not become a request failure. The registered vocabulary is
        # asserted by the coverage tests instead.
        return None


def accept(correlation_id=None):
    """Admit one new request, making its accepting event durable before anything else happens.

    Returns False when the work must not start: the sink is unavailable, or the accepting event
    could not be made durable inside its bound. An already-running request is never affected by a
    later failure, which is why this gate sits strictly in front of new work.
    """
    sink = _ACTIVE
    if sink is None:
        return True
    if not sink.admit():
        return False
    return (
        sink.emit_durable("http.request.started", "INFO", "started", correlation_id=correlation_id)
        is not None
    )


def classify(status):
    """The registered level, outcome and error code of one finished HTTP exchange.

    One table for the whole product: a response cannot invent a level or leak a raw reason, and a
    business status the platform does not own still collapses to the same honest code.
    """
    if status == 401:
        return "WARNING", "rejected", "unauthorized"
    if status == 403:
        return "WARNING", "rejected", "forbidden"
    if status == 404:
        return "WARNING", "rejected", "not_found"
    if status == 408:
        return "WARNING", "failed", "timeout"
    if status == 409:
        return "WARNING", "rejected", "version_conflict"
    if status == 413:
        return "WARNING", "rejected", "budget_exceeded"
    if status == 429:
        return "WARNING", "rejected", "too_many_requests"
    if 400 <= status < 500:
        return "WARNING", "rejected", "invalid_input"
    if status >= 500:
        return "ERROR", "failed", "dependency_unavailable"
    return "INFO", "succeeded", None


def finish(status, duration_ms, correlation_id=None, known_path=True, error_code=None):
    """Record one request's terminal state and the authorisation result it demonstrates.

    A 401 or 403 is authorisation refusing. Any other answered request got past authorisation to
    produce a business result, including a 5xx. An unknown path or method answers with the opaque
    404 before authorisation is ever consulted, so it is recorded as such and no claim is made
    either way rather than inventing an authentication result that never happened.

    `error_code` overrides the status-derived code: that is how an unexpected internal exception
    is recorded as the fixed `internal_error` while the wire answer keeps a code the published
    error contract actually allows.
    """
    level, outcome, code = classify(status)
    event(
        "http.request.finished",
        level,
        outcome,
        correlation_id=correlation_id,
        duration_ms=duration_ms,
        error_code=error_code or code,
    )
    if status in (401, 403):
        event(
            "http.auth.rejected",
            "WARNING",
            "rejected",
            correlation_id=correlation_id,
            error_code=code,
        )
    elif status != 404 or known_path:
        event("http.auth.succeeded", "INFO", "succeeded", correlation_id=correlation_id)
    if status == 404:
        event(
            "http.request.unknown_path",
            "WARNING",
            "rejected",
            correlation_id=correlation_id,
            error_code="not_found",
        )


OUTBOUND_KINDS = ("queued", "started", "succeeded", "failed", "timed_out", "cancelled")
_OUTBOUND = {
    "queued": ("DEBUG", "started"),
    "started": ("INFO", "started"),
    "succeeded": ("INFO", "succeeded"),
    "failed": ("WARNING", "failed"),
    "timed_out": ("WARNING", "failed"),
    "cancelled": ("INFO", "cancelled"),
}


def outbound(kind, *, correlation_id=None, duration_ms=None, error_code=None):
    """Emit one outbound-call event. `kind` picks a registered name, level and outcome.

    Callers cannot supply an event name or a level, so an outbound call can only ever be reported
    through the handful of states the contract registered for it.
    """
    if kind not in _OUTBOUND:
        return None
    level, outcome = _OUTBOUND[kind]
    return event(
        "outbound.call." + kind,
        level,
        outcome,
        correlation_id=correlation_id if correlation_id is not None else current_correlation(),
        duration_ms=duration_ms,
        error_code=error_code,
    )


def correlation_header():
    """The one header an existing outbound call carries: this task's id, when it already has one.

    Returns an empty mapping otherwise, so a call that belongs to no request is not given a
    fabricated correlation and no business header, body or authorisation is touched.
    """
    value = current_correlation()
    return {HEADER: value} if value else {}


class Span:
    """One timed operation: the monotonic duration of an event pair, never a wall-clock delta."""

    __slots__ = ("event", "started", "correlation_id")

    def __init__(self, event_name, correlation_id=None, clock=time.monotonic):
        self.event = event_name
        self.started = clock()
        self.correlation_id = (
            correlation_id if correlation_id is not None else current_correlation()
        )

    def elapsed(self, clock=time.monotonic):
        return max(0.0, clock() - self.started)

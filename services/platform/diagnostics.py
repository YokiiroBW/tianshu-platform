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

import asyncio
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
        # A record this product asked for but the frozen contract does not allow. Registered so the
        # refusal has a name of its own instead of being reported as something it is not.
        "log_record_invalid",
        # The deployment's configuration could not be loaded. Registered for the same reason: an
        # unregistered code is a record that cannot be written at all.
        "config_load_failed",
        # A cancelled outbound call that provably never left this process. Registered so that
        # "cancelled, nothing was sent" and "cancelled, delivery unknown" stay distinguishable
        # without adding a field to a frozen record.
        "outbound_not_sent",
    }
)

MAX_LINE_BYTES = 4096
SEGMENT_BYTES = 64 * 1024 * 1024
DEFAULT_DIRECTORY_BYTES = 1024 * 1024 * 1024
MIN_DIRECTORY_BYTES = 32 * 1024 * 1024
MAX_DIRECTORY_BYTES = 64 * 1024 * 1024 * 1024
QUEUE_LIMIT = 8192
# The bound on waiting for an accepting event's bytes, and the bound on waiting for a terminal
# event's. Both are ceilings on a caller's wait, never a promise about the disk.
ADMIT_TIMEOUT = 0.25
TERMINAL_TIMEOUT = 0.25

# durable | recovering | non_durable | unavailable. Only `durable` may back a production `ready`.
DURABLE = "durable"
NON_DURABLE = "non_durable"
UNAVAILABLE = "unavailable"
# A sink proving that it works again. Admission stays refused for the whole of it: a recovery is
# only real once a write of its own has actually been fsynced, so `recovering` is not `durable`
# and never answers readiness.
RECOVERING = "recovering"

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


class _Waiter:
    """One bounded wait for a sequence to become durable, usable from a thread or from a loop.

    The waiting is done by whoever is waiting, never by the writer: the drain thread only ever
    *resolves* a waiter, so a slow disk can never make the single writer block on a caller. A
    waiter registered from a running loop is resolved through `call_soon_threadsafe`, which is what
    keeps an admission on the event loop from freezing that loop while it waits for fsync.

    Being woken and being confirmed are two different facts, and the difference is the whole point:
    a sink that fails releases its waiters immediately so nobody burns a bound on a sink that is
    already known to be dead, and those waiters come back with `confirmed = False`. Without that
    distinction a write failure would look exactly like a successful fsync.
    """

    __slots__ = ("sequence", "thread_event", "loop", "async_event", "confirmed", "resolved")

    def __init__(self, sequence, loop=None):
        self.sequence = sequence
        self.thread_event = threading.Event()
        self.loop = loop
        self.async_event = asyncio.Event() if loop is not None else None
        self.confirmed = False
        self.resolved = False

    def resolve(self, confirmed):
        if self.resolved:
            return
        self.resolved = True
        self.confirmed = confirmed
        self.thread_event.set()
        if self.loop is not None and self.async_event is not None:
            try:
                self.loop.call_soon_threadsafe(self.async_event.set)
            except RuntimeError:
                # The loop is already gone; the thread-side event still carries the answer to any
                # synchronous caller that is holding this waiter.
                pass


class Diagnostics:
    """The process-wide event sink. Assembled by the entry point, never by a domain module.

    Callers only ever hand it registered names and bounded scalars, so the adapter needs no
    knowledge of the business to be safe. Everything that can fail about durability is turned
    into one in-memory state plus, at most, one fixed line on stderr.

    One rule holds the whole thing together: *the drain thread is the only owner of the file*.
    Open, write, fsync, seal and close all happen on that single thread, and no caller ever touches
    the sink object. That is what makes a bounded `close` safe: a caller that runs out of patience
    reports that it could not confirm durability and returns, while the owner finishes its own
    work in its own time - it is never pre-empted, and its file handle is never stolen from under
    it by a second thread sealing the same descriptor.
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
        terminal_timeout=TERMINAL_TIMEOUT,
        stderr=None,
        clock=time.monotonic,
    ):
        self.service = service
        self.instance_id = instance_id or str(uuid.uuid4())
        self.state = NON_DURABLE
        self.error = None
        self.queue_limit = queue_limit
        self.admit_timeout = admit_timeout
        self.terminal_timeout = terminal_timeout
        self.clock = clock
        self._stderr = sys.stderr if stderr is None else stderr
        self._warned = False
        self._sequence = 0
        self._synced = 0
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._queue = deque()
        self._stop_requested = False
        self._closed = False
        self._owner_done = threading.Event()
        self._final_ok = None
        self._waiters = []
        # Every bounded confirmation that ran out of time, counted so "we could not confirm this
        # one" is a fact about the process rather than a line someone has to go looking for.
        self._unconfirmed = 0
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
        it. A real durability failure stops admission, and a sink that is shutting down refuses
        new work from the instant `close` was asked for rather than until it finishes.
        """
        if self._closed:
            return False
        return self.state in (DURABLE, NON_DURABLE)

    @property
    def unconfirmed(self):
        """How many bounded confirmations this process could not complete."""
        return self._unconfirmed

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
        value exists so an admission path can react, not so a domain path can undo work. A record
        the contract does not allow is a different matter and is never swallowed: the sink stops
        admitting new work with one fixed code and the error reaches the caller, because a sink
        that silently drops events cannot also claim that registration is complete.
        """
        try:
            _, sequence = self._encode(
                event, level, outcome, correlation_id, duration_ms, error_code
            )
        except RecordError:
            self._fail("log_record_invalid")
            raise
        # A development sink has no persistent bytes to wait for, but the event is still accepted
        # and its sequence is still its identity.
        return sequence

    def admit_durable(self, *args, loop=None, timeout=None, **kwargs):
        """Emit an event and wait, bounded, until its bytes are on the platter.

        Returns the sequence when durability is confirmed and `None` when it is not. `loop` selects
        how the waiting happens: passing the running loop makes this awaitable through
        `wait_confirmation` and never blocks that loop, while a plain thread waits on its own
        event. Nothing here writes to the file - the single owner thread does that - so a slow
        disk costs the caller its bound and nothing else.
        """
        sequence = self.emit(*args, **kwargs)
        if sequence is None:
            return None
        waiter = self._register(sequence, loop)
        if waiter is None:
            # Already durable before anyone had to wait: no registration, no waiting, no cost.
            return sequence
        bound = self.admit_timeout if timeout is None else timeout
        if loop is None:
            woken = waiter.thread_event.wait(max(0.0, bound))
        else:
            woken = self._wait_async(waiter, bound)
        if not woken or not waiter.confirmed:
            self._abandon(waiter)
            self._unconfirmed += 1
            # A stuck sink stops admission instead of blocking a caller without limit. The event
            # itself stays queued: what could not be confirmed is never dropped and never resent.
            self._fail("log_flush_timeout")
            return None
        return sequence

    def emit_durable(self, *args, **kwargs):
        """`admit_durable` for a caller that is not on an event loop."""
        return self.admit_durable(*args, **kwargs)

    async def admit_durable_async(self, *args, timeout=None, **kwargs):
        """`admit_durable` for a caller on the event loop: the wait never blocks the loop."""
        loop = asyncio.get_running_loop()
        sequence = self.emit(*args, **kwargs)
        if sequence is None:
            return None
        waiter = self._register(sequence, loop)
        if waiter is None:
            return sequence
        bound = self.admit_timeout if timeout is None else timeout
        try:
            await asyncio.wait_for(waiter.async_event.wait(), max(0.0, bound))
        except (TimeoutError, asyncio.TimeoutError):
            self._abandon(waiter)
            self._unconfirmed += 1
            self._fail("log_flush_timeout")
            return None
        if not waiter.confirmed:
            # Released because the sink failed, not because the bytes landed.
            self._unconfirmed += 1
            self._fail("log_flush_timeout")
            return None
        return sequence

    def confirm(self, sequence, *, timeout=None, loop=None):
        """Wait, bounded, for an already-accepted event to become durable. True means confirmed."""
        if sequence is None:
            return False
        waiter = self._register(sequence, loop)
        if waiter is None:
            return True
        bound = self.terminal_timeout if timeout is None else timeout
        woken = waiter.thread_event.wait(max(0.0, bound))
        if not woken or not waiter.confirmed:
            self._abandon(waiter)
            self._unconfirmed += 1
            self._fail("log_flush_timeout")
            return False
        return True

    async def confirm_async(self, sequence, *, timeout=None):
        """`confirm` for a caller on the event loop; the wait yields instead of blocking."""
        if sequence is None:
            return False
        loop = asyncio.get_running_loop()
        waiter = self._register(sequence, loop)
        if waiter is None:
            return True
        bound = self.terminal_timeout if timeout is None else timeout
        try:
            await asyncio.wait_for(waiter.async_event.wait(), max(0.0, bound))
        except (TimeoutError, asyncio.TimeoutError):
            self._abandon(waiter)
            self._unconfirmed += 1
            self._fail("log_flush_timeout")
            return False
        if not waiter.confirmed:
            self._unconfirmed += 1
            self._fail("log_flush_timeout")
            return False
        return True

    def retransmit(self, record):
        """Re-send a previously emitted record unchanged.

        A gap must stay reconcilable: a retransmission keeps the original `event_id` and
        `sequence` so a collector sees one event twice, not two events.

        It is otherwise an ordinary event. It obeys the same capacity limit, it must belong to
        *this* instance and service - a record from another process is not ours to re-send - and it
        never advances the durable watermark, because its sequence was allocated by an earlier
        stream and a high number in it says nothing about the events still waiting here.
        """
        line = encode(record)
        if (
            record["instance_id"] != self.instance_id
            or record["service"] != self.service
            or type(record["sequence"]) is not int
            or not 1 <= record["sequence"] <= self._sequence
        ):
            return False
        with self._condition:
            if self._closed or self.state not in (DURABLE, NON_DURABLE):
                return False
            if self.state == NON_DURABLE:
                return self._write_stderr_line(line)
            if len(self._queue) >= self.queue_limit:
                overflow = True
            else:
                overflow = False
                self._queue.append(("line", line, int(record["sequence"]), False))
                self._condition.notify_all()
        if overflow:
            self._fail("log_queue_full")
            return False
        return True

    def recover(self):
        """Explicit maintenance: prove the sink works again by really writing an event.

        Probes never call this. Recovery is only ever a successful persistent write, because a
        readable directory, a writable bit or a stat result is not evidence that bytes landed.
        The sink therefore stays out of `durable` - and admission stays refused - for the whole of
        the attempt: `recovering` is a state that only a confirmed fsync can leave.
        """
        if self.state == DURABLE:
            return True
        if self._sink is None or self._closed:
            return False
        with self._condition:
            if self.state == UNAVAILABLE:
                pending = self.error
                self.state = RECOVERING
                self.error = None
                self._condition.notify_all()
            elif self.state == RECOVERING:
                pending = self.error
            else:
                return False
        # Held events are retried ahead of this one, so a sink that is still broken keeps the
        # state out of `durable` and this probe runs out of its bound rather than claiming a false
        # recovery.
        if self.admit_durable("logging.recovered", "WARNING", "succeeded") is None:
            with self._condition:
                self.state = UNAVAILABLE
                self.error = self.error or "log_write_failed"
                self._condition.notify_all()
            return False
        with self._condition:
            self.state = DURABLE
            self.error = None
            self._warned = False
            self._condition.notify_all()
        # The gap itself is then recorded, so the outage stays reconcilable in the log's own terms.
        if pending in ("log_capacity_exhausted", "log_directory_unusable"):
            self.emit("logging.capacity_exhausted", "ERROR", "degraded", error_code=pending)
        elif pending is not None:
            self.emit("logging.unavailable", "ERROR", "failed", error_code=pending)
        return True

    def flush(self, timeout=2.0):
        """Bounded drain: ask the owner to reach the current sequence, and wait for its answer.

        The fsync is done by the owner thread and never by the caller, so a flush can never race
        the writer for the descriptor. `False` means the wait expired or the sink is unavailable:
        durability was not confirmed, which is a different statement from "the events were lost".
        """
        deadline = self.clock() + timeout
        if self._sink is None:
            return True
        if self._owner_done.is_set():
            return bool(self._final_ok)
        with self._condition:
            if self.state == UNAVAILABLE or self.state == RECOVERING:
                return False
            if self.state == NON_DURABLE:
                return True
            barrier = threading.Event()
            self._queue.append(("barrier", self._sequence, barrier))
            self._condition.notify_all()
        return barrier.wait(max(0.0, deadline - self.clock()))

    def close(self, timeout=2.0):
        """Stop the writer after a bounded wait; a stuck sink must not hang shutdown.

        One monotonic deadline covers the whole operation, and the caller never seals or closes
        the file itself: it asks the owner to stop, waits as long as it was given, and reports
        whether the owner confirmed. When the bound expires the owner is left alone to finish - it
        keeps whatever it already accepted, seals its own segment and closes its own descriptor -
        so nothing is stolen, nothing accepted is dropped and no shutdown waits forever.
        """
        deadline = self.clock() + timeout
        with self._condition:
            # From this instant the sink refuses new work, whatever the writer is still doing.
            self._closed = True
            self._stop_requested = True
            if self._sink is not None and self._thread is not None:
                self._queue.append(("stop",))
            self._condition.notify_all()
        if self._sink is None or self._thread is None:
            self._owner_done.set()
            return True
        confirmed = self._owner_done.wait(max(0.0, deadline - self.clock()))
        if not confirmed:
            self._unconfirmed += 1
        return confirmed

    def sealed_segments(self):
        return tuple(self._sink.sealed) if self._sink is not None else ()

    # -- internals --------------------------------------------------------------------------

    def _register(self, sequence, loop):
        """Register one bounded wait, or return None when it is already satisfied."""
        with self._condition:
            if self._synced >= sequence:
                return None
            waiter = _Waiter(sequence, loop)
            self._waiters.append(waiter)
            return waiter

    def _abandon(self, waiter):
        """Stop waiting for one sequence. The event itself is untouched and stays queued."""
        with self._condition:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    def _wait_async(self, waiter, bound):
        """Wait for a waiter from a thread that is not the owner and not on a loop.

        Only used by the synchronous `admit_durable` path, which runs on a plain thread; the async
        paths await their own event instead so the loop is never blocked.
        """
        return waiter.thread_event.wait(max(0.0, bound))

    def _publish_synced(self, sequence):
        """Publish a durable watermark and release every wait it satisfies.

        The watermark only ever moves to a sequence the owner has written *and* fsynced, so a
        caller that is released here has a real promise behind it rather than a write syscall.
        """
        with self._condition:
            if sequence <= self._synced:
                return
            self._synced = sequence
            ready = [waiter for waiter in self._waiters if waiter.sequence <= sequence]
            if ready:
                self._waiters = [waiter for waiter in self._waiters if waiter.sequence > sequence]
        for waiter in ready:
            waiter.resolve(True)

    def _release_all(self):
        """Release every waiter: the sink can no longer promise them anything.

        Called when the sink fails or shuts down, so a bounded wait answers immediately with an
        explicit "not confirmed" instead of burning its whole bound on a sink that is already
        known to be unavailable. The release is *unconfirmed* on purpose: being woken early must
        never be readable as "the bytes landed".
        """
        with self._condition:
            waiting, self._waiters = self._waiters, []
        for waiter in waiting:
            waiter.resolve(False)

    def _encode(self, event, level, outcome, correlation_id, duration_ms, error_code):
        """Assemble, validate and queue one record. Returns `(line, sequence)`, or `(None, None)`.

        The sequence is committed only after the record has been built *and* encoded, so a caller
        that asks for something the contract does not allow cannot punch a hole in the numbering:
        the refusal happens before anything is allocated, which is what keeps a gap in the log
        meaningful instead of routine.
        """
        overflow = False
        unwritable = False
        with self._condition:
            if self.state == UNAVAILABLE or self._closed:
                return None, None
            if len(self._queue) >= self.queue_limit:
                overflow = True
            else:
                sequence = self._sequence + 1
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
                    # Development mode has no queue to protect, so the line is written before the
                    # sequence is committed and a stderr that cannot be written leaves no gap.
                    if self._write_stderr_line(line):
                        self._sequence = sequence
                        self._synced = sequence
                        self._condition.notify_all()
                        return None, sequence
                    unwritable = True
                else:
                    self._sequence = sequence
                    self._queue.append(("line", line, sequence, True))
                    self._condition.notify_all()
                    return line, sequence
        # Outside the lock: `_fail` takes the same condition this thread has just released, and
        # calling it while holding the lock would deadlock the emitter against itself.
        if overflow:
            self._fail("log_queue_full")
        elif unwritable:
            self._fail("log_write_failed")
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
        self._release_all()

    def _sync_sink(self):
        """The owner's own fsync. True means the bytes are on the platter."""
        try:
            self._sink.sync()
        except OSError:
            return False
        return True

    def _owner_seal(self):
        """Seal and close the segment - on the owner thread, and only there.

        Runs in the drain thread's `finally`, so the descriptor is released exactly once, by the
        thread that opened it, whether the loop ended normally, on a failure, or on a stop
        request that arrived while the sink was parked.
        """
        if self._sink is None:
            return
        try:
            self._sink.seal()
            self._final_ok = True
        except OSError:
            self._final_ok = False
            try:
                self._sink.close()
            except OSError:
                pass
        finally:
            try:
                self._sink.close()
            except OSError:
                pass

    def _park(self):
        """Wait after a failure. Returns True when the owner must stop instead of retrying.

        An unavailable sink is not retried in a loop: the owner waits for an explicit recovery, or
        for a stop - which it must honour, because a shutdown that can never finish is worse than a
        log that could not be written. Without this the stop request would be re-queued with the
        unwritten events and retried forever, burning a core on a device that is not answering.
        """
        with self._condition:
            while self.state == UNAVAILABLE and not self._stop_requested:
                self._condition.wait(0.2)
        return self._stop_requested

    def _drain(self):
        """The single owner of the file: it writes, fsyncs, seals and closes, and nothing else does.

        The loop is deliberately simple, and every path out of it passes through the `finally`
        that seals the segment. A failure puts the unwritten remainder back at the front of the
        queue - nothing accepted is ever dropped - and parks the owner until an explicit recovery
        or a shutdown, rather than spinning on a disk that is not going to answer.
        """
        try:
            while True:
                with self._condition:
                    while not self._queue and not self._stop_requested:
                        self._condition.wait(0.2)
                    if self._stop_requested and not self._queue:
                        return
                    batch = list(self._queue)
                    self._queue.clear()
                sealed = len(self._sink.sealed)
                last = 0
                failed = False
                stopping = False
                for index, item in enumerate(batch):
                    kind = item[0]
                    if kind == "line":
                        _, line, sequence, advances = item
                        try:
                            if self._sink.write(line) is False:
                                failed = True
                                break
                        except OSError:
                            failed = True
                            break
                        if advances:
                            # A retransmission carries a sequence from an earlier stream, so it
                            # must not be allowed to confirm the events still waiting here.
                            last = sequence
                    elif kind == "barrier":
                        # A flush is only true after a real fsync, and the fsync is the owner's.
                        if not self._sync_sink():
                            failed = True
                            break
                        self._publish_synced(item[1])
                        item[2].set()
                    else:
                        stopping = True
                if failed:
                    with self._condition:
                        self._queue.extendleft(reversed(batch[index:]))
                        self._condition.notify_all()
                    self._fail(self._sink.error or "log_write_failed")
                    if self._park():
                        return
                    continue
                if not self._sync_sink():
                    with self._condition:
                        self._queue.extendleft(reversed(batch[index + 1 :]))
                        self._condition.notify_all()
                    self._fail("log_write_failed")
                    if self._park():
                        return
                    continue
                if last:
                    # The promise an admitting call waits on is `fsync`, so the acknowledgement is
                    # published after the sync and not merely after the write syscall returned.
                    self._publish_synced(last)
                if len(self._sink.sealed) > sealed:
                    # A rolled segment is a durable fact about the log's own shape, recorded after
                    # the seal it describes and in place of no event that filled it.
                    self.emit("logging.segment_sealed", "INFO", "succeeded")
                if stopping:
                    return
        finally:
            self._owner_seal()
            self._owner_done.set()
            self._release_all()


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
    return sink.emit(
        event_name,
        level,
        outcome,
        correlation_id=correlation_id,
        duration_ms=duration_ms,
        error_code=error_code,
    )


def admitted():
    """Whether new business may start. A sink that is shutting down or broken says no."""
    sink = _ACTIVE
    return True if sink is None else sink.admit()


def accept(correlation_id=None):
    """Admit one new request, making its accepting event durable before anything else happens.

    Returns the confirming sequence, or None when the work must not start: the sink is
    unavailable, or the accepting event could not be made durable inside its bound. An
    already-running request is never affected by a later failure, which is why this gate sits
    strictly in front of new work.

    This is the thread-safe form. A caller on the event loop must use `accept_async`, which waits
    without blocking that loop.
    """
    sink = _ACTIVE
    if sink is None:
        return 0
    if not sink.admit():
        return None
    return sink.admit_durable(
        "http.request.started", "INFO", "started", correlation_id=correlation_id
    )


async def accept_async(correlation_id=None):
    """`accept` for a caller on the event loop: the durable wait yields instead of blocking."""
    sink = _ACTIVE
    if sink is None:
        return 0
    if not sink.admit():
        return None
    return await sink.admit_durable_async(
        "http.request.started", "INFO", "started", correlation_id=correlation_id
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


# What one answered request demonstrates about authorisation. Only `succeeded` and `rejected` are
# claims about authentication; the other two are the honest absence of one.
AUTH_SUCCEEDED = "succeeded"
AUTH_REJECTED = "rejected"
# The request never reached the code that authenticates: a rejected pre-auth body, an unknown path
# answered before dispatch, or a route that requires no identity at all.
AUTH_NOT_ATTEMPTED = "not_attempted"


def _terminal_events(status, correlation_id, error_code, known_path, auth):
    """The registered terminal events of one request, in the order they are written."""
    level, outcome, code = classify(status)
    events = [
        (
            "http.request.finished",
            level,
            outcome,
            error_code or code,
        )
    ]
    if auth == AUTH_REJECTED or (auth is None and status in (401, 403)):
        events.append(("http.auth.rejected", "WARNING", "rejected", code))
    elif auth == AUTH_SUCCEEDED:
        events.append(("http.auth.succeeded", "INFO", "succeeded", None))
    elif auth is None and (status != 404 or known_path):
        # Legacy callers that pass no disposition keep the old, weaker inference.
        events.append(("http.auth.succeeded", "INFO", "succeeded", None))
    if status == 404:
        events.append(("http.request.unknown_path", "WARNING", "rejected", "not_found"))
    return events


def finish(
    status,
    duration_ms,
    correlation_id=None,
    known_path=True,
    error_code=None,
    auth=None,
):
    """Record one request's terminal state and the authorisation result it demonstrates.

    `auth` is the disposition the boundary actually observed, and it is not inferred from the
    status alone: a 404 answered before dispatch, a 400 refused before authentication and a public
    static asset are all *not* evidence that anybody authenticated, so none of them may be written
    as `http.auth.succeeded`. A 401 or 403 is authorisation refusing, whatever else is true.

    `error_code` overrides the status-derived code: that is how an unexpected internal exception
    is recorded as the fixed `internal_error` while the wire answer keeps a code the published
    error contract actually allows.

    Returns True when every terminal event was confirmed durable inside the terminal bound, and
    False when it was not. The business result the caller already produced is never changed,
    retried or rolled back by this answer: it only says whether the log could confirm the record.
    """
    sequences = []
    for name, level, outcome, code in _terminal_events(
        status, correlation_id, error_code, known_path, auth
    ):
        sequences.append(
            event(
                name,
                level,
                outcome,
                correlation_id=correlation_id,
                duration_ms=duration_ms if name == "http.request.finished" else None,
                error_code=code,
            )
        )
    return _confirm_last(sequences)


async def finish_async(
    status,
    duration_ms,
    correlation_id=None,
    known_path=True,
    error_code=None,
    auth=None,
):
    """`finish` for a caller on the event loop, with the same bounded durable confirmation."""
    sequences = []
    for name, level, outcome, code in _terminal_events(
        status, correlation_id, error_code, known_path, auth
    ):
        sequences.append(
            event(
                name,
                level,
                outcome,
                correlation_id=correlation_id,
                duration_ms=duration_ms if name == "http.request.finished" else None,
                error_code=code,
            )
        )
    sink = _ACTIVE
    if sink is None:
        return True
    sequence = max([value for value in sequences if value is not None], default=None)
    return await sink.confirm_async(sequence)


def _confirm_last(sequences):
    """Wait, bounded, for the last of a group of events to become durable."""
    sink = _ACTIVE
    if sink is None:
        return True
    sequence = max([value for value in sequences if value is not None], default=None)
    return sink.confirm(sequence)


def confirm(sequence, timeout=None):
    """Wait, bounded, for one already-accepted event. The thread-safe form."""
    sink = _ACTIVE
    return True if sink is None else sink.confirm(sequence, timeout=timeout)


async def confirm_async(sequence, timeout=None):
    """Wait, bounded, for one already-accepted event, without blocking the loop."""
    sink = _ACTIVE
    return True if sink is None else await sink.confirm_async(sequence, timeout=timeout)


def cli_admit(action):
    """Admit one local CLI action before it does anything, exactly as a request is admitted.

    Returns True when the action may run. A refused action has not started, so nothing has to be
    undone: the caller reports the fixed code and stops.
    """
    sink = _ACTIVE
    if sink is None:
        return True
    if not sink.admit():
        return False
    return (
        sink.admit_durable(
            "cli.action.started", "INFO", "started", correlation_id=current_correlation()
        )
        is not None
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

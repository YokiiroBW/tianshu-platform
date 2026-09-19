"""Bounded IPC to one dedicated regex process, with hard termination and real reaping.

Every budget in this module is a *hard* budget. When one expires the child is **killed immediately** —
there is no polite wait before the kill — and the cleanup budget is then spent on the only thing that
still matters: waiting for the real exit code. ``re`` cannot be interrupted from inside, so killing the
process is the only thing that actually stops a catastrophic backtracking match; an ``asyncio.wait_for``
around a future merely stops waiting for it while the match keeps burning a core.

Ownership is the other half of the design. A child can come into existence on *every* await boundary of
``start`` — during ``create_subprocess_exec``, while the readiness line is read, or between the two — so

* creation and readiness run in a task the worker owns, not in the caller's task, and every exit from
  that task ends with the child either adopted by a live worker, killed because this worker was closed
  or cancelled, or given up on together with a real kill;
* ``aclose`` is a barrier: it marks the worker closed *before* it awaits anything, so a caller that
  returns from ``aclose`` can never observe a live child of this worker again;
* nothing outside this worker's own child is ever touched — no global taskkill and no other worker's
  process.

The published limits are enforced before an unbounded allocation happens:

* a request line may not exceed 1 MiB (measured before it is written);
* a response line may not exceed 16 KiB, enforced as the stream reader's high-water mark so an
  oversized response is refused by the transport instead of being accumulated;
* an over-limit response, a malformed line or a response shape violation is a fatal protocol
  violation: the worker is killed and reaped, and never reused.

Both ends parse JSON with duplicate keys refused: a repeated ``seq`` would otherwise let a worker
present two different facts under one key and have the last one win silently.

The protocol is checked strictly: ``seq`` and ``handle`` must be real integers (``true`` and ``1.0``
are refused), ``protocol`` must be the integer ``1`` rather than a truthy stand-in, the handshake
``pid`` must be the pid of *this* worker's own child, the response must carry exactly the fields of its
response kind, and a stale or unknown ``seq`` is refused. No message ever contains the expression, the
matched text or a local path.

Call budgets are read from this module at call time, so a caller that changes a published budget
changes what is actually enforced.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from typing import Any

from .types import (
    ERROR_INVALID_REGEX,
    REASON_EVALUATION_TIMEOUT,
    REASON_INPUT_TOO_LARGE,
    REASON_REGEX_TIMEOUT,
    REASON_REGEX_WORKER_FAILED,
    RuleEvaluationError,
)

STARTUP_TIMEOUT_SECONDS = 3.0
CALL_TIMEOUT_SECONDS = 0.05
REQUEST_TIMEOUT_SECONDS = 5.0
CLEANUP_TIMEOUT_SECONDS = 1.0

#: How long a *deliberate* close waits for the child to honour its closed input before killing it.
#: Not a published budget of its own: it is part of the cleanup budget, and it exists so a clean exit
#: is recorded as one instead of every close ending in ``-9``. A hard failure never waits this long —
#: it kills first, because a backtracking match does not react to a closed pipe at all.
_GRACE_SECONDS = 0.2

IPC_MAX_REQUEST_BYTES = 1024 * 1024
IPC_MAX_RESPONSE_BYTES = 16 * 1024

WORKER_SCRIPT_NAME = "regex_worker.py"

#: The frozen response shapes. A response must carry exactly the fields of its kind, so an unexpected
#: field is a protocol violation rather than something the caller has to tolerate.
_READY_FIELDS = frozenset({"ready", "protocol", "pid"})
_COMPILE_OK_FIELDS = frozenset({"seq", "ok", "handle"})
_SEARCH_OK_FIELDS = frozenset({"seq", "ok", "matched"})
_FORGET_OK_FIELDS = frozenset({"seq", "ok"})
_FAILURE_FIELDS = frozenset({"seq", "ok", "code"})
_RESPONSE_FIELDS = (
    _READY_FIELDS | _COMPILE_OK_FIELDS | _SEARCH_OK_FIELDS | _FORGET_OK_FIELDS | _FAILURE_FIELDS
)


def worker_script_path() -> str:
    """The absolute path of the bundled worker script.

    The path is derived from this file's own location, never from the environment, a settings
    document or a caller argument: a user-settable worker script would be a way to run arbitrary
    code under the server's identity.
    """

    return os.path.join(os.path.dirname(os.path.abspath(__file__)), WORKER_SCRIPT_NAME)


def remaining(deadline: float) -> float:
    """Seconds left of a monotonic request deadline, never negative."""

    return max(0.0, deadline - time.monotonic())


def _cleanup_budget() -> float:
    """The published cleanup budget, read at call time so a caller can tighten it."""

    return CLEANUP_TIMEOUT_SECONDS


class RegexWorker:
    """One dedicated stdlib regex process owned by exactly one in-flight request.

    Lifecycle states, in order: *new* (no child, may still start), *live* (a child this worker owns),
    *closed*. Closing is one-way and idempotent; a worker that was closed while its child was still
    being created still ends with no live child.
    """

    def __init__(self, script_path: str | None = None) -> None:
        self._script = script_path or worker_script_path()
        self._process: asyncio.subprocess.Process | None = None
        self._spawn: asyncio.Task[asyncio.subprocess.Process] | None = None
        self._returncode: int | None = None
        self._closed = False
        self._retire_started = 0.0
        self._sequence = 0

    @property
    def pid(self) -> int | None:
        """The child's pid once it exists; ``None`` before creation and after a full retirement."""

        if self._closed:
            return None
        return None if self._process is None else self._process.pid

    @property
    def returncode(self) -> int | None:
        """Exit code once the child has been reaped, else ``None``: the exit evidence.

        The code is remembered separately from the transport object so that "the child really ended"
        survives retirement: a caller can still read the evidence after the worker was closed.
        """

        if self._returncode is not None:
            return self._returncode
        return None if self._process is None else self._process.returncode

    @property
    def alive(self) -> bool:
        """True while a child this worker owns still exists.

        A closed worker is never alive, whatever moment it was closed in: a child that appears after
        the close was already ordered killed is not this worker's child any more.
        """

        if self._closed or self._returncode is not None:
            return False
        return self._process is not None and self._process.returncode is None

    @property
    def managed_process(self) -> asyncio.subprocess.Process | None:
        """The transport object this worker holds, for exit evidence in tests and diagnostics."""

        return self._process

    def force_kill(self) -> None:
        """Synchronously kill the child, for the paths where no ``await`` is allowed."""

        terminate_now(self._process)

    async def start(self, *, timeout: float = STARTUP_TIMEOUT_SECONDS) -> None:
        """Start the child and finish the handshake inside ``timeout`` seconds.

        ``timeout`` covers process creation *and* the handshake. The work runs in a task this worker
        owns, so an external cancellation arriving at any await boundary — including while the
        interpreter is still being created — is answered by killing and reaping whatever child came
        into existence, and only then by propagating the cancellation.
        """

        if self._closed:
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker")
        if self._process is not None or self._spawn is not None:
            return
        created = asyncio.ensure_future(self._run_start(timeout))
        self._spawn = created
        created.add_done_callback(self._spawn_settled)
        try:
            # Shielded, not shared: a cancellation of *this* coroutine must give the creation the
            # chance to hand over the child it produced, and must be answered by killing that child.
            # The creation task cleans up after itself on its own failures, so this only has to cover
            # a child that already exists.
            process = await asyncio.shield(created)
        except asyncio.CancelledError:
            if not created.cancelled():
                # The child may exist by now even though we never received it. Kill it and record the
                # exit before the cancellation is allowed to leave: an abandoned backtracking match
                # is worse than a slightly slower cancel.
                await self._retire(_cleanup_budget(), immediate=True)
            raise
        finally:
            if created.done():
                self._spawn = None
        if self._closed or process.returncode is not None:
            await self._retire(_cleanup_budget(), immediate=True)
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker")

    async def _run_start(self, timeout: float) -> asyncio.subprocess.Process:
        """Create the child and complete the handshake inside one deadline.

        Runs as its own task so that the caller's cancellation cannot orphan a child that is created
        after the cancellation was requested: every exit from this coroutine — success, failure or
        cancellation — leaves no child of this worker running unowned. A failure raises
        ``RuleEvaluationError``; a cancellation re-raises after killing and reaping what it created.
        """

        try:
            return await self._start_child(timeout)
        except asyncio.CancelledError:
            # The creation is being given up. Kill and reap first: an abandoned child is exactly the
            # failure mode this layer exists to prevent.
            await self._retire(_cleanup_budget(), immediate=True)
            raise
        except RuleEvaluationError:
            await self._retire(_cleanup_budget(), immediate=True)
            raise

    async def _start_child(self, timeout: float) -> asyncio.subprocess.Process:
        """Create the child and read its readiness line inside ``timeout`` seconds."""

        try:
            present = os.path.isfile(self._script)
        except (OSError, ValueError) as error:
            # A script path this host cannot even inspect (for example an unusable UNC form) is a
            # worker that cannot be started, which is a named failure rather than a raw OSError.
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker") from error
        if not present:
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker")
        if timeout <= 0:
            raise RuleEvaluationError(REASON_EVALUATION_TIMEOUT, "request")
        deadline = time.monotonic() + timeout
        try:
            # Creation is inside the deadline, not before it: on a loaded host the sandboxed
            # interpreter itself can take longer to appear than the whole published budget, and a
            # start that outlives its budget must still reap whatever it created.
            async with asyncio.timeout(remaining(deadline)):
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-I",
                    "-u",
                    self._script,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    limit=IPC_MAX_RESPONSE_BYTES,
                )
        except TimeoutError:
            # A published startup budget that expires before the child appears is a start failure.
            # A child that did appear is killed and reaped by ``start``.
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker") from None
        except (OSError, ValueError) as error:
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker") from error
        self._process = process
        try:
            line = await self._read_line(remaining(deadline))
        except TimeoutError:
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker") from None
        if line is None or not self._is_ready(line):
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker")
        return process

    def _spawn_settled(self, task: asyncio.Task[asyncio.subprocess.Process]) -> None:
        """Kill a child that arrived after this worker was already closed or cancelled.

        Runs in the same event-loop callback as the task's own completion, so the kill is issued
        before any other task can observe the finished creation: there is no window in which an
        unowned child of a closed worker is alive.
        """

        if not self._closed or task.cancelled():
            return
        if task.exception() is not None:
            return
        terminate_now(task.result())

    async def compile(self, pattern: str, flags: int, *, timeout: float) -> int:
        """Compile one expression in the child and return its handle."""

        sequence = self._next()
        response = await self._call(
            {"seq": sequence, "op": "compile", "pattern": pattern, "flags": flags},
            sequence,
            timeout=timeout,
        )
        if response.get("ok") is not True:
            if response.get("code") == "invalid_regex":
                raise RuleEvaluationError(ERROR_INVALID_REGEX, "regex")
            await self._retire(_cleanup_budget(), immediate=True)
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker")
        handle = response.get("handle")
        if type(handle) is not int or handle != sequence:
            await self._retire(_cleanup_budget(), immediate=True)
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker")
        return handle

    async def search(self, handle: int, text: str, *, timeout: float) -> bool:
        """Run ``re.search`` for one compiled handle; any failure retires this worker."""

        sequence = self._next()
        response = await self._call(
            {"seq": sequence, "op": "search", "handle": handle, "text": text},
            sequence,
            timeout=timeout,
        )
        if response.get("ok") is not True or type(response.get("matched")) is not bool:
            await self._retire(_cleanup_budget(), immediate=True)
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker")
        return bool(response["matched"])

    async def aclose(self, *, timeout: float = CLEANUP_TIMEOUT_SECONDS) -> None:
        """Close this worker as a barrier: ask the child to exit, kill it if it will not, reap it.

        The closed mark is set before the first await, so a concurrent creation cannot hand back a
        live child after this coroutine has returned.
        """

        self._closed = True
        await self._close_child(timeout)

    async def close_quietly(self, timeout: float = CLEANUP_TIMEOUT_SECONDS) -> None:
        """Close this worker without disturbing the failure its caller is already reporting."""

        self._closed = True
        await self._close_child(timeout)

    async def _close_child(self, timeout: float) -> None:
        """Retire this worker for good, giving the child a bounded chance to exit on its own.

        A deliberate close is not a hard failure, so a child that reacts to its input being closed is
        allowed to leave cleanly — which is what makes the recorded exit code meaningful rather than
        always ``-9``. The grace period is bounded by ``_GRACE_SECONDS`` and is *part of* the published
        cleanup budget, never a second one, so a missing child cannot make a close cost two budgets.
        """

        await self._retire(min(timeout, _GRACE_SECONDS))

    def _note_exit(self, process: asyncio.subprocess.Process) -> None:
        """Record a real exit code, and treat the worker as retired once it is known."""

        if process.returncode is None:
            return
        self._returncode = process.returncode

    async def _retire(
        self, timeout: float = CLEANUP_TIMEOUT_SECONDS, *, immediate: bool = False
    ) -> None:
        """Bring this worker to its final state exactly once, inside ``timeout`` seconds.

        The escalation is deliberate and one-way. A hard failure — an expired budget, a cancellation,
        a protocol violation — issues the kill *before* the first await, so the cleanup budget is
        spent waiting for the real exit rather than on a polite wait that a backtracking match will
        never honour. A deliberate close first asks the child to exit by closing its input, and kills
        it once the grace period is over. Both waits share one deadline, and the exit code is only
        recorded once it really exists: a child that cannot be waited for keeps ``returncode is None``
        instead of an invented clean exit.

        Idempotent by construction: a second call finds the exit code already recorded and returns.
        """

        process = self._process
        if process is None:
            # A child may still be coming into existence. Settle that first: whatever it creates is
            # killed by the worker that owns the creation, so this returns with nothing alive.
            if self._spawn is not None and self._spawn is asyncio.current_task():
                # This coroutine *is* the creation: it produced no child, so there is nothing to
                # settle and nothing to wait for. Settling here would cancel the very task that is
                # failing, which would turn a named worker failure into a cancellation.
                self._closed = True
                return
            process = await self._settle_spawn(timeout)
            if process is None:
                self._closed = True
                return
        if process.returncode is not None and not self._spawn_pending():
            self._note_exit(process)
            return
        budget = max(timeout, _cleanup_budget())
        self._retire_started = time.monotonic()
        shutdown_stdin(process)
        cancelled: asyncio.CancelledError | None = None
        if immediate:
            # A hard failure: the child is very likely spinning inside ``re``, where a closed pipe
            # means nothing. Kill before the first await so the budget buys the exit, not the hope.
            terminate_now(process)
        else:
            # A deliberate close: give the child its bounded grace to exit by itself, then kill.
            grace = min(_GRACE_SECONDS, budget)
            try:
                async with asyncio.timeout(grace):
                    await process.wait()
            except TimeoutError:
                pass
            except asyncio.CancelledError as error:
                # The cleanup still has to finish — the child is about to be killed and reaped — so the
                # cancellation is held here and re-raised once nothing is left alive.
                cancelled = own_cancellation(error)
            if process.returncode is None:
                terminate_now(process)
        try:
            await self._reap_killed(
                process, max(0.0, budget - (time.monotonic() - self._retire_started))
            )
        except asyncio.CancelledError as error:
            cancelled = cancelled or error
        self._closed = True
        if cancelled is not None:
            raise cancelled

    def _spawn_pending(self) -> bool:
        """True while a creation this worker owns has not settled yet."""

        return self._spawn is not None and not self._spawn.done()

    async def _settle_spawn(self, timeout: float) -> asyncio.subprocess.Process | None:
        """End an in-flight creation, within ``timeout``, and return whatever child it produced.

        Used when a close races the creation of the very first child. The creation task is cancelled
        first, because a creation that is still blocked would otherwise consume the entire cleanup
        budget before the child it is about to produce could be killed. Cancelling it does not leak a
        child: the task's own done callback kills anything it created, and the cancellation cannot
        complete until that callback has run. Once the task is settled there is nothing this worker
        does not know about.

        The creation's own outcome is always consumed here, whether it succeeded, failed or was
        cancelled. A failure that nobody reads would surface later as an unretrieved task exception —
        noise that hides the real one — so this coroutine is the single place that reads it.
        """

        spawn = self._spawn
        if spawn is None:
            return self._process
        if not spawn.done():
            spawn.cancel()
        cancelled: asyncio.CancelledError | None = None
        try:
            # ``asyncio.wait`` is used rather than ``asyncio.shield`` on purpose: it never re-raises the
            # creation's *own* cancellation, which is an expected outcome here (this coroutine cancels
            # it above), and it never cancels the creation when this coroutine is cancelled. Only a real
            # cancellation of this caller reaches the handler below.
            await asyncio.wait({spawn}, timeout=max(0.0, timeout))
        except asyncio.CancelledError as error:
            # Held, not dropped: this coroutine has to read the creation's outcome either way, and the
            # caller still has to learn that its own request was cancelled.
            cancelled = error
        finally:
            self._consume_spawn(spawn)
        if cancelled is not None:
            raise cancelled
        if not spawn.done():
            return self._process
        if self._process is not None:
            return self._process
        if spawn.cancelled() or spawn.exception() is not None:
            return None
        return spawn.result()

    @staticmethod
    def _consume_spawn(spawn: asyncio.Task[asyncio.subprocess.Process]) -> None:
        """Read a settled creation's outcome so it is never reported as never retrieved."""

        if spawn.done() and not spawn.cancelled():
            spawn.exception()

    async def _reap_killed(
        self, process: asyncio.subprocess.Process, timeout: float = CLEANUP_TIMEOUT_SECONDS
    ) -> None:
        """Kill the child if needed and then **wait** for it, inside ``timeout`` seconds.

        A signal is not an exit code. The wait below runs even when the transport already reports a
        code, so the recorded evidence is a real reaped exit rather than the echo of our own kill.
        A child that still cannot be waited for keeps ``returncode is None``, so the caller never
        sees an invented exit.
        """

        deadline = time.monotonic() + max(0.0, timeout)
        cancelled: asyncio.CancelledError | None = None
        for attempt in range(2):
            if process.returncode is not None:
                break
            if attempt:
                terminate_now(process)
            budget = max(0.0, deadline - time.monotonic())
            try:
                async with asyncio.timeout(budget):
                    await process.wait()
            except TimeoutError:
                pass
            except asyncio.CancelledError as error:
                # Hold the cancellation, not drop it: the second attempt still has to kill and reap the
                # child, and the caller still has to learn that its own request was cancelled.
                cancelled = cancelled or own_cancellation(error)
        self._note_exit(process)
        if cancelled is not None:
            raise cancelled

    async def _call(
        self, command: dict[str, object], sequence: int, *, timeout: float
    ) -> dict[str, object]:
        """Send one command and read its response inside the smaller of the two budgets.

        Whichever deadline expires is the one reported. The call budget is always the smaller of the
        published 50 ms and what is left of the request, so "no time left to even send this call" is
        the request deadline, and a call that was sent but answered too slowly is the call budget.
        """

        process = self._process
        if process is None or process.stdin is None or self._closed:
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker")
        budget = min(CALL_TIMEOUT_SECONDS, timeout)
        if budget <= 0:
            raise RuleEvaluationError(REASON_EVALUATION_TIMEOUT, "request")
        encoded = (json.dumps(command, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        if len(encoded) > IPC_MAX_REQUEST_BYTES:
            # The worker was asked for something outside the protocol; it is retired rather than
            # reused, and the caller is told the input was the problem.
            await self._retire(_cleanup_budget(), immediate=True)
            raise RuleEvaluationError(REASON_INPUT_TOO_LARGE, "rule_value")
        try:
            async with asyncio.timeout(budget):
                process.stdin.write(encoded)
                await process.stdin.drain()
                line = await self._read_line_untimed()
        except TimeoutError:
            # Which deadline expired decides the code, and the decision is made from the budgets
            # themselves, never by looking at the clock after cleanup: cleanup takes time of its own
            # and would blur the answer. ``budget`` is ``min(published call budget, what the caller
            # passed)``, so
            #   * budget == published 50 ms  -> the caller still had more time; the call's own 50 ms
            #     expired first -> regex_timeout;
            #   * budget <  published 50 ms  -> the caller's remaining time *capped* this call, so the
            #     request deadline is what expired -> evaluation_timeout.
            request_expired = budget < CALL_TIMEOUT_SECONDS
            await self._retire(_cleanup_budget(), immediate=True)
            if request_expired:
                raise RuleEvaluationError(REASON_EVALUATION_TIMEOUT, "request") from None
            raise RuleEvaluationError(REASON_REGEX_TIMEOUT, "regex") from None
        except asyncio.CancelledError:
            # The caller is going away. The child is killed and reaped before the cancellation is
            # allowed to propagate, so a backtracking match cannot outlive the request that started it.
            await self._retire(_cleanup_budget(), immediate=True)
            raise
        if line is None:
            await self._retire(_cleanup_budget(), immediate=True)
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker")
        response = _decode(line)
        if response is None or not _valid_response(response, sequence):
            await self._retire(_cleanup_budget(), immediate=True)
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker")
        return response

    async def _read_line(self, timeout: float) -> bytes | None:
        """Bounded read of one response line; ``TimeoutError`` if the budget expires."""

        async with asyncio.timeout(max(0.0, timeout)):
            return await self._read_line_untimed()

    async def _read_line_untimed(self) -> bytes | None:
        process = self._process
        if process is None or process.stdout is None:
            return None
        try:
            line = await process.stdout.readline()
        except (ValueError, asyncio.LimitOverrunError) as error:
            # The reader's high-water mark rejected an oversized line before it was accumulated. The
            # worker is retired: an over-limit response is a protocol violation, not a retryable read.
            await self._retire(_cleanup_budget(), immediate=True)
            raise RuleEvaluationError(REASON_INPUT_TOO_LARGE, "regex_worker") from error
        return line or None

    def _is_ready(self, line: bytes) -> bool:
        """The frozen handshake: ``ready`` true, ``protocol`` the integer 1, and our own child's pid.

        ``protocol`` is compared as an integer, not merely as a truthy value: ``true`` and ``1.0``
        would both pass a loose comparison while proving nothing about the peer. The ``pid`` must be
        the pid of the child this worker created, so a reply injected by anything else is refused.
        """

        payload = _decode(line)
        if payload is None or set(payload) != _READY_FIELDS:
            return False
        if payload.get("ready") is not True or type(payload.get("protocol")) is not int:
            return False
        if payload.get("protocol") != 1:
            return False
        pid = payload.get("pid")
        if type(pid) is not int or pid <= 0:
            return False
        return self._process is None or pid == self._process.pid

    def _next(self) -> int:
        self._sequence += 1
        return self._sequence


def own_cancellation(error: asyncio.CancelledError) -> asyncio.CancelledError | None:
    """Return ``error`` when *this* task was cancelled, and ``None`` when something inside it was.

    ``CancelledError`` is raised for two different events: the caller cancelling the request, and a
    waiter that the transport cancelled on its own. Only the first one belongs to the caller, and only
    the first one may leave a cleanup coroutine — so the two are told apart by asking whether this task
    has a cancellation request against it, rather than by guessing from the exception alone.
    """

    task = asyncio.current_task()
    if task is not None and task.cancelling():
        return error
    return None


def shutdown_stdin(process: asyncio.subprocess.Process) -> None:
    """Ask a child for a clean exit by closing its input; a failure here is not fatal."""
    stdin = process.stdin
    if stdin is None:
        return
    with contextlib.suppress(OSError, RuntimeError, ValueError):
        stdin.close()


def terminate_now(process: asyncio.subprocess.Process | None) -> None:
    """Kill a child synchronously.

    Used whenever a budget expires or an awaited cleanup is itself cancelled: the kill is issued
    before any further ``await``, so a cancelled request can never leave a spinning backtracking match
    behind. Safe to call on an already-exited or already-reaped child.
    """

    if process is None or process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        process.kill()


def _duplicate_free(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a JSON object, refusing a key that appears twice.

    The default ``json.loads`` keeps the last value of a repeated key, which would let a worker send
    ``{"seq": 1, "seq": 2, ...}`` and have one of the two facts silently disappear. Two different
    facts under one key is a broken peer, so the object is refused instead of half-believed.
    """

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _decode(line: bytes) -> dict[str, Any] | None:
    try:
        payload = json.loads(
            line.decode("utf-8", errors="strict"), object_pairs_hook=_duplicate_free
        )
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _valid_response(response: dict[str, Any], sequence: int) -> bool:
    """Strict protocol shape and identity check for one response line.

    ``seq`` must be the exact integer this call sent — ``true`` is refused even though
    ``True == 1`` — and the response must carry exactly the fields of its kind, so a worker cannot
    smuggle an unexpected field past the caller.
    """

    if set(response) - _RESPONSE_FIELDS:
        return False
    if type(response.get("seq")) is not int or response["seq"] != sequence:
        return False
    ok = response.get("ok")
    if ok is True:
        return set(response) in (_COMPILE_OK_FIELDS, _SEARCH_OK_FIELDS, _FORGET_OK_FIELDS)
    if ok is False:
        return set(response) == _FAILURE_FIELDS and isinstance(response.get("code"), str)
    return False


__all__ = [
    "CALL_TIMEOUT_SECONDS",
    "CLEANUP_TIMEOUT_SECONDS",
    "IPC_MAX_REQUEST_BYTES",
    "IPC_MAX_RESPONSE_BYTES",
    "REQUEST_TIMEOUT_SECONDS",
    "STARTUP_TIMEOUT_SECONDS",
    "WORKER_SCRIPT_NAME",
    "RegexWorker",
    "remaining",
    "worker_script_path",
]

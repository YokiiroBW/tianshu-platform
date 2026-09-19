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
    *closed*. Closing is one-way and idempotent.

    Ownership is the whole point of this class, and it has one gap that no flag can close by itself:
    between ``create_subprocess_exec`` returning a real child and that child being handed back to this
    worker, the child exists and this worker cannot see it. That window is therefore *modelled*, not
    declared away: ``_handover_pending`` stays true until the creation settles, the one cleanup waits
    for it inside the published cleanup budget, and both ``alive`` and ``settled`` report the window
    instead of pretending a closed worker has nothing to answer for. The creation's own completion
    callback kills whatever arrives after a close, so a child that is handed over late is killed even
    when the cleanup budget has already run out.
    """

    def __init__(self, script_path: str | None = None) -> None:
        self._script = script_path or worker_script_path()
        self._process: asyncio.subprocess.Process | None = None
        self._spawn: asyncio.Task[asyncio.subprocess.Process] | None = None
        self._returncode: int | None = None
        self._closed = False
        self._handover_pending = False
        self._cleanup: asyncio.Task[None] | None = None
        self._cleanup_deadline: float | None = None
        self._sequence = 0

    @property
    def pid(self) -> int | None:
        """The child's pid once this worker holds it; ``None`` while no child is in its hands.

        The closed mark is deliberately not consulted: after a close that raced the creation, "no pid"
        means "no child was handed over", which is not the same claim as "no child exists" — that one
        is made by :attr:`alive`.
        """

        return None if self._process is None else self._process.pid

    @property
    def returncode(self) -> int | None:
        """Exit code once the child has been reaped, else ``None``: the exit evidence.

        The code is remembered separately from the transport object so that "the child really ended"
        survives retirement: a caller can still read the evidence after the worker was closed. It is
        never invented: a child this worker could not wait for keeps ``None``.
        """

        if self._returncode is not None:
            return self._returncode
        return None if self._process is None else self._process.returncode

    @property
    def alive(self) -> bool:
        """True while a child this worker owns may still be running.

        Two states are alive: a child this worker holds and has not seen exit, and a creation that has
        not handed its child over yet. The second is unknowable from the outside, so it is reported as
        alive rather than as clean — "I closed" must never be used to mean "nothing is running" while
        a child may exist that this worker has not been given.
        """

        if self._returncode is not None:
            return False
        if self._process is not None:
            return self._process.returncode is None
        return self._handover_pending

    @property
    def settled(self) -> bool:
        """True when this worker owns nothing that could still be running.

        The distinction from :attr:`alive` is the point of this property: a caller that keeps a set of
        workers it is responsible for may only drop one whose child is reaped *and* whose creation can
        no longer produce another.
        """

        if self._returncode is not None:
            return True
        return self._process is None and not self._handover_pending and not self._cleanup_running()

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

        Every exit from here goes through :meth:`_retire`, which is the one place that starts the one
        cleanup this worker runs: success followed by a close race, a named failure, a cancellation and
        an unclassified exception all end in the same cleanup, sharing one deadline.
        """

        if self._closed:
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker")
        if self._process is not None or self._spawn is not None:
            return
        # From here until the creation settles, a child may exist that this worker has not been handed:
        # that window is what ``alive`` and the cleanup below have to cover.
        self._handover_pending = True
        created = asyncio.ensure_future(self._run_start(timeout))
        self._spawn = created
        created.add_done_callback(self._spawn_settled)
        try:
            # Shielded, not shared: a cancellation of *this* coroutine must give the creation the
            # chance to hand over the child it produced, and must be answered by killing that child.
            process = await asyncio.shield(created)
        except asyncio.CancelledError:
            # Either this caller was cancelled or the creation itself was. Both end the same way: what
            # exists is killed and reaped, and only then does the cancellation leave.
            await self._retire(_cleanup_budget(), immediate=True)
            raise
        except BaseException:
            # Named failures already started their cleanup from inside the creation; an unclassified
            # one (a parser that blew its own limits, say) must not skip the reaping just because it
            # has no name yet.
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
        cancellation — leaves no child of this worker running unowned.

        The cleanup is *started* here but never awaited: this coroutine is the creation that cleanup
        waits for, so awaiting it would be waiting for itself. Starting it is enough — the cleanup
        task finds the settled creation, takes the child it produced and reaps it.
        """

        try:
            return await self._start_child(timeout)
        except asyncio.CancelledError:
            # The creation is being given up. Kill and reap first: an abandoned child is exactly the
            # failure mode this layer exists to prevent.
            self._start_cleanup(_cleanup_budget(), immediate=True)
            raise
        except RuleEvaluationError:
            self._start_cleanup(_cleanup_budget(), immediate=True)
            raise
        except Exception as error:
            # Anything else is still a worker failure, and still has to reap what it created. Letting
            # it escape unnamed would leave a live child behind a traceback nobody classified — and
            # would also leave the creation's exception unread.
            self._start_cleanup(_cleanup_budget(), immediate=True)
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker") from error

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
        # The handover is complete: from this line on the child is in this worker's hands, so the
        # "created but not handed over" window is over even if the handshake below still fails.
        self._handover_pending = False
        if self._closed:
            # This child appeared after the worker was closed — the close raced the creation and lost.
            # It is killed at the moment it becomes visible instead of after the handshake it would
            # otherwise be allowed to wait for, and the caller's failure path reaps it.
            terminate_now(process)
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker")
        try:
            line = await self._read_line(remaining(deadline))
        except TimeoutError:
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker") from None
        if line is None or not self._is_ready(line):
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker")
        return process

    def _spawn_settled(self, task: asyncio.Task[asyncio.subprocess.Process]) -> None:
        """Take over whatever the creation produced, and kill it when it arrived after a close.

        Runs in the same event-loop callback as the task's own completion, so the kill is issued before
        any other task can observe the finished creation: there is no window in which an unowned child
        of a closed worker is alive. This is also the one place that reads a creation's exception, so a
        failure nobody awaited is never reported later as "never retrieved".

        The child of a *closed* worker is not adopted: it is killed here and handed to a cleanup round,
        because a kill is not an exit code and only a real wait produces one.
        """

        process: asyncio.subprocess.Process | None = None
        if not task.cancelled() and task.exception() is None:
            process = task.result()
        if process is not None and self._process is None:
            self._process = process
        if self._process is None:
            # The creation produced nothing at all, so there is no handover left to wait for.
            self._handover_pending = False
            return
        self._handover_pending = False
        if not self._closed:
            return
        terminate_now(self._process)
        if not self._cleanup_running():
            # The cleanup that already ran could not see this child. It gets its own round rather than
            # being left as a kill with no exit code; the running one, if there is one, waited for this
            # very creation and will find the child itself.
            self._start_cleanup(_cleanup_budget(), immediate=True)

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
        live child after this coroutine has returned. When the close races the creation of the very
        first child, the close waits for that handover inside the same cleanup deadline: a child that
        already exists behind ``create_subprocess_exec`` is this worker's responsibility, and reporting
        "closed" while it runs would be a claim this worker cannot support.
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

        The handover wait, by contrast, gets the whole shared budget rather than the grace: 0.2 s is a
        sensible patience for a child that was *asked* to stop, and no patience at all for a creation
        that has not handed over a child which already exists.
        """

        await self._retire(min(timeout, _GRACE_SECONDS))

    def _note_exit(self, process: asyncio.subprocess.Process) -> None:
        """Record a real exit code, and treat the worker as retired once it is known."""

        if process.returncode is None:
            return
        self._returncode = process.returncode

    def _cleanup_running(self) -> bool:
        """True while the one cleanup this worker runs has not finished yet."""

        return self._cleanup is not None and not self._cleanup.done()

    def _has_unreaped_child(self) -> bool:
        """True while a child this worker holds has no recorded exit code."""

        return self._process is not None and self._returncode is None

    def _start_cleanup(self, timeout: float, *, immediate: bool) -> asyncio.Task[None]:
        """Start — but do not await — the one cleanup this worker runs, and return its task.

        This is the only place a cleanup is created, so every failure path shares one task, one
        absolute deadline and one result. It exists separately from :meth:`_retire` because the
        creation task must be able to start its own cleanup: that cleanup waits for the creation to
        settle, so a creation that awaited it would be waiting for itself.
        """

        self._closed = True
        deadline = time.monotonic() + max(timeout, _cleanup_budget())
        self._cleanup_deadline = deadline
        task = asyncio.ensure_future(self._run_cleanup(deadline, immediate))
        self._cleanup = task
        task.add_done_callback(_consume_task)
        return task

    async def _retire(
        self, timeout: float = CLEANUP_TIMEOUT_SECONDS, *, immediate: bool = False
    ) -> None:
        """Bring this worker to its final state, inside one cleanup budget shared by every caller.

        The escalation is deliberate and one-way. A hard failure — an expired budget, a cancellation,
        a protocol violation — issues the kill *before* the first await, so the cleanup budget is
        spent waiting for the real exit rather than on a polite wait that a backtracking match will
        never honour. A deliberate close first asks the child to exit by closing its input, and kills
        it once the grace period is over. Both waits share one deadline, and the exit code is only
        recorded once it really exists: a child that cannot be waited for keeps ``returncode is None``
        instead of an invented clean exit.

        Concurrency is by sharing, not by repeating: the first caller starts the cleanup, every later
        caller — a second close, a cancelled request, an evaluator sweeping its workers — waits for
        that same task, so N concurrent retirements cost one budget and cannot each restart a fresh
        second of waiting. The only second round is for a child that did not exist when the first one
        ran, which is a different child rather than the same wait restarted.
        """

        self._closed = True
        task = self._cleanup
        if task is None or (task.done() and self._has_unreaped_child()):
            task = self._start_cleanup(timeout, immediate=immediate)
        cancelled = await self._await_cleanup(task)
        if cancelled is not None:
            raise cancelled

    async def _await_cleanup(self, task: asyncio.Task[None]) -> asyncio.CancelledError | None:
        """Wait for the shared cleanup, holding this caller's own cancellation until it has finished.

        A caller that is cancelled while it waits still gets a finished cleanup: the child is killed
        and reaped before the cancellation is re-raised. A cancellation that is not this task's — the
        transport's own waiter being cancelled, say — is not the caller's and is not propagated.
        """

        cancelled: asyncio.CancelledError | None = None
        while True:
            try:
                # ``asyncio.wait`` never cancels the cleanup and never re-raises *its* outcome: this
                # coroutine is a waiter, not the owner, and the cleanup's failures are its own.
                await asyncio.wait({task})
                return cancelled
            except asyncio.CancelledError as error:
                cancelled = cancelled or own_cancellation(error)
                if task.done():
                    return cancelled

    async def _run_cleanup(self, deadline: float, immediate: bool) -> None:
        """The one cleanup: wait for the handover, then kill and reap, all inside ``deadline``.

        Every stage spends the *same* absolute deadline, so a slow handover shortens the reaping wait
        instead of buying it a fresh budget. When the deadline runs out with the creation still holding
        a child it has not handed over, this returns without pretending the worker is clean: the
        handover stays pending, ``alive`` stays true, and the creation's completion callback kills and
        reaps whatever finally arrives.
        """

        process = self._process
        if process is None and self._handover_pending:
            process = await self._await_handover(deadline)
        if process is None:
            # Either no child ever existed, or one is still held by a creation that has not handed it
            # over. The second case is not a completed cleanup, and is not reported as one.
            return
        shutdown_stdin(process)
        if immediate:
            # A hard failure: the child is very likely spinning inside ``re``, where a closed pipe
            # means nothing. Kill before the first await so the budget buys the exit, not the hope.
            terminate_now(process)
        else:
            # A deliberate close: give the child its bounded grace to exit by itself, then kill.
            try:
                async with asyncio.timeout(min(_GRACE_SECONDS, remaining(deadline))):
                    await process.wait()
            except TimeoutError:
                pass
            if process.returncode is None:
                terminate_now(process)
        await self._reap_killed(process, remaining(deadline))

    async def _await_handover(self, deadline: float) -> asyncio.subprocess.Process | None:
        """Wait, inside ``deadline``, for the creation to hand over the child it produced.

        The creation is *not* cancelled here. Cancelling it would be the one action that can turn a
        child that exists into a child nobody owns: a creation that is already past
        ``create_subprocess_exec`` cannot un-create it, and the handover is exactly what this wait is
        for. Letting it finish — bounded by the shared deadline — is what keeps the ownership real.
        """

        spawn = self._spawn
        if spawn is not None and not spawn.done():
            await asyncio.wait({spawn}, timeout=remaining(deadline))
        _consume_task(spawn)
        if self._process is None and (spawn is None or spawn.done()):
            # The creation settled without producing a child: the window is closed for good.
            self._handover_pending = False
        return self._process

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
        except RecursionError as error:
            # A line small enough to be read but too deeply nested to parse. It is refused as an
            # unreadable response rather than escaping as a raw parser failure, and the worker that
            # sent it is retired exactly as for any other protocol violation.
            await self._retire(_cleanup_budget(), immediate=True)
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker") from error
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


def _consume_task(task: asyncio.Task[Any] | None) -> None:
    """Read a settled task's outcome so it is never reported later as never retrieved.

    Used for the two tasks this module owns: the creation and the cleanup. Neither is awaited by
    everybody who may need to observe it — a creation that failed after its caller was cancelled, or a
    cleanup started from a done callback — and an unread exception would surface as unrelated noise at
    interpreter shutdown, hiding the real failure. A cancelled task has no exception to read.
    """

    if task is None or not task.done() or task.cancelled():
        return
    task.exception()


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
    """Parse one protocol line, refusing anything that is not a plain JSON object.

    ``RecursionError`` is caught alongside the decode errors on purpose: ``json.loads`` raises it for
    deeply nested input that is small enough to pass every size limit — 5000 nested arrays fit in a few
    kilobytes — and it is not a ``ValueError``, so leaving it out let a hostile line escape the bounded
    reader as an unclassified exception. A refused line is a refused line, whatever shape the refusal
    took inside the parser, and the caller answers it the same way: a named worker failure and a real
    reap.
    """

    try:
        payload = json.loads(
            line.decode("utf-8", errors="strict"), object_pairs_hook=_duplicate_free
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
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

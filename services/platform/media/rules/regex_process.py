"""Bounded IPC to one dedicated regex process, with hard termination and real reaping.

Every budget in this module is a *hard* budget: when one expires the child is terminated, killed if
it survives the termination, and waited for until its exit code is recorded. ``re`` cannot be
interrupted from inside, so the process boundary is the only thing that actually stops a catastrophic
backtracking match; an ``asyncio.wait_for`` around a future would leave the match spinning in the
background.

Retirement is one operation with one deadline. ``_retire`` keeps the managed process in hand until
its exit code is known, closes stdin to ask for a clean exit, escalates to ``kill`` if the child is
still there, and only then releases it. It is idempotent, so a repeated close, a concurrent close and
a close during cancellation all end in the same state: a reaped child and a recorded exit code. The
caller's ``CancelledError`` is never swallowed — after the child is gone it is re-raised, which is why
the wait is shielded: a cancelled request must not abandon its own child.

The published limits are enforced before an unbounded allocation happens:

* a request line may not exceed 1 MiB (measured before it is written);
* a response line may not exceed 16 KiB, enforced as the stream reader's high-water mark so an
  oversized response is refused by the transport instead of being accumulated;
* an over-limit response, a malformed line or a response shape violation is a fatal protocol
  violation: the worker is retired and never reused.

The protocol is checked strictly: ``seq`` and ``handle`` must be real integers (``true`` and ``1.0``
are refused), the response must carry exactly the fields of its response kind, and a stale or unknown
``seq`` is refused. No message ever contains the expression, the matched text or a local path.

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
    """One dedicated stdlib regex process owned by exactly one in-flight request."""

    def __init__(self, script_path: str | None = None) -> None:
        self._script = script_path or worker_script_path()
        self._process: asyncio.subprocess.Process | None = None
        self._returncode: int | None = None
        self._retired = False
        self._retire_started = 0.0
        self._sequence = 0

    @property
    def pid(self) -> int | None:
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
        """True while the child exists. After retirement this is the reaped result, not a guess."""

        if self._returncode is not None:
            return False
        return self._process is not None and self._process.returncode is None

    @property
    def managed_process(self) -> asyncio.subprocess.Process | None:
        """The transport object this worker holds, for exit evidence in tests and diagnostics."""

        return self._process

    async def start(self, *, timeout: float = STARTUP_TIMEOUT_SECONDS) -> None:
        """Start the child and finish the handshake inside ``timeout`` seconds.

        ``timeout`` covers process creation *and* the handshake, so a machine that cannot start a
        Python child reports ``regex_worker_failed`` inside its budget instead of hanging the request.
        Every failure path retires the child before raising, which is what makes this safe to call
        from a ``try`` block whose own cleanup may never run if the task is cancelled.
        """

        if self._process is not None or self._retired:
            return
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
            # start that outlives its budget without a child to kill would be unaccounted for.
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
            # ``asyncio.timeout`` cancels the creation; whatever it managed to spawn is not handed
            # back to us, so nothing is stored and nothing is claimed to be owned.
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker") from None
        except (OSError, ValueError) as error:
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker") from error
        self._process = process
        await self._handshake(deadline)

    async def _handshake(self, deadline: float) -> None:
        """Read the readiness line inside the shared start deadline, retiring on any failure."""

        try:
            line = await self._read_line(remaining(deadline))
        except TimeoutError:
            await self._retire(_cleanup_budget())
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker") from None
        except RuleEvaluationError:
            await self._retire(_cleanup_budget())
            raise
        if line is None or not self._is_ready(line):
            await self._retire(_cleanup_budget())
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker")

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
            await self._retire(_cleanup_budget())
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker")
        handle = response.get("handle")
        if type(handle) is not int or handle != sequence:
            await self._retire(_cleanup_budget())
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
            await self._retire(_cleanup_budget())
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker")
        return bool(response["matched"])

    async def aclose(self, *, timeout: float = CLEANUP_TIMEOUT_SECONDS) -> None:
        """Retire this worker, escalating to kill inside one cleanup budget."""

        await self._retire(timeout)

    async def close_quietly(self, timeout: float = CLEANUP_TIMEOUT_SECONDS) -> None:
        """Retire this worker, keeping whatever failure the caller is already reporting."""

        await self._retire(timeout)

    async def _retire(self, timeout: float = CLEANUP_TIMEOUT_SECONDS) -> None:
        """Reap this worker exactly once, within ``timeout`` seconds.

        The sequence is: ask for a clean exit by closing stdin, wait, kill what is still there, wait
        again, and report the real exit code. Both waits share one deadline, so retirement can never
        cost twice the published budget. Idempotent by construction: a second call finds the child
        already reaped and returns without touching anything.
        """

        process = self._process
        if process is None:
            return
        if process.returncode is not None:
            self._returncode = process.returncode
            self._retired = True
            return
        self._retire_started = time.monotonic()
        shutdown_stdin(process)
        try:
            await asyncio.wait_for(process.wait(), max(0.0, timeout))
        except (TimeoutError, asyncio.CancelledError) as outcome:
            # Out of budget (or the caller was cancelled): stop waiting politely and kill. The kill
            # is synchronous, so it is issued before this coroutine yields again. The waiting is
            # shielded, so an outer cancellation cannot abandon a child that is already being reaped.
            terminate_now(process)
            await self._reap_killed(process)
            if isinstance(outcome, asyncio.CancelledError):
                raise
        else:
            self._returncode = process.returncode
            self._retired = True

    async def _reap_killed(self, process: asyncio.subprocess.Process) -> None:
        """Collect the exit code of a killed child, and never leave it unreaped.

        The extra wait is bounded by what is left of the cleanup budget after the polite wait, so the
        two waits together cost at most ``max(CLEANUP_TIMEOUT_SECONDS, timeout)`` — never two full
        budgets. A child that still cannot be waited for keeps ``returncode is None``, so the caller
        never sees an invented clean exit.
        """

        remaining_budget = max(0.0, _cleanup_budget() - (time.monotonic() - self._retire_started))
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.shield(asyncio.wait_for(process.wait(), remaining_budget))
        if process.returncode is None:
            terminate_now(process)
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.shield(asyncio.wait_for(process.wait(), remaining_budget))
        self._returncode = process.returncode
        self._retired = True

    def force_kill(self) -> None:
        """Synchronously kill the child, for the paths where no ``await`` is allowed."""

        terminate_now(self._process)

    async def _call(
        self, command: dict[str, object], sequence: int, *, timeout: float
    ) -> dict[str, object]:
        process = self._process
        if process is None or process.stdin is None or self._retired:
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker")
        budget = min(CALL_TIMEOUT_SECONDS, timeout)
        if budget <= 0:
            # The request budget ran out before this call could be sent: that is the request
            # deadline, not a slow single call.
            raise RuleEvaluationError(REASON_EVALUATION_TIMEOUT, "request")
        encoded = (json.dumps(command, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        if len(encoded) > IPC_MAX_REQUEST_BYTES:
            # The worker was asked for something outside the protocol; it is retired rather than
            # reused, and the caller is told the input was the problem.
            await self._retire(_cleanup_budget())
            raise RuleEvaluationError(REASON_INPUT_TOO_LARGE, "rule_value")
        try:
            async with asyncio.timeout(budget):
                process.stdin.write(encoded)
                await process.stdin.drain()
                line = await self._read_line_untimed()
        except TimeoutError:
            # One expired budget here means one hard kill: the call budget is always the smaller of
            # the published 50 ms and what is left of the request.
            await self._retire(_cleanup_budget())
            raise RuleEvaluationError(REASON_REGEX_TIMEOUT, "regex") from None
        if line is None:
            await self._retire(_cleanup_budget())
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker")
        response = _decode(line)
        if response is None or not _valid_response(response, sequence):
            await self._retire(_cleanup_budget())
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
            await self._retire(_cleanup_budget())
            raise RuleEvaluationError(REASON_INPUT_TOO_LARGE, "regex_worker") from error
        return line or None

    def _is_ready(self, line: bytes) -> bool:
        """The frozen handshake: ``ready`` true, ``protocol`` 1 and a real positive pid."""

        payload = _decode(line)
        if payload is None or set(payload) != _READY_FIELDS:
            return False
        if payload.get("ready") is not True or payload.get("protocol") != 1:
            return False
        pid = payload.get("pid")
        return type(pid) is int and pid > 0

    def _next(self) -> int:
        self._sequence += 1
        return self._sequence


def shutdown_stdin(process: asyncio.subprocess.Process) -> None:
    """Ask a child for a clean exit by closing its input; a failure here is not fatal."""

    stdin = process.stdin
    if stdin is None:
        return
    with contextlib.suppress(OSError, RuntimeError, ValueError):
        stdin.close()


def terminate_now(process: asyncio.subprocess.Process | None) -> None:
    """Kill a child synchronously.

    Used whenever an awaited cleanup is itself cancelled or out of budget: the kill is issued before
    any further ``await``, so a cancelled request can never leave a spinning backtracking match
    behind. Safe to call on an already-exited or already-reaped child.
    """

    if process is None or process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        process.kill()


def _decode(line: bytes) -> dict[str, Any] | None:
    try:
        payload = json.loads(line.decode("utf-8", errors="strict"))
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

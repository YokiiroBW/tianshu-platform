"""Bounded IPC to one dedicated regex process, with hard termination and real reaping.

Every budget in this module is a *hard* budget: when one expires the child is terminated, killed if
it survives the termination, and waited for. ``re`` cannot be interrupted from inside, so the
process boundary is the only thing that actually stops a catastrophic backtracking match; an
``asyncio.wait_for`` around a future would leave the match spinning in the background.

The published limits are enforced before an unbounded allocation happens:

* a request line may not exceed 1 MiB (measured before it is written);
* a response line may not exceed 16 KiB, enforced as the stream reader's high-water mark so an
  oversized response is refused by the transport instead of being accumulated;
* an over-limit response is a fatal protocol violation: the worker is retired and never reused.

Sequence numbers are matched exactly. A response carrying an unknown or stale ``seq``, a response
that is not a JSON object, and a worker that exits while a call is pending are all refused as
``regex_worker_failed``. No message ever contains the expression, the matched text or a local path.
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


class RegexWorker:
    """One dedicated stdlib regex process owned by exactly one in-flight request."""

    def __init__(self, script_path: str | None = None) -> None:
        self._script = script_path or worker_script_path()
        self._process: asyncio.subprocess.Process | None = None
        self._sequence = 0

    @property
    def pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    @property
    def returncode(self) -> int | None:
        """Exit code once the child has been reaped, else ``None``: the exit evidence."""

        return None if self._process is None else self._process.returncode

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self, *, timeout: float = STARTUP_TIMEOUT_SECONDS) -> None:
        """Start the child and finish the handshake inside ``timeout`` seconds.

        The handshake is part of the request budget, so a machine that cannot start a Python child
        inside three seconds reports ``regex_worker_failed`` rather than hanging the request. Any
        failure retires the child here, which is what makes this safe to call from a ``try`` block
        whose own cleanup may never run if the task is cancelled.
        """

        if self._process is not None:
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
        try:
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
        except (OSError, ValueError) as error:
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker") from error
        self._process = process
        try:
            line = await self._read_line(timeout)
        except TimeoutError:
            await self._abort_quietly()
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker") from None
        if line is None or not self._is_ready(line):
            await self._abort_quietly()
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
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker")
        handle = response.get("handle")
        if type(handle) is not int or handle != sequence:
            await self._abort_quietly()
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker")
        return handle

    async def search(self, handle: int, text: str, *, timeout: float) -> bool:
        """Run ``re.search`` for one compiled handle; any failure retires this worker."""

        sequence = self._next()
        try:
            response = await self._call(
                {"seq": sequence, "op": "search", "handle": handle, "text": text},
                sequence,
                timeout=timeout,
            )
        except RuleEvaluationError:
            await self._abort_quietly()
            raise
        if response.get("ok") is not True or type(response.get("matched")) is not bool:
            await self._abort_quietly()
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker")
        return bool(response["matched"])

    async def aclose(self, *, timeout: float = CLEANUP_TIMEOUT_SECONDS) -> None:
        """Close stdin, preferring a clean exit, and escalate to kill inside the same budget."""

        await self.close_quietly(timeout)

    async def close_quietly(self, timeout: float = CLEANUP_TIMEOUT_SECONDS) -> None:
        """Retire this worker, keeping whatever failure the caller is already reporting."""

        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            return
        if process.stdin is not None:
            with contextlib.suppress(OSError, RuntimeError):
                process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout)
            return
        except (TimeoutError, asyncio.CancelledError):
            self.force_kill()
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(process.wait(), timeout)

    def force_kill(self) -> None:
        """Synchronously terminate the child.

        Used when an awaited cleanup is itself cancelled: the kill is issued before any further
        ``await``, so a cancelled request can never leave a spinning backtracking match behind.
        """

        process = self._process
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, OSError):
            process.kill()

    async def _call(
        self, command: dict[str, object], sequence: int, *, timeout: float
    ) -> dict[str, object]:
        process = self._process
        if process is None or process.stdin is None:
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker")
        if timeout <= 0:
            raise RuleEvaluationError(REASON_EVALUATION_TIMEOUT, "request")
        encoded = (json.dumps(command, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        if len(encoded) > IPC_MAX_REQUEST_BYTES:
            raise RuleEvaluationError(REASON_INPUT_TOO_LARGE, "rule_value")
        try:
            async with asyncio.timeout(timeout):
                process.stdin.write(encoded)
                await process.stdin.drain()
                line = await self._read_line_untimed()
        except TimeoutError:
            # The per-call budget is the only budget here, so one expiry is one hard kill.
            self.force_kill()
            await self._wait_quietly(process, CLEANUP_TIMEOUT_SECONDS)
            raise RuleEvaluationError(REASON_REGEX_TIMEOUT, "regex") from None
        if line is None:
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker")
        response = _decode(line)
        if response is None or response.get("seq") != sequence:
            raise RuleEvaluationError(REASON_REGEX_WORKER_FAILED, "regex_worker")
        return response

    async def _read_line(self, timeout: float) -> bytes | None:
        """Bounded read of one response line; ``TimeoutError`` if the budget expires."""

        async with asyncio.timeout(timeout):
            return await self._read_line_untimed()

    async def _read_line_untimed(self) -> bytes | None:
        process = self._process
        if process is None or process.stdout is None:
            return None
        try:
            line = await process.stdout.readline()
        except (ValueError, asyncio.LimitOverrunError) as error:
            # The reader's high-water mark rejected an oversized line before it was accumulated.
            raise RuleEvaluationError(REASON_INPUT_TOO_LARGE, "regex_worker") from error
        return line or None

    async def _wait_quietly(
        self, process: asyncio.subprocess.Process | None, timeout: float
    ) -> None:
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(process.wait(), timeout)

    async def _abort_quietly(self) -> None:
        """Retire this worker now, keeping whatever failure the caller is already reporting."""

        process = self._process
        self._process = None
        if process is None:
            return
        self.force_kill()
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(process.wait(), CLEANUP_TIMEOUT_SECONDS)

    def _is_ready(self, line: bytes) -> bool:
        payload = _decode(line)
        return payload is not None and payload.get("ready") is True

    def _next(self) -> int:
        self._sequence += 1
        return self._sequence


def _decode(line: bytes) -> dict[str, Any] | None:
    try:
        payload = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


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

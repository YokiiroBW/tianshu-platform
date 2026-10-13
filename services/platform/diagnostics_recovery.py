"""Runtime scheduling for the existing durable log recovery, independent of business and probes."""

import asyncio
import threading
import time

from .diagnostics import UNAVAILABLE

INTERVAL_SECONDS = 1.0
MAX_INTERVAL_SECONDS = 30.0
STOP_TIMEOUT_SECONDS = 6.0
RECOVERABLE_ERRORS = frozenset(
    {"log_write_failed", "log_flush_timeout", "log_queue_full", "log_capacity_exhausted"}
)


class DiagnosticsRecovery:
    """One recovery worker: bounded confirmations, interruptible backoff, no disk access.

    `recover` only waits for the sink's file owner, which remains the only disk writer. This
    dedicated daemon is deliberately outside asyncio's executor: a stuck fsync must never make
    `asyncio.run` wait for an executor thread at process exit.
    """

    def __init__(self, sink, *, interval=INTERVAL_SECONDS, max_interval=MAX_INTERVAL_SECONDS):
        self.sink = sink
        self.interval = interval
        self.max_interval = max(interval, max_interval)
        self._stop = threading.Event()
        self._thread = None
        self._lifecycle_lock = threading.Lock()

    def start(self):
        """Start once; a retired worker can never be restarted."""
        with self._lifecycle_lock:
            if self._thread is None and not self._stop.is_set():
                self._thread = threading.Thread(
                    target=self._run, name="tianshu-diagnostics-recovery", daemon=True
                )
                self._thread.start()

    def _run(self):
        delay = self.interval
        while not self._stop.wait(delay):
            if self.sink.state == UNAVAILABLE and self.sink.error in RECOVERABLE_ERRORS:
                recovered = self.sink.recover()
                delay = self.interval if recovered else min(delay * 2, self.max_interval)
            else:
                delay = self.interval

    async def stop(self):
        """Close admission first, interrupt the timer, then wait only for a bounded attempt.

        The event loop remains free to drain in-flight HTTP. No worker can reopen admission once
        this starts, even if its fsync confirmation arrives during this wait.
        """
        with self._lifecycle_lock:
            self.sink.stop_admission()
            self._stop.set()
        deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
        while self._thread is not None and self._thread.is_alive():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.01, remaining))
        return True

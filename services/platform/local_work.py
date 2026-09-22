"""Bounded off-loop execution of complete local application operations."""

import asyncio
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from .contracts import Fault


class LocalWork:
    def __init__(self, workers=4, capacity=16):
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="platform-local")
        self.capacity = capacity
        self.active = 0
        self.closed = False
        self.lock = threading.Lock()

    async def run(self, operation, *args, **kwargs):
        with self.lock:
            if self.closed or self.active >= self.capacity:
                raise Fault("dependency_unavailable", 503)
            self.active += 1
            try:
                context = contextvars.copy_context()
                future = self.executor.submit(context.run, partial(operation, *args, **kwargs))
            except BaseException:
                self.active -= 1
                raise
        # Cancellation of a caller cannot release a still-running SQLite operation's
        # capacity. No operation is replayed or its transaction split across threads.
        future.add_done_callback(self._finished)
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(lambda item: item.exception() if not item.cancelled() else None)
        try:
            return await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            # A queued operation can still be prevented; an already running one
            # keeps its slot until the executor's completion callback fires.
            future.cancel()
            raise

    def _finished(self, future):
        with self.lock:
            self.active -= 1

    def close(self):
        with self.lock:
            self.closed = True
        self.executor.shutdown(wait=False, cancel_futures=True)

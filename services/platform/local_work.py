"""Bounded off-loop execution of complete local application operations."""

import asyncio
import contextvars
import threading
from collections import deque
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
        self._waiters = deque()

    @staticmethod
    def _wake(waiter):
        if not waiter.done():
            waiter.set_result(None)

    def _notify(self, waiter):
        try:
            waiter.get_loop().call_soon_threadsafe(self._wake, waiter)
        except RuntimeError:
            # A closed event loop no longer has a caller that can submit work.
            return False
        return True

    def _wake_next(self):
        # Called under self.lock. A notified waiter stays at the head until it
        # takes its slot, so newcomers cannot overtake it before it resumes.
        while not self.closed and self.active < self.capacity and self._waiters:
            waiter = self._waiters[0]
            if not waiter.cancelled() and self._notify(waiter):
                break
            self._waiters.popleft()

    async def _acquire(self):
        with self.lock:
            if self.closed:
                raise Fault("dependency_unavailable", 503)
            if self.active < self.capacity and not self._waiters:
                self.active += 1
                return
            waiter = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)
            self._wake_next()
        try:
            await waiter
            with self.lock:
                if self.closed:
                    raise Fault("dependency_unavailable", 503)
                self._waiters.popleft()
                self.active += 1
                self._wake_next()
        except BaseException:
            with self.lock:
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass
                self._wake_next()
            raise

    async def run(self, operation, *args, **kwargs):
        await self._acquire()
        with self.lock:
            try:
                if self.closed:
                    raise Fault("dependency_unavailable", 503)
                context = contextvars.copy_context()
                future = self.executor.submit(context.run, partial(operation, *args, **kwargs))
            except BaseException:
                self.active -= 1
                self._wake_next()
                raise
        # Cancellation of a caller cannot release a still-running local operation's
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
            self._wake_next()

    def close(self):
        with self.lock:
            self.closed = True
            waiters, self._waiters = self._waiters, deque()
        for waiter in waiters:
            self._notify(waiter)
        self.executor.shutdown(wait=False, cancel_futures=True)

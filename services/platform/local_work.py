"""Bounded off-loop execution of complete local application operations."""

import asyncio
import contextvars
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import partial

from .contracts import Fault

_BACKGROUND = contextvars.ContextVar("platform-local-background", default=False)


@contextmanager
def background_scope():
    """Trusted producer context; a scope is not an acquired worker permit."""
    token = _BACKGROUND.set(True)
    try:
        yield
    finally:
        _BACKGROUND.reset(token)


async def settled(local, operation, *args, **kwargs):
    """Retain the caller's resource ownership until its local operation really ends."""
    task = asyncio.create_task(local(operation, *args, **kwargs))
    cancelled = None
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError as error:
            if task.cancelled():
                raise
            if cancelled is None:
                cancelled = error
        except Exception:
            if cancelled is not None:
                raise cancelled from None
            raise
    if cancelled is not None:
        raise cancelled
    return result


class _Capacity:
    """FIFO admission owned until actual completion, usable across caller loops."""

    def __init__(self, capacity):
        self.capacity = capacity
        self.active = 0
        self.closed = False
        self.lock = threading.Lock()
        self._waiters = deque()
        self._idle_waiters = set()

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

    def _release(self):
        with self.lock:
            self.active -= 1
            self._wake_next()
            if not self.active:
                for waiter in self._idle_waiters:
                    self._notify(waiter)

    async def drain(self):
        """Wait for accepted local work without closing callback admission or the store."""
        while True:
            with self.lock:
                if not self.active:
                    return
                waiter = asyncio.get_running_loop().create_future()
                self._idle_waiters.add(waiter)
            try:
                await waiter
            finally:
                with self.lock:
                    self._idle_waiters.discard(waiter)

    def close(self):
        with self.lock:
            self.closed = True
            waiters, self._waiters = self._waiters, deque()
        for waiter in waiters:
            self._notify(waiter)


class LocalWork(_Capacity):
    def __init__(self, workers=4, capacity=16, *, background_capacity=None):
        if background_capacity is not None and (
            type(background_capacity) is not int
            or not 0 < background_capacity < min(workers, capacity)
        ):
            raise ValueError("background_capacity_requires_reserved_worker")
        super().__init__(capacity)
        self._background = _Capacity(background_capacity) if background_capacity else None
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="platform-local")

    async def run(self, operation, *args, **kwargs):
        background = self._background if _BACKGROUND.get() else None
        # Wait outside the shared FIFO. A fourth background operation must not
        # occupy a worker, a shared capacity slot or its interactive queue head.
        if background is not None:
            await background._acquire()
        admitted = False
        try:
            await self._acquire()
            admitted = True
            with self.lock:
                if self.closed:
                    raise Fault("dependency_unavailable", 503)
                context = contextvars.copy_context()
                future = self.executor.submit(context.run, partial(operation, *args, **kwargs))
        except BaseException:
            if admitted:
                self._release()
            if background is not None:
                background._release()
            raise
        # Cancellation cannot release either permit before real completion.
        # No operation is replayed or its transaction split across threads.
        future.add_done_callback(lambda item: self._finished(item, background))
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(lambda item: item.exception() if not item.cancelled() else None)
        try:
            return await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            # A queued operation can still be prevented. Running work keeps both
            # permits until its concurrent future actually completes.
            future.cancel()
            raise

    def _finished(self, future, background=None):
        self._release()
        if background is not None:
            background._release()

    async def run_settled(self, operation, *args, **kwargs):
        return await settled(self.run, operation, *args, **kwargs)

    async def drain(self):
        waiting = [super().drain()]
        if self._background is not None:
            waiting.append(self._background.drain())
        await asyncio.gather(*waiting)

    def close(self):
        super().close()
        if self._background is not None:
            self._background.close()
        self.executor.shutdown(wait=False, cancel_futures=True)

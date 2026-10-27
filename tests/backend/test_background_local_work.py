"""Real worker/producer saturation; no mocked executor or released-running slots."""

import asyncio
import threading
import unittest
from unittest.mock import patch

from services.platform.contracts import Fault
from services.platform.local_work import LocalWork, background_scope
from services.platform.media.background import Background


class BackgroundLocalWorkTests(unittest.IsolatedAsyncioTestCase):
    async def until(self, predicate):
        async with asyncio.timeout(2):
            while not predicate():
                await asyncio.sleep(0.005)

    async def test_three_blocked_producers_keep_interactive_worker_and_running_cancel_holds_permit(
        self,
    ):
        work, owner = LocalWork(background_capacity=3), Background()
        release = threading.Event()
        calls, lock = [], threading.Lock()

        def held(index):
            with lock:
                calls.append(index)
            release.wait(5)

        async def pump(index):
            await work.run(held, index)
            await asyncio.Event().wait()

        draining = None
        try:
            for index in range(4):
                owner.start(str(index), pump, index)
            await self.until(lambda: len(calls) == 3)
            self.assertEqual((work.active, work._background.active, work.capacity), (3, 3, 16))
            self.assertEqual(work.executor._max_workers, 4)
            self.assertFalse(work._waiters)
            self.assertEqual(len(work._background._waiters), 1)
            self.assertEqual(await asyncio.wait_for(work.run(lambda: "renewal"), 1), "renewal")
            self.assertTrue(all(row["restarts"] == 0 for row in owner.view()))

            owner.tasks["0"].cancel()
            await asyncio.gather(owner.tasks["0"], return_exceptions=True)
            self.assertEqual(work._background.active, 3)
            self.assertCountEqual(calls, [0, 1, 2])
            draining = asyncio.create_task(work.drain())
            await asyncio.sleep(0)
            self.assertFalse(draining.done())
            self.assertEqual(await asyncio.wait_for(work.run(lambda: "callback"), 1), "callback")
            owner.tasks["3"].cancel()
            await asyncio.gather(owner.tasks["3"], return_exceptions=True)
            release.set()
            await asyncio.wait_for(draining, 2)
            self.assertEqual((work.active, work._background.active), (0, 0))
            self.assertCountEqual(calls, (0, 1, 2))
        finally:
            release.set()
            await owner.close()
            if draining:
                await draining
            await work.drain()
            work.close()

    async def test_cancelling_shared_capacity_or_executor_queue_returns_background_permit(self):
        for capacity in (2, 4):
            with self.subTest(capacity=capacity):
                work = LocalWork(workers=2, capacity=capacity, background_capacity=1)
                release, lock = threading.Event(), threading.Lock()
                calls, started = [], []

                def held(index, lock=lock, started=started, release=release):
                    with lock:
                        started.append(index)
                    release.wait(5)

                async def background(work=work, calls=calls):
                    with background_scope():
                        await work.run(calls.append, "must not execute")

                foreground = [asyncio.create_task(work.run(held, index)) for index in range(2)]
                task = None
                try:
                    await self.until(lambda started=started: len(started) == 2)
                    with patch.object(
                        work.executor, "submit", wraps=work.executor.submit
                    ) as submit:
                        task = asyncio.create_task(background())
                        await self.until(lambda work=work: work._background.active == 1)
                        await asyncio.sleep(0)
                        self.assertEqual(submit.call_count, int(capacity == 4))
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                        await self.until(lambda work=work: work._background.active == 0)
                        self.assertEqual(work.active, 2)
                        self.assertFalse(calls)
                    release.set()
                    await asyncio.gather(*foreground)
                    await work.drain()
                finally:
                    release.set()
                    await asyncio.gather(
                        *foreground, *([task] if task else []), return_exceptions=True
                    )
                    work.close()

    async def test_close_wakes_both_queues_and_drain_waits_for_real_background_completion(self):
        work = LocalWork(workers=2, capacity=2, background_capacity=1)
        release, entered = threading.Event(), threading.Event()
        calls = []

        def held():
            entered.set()
            release.wait(5)

        async def background(operation, *args):
            with background_scope():
                return await work.run(operation, *args)

        first = asyncio.create_task(background(held))
        tasks, draining = [], None
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            second = asyncio.create_task(work.run(held))
            tasks.append(second)
            await self.until(lambda: work.active == 2)
            waiting = [
                asyncio.create_task(background(calls.append, "bg")),
                asyncio.create_task(work.run(calls.append, "fg")),
            ]
            tasks.extend(waiting)
            await asyncio.sleep(0)
            await asyncio.to_thread(work.close)
            results = await asyncio.wait_for(asyncio.gather(*waiting, return_exceptions=True), 1)
            self.assertTrue(
                all(isinstance(result, Fault) and result.status == 503 for result in results)
            )
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertEqual(work._background.active, 1)
            draining = asyncio.create_task(work.drain())
            await asyncio.sleep(0)
            self.assertFalse(draining.done())
            self.assertFalse(calls)
            release.set()
            await asyncio.wait_for(draining, 2)
            self.assertEqual((work.active, work._background.active), (0, 0))
        finally:
            release.set()
            await asyncio.gather(
                first, *tasks, *([draining] if draining else []), return_exceptions=True
            )
            work.close()

    async def test_remote_await_keeps_context_but_no_background_worker_permit(self):
        work, owner = LocalWork(background_capacity=3), Background()
        remote, remote_waiters = asyncio.Event(), []
        observed = []

        async def callback(index):
            await work.run(observed.append, ("callback", index))

        async def pump(index):
            await work.run(observed.append, ("before", index))
            remote_waiters.append(index)
            await remote.wait()
            # A fresh callback task inherits BG after the remote wait. This is
            # context inheritance, not a claim of exercising the module bridge.
            await asyncio.create_task(callback(index))
            await asyncio.Event().wait()

        try:
            for index in range(3):
                owner.start(str(index), pump, index)
            await self.until(lambda: len(remote_waiters) == 3)
            self.assertEqual((work.active, work._background.active), (0, 0))
            self.assertEqual(
                await asyncio.wait_for(work.run(lambda: "interactive"), 1), "interactive"
            )
            with background_scope():
                await asyncio.wait_for(work.run(observed.append, ("fourth", 3)), 1)
            remote.set()
            await self.until(lambda: sum(label == "callback" for label, _ in observed) == 3)
            self.assertEqual((work.active, work._background.active), (0, 0))
            await owner.close()
            # Scope was reset; a closed background gate cannot affect this root.
            work._background.close()
            self.assertEqual(await work.run(lambda: "unmarked"), "unmarked")
        finally:
            remote.set()
            await owner.close()
            await work.drain()
            work.close()

    async def test_submission_failure_returns_both_permits_without_retry(self):
        work = LocalWork(background_capacity=3)
        try:
            with background_scope():
                with (
                    patch.object(
                        work.executor, "submit", side_effect=RuntimeError("synthetic submit")
                    ),
                    self.assertRaises(RuntimeError),
                ):
                    await work.run(lambda: None)
                self.assertEqual((work.active, work._background.active), (0, 0))
                self.assertEqual(await work.run(lambda: "next"), "next")
        finally:
            work.close()

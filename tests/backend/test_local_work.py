import asyncio
import contextvars
import threading
import unittest
from unittest.mock import patch

from services.platform.contracts import Fault
from services.platform.local_work import LocalWork


class LocalWorkTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_limit_waits_without_blocking_loop_or_replaying_work(self):
        work = LocalWork()
        release, four_running = threading.Event(), threading.Event()
        lock = threading.Lock()
        calls, running, peak = [], 0, 0

        def operation(identity):
            nonlocal running, peak
            with lock:
                calls.append(identity)
                running += 1
                peak = max(peak, running)
                if running == 4:
                    four_running.set()
            try:
                release.wait(5)
                return identity
            finally:
                with lock:
                    running -= 1

        tasks = []
        try:
            with patch.object(work.executor, "submit", wraps=work.executor.submit) as submit:
                tasks = [asyncio.create_task(work.run(operation, i)) for i in range(24)]
                self.assertTrue(await asyncio.to_thread(four_running.wait, 2))
                await asyncio.sleep(0)
                self.assertEqual(work.active, 16)
                self.assertEqual(submit.call_count, 16)
                self.assertEqual(len(calls), 4)
                self.assertFalse(any(task.done() for task in tasks))
                async with asyncio.timeout(0.5):
                    await asyncio.sleep(0)
                release.set()
                self.assertEqual(await asyncio.wait_for(asyncio.gather(*tasks), 3), list(range(24)))
                self.assertEqual(submit.call_count, 24)
            self.assertCountEqual(calls, range(24))
            self.assertEqual(peak, 4)
            self.assertEqual(work.active, 0)
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            work.close()

    async def test_cancelled_capacity_waiter_never_submits_and_survivors_keep_fifo(self):
        work = LocalWork(workers=1, capacity=1)
        entered, release = threading.Event(), threading.Event()
        calls = []
        identity = contextvars.ContextVar("local-work-test-identity", default=None)

        def first():
            entered.set()
            release.wait(5)

        def operation(label):
            calls.append((label, identity.get()))
            return label

        first_task = asyncio.create_task(work.run(first))
        pending = []
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            with patch.object(work.executor, "submit", wraps=work.executor.submit) as submit:
                for label in ("cancelled", "second", "third"):
                    identity.set(label)
                    pending.append(asyncio.create_task(work.run(operation, label)))
                    await asyncio.sleep(0)
                pending[0].cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await pending[0]
                identity.set("newcomer")
                pending.append(asyncio.create_task(work.run(operation, "newcomer")))
                await asyncio.sleep(0)
                self.assertEqual(submit.call_count, 0)
                release.set()
                await first_task
                self.assertEqual(
                    await asyncio.wait_for(asyncio.gather(*pending[1:]), 2),
                    ["second", "third", "newcomer"],
                )
                self.assertEqual(submit.call_count, 3)
            self.assertEqual(calls, [(name, name) for name in ("second", "third", "newcomer")])
            self.assertEqual(work.active, 0)
        finally:
            release.set()
            await asyncio.gather(first_task, *pending, return_exceptions=True)
            work.close()

    async def test_close_from_another_thread_wakes_every_waiter_and_cancels_queued_work(self):
        work = LocalWork(workers=1, capacity=2)
        entered, release = threading.Event(), threading.Event()
        calls = []

        def first():
            entered.set()
            release.wait(5)
            return "finished"

        first_task = asyncio.create_task(work.run(first))
        queued = None
        pending = []
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            queued = asyncio.create_task(work.run(calls.append, "queued"))
            await asyncio.sleep(0)
            self.assertEqual(work.active, 2)
            pending = [asyncio.create_task(work.run(calls.append, i)) for i in range(4)]
            await asyncio.sleep(0)
            await asyncio.to_thread(work.close)
            results = await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 1)
            for result in results:
                self.assertIsInstance(result, Fault)
                self.assertEqual((result.code, result.status), ("dependency_unavailable", 503))
            with self.assertRaises(asyncio.CancelledError):
                await queued
            self.assertEqual(work.active, 1)
            self.assertFalse(first_task.done())
            self.assertEqual(calls, [])
            with self.assertRaises(Fault):
                await work.run(calls.append, "closed")
            work.close()
            release.set()
            self.assertEqual(await asyncio.wait_for(first_task, 2), "finished")
            self.assertEqual(work.active, 0)
            self.assertEqual(calls, [])
        finally:
            release.set()
            await asyncio.gather(
                first_task,
                *([queued] if queued is not None else []),
                *pending,
                return_exceptions=True,
            )
            work.close()

    async def test_cancellation_after_notification_does_not_submit_or_lose_capacity(self):
        work = LocalWork(workers=1, capacity=1)
        entered, release = threading.Event(), threading.Event()
        calls = []

        def held():
            entered.set()
            release.wait(5)

        first = asyncio.create_task(work.run(held))
        pending = []
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            pending = [
                asyncio.create_task(work.run(calls.append, name)) for name in ("cancel", "next")
            ]
            await asyncio.sleep(0)
            wake = work._wake
            cancelled = False

            def cancel_after_wake(waiter):
                nonlocal cancelled
                wake(waiter)
                if not cancelled:
                    cancelled = True
                    pending[0].cancel()

            # Notify the head, then cancel it before its task can resume admission.
            with patch.object(work, "_wake", side_effect=cancel_after_wake):
                release.set()
                await first
                with self.assertRaises(asyncio.CancelledError):
                    await pending[0]
                await asyncio.wait_for(pending[1], 2)
            self.assertTrue(cancelled)
            self.assertEqual(calls, ["next"])
            self.assertEqual(work.active, 0)
        finally:
            release.set()
            await asyncio.gather(first, *pending, return_exceptions=True)
            work.close()

    async def test_close_after_notification_prevents_submission(self):
        work = LocalWork(workers=1, capacity=1)
        entered, release = threading.Event(), threading.Event()
        calls = []

        def held():
            entered.set()
            release.wait(5)

        first = asyncio.create_task(work.run(held))
        pending = []
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            pending = [asyncio.create_task(work.run(calls.append, i)) for i in range(3)]
            await asyncio.sleep(0)
            wake = work._wake

            def close_after_wake(waiter):
                wake(waiter)
                if not work.closed:
                    work.close()

            with patch.object(work, "_wake", side_effect=close_after_wake):
                release.set()
                await first
                results = await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True), 2
                )
            self.assertTrue(all(isinstance(result, Fault) for result in results))
            self.assertEqual(calls, [])
            self.assertEqual(work.active, 0)
        finally:
            release.set()
            await asyncio.gather(first, *pending, return_exceptions=True)
            work.close()

    async def test_operation_failure_releases_capacity_without_retry(self):
        work = LocalWork(workers=1, capacity=1)
        entered, release = threading.Event(), threading.Event()
        calls = []
        problem = ValueError("synthetic operation failure")

        def failing():
            calls.append("failing")
            entered.set()
            release.wait(5)
            raise problem

        first = asyncio.create_task(work.run(failing))
        second = None
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            second = asyncio.create_task(work.run(calls.append, "next"))
            await asyncio.sleep(0)
            release.set()
            with self.assertRaises(ValueError) as caught:
                await first
            self.assertIs(caught.exception, problem)
            await asyncio.wait_for(second, 2)
            self.assertEqual(calls, ["failing", "next"])
            self.assertEqual(work.active, 0)
        finally:
            release.set()
            await asyncio.gather(
                first, *([second] if second is not None else []), return_exceptions=True
            )
            work.close()

    async def test_executor_submission_failure_returns_capacity(self):
        work = LocalWork(workers=1, capacity=1)
        try:
            with patch.object(
                work.executor, "submit", side_effect=RuntimeError("synthetic submit")
            ):
                with self.assertRaises(RuntimeError):
                    await work.run(lambda: None)
            self.assertEqual(work.active, 0)
            self.assertEqual(await asyncio.wait_for(work.run(lambda: "next"), 1), "next")
        finally:
            work.close()

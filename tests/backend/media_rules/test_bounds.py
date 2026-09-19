"""Budgets, concurrency and lifecycle: the limits that keep one bad policy from eating the server.

Three properties are checked against real resources rather than by mocking:

* at most two requests are in flight per evaluator instance, the third gets ``busy`` immediately and
  the capacity comes back afterwards — no hidden queue, no slow request blocking a fast one;
* a request that spends its whole 5-second budget reports ``evaluation_timeout`` while a field that
  is over the 64 KiB projection budget reports ``input_too_large``, and neither is reported as a
  normal skip;
* closing the evaluator refuses new calls and leaves no child process behind.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from unittest import mock

try:
    from ._fixtures import document, group, metadata, rule, text_policy
except ImportError:  # narrow discovery: this directory is the top-level start directory
    from _fixtures import document, group, metadata, rule, text_policy

import services.platform.media.rules as rules
import services.platform.media.rules.evaluator as evaluator_module
import services.platform.media.rules.regex_process as regex_process
from services.platform.media.rules import (
    DECISION_DOWNLOAD,
    DECISION_RULE_ERROR,
    DECISION_SKIP,
    FIELD_PROJECTION_MAX_BYTES,
    MAX_CONCURRENT_REQUESTS,
    REASON_BUSY,
    REASON_EVALUATION_TIMEOUT,
    REASON_INPUT_TOO_LARGE,
    REASON_REGEX_TIMEOUT,
    RegexWorker,
    RuleEvaluationError,
    RuleEvaluator,
)

SLOW_WORKER = '''
    """Ready, then answers nothing: only the parent's budgets can end a call to it."""

    import sys
    import time

    sys.stdout.write('{"ready": true}\\n')
    sys.stdout.flush()
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        time.sleep(30)
'''

CATASTROPHIC_PATTERN = r"^(a+)+$"


def oversized_projection():
    """Make one projected field exceed the 64 KiB budget without inventing an invalid record.

    The evaluator looks the projection helper up in its own module, so that binding is the one to
    replace; the report itself still comes from the real decision path.
    """

    return mock.patch.object(
        evaluator_module, "field_values", return_value=("x" * (FIELD_PROJECTION_MAX_BYTES + 1),)
    )


class ConcurrencyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.evaluator = RuleEvaluator()

    async def asyncTearDown(self) -> None:
        await self.evaluator.aclose()

    async def evaluate(self, policy: object, record=None):
        return await self.evaluator.evaluate(
            record if record is not None else metadata(),
            policy,
            accessible=True,
            quality_satisfied=False,
            snapshot_revision="rev-1",
        )

    async def test_two_requests_run_and_the_third_is_busy_without_queueing(self):
        self.assertEqual(MAX_CONCURRENT_REQUESTS, 2)
        policy = text_policy()
        first = asyncio.ensure_future(self.evaluate(policy))
        second = asyncio.ensure_future(self.evaluate(policy))
        await asyncio.sleep(0)
        third = await self.evaluate(policy)
        self.assertEqual(third.decision, DECISION_RULE_ERROR)
        self.assertEqual(third.reason, REASON_BUSY)
        self.assertTrue(third.requires_rule_attention)
        self.assertFalse(third.automatic_enqueue_allowed)
        self.assertEqual(third.trace, ())
        self.assertEqual(third.snapshot_revision, "rev-1")
        self.assertEqual(third.item_key, metadata().item_key)
        results = await asyncio.gather(first, second)
        self.assertEqual([entry.decision for entry in results], [DECISION_SKIP, DECISION_SKIP])

    async def test_capacity_returns_after_both_requests_finish(self):
        policy = text_policy()
        await asyncio.gather(self.evaluate(policy), self.evaluate(policy))
        recovered = await self.evaluate(policy)
        self.assertEqual(recovered.decision, DECISION_SKIP)
        self.assertEqual(recovered.reason, "blacklist_match")

    async def test_validate_policy_reports_busy_as_an_error_not_as_a_decision(self):
        policy = text_policy()
        first = asyncio.ensure_future(self.evaluate(policy))
        second = asyncio.ensure_future(self.evaluate(policy))
        await asyncio.sleep(0)
        with self.assertRaises(RuleEvaluationError) as caught:
            await self.evaluator.validate_policy(policy)
        self.assertEqual(caught.exception.code, REASON_BUSY)
        await asyncio.gather(first, second)
        validated = await self.evaluator.validate_policy(policy)
        self.assertEqual(validated.revision, 7)

    async def test_capacity_returns_even_when_an_evaluation_fails(self):
        over_budget = document(whitelist=[group("wl", rule("r1", "description", "equals", "x"))])
        with oversized_projection():
            failed = await self.evaluate(over_budget)
        self.assertEqual(failed.reason, REASON_INPUT_TOO_LARGE)
        recovered = await self.evaluate(text_policy())
        self.assertEqual(recovered.decision, DECISION_SKIP)


class ProjectionBudgetTest(unittest.IsolatedAsyncioTestCase):
    """The 64 KiB per-field projection budget.

    The budget is a guard, not a limit a valid record can reach: TS-090 already caps a title at 512
    characters, a description at 65536 and a tag at 128, so no normalized record this block accepts
    can exceed 64 KiB in one projected field. The guard therefore protects a future caller that
    projects something larger, and it is exercised here at the boundary it guards — the projection
    is made oversized on purpose rather than by inventing an impossible record.
    """

    async def asyncSetUp(self) -> None:
        self.evaluator = RuleEvaluator()

    async def asyncTearDown(self) -> None:
        await self.evaluator.aclose()

    async def test_the_largest_valid_description_is_inside_the_budget(self):
        # 65536 characters is the largest description TS-090 accepts, so this is the widest real
        # projection this block can be handed. A rule on text at the very end of it matching proves
        # the description was compared whole: neither truncated nor refused.
        described = "x" * (65536 - 4) + "END!"
        record = metadata(description=described)
        decision = await self.evaluator.evaluate(
            record,
            document(whitelist=[group("wl", rule("r1", "description", "suffix", "END!"))]),
            accessible=True,
            quality_satisfied=False,
            snapshot_revision="rev-1",
        )
        self.assertEqual(decision.decision, DECISION_DOWNLOAD)
        self.assertEqual([entry.result for entry in decision.trace[0].rules], ["matched"])

    async def test_an_over_budget_field_is_refused_instead_of_truncated(self):
        policy = document(whitelist=[group("wl", rule("r1", "description", "equals", "x" * 4096))])
        with oversized_projection():
            decision = await self.evaluator.evaluate(
                metadata(),
                policy,
                accessible=True,
                quality_satisfied=False,
                snapshot_revision="rev-1",
            )
        self.assertEqual(decision.decision, DECISION_RULE_ERROR)
        self.assertEqual(decision.reason, REASON_INPUT_TOO_LARGE)
        self.assertTrue(decision.requires_rule_attention)
        self.assertFalse(decision.automatic_enqueue_allowed)
        self.assertEqual([entry.result for entry in decision.trace[0].rules], ["error"])

    async def test_a_field_exactly_at_the_budget_is_accepted(self):
        # The budget is inclusive: a projection of exactly 64 KiB still evaluates normally.
        at_budget = ("x" * (FIELD_PROJECTION_MAX_BYTES - 4) + "END!",)
        policy = document(whitelist=[group("wl", rule("r1", "description", "suffix", "END!"))])
        with mock.patch.object(evaluator_module, "field_values", return_value=at_budget):
            decision = await self.evaluator.evaluate(
                metadata(),
                policy,
                accessible=True,
                quality_satisfied=False,
                snapshot_revision="rev-1",
            )
        self.assertEqual(decision.decision, DECISION_DOWNLOAD)
        self.assertEqual([entry.result for entry in decision.trace[0].rules], ["matched"])


class RequestBudgetTest(unittest.IsolatedAsyncioTestCase):
    """The 5-second request budget, exercised with a shortened budget and a real slow child."""

    async def test_the_published_request_budget_is_five_seconds(self):
        self.assertEqual(rules.REQUEST_TIMEOUT_SECONDS, 5.0)
        self.assertEqual(rules.CALL_TIMEOUT_SECONDS, 0.05)

    async def test_a_request_that_spends_its_budget_reports_evaluation_timeout(self):
        with SlowWorkerScript() as script:
            evaluator = RuleEvaluator(worker_script=script)
            async with evaluator:
                with mock.patch.object(regex_process, "REQUEST_TIMEOUT_SECONDS", 0.35):
                    decision = await evaluator.evaluate(
                        metadata(),
                        document(whitelist=[group("wl", rule("r1", "title", "regex", "合集"))]),
                        accessible=True,
                        quality_satisfied=False,
                        snapshot_revision="rev-1",
                    )
        self.assertEqual(decision.decision, DECISION_RULE_ERROR)
        self.assertEqual(decision.reason, REASON_EVALUATION_TIMEOUT)
        self.assertTrue(decision.requires_rule_attention)

    async def test_a_shorter_request_budget_converts_a_slow_call_into_a_timeout(self):
        with SlowWorkerScript() as script:
            evaluator = RuleEvaluator(worker_script=script)
            async with evaluator:
                with (
                    mock.patch.object(regex_process, "REQUEST_TIMEOUT_SECONDS", 0.05),
                    mock.patch.object(regex_process, "CALL_TIMEOUT_SECONDS", 0.2),
                ):
                    decision = await evaluator.evaluate(
                        metadata(),
                        document(whitelist=[group("wl", rule("r1", "title", "regex", "合集"))]),
                        accessible=True,
                        quality_satisfied=False,
                        snapshot_revision="rev-1",
                    )
        self.assertEqual(decision.reason, REASON_REGEX_TIMEOUT)

    async def test_the_shortened_budget_does_not_leak_into_the_next_request(self):
        evaluator = RuleEvaluator()
        async with evaluator:
            with mock.patch.object(regex_process, "REQUEST_TIMEOUT_SECONDS", 0.001):
                pass
            decision = await evaluator.evaluate(
                metadata(),
                text_policy(),
                accessible=True,
                quality_satisfied=False,
                snapshot_revision="rev-1",
            )
        self.assertEqual(decision.decision, DECISION_SKIP)


class SlowWorkerScript:
    """Context manager writing a worker that never answers a request."""

    def __init__(self) -> None:
        self._directory = tempfile.TemporaryDirectory(
            prefix="ts098-slow-", ignore_cleanup_errors=True
        )
        self.path = self._directory.name

    def __enter__(self) -> str:
        target = os.path.join(self.path, "slow_worker.py")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(SLOW_WORKER)
        return target

    def __exit__(self, *exc_info: object) -> None:
        self._directory.cleanup()


class RecordingWorkers:
    """Patch the process class so a test can inspect the children a request really created."""

    def __init__(self) -> None:
        self.workers: list[RegexWorker] = []
        real = rules.RegexWorker
        recorded = self.workers

        class Recording(real):  # type: ignore[misc, valid-type]
            def __init__(self, script_path: str | None = None) -> None:
                super().__init__(script_path)
                recorded.append(self)

        self._patch = mock.patch.object(rules, "RegexWorker", Recording)

    def __enter__(self) -> list[RegexWorker]:
        self._patch.start()
        return self.workers

    def __exit__(self, *exc_info: object) -> None:
        self._patch.stop()


class LifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_closing_the_evaluator_refuses_new_calls(self):
        evaluator = RuleEvaluator()
        await evaluator.aclose()
        with self.assertRaises(RuleEvaluationError) as caught:
            await evaluator.validate_policy(document())
        self.assertEqual(caught.exception.code, "closed")
        with self.assertRaises(RuleEvaluationError):
            await evaluator.evaluate(
                metadata(),
                document(),
                accessible=True,
                quality_satisfied=False,
                snapshot_revision="rev-1",
            )

    async def test_the_context_manager_closes_the_evaluator(self):
        async with RuleEvaluator() as evaluator:
            self.assertEqual((await evaluator.validate_policy(document())).revision, 7)
        with self.assertRaises(RuleEvaluationError):
            await evaluator.validate_policy(document())

    async def test_closing_retires_a_worker_left_by_a_cancelled_request(self):
        with SlowWorkerScript() as script, RecordingWorkers() as workers:
            evaluator = RuleEvaluator(worker_script=script)
            task = asyncio.ensure_future(
                evaluator.evaluate(
                    metadata(),
                    document(whitelist=[group("wl", rule("r1", "title", "regex", "合集"))]),
                    accessible=True,
                    quality_satisfied=False,
                    snapshot_revision="rev-1",
                )
            )
            for _ in range(400):
                if workers:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(workers, "the request must have started its own worker")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await evaluator.aclose()
        for worker in workers:
            self.assertIsNotNone(worker.returncode, "no child may survive a cancelled request")
            self.assertFalse(worker.alive)

    async def test_a_cancelled_evaluation_does_not_return_a_decision(self):
        with SlowWorkerScript() as script:
            evaluator = RuleEvaluator(worker_script=script)
            async with evaluator:
                task = asyncio.ensure_future(
                    evaluator.evaluate(
                        metadata(),
                        document(whitelist=[group("wl", rule("r1", "title", "regex", "合集"))]),
                        accessible=True,
                        quality_satisfied=False,
                        snapshot_revision="rev-1",
                    )
                )
                await asyncio.sleep(0.15)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                # The evaluator still works afterwards: a cancelled request released its slot.
                recovered = await evaluator.evaluate(
                    metadata(),
                    text_policy(),
                    accessible=True,
                    quality_satisfied=False,
                    snapshot_revision="rev-1",
                )
        self.assertEqual(recovered.decision, DECISION_SKIP)

    async def test_a_catastrophic_expression_leaves_no_running_child(self):
        with RecordingWorkers() as workers:
            evaluator = RuleEvaluator()
            async with evaluator:
                decision = await evaluator.evaluate(
                    metadata(title="a" * 40 + "b"),
                    document(
                        whitelist=[group("wl", rule("r1", "title", "regex", CATASTROPHIC_PATTERN))]
                    ),
                    accessible=True,
                    quality_satisfied=False,
                    snapshot_revision="rev-1",
                )
        self.assertEqual(decision.reason, REASON_REGEX_TIMEOUT)
        self.assertTrue(workers)
        for worker in workers:
            self.assertIsNotNone(worker.returncode)
            self.assertFalse(worker.alive)

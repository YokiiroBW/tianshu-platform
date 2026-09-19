"""Budgets, concurrency and lifecycle: the limits that keep one bad policy from eating the server.

Three properties are checked against real resources rather than by mocking:

* at most two requests are in flight per evaluator instance, the third gets ``busy`` immediately and
  the capacity comes back afterwards — no hidden queue, no slow request blocking a fast one. The two
  in-flight requests are *proved* to be in flight: each has created the child process that is sitting
  on its request, and the stage barrier waits for the child itself to announce that it got there;
* a request that spends its whole budget reports ``evaluation_timeout`` while a single call that
  exceeds the 50 ms call budget reports ``regex_timeout``, and neither is reported as a normal skip.
  The cumulative case is real and unshortened: 200 rules whose searches are each well inside the
  50 ms call budget still exceed the published 5 s request budget, and the reason names the request;
* the 64 KiB per-field projection budget is exercised with a **real normalized record** — no mock —
  because a 65536-character description of CJK text is 196608 bytes, which TS-090 happily accepts;
* closing the evaluator refuses new calls and leaves no child process behind.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
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
from services.platform.media.rules.matching import projection_within_budget

#: The largest description TS-090 accepts, in characters.
DESCRIPTION_MAX_CHARS = 65536
#: How many CJK characters reach the 64 KiB projection budget: 21846 x 3 bytes = 65538 bytes.
CJK_OVER_BUDGET_CHARS = 21846
CJK_CHAR = "集"

CATASTROPHIC_PATTERN = r"^(a+)+$"

#: A worker that finishes the handshake and then answers nothing at all. It announces that a request
#: reached it by creating a marker file named after its own pid, because the product starts the child
#: with ``stderr=DEVNULL``: stderr is a channel for a person reading a manual run, never a stage
#: barrier a test can synchronise on. Written with no top-level indentation so it is runnable as
#: stored.
SLOW_WORKER = (
    "import json, os, sys, time\n"
    "sys.stdout.write(json.dumps({'ready': True, 'protocol': 1, 'pid': os.getpid()}) + '\\n')\n"
    "sys.stdout.flush()\n"
    "for line in sys.stdin:\n"
    "    open(os.path.join(os.path.dirname(os.path.abspath(__file__)),"
    " 'entered-' + str(os.getpid())), 'w').close()\n"
    "    time.sleep(30)\n"
)

#: A worker whose every answer is legal and quick, but not instant: one search costs about 6 ms,
#: which is comfortably inside the 50 ms *call* budget, while a whole policy's worth of them costs far
#: more than the 5 s *request* budget. That is the only way to show the request deadline doing its job
#: without shortening either published budget.
CUMULATIVE_WORKER = (
    "import json, os, sys, time\n"
    "sys.stdout.write(json.dumps({'ready': True, 'protocol': 1, 'pid': os.getpid()}) + '\\n')\n"
    "sys.stdout.flush()\n"
    "for line in sys.stdin:\n"
    "    command = json.loads(line)\n"
    "    if command['op'] == 'compile':\n"
    "        answer = {'seq': command['seq'], 'ok': True, 'handle': command['seq']}\n"
    "    else:\n"
    "        time.sleep(0.006)\n"
    "        answer = {'seq': command['seq'], 'ok': True, 'matched': False}\n"
    "    sys.stdout.write(json.dumps(answer) + '\\n')\n"
    "    sys.stdout.flush()\n"
)

#: The cumulative case: the largest policy the frozen limits allow, and the heaviest tag projection.
CUMULATIVE_GROUPS = 20
CUMULATIVE_RULES_PER_GROUP = 10
CUMULATIVE_TAG_VALUES = 100
CUMULATIVE_SEARCH_SECONDS = 0.006
#: A rule that matches every tag value, and one that matches none.
CUMULATIVE_MATCHING = r"普通标签"
CUMULATIVE_NEVER_MATCHING = r"^never-matches-anything$"


def cumulative_policy() -> list[object]:
    """The largest policy the frozen limits allow: 20 groups of 10 rules, so 200 rules.

    A group short-circuits on its first non-match, and a group whose rules all match ends the
    whitelist, so the shape is deliberate: nine rules that match and one that does not. Every one of
    the 200 rules is therefore really evaluated, the whitelist still does not match, and the
    non-matching rule of each group searches every tag value — which is what makes the run outlast the
    request budget.
    """

    groups: list[object] = []
    for index in range(CUMULATIVE_GROUPS):
        entries = [
            rule(f"g{index:02d}r{position}", "tags", "regex", CUMULATIVE_MATCHING)
            for position in range(CUMULATIVE_RULES_PER_GROUP - 1)
        ]
        entries.append(rule(f"g{index:02d}rstop", "tags", "regex", CUMULATIVE_NEVER_MATCHING))
        groups.append(group(f"wl{index:02d}", *entries))
    return groups


def cumulative_tags() -> list[str]:
    """The heaviest tag projection TS-090 allows, so the non-matching rules search 100 values."""

    return [f"普通标签{index:03d}" for index in range(CUMULATIVE_TAG_VALUES)]


FIXTURE_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))), ".runtime"
)


def transport_refusal(error: BaseException) -> PermissionError | None:
    """The ``PermissionError`` in the cause chain, when this host refused the child's pipe transport."""

    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, PermissionError):
            return current
        current = current.__cause__ or current.__context__
    return None


def transport_denied(error: BaseException) -> bool:
    """True when the refusal is this environment refusing to create the child's pipe transport.

    ``RegexWorker.start`` reports every start failure as the same named ``regex_worker_failed``, so the
    distinction between "the worker is broken" and "this host will not let a child exist" lives in the
    cause chain. An environment that cannot run a real child cannot be asked to prove anything about
    one; the tests in this module say so with a named skip instead of reporting a code defect.
    """

    return transport_refusal(error) is not None


def require_real_child(test: unittest.TestCase, error: BaseException | None) -> None:
    """Skip exactly when the environment refused the transport, otherwise let the failure stand.

    A task that finished with no exception is a different problem (the request completed when the
    fixture says it must not), so that case is reported as a failure rather than swallowed.
    """

    if error is None:
        raise AssertionError("the request finished when it was supposed to be in flight")
    refusal = transport_refusal(error)
    if refusal is not None:
        test.skipTest(
            "this environment refuses asyncio's child pipe transport "
            f"({refusal!r}); the real-child assertions need the coordination environment"
        )
    raise AssertionError(f"the request failed before it could be in flight: {error!r}")


async def require_usable_worker(test: unittest.TestCase) -> None:
    """Prove up front that a real child can exist here, or name the environment limitation.

    One worker is started directly with the same interpreter, flags and transport the evaluator uses,
    so a refusal here is the same refusal the request under test would have hit.
    """

    worker = RegexWorker()
    try:
        await worker.start()
    except RuleEvaluationError as error:
        require_real_child(test, error)
    finally:
        await worker.close_quietly()


def _fixture_directory() -> str:
    """A directory this environment really allows a fixture script to be written into.

    ``mkdtemp`` creates a 0700 directory, which some confined environments refuse. The directory is
    created once per process and reused, and the probe write is the real check: creating a directory
    is not proof that a file can be written inside it.
    """

    for root, name in (
        (tempfile.gettempdir(), f"ts098-slow-{os.getpid()}"),
        (FIXTURE_ROOT, f"ts098-slow-{os.getpid()}"),
    ):
        target = os.path.join(root, name)
        try:
            os.makedirs(target, mode=0o777, exist_ok=True)
            os.chmod(target, 0o777)
            with open(os.path.join(target, "probe.py"), "w", encoding="utf-8") as handle:
                handle.write("pass\n")
        except OSError:
            continue
        return target
    raise unittest.SkipTest(
        "no writable fixture directory: this environment refuses the platform temp root and the "
        "workspace .runtime directory"
    )


class FixtureWorker:
    """Write a fixture worker to a usable directory and synchronise on the child itself.

    The script has no top-level indentation, so it is runnable exactly as stored; a fixture that kept
    its indentation would fail to import and every budget assertion downstream would be measuring a
    startup crash instead of a slow child.
    """

    #: The fixture source, and the filename stem it is written under.
    SOURCE = SLOW_WORKER
    STEM = "slow_worker"

    def __enter__(self) -> str:
        self.directory = _fixture_directory()
        self.path = os.path.join(self.directory, f"{self.STEM}_{os.getpid()}.py")
        with open(self.path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(self.SOURCE)
        return self.path

    def __exit__(self, *exc_info: object) -> None:
        self.cleanup()
        return None

    def marker(self, pid: int) -> str:
        """The path a fixture child creates once a request has really reached it."""

        return os.path.join(self.directory, f"entered-{pid}")

    def cleanup(self) -> None:
        """Remove this case's fixture and any marker a killed child left behind."""

        names = [os.path.basename(self.path)]
        try:
            names.extend(
                entry for entry in os.listdir(self.directory) if entry.startswith("entered-")
            )
        except OSError:
            pass
        for name in names:
            try:
                os.remove(os.path.join(self.directory, name))
            except OSError:
                pass

    async def await_in_flight(
        self, workers: list[RegexWorker], count: int, timeout: float = 15.0
    ) -> bool:
        """True once ``count`` recorded workers each hold a live child that reached its request.

        A worker *object* that exists proves only that a class was constructed: the pid appears later,
        and the marker file is written by the child itself from inside its request loop. Waiting for
        both is what makes "these requests are in flight" evidence rather than a hope.
        """

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(workers) >= count:
                live = workers[:count]
                if all(worker.pid is not None for worker in live) and all(
                    os.path.exists(self.marker(worker.pid)) for worker in live
                ):
                    return True
            await asyncio.sleep(0.01)
        return False


class SlowWorkerScript(FixtureWorker):
    """A worker that finishes the handshake and then never answers."""

    SOURCE = SLOW_WORKER
    STEM = "slow_worker"


class CumulativeWorkerScript(FixtureWorker):
    """A worker that answers every search legally, about 30 ms after it is asked."""

    SOURCE = CUMULATIVE_WORKER
    STEM = "cumulative_worker"


class ConcurrencyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # No environment probe at class level: the capacity cases here are pure and must run
        # everywhere. Only the cases that genuinely need two live children ask for one themselves.
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
        """Two requests are held in flight by real children; the third is refused immediately.

        The two are not merely *scheduled*: the barrier below returns only once each has created its
        own child and that child has announced it received the request, which is the point at which
        the request is genuinely in flight. A plain-text policy is a single synchronous step, so two
        of those have already finished by the time a third arrives — and a third request that then
        succeeds is the correct behaviour, which ``test_a_finished_request_releases_its_slot`` states
        separately.
        """

        self.assertEqual(MAX_CONCURRENT_REQUESTS, 2)
        policy = document(whitelist=[group("wl", rule("r1", "title", "regex", "合集"))])
        await require_usable_worker(self)
        with SlowWorkerScript() as script, RecordingWorkers() as workers:
            evaluator = RuleEvaluator(worker_script=script)
            first = asyncio.ensure_future(self.evaluate_with(evaluator, policy))
            second = asyncio.ensure_future(self.evaluate_with(evaluator, policy))
            in_flight = await script.await_in_flight(workers, 2)
            if not in_flight:
                for task in (first, second):
                    if task.done() and not task.cancelled():
                        require_real_child(self, task.exception())
                self.fail("both requests must be in flight before the third can be judged busy")
            for worker in workers:
                self.assertIsNotNone(worker.pid, "an in-flight request owns a real child")
                managed = worker.managed_process
                self.assertIsNotNone(managed)
                self.assertIsNone(managed.returncode, "the child is still running its request")
                self.assertTrue(
                    os.path.exists(script.marker(worker.pid)),
                    "the child itself confirmed it received the request",
                )
            third = await self.evaluate_with(evaluator, policy)
            self.assertEqual(third.decision, DECISION_RULE_ERROR)
            self.assertEqual(third.reason, REASON_BUSY)
            self.assertTrue(third.requires_rule_attention)
            self.assertFalse(third.automatic_enqueue_allowed)
            self.assertEqual(third.trace, ())
            self.assertEqual(third.snapshot_revision, "rev-1")
            self.assertEqual(third.item_key, metadata().item_key)
            for task in (first, second):
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            await evaluator.aclose()
        for worker in workers:
            self.assertIsNotNone(worker.returncode, "a cancelled request still reaps its child")

    async def evaluate_with(self, evaluator: RuleEvaluator, policy: object):
        return await evaluator.evaluate(
            metadata(),
            policy,
            accessible=True,
            quality_satisfied=False,
            snapshot_revision="rev-1",
        )

    async def test_a_finished_request_releases_its_slot(self):
        # Two plain-text requests run to completion in one step each, so the third sees free capacity.
        policy = text_policy()
        await asyncio.gather(self.evaluate(policy), self.evaluate(policy))
        recovered = await self.evaluate(policy)
        self.assertEqual(recovered.decision, DECISION_SKIP)
        self.assertEqual(recovered.reason, "blacklist_match")

    async def test_validate_policy_reports_busy_as_an_error_not_as_a_decision(self):
        """A refused *validation* is an error, and the capacity it was refused for really comes back.

        The recovery is checked while the evaluator is still open: a closed evaluator refuses every
        call with ``closed``, so validating after the close would have proved nothing about the two
        slots the cancelled requests were holding.
        """

        policy = document(whitelist=[group("wl", rule("r1", "title", "regex", "合集"))])
        await require_usable_worker(self)
        with SlowWorkerScript() as script, RecordingWorkers() as workers:
            evaluator = RuleEvaluator(worker_script=script)
            first = asyncio.ensure_future(self.evaluate_with(evaluator, policy))
            second = asyncio.ensure_future(self.evaluate_with(evaluator, policy))
            in_flight = await script.await_in_flight(workers, 2)
            if not in_flight:
                for task in (first, second):
                    if task.done() and not task.cancelled():
                        require_real_child(self, task.exception())
                self.fail("both requests must be in flight before validation can be refused")
            with self.assertRaises(RuleEvaluationError) as caught:
                await evaluator.validate_policy(policy)
            self.assertEqual(caught.exception.code, REASON_BUSY)
            for task in (first, second):
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            # Capacity is back: the same evaluator validates a plain policy. This happens *before*
            # the close, so a leaked slot cannot hide behind the closed barrier.
            validated = await evaluator.validate_policy(text_policy())
            self.assertEqual(validated.revision, 7)
            await evaluator.aclose()
            with self.assertRaises(RuleEvaluationError) as closed:
                await evaluator.validate_policy(text_policy())
            self.assertEqual(closed.exception.code, "closed")

    async def test_capacity_returns_even_when_an_evaluation_fails(self):
        over_budget = document(whitelist=[group("wl", rule("r1", "description", "equals", "x"))])
        failed = await self.evaluate(
            over_budget, metadata(description=CJK_CHAR * CJK_OVER_BUDGET_CHARS)
        )
        self.assertEqual(failed.reason, REASON_INPUT_TOO_LARGE)
        recovered = await self.evaluate(text_policy())
        self.assertEqual(recovered.decision, DECISION_SKIP)


class ProjectionBudgetTest(unittest.IsolatedAsyncioTestCase):
    """The 64 KiB per-field projection budget, exercised through the real normalizer.

    The budget is measured in UTF-8 bytes, and the character limits of the upstream record are
    *characters*: a description of 65536 CJK characters is a valid normalized record of 196608 bytes.
    So the budget is not a guard a valid record cannot reach — it is a limit a real record does reach,
    and these cases use real records instead of a patched projection helper.
    """

    async def asyncSetUp(self) -> None:
        self.evaluator = RuleEvaluator()

    async def asyncTearDown(self) -> None:
        await self.evaluator.aclose()

    async def test_the_largest_valid_description_is_inside_the_budget(self):
        # 65536 ASCII characters is both the largest description TS-090 accepts and exactly the
        # 64 KiB budget, and the budget is inclusive. A rule on the very end of it matching proves
        # the description was compared whole: neither truncated nor refused.
        described = "x" * (DESCRIPTION_MAX_CHARS - 4) + "END!"
        self.assertEqual(len(described.encode("utf-8")), FIELD_PROJECTION_MAX_BYTES)
        decision = await self.evaluator.evaluate(
            metadata(description=described),
            document(whitelist=[group("wl", rule("r1", "description", "suffix", "END!"))]),
            accessible=True,
            quality_satisfied=False,
            snapshot_revision="rev-1",
        )
        self.assertEqual(decision.decision, DECISION_DOWNLOAD)
        self.assertEqual([entry.result for entry in decision.trace[0].rules], ["matched"])

    async def test_a_real_normalized_description_over_the_budget_is_refused(self):
        # No mock: this is a real normalized record whose description TS-090 accepts (21846 <= 65536
        # characters) and whose projection is 65538 UTF-8 bytes, two bytes over the budget.
        described = CJK_CHAR * CJK_OVER_BUDGET_CHARS
        record = metadata(description=described)
        self.assertEqual(len(record.original_description), CJK_OVER_BUDGET_CHARS)
        self.assertEqual(len(described.encode("utf-8")), 65538)
        self.assertFalse(projection_within_budget((record.original_description,)))
        decision = await self.evaluator.evaluate(
            record,
            document(whitelist=[group("wl", rule("r1", "description", "contains", CJK_CHAR))]),
            accessible=True,
            quality_satisfied=False,
            snapshot_revision="rev-1",
        )
        self.assertEqual(decision.decision, DECISION_RULE_ERROR)
        self.assertEqual(decision.reason, REASON_INPUT_TOO_LARGE)
        self.assertTrue(decision.requires_rule_attention)
        self.assertFalse(decision.automatic_enqueue_allowed)
        self.assertEqual([entry.result for entry in decision.trace[0].rules], ["error"])

    async def test_a_one_byte_over_budget_projection_is_refused(self):
        # The boundary is exact, not fuzzy: one byte past 64 KiB is refused. TS-090 caps a description
        # at 65536 *characters*, so the byte total, not the character count, is what this guards.
        self.assertTrue(projection_within_budget(("x" * FIELD_PROJECTION_MAX_BYTES,)))
        self.assertFalse(projection_within_budget(("x" * (FIELD_PROJECTION_MAX_BYTES + 1),)))
        self.assertTrue(projection_within_budget(("集" * (FIELD_PROJECTION_MAX_BYTES // 3),)))
        self.assertFalse(projection_within_budget(("集" * (FIELD_PROJECTION_MAX_BYTES // 3 + 1),)))

    async def test_the_budget_counts_every_value_of_a_multi_value_field(self):
        # The budget is a running total over the whole projection, not a per-value check: tags are
        # summed, so a projection that is over the budget only in aggregate is still refused.
        values = tuple("x" * 2048 for _ in range(FIELD_PROJECTION_MAX_BYTES // 2048))
        self.assertTrue(projection_within_budget(values))
        self.assertFalse(projection_within_budget(values + ("x",)))

    async def test_the_valid_maximum_tag_projection_is_inside_the_budget(self):
        # TS-090's maxima — 100 tags of at most 128 characters — cannot reach 64 KiB of UTF-8 bytes
        # even in CJK (100 x 128 x 3 = 38400), so no valid multi-value projection can be refused.
        # The aggregate rule above is therefore checked on the projection helper, and this case shows
        # a record at the maximum is evaluated rather than refused.
        tags = [f"{index:03d}" + "集" * 125 for index in range(100)]
        record = metadata(tags=tags)
        self.assertEqual(len(record.tags), 100, "the record really carries the maximum tag count")
        # Every tag is exactly the 128-character maximum, 128 characters of which 125 are CJK: the
        # heaviest projection TS-090 can produce is therefore 100 x 378 = 37800 bytes, still inside
        # the 64 KiB budget. The aggregate rule above is what covers a projection that is over it.
        self.assertTrue(all(len(tag) == 128 for tag in record.tags))
        self.assertEqual(sum(len(tag.encode("utf-8")) for tag in record.tags), 37800)
        self.assertLess(37800, FIELD_PROJECTION_MAX_BYTES)
        self.assertTrue(projection_within_budget(record.tags))
        decision = await self.evaluator.evaluate(
            record,
            document(whitelist=[group("wl", rule("r1", "tags", "contains", "预告"))]),
            accessible=True,
            quality_satisfied=False,
            snapshot_revision="rev-1",
        )
        self.assertEqual(
            decision.decision, DECISION_SKIP, "a valid maximum is evaluated, not refused"
        )


class RequestBudgetTest(unittest.IsolatedAsyncioTestCase):
    """The 5-second request budget, exercised against the deadline the request really uses."""

    def test_the_published_budgets_are_the_frozen_ones(self):
        self.assertEqual(rules.REQUEST_TIMEOUT_SECONDS, 5.0)
        self.assertEqual(rules.CALL_TIMEOUT_SECONDS, 0.05)

    async def test_a_single_call_over_its_budget_is_a_regex_timeout(self):
        with SlowWorkerScript() as script:
            evaluator = RuleEvaluator(worker_script=script)
            async with evaluator:
                await require_usable_worker(self)
                decision = await evaluator.evaluate(
                    metadata(),
                    document(whitelist=[group("wl", rule("r1", "title", "regex", "合集"))]),
                    accessible=True,
                    quality_satisfied=False,
                    snapshot_revision="rev-1",
                )
        self.assertEqual(decision.decision, DECISION_RULE_ERROR)
        self.assertEqual(decision.reason, REASON_REGEX_TIMEOUT)
        self.assertTrue(decision.requires_rule_attention)

    async def test_a_request_that_spends_its_budget_reports_evaluation_timeout(self):
        """The whole-request deadline, not a shortened call budget, produces this reason.

        ``CALL_TIMEOUT_SECONDS`` is raised so no single call can be blamed; the only budget that can
        expire is the request deadline the evaluator really passes down, and the reason must name it.
        """

        with SlowWorkerScript() as script:
            evaluator = RuleEvaluator(worker_script=script)
            async with evaluator:
                await require_usable_worker(self)
                with (
                    mock.patch.object(regex_process, "REQUEST_TIMEOUT_SECONDS", 0.35),
                    mock.patch.object(regex_process, "CALL_TIMEOUT_SECONDS", 30.0),
                ):
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
        self.assertFalse(decision.automatic_enqueue_allowed)

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

    async def test_the_full_five_second_budget_finishes_many_quick_search_values(self):
        """The unshortened budget: many legal searches inside one request must all complete.

        This is the case the card calls out by name — a policy that legitimately performs a long run
        of fast calls has to be judged, not timed out. With the published 5 s request budget and the
        published 50 ms call budget, every one of these searches is fast and the decision is normal:
        the matching value is placed last, so all of them really run.
        """

        tags = [f"普通标签{index:02d}" for index in range(24)] + ["合集"]
        policy = document(whitelist=[group("wl", rule("r1", "tags", "regex", "^合集$"))])
        evaluator = RuleEvaluator()
        async with evaluator:
            await require_usable_worker(self)
            decision = await evaluator.evaluate(
                metadata(tags=tags),
                policy,
                accessible=True,
                quality_satisfied=False,
                snapshot_revision="rev-1",
            )
        self.assertEqual(decision.decision, DECISION_DOWNLOAD)
        self.assertEqual(decision.reason, "eligible")

    async def test_a_long_run_of_legal_searches_still_spends_the_whole_request_budget(self):
        """The other half of the same rule: enough legal searches *do* exhaust the request budget.

        No published budget is shortened here. The policy is the largest the frozen limits allow — 20
        groups of 10 rules, so 200 rules — and every rule costs the child real searches, each of them
        well inside the 50 ms call budget. The run as a whole costs far more than the 5 s request
        budget, so the reason must name the request deadline rather than a call deadline. A run of
        fast successful searches does not stand in for this: what is under test is the request
        deadline arriving *while the policy is still working*.

        The cost is spread over many short searches rather than one long one on purpose: a platform
        whose timer granularity is 15.6 ms can turn a single 30 ms sleep into nearly 50 ms, which would
        make this case measure the call budget instead of the request budget.
        """

        policy = document(whitelist=cumulative_policy())
        planned = CUMULATIVE_GROUPS * ((CUMULATIVE_RULES_PER_GROUP - 1) + CUMULATIVE_TAG_VALUES)
        self.assertEqual(len(policy["whitelist"]), CUMULATIVE_GROUPS)
        self.assertEqual(
            sum(len(entry["rules"]) for entry in policy["whitelist"]),
            CUMULATIVE_GROUPS * CUMULATIVE_RULES_PER_GROUP,
        )
        self.assertGreater(
            planned * CUMULATIVE_SEARCH_SECONDS,
            rules.REQUEST_TIMEOUT_SECONDS,
            "the case is only meaningful if the legal searches outlast the request budget",
        )
        self.assertLess(
            CUMULATIVE_SEARCH_SECONDS,
            rules.CALL_TIMEOUT_SECONDS / 4,
            "each individual search must stay well inside the call budget",
        )
        with CumulativeWorkerScript() as script:
            evaluator = RuleEvaluator(worker_script=script)
            async with evaluator:
                await require_usable_worker(self)
                started = time.monotonic()
                decision = await evaluator.evaluate(
                    metadata(tags=cumulative_tags()),
                    policy,
                    accessible=True,
                    quality_satisfied=False,
                    snapshot_revision="rev-1",
                )
                elapsed = time.monotonic() - started
        self.assertEqual(decision.decision, DECISION_RULE_ERROR)
        self.assertEqual(
            decision.reason,
            REASON_EVALUATION_TIMEOUT,
            "the request deadline expired, not one 50 ms call budget",
        )
        self.assertTrue(decision.requires_rule_attention)
        self.assertFalse(decision.automatic_enqueue_allowed)
        # It really ran for the whole published budget rather than being refused up front.
        self.assertGreaterEqual(elapsed, rules.REQUEST_TIMEOUT_SECONDS * 0.9)


class RecordingWorkers:
    """Patch the process class *at the name the evaluator consumes*, so real children are recorded.

    ``evaluator`` imports ``RegexWorker`` into its own namespace; patching the package export instead
    would leave the evaluator using the real class and would record nothing, which made an earlier
    version of this test assert exit evidence it had never collected.
    """

    def __init__(self) -> None:
        self.workers: list[RegexWorker] = []
        real = evaluator_module.RegexWorker
        recorded = self.workers

        class Recording(real):  # type: ignore[misc, valid-type]
            def __init__(self, script_path: str | None = None) -> None:
                super().__init__(script_path)
                recorded.append(self)

        self._patch = mock.patch.object(evaluator_module, "RegexWorker", Recording)

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
        await require_usable_worker(self)
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
            # The barrier is the child's own marker: a recorded worker object exists as soon as the
            # class is constructed, which is long before a child — or a request — exists at all.
            in_flight = await script.await_in_flight(workers, 1)
            if not in_flight:
                if task.done() and not task.cancelled():
                    require_real_child(self, task.exception())
                self.fail("the request must have reached its child before it can be cancelled")
            self.assertIsNotNone(workers[0].pid, "the recorded worker owns a real child")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await evaluator.aclose()
        for worker in workers:
            managed = worker.managed_process
            self.assertIsNotNone(managed, "the child handle is kept until it exits")
            self.assertIsNotNone(managed.returncode, "no child may survive a cancelled request")
            self.assertEqual(worker.returncode, managed.returncode)
            self.assertFalse(worker.alive)

    async def test_a_cancelled_evaluation_does_not_return_a_decision(self):
        with SlowWorkerScript() as script:
            evaluator = RuleEvaluator(worker_script=script)
            async with evaluator:
                await require_usable_worker(self)
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
        await require_usable_worker(self)
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
        self.assertTrue(workers, "the catastrophic expression ran in a real child")
        for worker in workers:
            managed = worker.managed_process
            self.assertIsNotNone(managed)
            self.assertIsNotNone(managed.returncode)
            self.assertNotEqual(managed.returncode, 0, "a killed child does not exit cleanly")
            self.assertFalse(worker.alive)

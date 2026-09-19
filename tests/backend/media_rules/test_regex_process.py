"""The regex child process: real subprocesses, real faults, real exit evidence.

Nothing in this module mocks a timeout or a crash. Every case starts a genuine ``sys.executable -I``
child — either the bundled worker or a small controlled script written to a temporary directory —
and the assertions read the child's own exit code afterwards. That is the only way to show the two
properties the card cares about: a catastrophic expression is really stopped (not merely abandoned
while it keeps burning a core), and a dead or misbehaving worker is refused instead of believed.

The controlled scripts are injected through ``RuleEvaluator(worker_script=...)`` / ``RegexWorker``,
which is the documented fault-injection seam: the production default is the absolute path of the
bundled worker inside this package and is not reachable from a setting, an environment variable or
a request field.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import textwrap
import unittest

try:
    from . import _fixtures as fx
    from ._fixtures import document, group, metadata, rule
except ImportError:  # narrow discovery: this directory is the top-level start directory
    import _fixtures as fx
    from _fixtures import document, group, metadata, rule

from services.platform.media.rules import (
    CALL_TIMEOUT_SECONDS,
    CLEANUP_TIMEOUT_SECONDS,
    DECISION_DOWNLOAD,
    DECISION_RULE_ERROR,
    ERROR_INVALID_REGEX,
    IPC_MAX_REQUEST_BYTES,
    IPC_MAX_RESPONSE_BYTES,
    REASON_INPUT_TOO_LARGE,
    REASON_REGEX_TIMEOUT,
    REASON_REGEX_WORKER_FAILED,
    REQUEST_TIMEOUT_SECONDS,
    STARTUP_TIMEOUT_SECONDS,
    RegexWorker,
    RuleEvaluationError,
    RuleEvaluator,
    RuleValidationError,
    worker_script_path,
)

CATASTROPHIC_PATTERN = r"^(a+)+$"
CATASTROPHIC_TEXT = "a" * 40 + "b"

OVERSIZED_RESPONSE_WORKER = '''
    """Ready, then one response line far beyond the published 16 KiB limit."""

    import sys

    sys.stdout.write('{"ready": true}\\n')
    sys.stdout.write('{"seq": 1, "ok": true, "matched": false, "pad": "' + "p" * 40000 + '"}\\n')
    sys.stdout.flush()
    sys.stdin.readline()
'''


def refused(call) -> RuleValidationError:
    try:
        call()
    except RuleValidationError as error:
        return error
    raise AssertionError("expected RuleValidationError")


class ControlledScripts:
    """Writes small fake workers to one temporary directory for the life of a test case."""

    def __init__(self) -> None:
        # Best-effort cleanup: some confined environments refuse to remove directories under the
        # platform temporary root, and that must not be reported as a failing worker check.
        self._directory = tempfile.TemporaryDirectory(
            prefix="ts098-worker-", ignore_cleanup_errors=True
        )
        self.path = self._directory.name

    def write(self, name: str, source: str) -> str:
        target = os.path.join(self.path, name)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(textwrap.dedent(source))
        return target

    def cleanup(self) -> None:
        self._directory.cleanup()

    def __enter__(self) -> "ControlledScripts":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.cleanup()


class WorkerScriptPathTest(unittest.TestCase):
    def test_the_production_script_path_is_inside_this_package(self):
        path = worker_script_path()
        self.assertTrue(os.path.isabs(path))
        self.assertEqual(os.path.basename(path), "regex_worker.py")
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(path.endswith(os.path.join("media", "rules", "regex_worker.py")))

    def test_published_budgets_are_the_frozen_ones(self):
        self.assertEqual(STARTUP_TIMEOUT_SECONDS, 3.0)
        self.assertEqual(CALL_TIMEOUT_SECONDS, 0.05)
        self.assertEqual(REQUEST_TIMEOUT_SECONDS, 5.0)
        self.assertEqual(CLEANUP_TIMEOUT_SECONDS, 1.0)
        self.assertEqual(IPC_MAX_REQUEST_BYTES, 1024 * 1024)
        self.assertEqual(IPC_MAX_RESPONSE_BYTES, 16 * 1024)


class RealWorkerTest(unittest.IsolatedAsyncioTestCase):
    async def test_handshake_compile_search_and_clean_exit(self):
        worker = RegexWorker()
        await worker.start()
        try:
            self.assertIsNotNone(worker.pid)
            self.assertTrue(worker.alive)
            handle = await worker.compile("4[kK]", re.IGNORECASE, timeout=CALL_TIMEOUT_SECONDS)
            self.assertIsInstance(handle, int)
            self.assertTrue(
                await worker.search(handle, "总集篇 4K 合集", timeout=CALL_TIMEOUT_SECONDS)
            )
            self.assertFalse(
                await worker.search(handle, "只有 1080P", timeout=CALL_TIMEOUT_SECONDS)
            )
            plain = await worker.compile("预告", 0, timeout=CALL_TIMEOUT_SECONDS)
            self.assertFalse(await worker.search(plain, "总集篇", timeout=CALL_TIMEOUT_SECONDS))
        finally:
            await worker.aclose()
        self.assertIsNotNone(
            worker.returncode, "the child must be reaped with a recorded exit code"
        )
        self.assertEqual(worker.returncode, 0)
        self.assertFalse(worker.alive)

    async def test_an_invalid_expression_is_refused_by_the_child(self):
        worker = RegexWorker()
        await worker.start()
        try:
            with self.assertRaises(RuleEvaluationError) as caught:
                await worker.compile("a(", 0, timeout=CALL_TIMEOUT_SECONDS)
            self.assertEqual(caught.exception.code, ERROR_INVALID_REGEX)
            # The worker survives a refused expression: it is a caller mistake, not a fault.
            self.assertTrue(worker.alive)
            handle = await worker.compile("a(b", 0, timeout=CALL_TIMEOUT_SECONDS)
            self.assertTrue(await worker.search(handle, "ab", timeout=CALL_TIMEOUT_SECONDS))
        finally:
            await worker.aclose()
        self.assertEqual(worker.returncode, 0)

    async def test_catastrophic_backtracking_is_stopped_by_a_hard_kill(self):
        worker = RegexWorker()
        await worker.start()
        try:
            handle = await worker.compile(CATASTROPHIC_PATTERN, 0, timeout=CALL_TIMEOUT_SECONDS)
            pid = worker.pid
            self.assertIsNotNone(pid)
            with self.assertRaises(RuleEvaluationError) as caught:
                await worker.search(handle, CATASTROPHIC_TEXT, timeout=CALL_TIMEOUT_SECONDS)
            self.assertEqual(caught.exception.code, REASON_REGEX_TIMEOUT)
        finally:
            await worker.aclose()
        self.assertIsNotNone(worker.returncode, "the killed child must be reaped, not left behind")
        self.assertFalse(worker.alive)

    async def test_an_oversized_response_is_refused_before_it_is_accumulated(self):
        with ControlledScripts() as scripts:
            script = scripts.write("oversized_worker.py", OVERSIZED_RESPONSE_WORKER)
            worker = RegexWorker(script)
            try:
                with self.assertRaises(RuleEvaluationError) as caught:
                    await worker.start()
                self.assertEqual(caught.exception.code, REASON_INPUT_TOO_LARGE)
            finally:
                await worker.close_quietly()

    async def test_an_oversized_request_is_refused_before_it_is_written(self):
        worker = RegexWorker()
        await worker.start()
        try:
            handle = await worker.compile("x", 0, timeout=CALL_TIMEOUT_SECONDS)
            with self.assertRaises(RuleEvaluationError) as caught:
                await worker.search(
                    handle, "y" * (IPC_MAX_REQUEST_BYTES + 1), timeout=CALL_TIMEOUT_SECONDS
                )
            self.assertEqual(caught.exception.code, REASON_INPUT_TOO_LARGE)
            # Nothing was written for the refused request, so the protocol is still in step.
            self.assertTrue(await worker.search(handle, "xyz", timeout=CALL_TIMEOUT_SECONDS))
        finally:
            await worker.aclose()
        self.assertEqual(worker.returncode, 0)


class FaultyWorkerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.scripts = ControlledScripts()

    def tearDown(self) -> None:
        self.scripts.cleanup()

    async def test_a_worker_that_exits_immediately_cannot_be_used(self):
        script = self.scripts.write("dies.py", "import sys\nsys.exit(3)\n")
        worker = RegexWorker(script)
        with self.assertRaises(RuleEvaluationError) as caught:
            await worker.start()
        self.assertEqual(caught.exception.code, REASON_REGEX_WORKER_FAILED)
        self.assertFalse(worker.alive)

    async def test_a_worker_that_never_says_ready_is_killed_inside_its_budget(self):
        script = self.scripts.write("silent.py", "import time\ntime.sleep(30)\n")
        worker = RegexWorker(script)
        with self.assertRaises(RuleEvaluationError) as caught:
            await worker.start(timeout=0.2)
        self.assertEqual(caught.exception.code, REASON_REGEX_WORKER_FAILED)
        self.assertFalse(worker.alive, "a silent worker must not be left running")
        self.assertIsNotNone(worker.returncode)

    async def test_a_worker_that_sends_garbage_is_refused(self):
        script = self.scripts.write(
            "garbage.py",
            """
            import sys
            sys.stdout.write('{"ready": false}\\n')
            sys.stdout.flush()
            sys.stdin.readline()
            """,
        )
        worker = RegexWorker(script)
        with self.assertRaises(RuleEvaluationError) as caught:
            await worker.start()
        self.assertEqual(caught.exception.code, REASON_REGEX_WORKER_FAILED)

    async def test_an_unknown_sequence_number_is_refused(self):
        script = self.scripts.write(
            "wrong_seq.py",
            """
            import sys
            sys.stdout.write('{"ready": true}\\n')
            sys.stdout.flush()
            while sys.stdin.readline():
                sys.stdout.write('{"seq": 9999, "ok": true, "handle": 1}\\n')
                sys.stdout.flush()
            """,
        )
        worker = RegexWorker(script)
        await worker.start()
        with self.assertRaises(RuleEvaluationError) as caught:
            await worker.compile("x", 0, timeout=CALL_TIMEOUT_SECONDS)
        self.assertEqual(caught.exception.code, REASON_REGEX_WORKER_FAILED)
        await worker.close_quietly()
        self.assertIsNotNone(worker.returncode)

    async def test_a_non_json_response_is_refused(self):
        script = self.scripts.write(
            "not_json.py",
            """
            import sys
            sys.stdout.write('{"ready": true}\\n')
            sys.stdout.flush()
            while sys.stdin.readline():
                sys.stdout.write('not json at all\\n')
                sys.stdout.flush()
            """,
        )
        worker = RegexWorker(script)
        await worker.start()
        with self.assertRaises(RuleEvaluationError) as caught:
            await worker.compile("x", 0, timeout=CALL_TIMEOUT_SECONDS)
        self.assertEqual(caught.exception.code, REASON_REGEX_WORKER_FAILED)
        await worker.close_quietly()

    async def test_a_worker_that_dies_mid_call_is_refused(self):
        script = self.scripts.write(
            "dies_on_call.py",
            """
            import sys
            sys.stdout.write('{"ready": true}\\n')
            sys.stdout.flush()
            sys.stdin.readline()
            sys.exit(5)
            """,
        )
        worker = RegexWorker(script)
        await worker.start()
        with self.assertRaises(RuleEvaluationError) as caught:
            await worker.compile("x", 0, timeout=CALL_TIMEOUT_SECONDS)
        self.assertEqual(caught.exception.code, REASON_REGEX_WORKER_FAILED)
        await worker.close_quietly()
        self.assertEqual(worker.returncode, 5)

    async def test_a_missing_worker_script_is_refused_without_starting_anything(self):
        worker = RegexWorker(os.path.join(self.scripts.path, "absent.py"))
        with self.assertRaises(RuleEvaluationError) as caught:
            await worker.start()
        self.assertEqual(caught.exception.code, REASON_REGEX_WORKER_FAILED)
        self.assertIsNone(worker.pid)

    async def test_a_cancelled_request_kills_the_child_and_propagates(self):
        script = self.scripts.write(
            "slow_search.py",
            """
            import sys
            import time

            sys.stdout.write('{"ready": true}\\n')
            sys.stdout.flush()
            while True:
                line = sys.stdin.readline()
                if not line:
                    break
                if '"compile"' in line:
                    sys.stdout.write('{"seq": 1, "ok": true, "handle": 1}\\n')
                else:
                    time.sleep(30)
                    sys.stdout.write('{"seq": 2, "ok": true, "matched": true}\\n')
                sys.stdout.flush()
            """,
        )
        worker = RegexWorker(script)
        await worker.start()
        try:
            handle = await worker.compile("x", 0, timeout=CALL_TIMEOUT_SECONDS)
            task = asyncio.ensure_future(worker.search(handle, "abc", timeout=30.0))
            await asyncio.sleep(0.1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertIsNotNone(worker.returncode, "cancellation must reap the child")
        finally:
            await worker.close_quietly()


class EvaluatorRegexPathTest(unittest.IsolatedAsyncioTestCase):
    """The evaluator through the real worker: matches, validation and syntax refusals."""

    async def asyncSetUp(self) -> None:
        self.evaluator = RuleEvaluator()

    async def asyncTearDown(self) -> None:
        await self.evaluator.aclose()

    async def test_a_regex_rule_matches_and_a_whitelist_group_decides(self):
        policy = document(
            whitelist=[
                group(
                    "wl",
                    rule("r_regex", "title", "regex", r"4[kK]\s*合集"),
                    rule("r_tags", "tags", "regex", r"^动"),
                )
            ]
        )
        decision = await self.evaluator.evaluate(
            metadata(),
            policy,
            accessible=True,
            quality_satisfied=False,
            snapshot_revision="rev-1",
        )
        self.assertEqual((decision.decision, decision.reason), (DECISION_DOWNLOAD, "eligible"))
        self.assertEqual(
            [(entry.group_id, entry.result) for entry in decision.trace], [("wl", "matched")]
        )

    async def test_a_case_insensitive_regex_uses_ignorecase_without_rewriting_the_expression(self):
        # Casefolded text matching would turn the pattern into something else; IGNORECASE instead is
        # what makes the same expression match both spellings of the tag.
        policy = document(whitelist=[group("wl", rule("r1", "tags", "regex", "动画|ANIMATION"))])
        matched = await self.evaluator.evaluate(
            metadata(tags=["animation"]),
            policy,
            accessible=True,
            quality_satisfied=False,
            snapshot_revision="rev-1",
        )
        self.assertEqual(matched.decision, DECISION_DOWNLOAD)
        strict = document(
            whitelist=[
                group(
                    "wl",
                    rule("r1", "tags", "regex", "ANIMATION", case_sensitive=True),
                )
            ]
        )
        unmatched = await self.evaluator.evaluate(
            metadata(tags=["animation"]),
            strict,
            accessible=True,
            quality_satisfied=False,
            snapshot_revision="rev-1",
        )
        self.assertEqual(unmatched.decision, "skip")

    async def test_an_empty_field_is_not_matched_by_a_match_everything_expression(self):
        policy = document(whitelist=[group("wl", rule("r1", "title", "regex", ".*"))])
        blank = await self.evaluator.evaluate(
            metadata(title="   "),
            policy,
            accessible=True,
            quality_satisfied=False,
            snapshot_revision="rev-1",
        )
        self.assertEqual(blank.decision, "skip")
        present = await self.evaluator.evaluate(
            metadata(title="有标题"),
            policy,
            accessible=True,
            quality_satisfied=False,
            snapshot_revision="rev-1",
        )
        self.assertEqual(present.decision, DECISION_DOWNLOAD)

    async def test_a_lurking_syntax_error_is_refused_on_an_inaccessible_item(self):
        policy = document(
            blacklist=[
                group(
                    "bl",
                    rule("r_first", "title", "contains", "预告"),
                    rule("r_broken", "title", "regex", "a("),
                )
            ]
        )
        with self.assertRaises(RuleValidationError) as caught:
            await self.evaluator.evaluate(
                metadata(),
                policy,
                accessible=False,
                quality_satisfied=False,
                snapshot_revision="rev-1",
            )
        self.assertEqual(caught.exception.code, ERROR_INVALID_REGEX)
        self.assertEqual(caught.exception.field, "blacklist[0].rules[1].value")

    async def test_a_lurking_syntax_error_is_refused_on_a_blacklisted_item(self):
        policy = document(
            whitelist=[group("wl", rule("r_wl", "title", "regex", "合集"))],
            blacklist=[group("bl", rule("r_bl", "description", "regex", "[unclosed"))],
        )
        with self.assertRaises(RuleValidationError) as caught:
            await self.evaluator.evaluate(
                metadata(),
                policy,
                accessible=True,
                quality_satisfied=False,
                snapshot_revision="rev-1",
            )
        self.assertEqual(caught.exception.code, ERROR_INVALID_REGEX)
        self.assertEqual(caught.exception.field, "blacklist[0].rules[0].value")

    async def test_validate_policy_reports_the_same_path_for_a_broken_expression(self):
        policy = document(
            whitelist=[
                group("wl", rule("r_ok", "title", "regex", "合集")),
                group("wl2", rule("r_bad", "tags", "regex", "(?P<broken")),
            ]
        )
        with self.assertRaises(RuleValidationError) as caught:
            await self.evaluator.validate_policy(policy)
        self.assertEqual(caught.exception.code, ERROR_INVALID_REGEX)
        self.assertEqual(caught.exception.field, "whitelist[1].rules[0].value")

    async def test_validate_policy_accepts_a_regex_policy_and_never_runs_it(self):
        policy = document(whitelist=[group("wl", rule("r1", "title", "regex", "合集"))])
        validated = await self.evaluator.validate_policy(policy)
        self.assertEqual(validated.revision, 7)
        self.assertEqual(validated.rule_count, 1)
        self.assertTrue(validated.has_regex)

    async def test_validate_policy_does_not_start_a_process_for_plain_text(self):
        # The injected path does not exist: if a plain-text policy tried to start a worker, this
        # call would fail instead of validating.
        evaluator = RuleEvaluator(worker_script="/definitely/not/here.py")
        async with evaluator:
            validated = await evaluator.validate_policy(fx.text_policy())
        self.assertEqual(validated.rule_count, 2)
        self.assertFalse(validated.has_regex)

    async def test_a_dead_worker_turns_into_a_rule_error_decision(self):
        with ControlledScripts() as scripts:
            script = scripts.write("dies.py", "import sys\nsys.exit(9)\n")
            evaluator = RuleEvaluator(worker_script=script)
            async with evaluator:
                decision = await evaluator.evaluate(
                    metadata(),
                    document(whitelist=[group("wl", rule("r1", "title", "regex", "合集"))]),
                    accessible=True,
                    quality_satisfied=False,
                    snapshot_revision="rev-1",
                )
        self.assertEqual(decision.decision, DECISION_RULE_ERROR)
        self.assertEqual(decision.reason, REASON_REGEX_WORKER_FAILED)
        self.assertFalse(decision.automatic_enqueue_allowed)
        self.assertTrue(decision.requires_rule_attention)
        self.assertEqual(
            [entry.result for entry in decision.trace[0].rules],
            ["error"],
        )

    async def test_a_worker_that_cannot_start_reports_a_rule_error_without_inventing_a_culprit(
        self,
    ):
        evaluator = RuleEvaluator(worker_script="/definitely/not/here.py")
        async with evaluator:
            decision = await evaluator.evaluate(
                metadata(),
                document(whitelist=[group("wl", rule("r1", "title", "regex", "合集"))]),
                accessible=True,
                quality_satisfied=False,
                snapshot_revision="rev-1",
            )
        self.assertEqual(decision.decision, DECISION_RULE_ERROR)
        self.assertEqual(decision.reason, REASON_REGEX_WORKER_FAILED)
        rule_traces = decision.trace[0].rules
        self.assertEqual([entry.result for entry in rule_traces], ["not_evaluated"])
        self.assertEqual([entry.reason for entry in rule_traces], [REASON_REGEX_WORKER_FAILED])

    async def test_validate_policy_reports_a_worker_failure_as_a_named_error(self):
        evaluator = RuleEvaluator(worker_script="/definitely/not/here.py")
        async with evaluator:
            with self.assertRaises(RuleEvaluationError) as caught:
                await evaluator.validate_policy(
                    document(whitelist=[group("wl", rule("r1", "title", "regex", "合集"))])
                )
        self.assertEqual(caught.exception.code, REASON_REGEX_WORKER_FAILED)

    async def test_a_catastrophic_expression_is_a_regex_timeout_decision(self):
        policy = document(
            whitelist=[group("wl", rule("r1", "title", "regex", CATASTROPHIC_PATTERN))]
        )
        decision = await self.evaluator.evaluate(
            metadata(title=CATASTROPHIC_TEXT),
            policy,
            accessible=True,
            quality_satisfied=False,
            snapshot_revision="rev-1",
        )
        self.assertEqual(decision.decision, DECISION_RULE_ERROR)
        self.assertEqual(decision.reason, REASON_REGEX_TIMEOUT)
        self.assertTrue(decision.requires_rule_attention)
        self.assertFalse(decision.automatic_enqueue_allowed)


class WorkerPathIsolationTest(unittest.TestCase):
    def test_the_default_worker_path_ignores_the_environment(self):
        path = worker_script_path()
        self.assertNotIn(os.environ.get("TEMP", "\x00"), path)
        self.assertTrue(os.path.isfile(path))

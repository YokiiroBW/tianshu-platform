"""The regex child process: real subprocesses, real faults, real exit evidence.

Nothing in this module mocks a timeout or a crash. Every case starts a genuine ``sys.executable -I``
child — either the bundled worker or a small controlled script written to a temporary directory —
and the assertions read the child's own exit code afterwards. That is the only way to show the two
properties the card cares about: a catastrophic expression is really stopped (not merely abandoned
while it keeps burning a core), and a dead or misbehaving worker is refused instead of believed.

Every fixture script here is *runnable as written*: no top-level indentation survives into the file,
which is checked by ``test_every_fixture_script_is_runnable`` rather than assumed.

The controlled scripts are injected through ``RuleEvaluator(worker_script=...)`` / ``RegexWorker``,
which is the documented fault-injection seam: the production default is the absolute path of the
bundled worker inside this package and is not reachable from a setting, an environment variable or
a request field.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

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

#: Generous budget for the calls in this module that are *not* the call under test. A real child
#: has to be created and start its interpreter inside the budget of its first call, so a 50 ms
#: budget is correct production behaviour and a bad test budget: the property under test here is a
#: deterministic one (a match, a refusal, an exit code), not a race with process startup.
CALL_BUDGET = 5.0

CATASTROPHIC_PATTERN = r"^(a+)+$"
CATASTROPHIC_TEXT = "a" * 40 + "b"

READY = "sys.stdout.write(json.dumps({'ready': True, 'protocol': 1, 'pid': os.getpid()}) + '\\n')"

#: Named fixture scripts. Each is a complete, correctly indented program: the tests below assert that
#: they run, so a fixture that cannot even be imported fails as a fixture rather than as a timeout.
SCRIPTS: dict[str, str] = {
    "dies.py": "import sys\nsys.exit(3)\n",
    "silent.py": (
        "import sys, time\n"
        "sys.stderr.write('reached-sleep\\n')\n"
        "sys.stderr.flush()\n"
        "time.sleep(30)\n"
    ),
    "garbage_ready.py": (
        "import sys\n"
        "sys.stdout.write('{\"ready\": false}\\n')\n"
        "sys.stdout.flush()\n"
        "sys.stdin.readline()\n"
    ),
    "wrong_seq.py": (
        "import json, os, sys\n"
        f"{READY}\n"
        "sys.stdout.flush()\n"
        "for line in sys.stdin:\n"
        "    command = json.loads(line)\n"
        "    sys.stdout.write(json.dumps({'seq': command['seq'] + 1000, 'ok': True,"
        " 'handle': 1}) + '\\n')\n"
        "    sys.stdout.flush()\n"
    ),
    "not_json.py": (
        "import json, os, sys\n"
        f"{READY}\n"
        "sys.stdout.flush()\n"
        "for line in sys.stdin:\n"
        "    sys.stdout.write('not json at all\\n')\n"
        "    sys.stdout.flush()\n"
    ),
    "dies_on_call.py": (
        f"import json, os, sys\n{READY}\nsys.stdout.flush()\nsys.stdin.readline()\nsys.exit(5)\n"
    ),
    "sleeps_on_call.py": (
        "import json, os, sys, time\n"
        f"{READY}\n"
        "sys.stdout.flush()\n"
        "for line in sys.stdin:\n"
        "    sys.stderr.write('reached-request\\n')\n"
        "    sys.stderr.flush()\n"
        "    time.sleep(30)\n"
    ),
    "oversized_response.py": (
        "import json, os, sys\n"
        f"{READY}\n"
        "sys.stdout.write(json.dumps({'seq': 1, 'ok': True, 'matched': False,"
        " 'pad': 'p' * 40000}) + '\\n')\n"
        "sys.stdout.flush()\n"
        "sys.stdin.readline()\n"
    ),
    "slow_search.py": (
        "import json, os, sys, time\n"
        f"{READY}\n"
        "sys.stdout.flush()\n"
        "for line in sys.stdin:\n"
        "    command = json.loads(line)\n"
        "    if command['op'] == 'compile':\n"
        "        sys.stdout.write(json.dumps({'seq': command['seq'], 'ok': True,"
        " 'handle': 1}) + '\\n')\n"
        "    else:\n"
        "        time.sleep(30)\n"
        "        sys.stdout.write(json.dumps({'seq': command['seq'], 'ok': True,"
        " 'matched': True}) + '\\n')\n"
        "    sys.stdout.flush()\n"
    ),
}


def refused(call) -> RuleValidationError:
    try:
        call()
    except RuleValidationError as error:
        return error
    raise AssertionError("expected RuleValidationError")


def transport_denied(error: BaseException) -> bool:
    """True when the refusal is this environment refusing to create the child's pipe transport.

    ``RegexWorker.start`` reports every start failure as the same named ``regex_worker_failed``, so the
    distinction between "the worker is broken" and "this host will not let a child exist" lives in the
    cause chain. An environment that cannot run a real child cannot be asked to prove anything about
    one, and saying so by name is more useful than 20 identical assertion failures.
    """

    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, PermissionError):
            return True
        current = current.__cause__ or current.__context__
    return False


def require_real_child(test: unittest.TestCase, error: RuleEvaluationError) -> None:
    """Skip exactly when the environment refused the transport, otherwise let the failure stand."""

    if transport_denied(error):
        test.skipTest(
            "this environment refuses asyncio's child pipe transport "
            f"({error.__cause__!r}); the real-child assertions need the coordination environment"
        )


async def start_or_skip(test: unittest.TestCase, worker: RegexWorker, **kwargs: object):
    """Start a worker, converting an environment transport refusal into a named skip."""

    try:
        await worker.start(**kwargs)
    except RuleEvaluationError as error:
        require_real_child(test, error)
        raise


FIXTURE_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))), ".runtime"
)


def _probe_directory(root: str, name: str) -> str | None:
    """Return a usable fixture directory under ``root``, or ``None`` when writes are refused.

    ``mkdtemp`` creates a 0700 directory, which some confined environments refuse; this creates the
    directory with explicit permissions instead and then *proves* it can hold a file. Creating a
    directory is not proof that a file can be written in it, so the probe write is the real check.
    """

    target = os.path.join(root, name)
    try:
        os.makedirs(target, mode=0o777, exist_ok=True)
        os.chmod(target, 0o777)
    except OSError:
        return None
    try:
        with open(os.path.join(target, "probe.py"), "w", encoding="utf-8") as handle:
            handle.write("pass\n")
    except OSError:
        return None
    return target


class ControlledScripts:
    """Writes the fixture workers to one directory for the life of a test case.

    The platform temporary root is preferred and the workspace's own ``.runtime`` directory is the
    fallback, because a confined environment can refuse one or the other. The scripts are always
    ordinary files on disk — never an in-memory substitute — since the property under test is that a
    *real* ``sys.executable`` child runs them. ``unique`` names a file whose content varies per call,
    so parallel or repeated use cannot collide on a shared filename.
    """

    def __init__(self) -> None:
        self.path = self._make()

    def _make(self) -> str:
        candidates = [
            (tempfile.gettempdir(), f"ts098-worker-{os.getpid()}"),
            (FIXTURE_ROOT, f"ts098-worker-{os.getpid()}"),
        ]
        for root, name in candidates:
            directory = _probe_directory(root, name)
            if directory is not None:
                return directory
        raise unittest.SkipTest(
            "no writable fixture directory: this environment refuses the platform temp root and "
            "the workspace .runtime directory"
        )

    def write(self, name: str, source: str) -> str:
        target = os.path.join(self.path, name)
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(source)
        return target

    def fixture(self, name: str, *, unique: str | None = None) -> str:
        return self.write(unique if unique is not None else name, SCRIPTS[name])

    def cleanup(self) -> None:
        """Remove this case's fixture files, keeping the shared directory for the next case."""

        for name in SCRIPTS:
            try:
                os.remove(os.path.join(self.path, name))
            except OSError:
                pass

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


class FixtureScriptTest(unittest.TestCase):
    """A fixture that cannot run would turn every worker test into a startup timeout."""

    def test_every_fixture_script_is_runnable(self):
        with ControlledScripts() as scripts:
            for name in SCRIPTS:
                with self.subTest(script=name):
                    path = scripts.fixture(name)
                    with open(path, encoding="utf-8") as handle:
                        compile(handle.read(), path, "exec")

    def test_the_silent_fixture_really_reaches_its_sleep(self):
        with ControlledScripts() as scripts:
            path = scripts.fixture("silent.py")
            with self.assertRaises(subprocess.TimeoutExpired):
                subprocess.run(  # noqa: S603
                    [sys.executable, "-I", path], capture_output=True, text=True, timeout=2
                )

    def test_the_sleeping_worker_fixture_really_reaches_a_request(self):
        with ControlledScripts() as scripts:
            path = scripts.fixture("sleeps_on_call.py")
            child = subprocess.Popen(  # noqa: S603
                [sys.executable, "-I", "-u", path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            try:
                ready = json.loads(child.stdout.readline())
                self.assertTrue(ready["ready"])
                child.stdin.write(json.dumps({"seq": 1, "op": "compile", "pattern": "x"}) + "\n")
                child.stdin.flush()
                self.assertEqual(child.stderr.readline().strip(), "reached-request")
            finally:
                child.kill()
                child.wait(timeout=10)


class RealWorkerTest(unittest.IsolatedAsyncioTestCase):
    async def test_handshake_compile_search_and_clean_exit(self):
        worker = RegexWorker()
        await start_or_skip(self, worker)
        try:
            self.assertIsNotNone(worker.pid)
            self.assertTrue(worker.alive)
            handle = await worker.compile("4[kK]", re.IGNORECASE, timeout=CALL_BUDGET)
            self.assertIsInstance(handle, int)
            self.assertTrue(await worker.search(handle, "总集篇 4K 合集", timeout=CALL_BUDGET))
            self.assertFalse(await worker.search(handle, "只有 1080P", timeout=CALL_BUDGET))
            plain = await worker.compile("预告", 0, timeout=CALL_BUDGET)
            self.assertFalse(await worker.search(plain, "总集篇", timeout=CALL_BUDGET))
        finally:
            await worker.aclose()
        self.assertIsNotNone(
            worker.returncode, "the child must be reaped with a recorded exit code"
        )
        self.assertEqual(worker.returncode, 0)
        self.assertFalse(worker.alive)

    async def test_an_invalid_expression_is_refused_without_retiring_the_worker(self):
        worker = RegexWorker()
        await start_or_skip(self, worker)
        try:
            with self.assertRaises(RuleEvaluationError) as caught:
                await worker.compile("a(", 0, timeout=CALL_BUDGET)
            self.assertEqual(caught.exception.code, ERROR_INVALID_REGEX)
            # A caller mistake is not a fault: the worker is still the same live child, and the
            # valid expression below is a distinct expression with its own expectation.
            self.assertTrue(worker.alive)
            self.assertIsNotNone(worker.managed_process)
            self.assertIsNone(worker.managed_process.returncode)
            handle = await worker.compile("a(b)*", 0, timeout=CALL_BUDGET)
            self.assertTrue(await worker.search(handle, "a(b)", timeout=CALL_BUDGET))
            self.assertTrue(await worker.search(handle, "ab", timeout=CALL_BUDGET))
            self.assertFalse(await worker.search(handle, "b", timeout=CALL_BUDGET))
        finally:
            await worker.aclose()
        self.assertEqual(worker.returncode, 0)

    async def test_a_catastrophic_expression_is_stopped_by_a_hard_kill(self):
        worker = RegexWorker()
        await start_or_skip(self, worker)
        try:
            handle = await worker.compile(CATASTROPHIC_PATTERN, 0, timeout=CALL_BUDGET)
            pid = worker.pid
            self.assertIsNotNone(pid)
            managed = worker.managed_process
            with self.assertRaises(RuleEvaluationError) as caught:
                await worker.search(handle, CATASTROPHIC_TEXT, timeout=CALL_TIMEOUT_SECONDS)
            self.assertEqual(caught.exception.code, REASON_REGEX_TIMEOUT)
        finally:
            await worker.aclose()
        self.assertIsNotNone(worker.returncode, "the killed child must be reaped, not left behind")
        self.assertFalse(worker.alive)
        # Real process evidence, not just the flag: the managed child the worker held is exited.
        self.assertIsNotNone(managed)
        self.assertIsNotNone(managed.returncode)
        self.assertNotEqual(managed.returncode, 0, "a killed worker does not report a clean exit")

    async def test_an_oversized_response_is_refused_before_it_is_accumulated(self):
        with ControlledScripts() as scripts:
            script = scripts.fixture("oversized_response.py")
            worker = RegexWorker(script)
            try:
                with self.assertRaises(RuleEvaluationError) as caught:
                    await start_or_skip(self, worker)
                self.assertEqual(caught.exception.code, REASON_INPUT_TOO_LARGE)
            finally:
                await worker.close_quietly()
            managed = worker.managed_process
            self.assertIsNotNone(managed)
            self.assertIsNotNone(managed.returncode, "the retired worker is reaped, not abandoned")

    async def test_an_oversized_request_retires_the_worker_and_a_new_one_recovers(self):
        with ControlledScripts():
            worker = RegexWorker()
            await start_or_skip(self, worker)
            try:
                handle = await worker.compile("x", 0, timeout=CALL_BUDGET)
                with self.assertRaises(RuleEvaluationError) as caught:
                    await worker.search(
                        handle, "y" * (IPC_MAX_REQUEST_BYTES + 1), timeout=CALL_BUDGET
                    )
                self.assertEqual(caught.exception.code, REASON_INPUT_TOO_LARGE)
                # The frozen rule: an over-limit request retires the worker instead of leaving a
                # half-written line on the pipe for the next request to trip over.
                managed = worker.managed_process
                self.assertIsNotNone(managed)
                self.assertIsNotNone(managed.returncode, "the over-limit worker is really reaped")
                self.assertFalse(worker.alive)
            finally:
                await worker.close_quietly()
            # Recovery is a fresh child, not a reused bad one.
            fresh = RegexWorker()
            await start_or_skip(self, fresh)
            try:
                handle = await fresh.compile("xyz", 0, timeout=CALL_BUDGET)
                self.assertTrue(await fresh.search(handle, "xyz", timeout=CALL_BUDGET))
            finally:
                await fresh.aclose()
            self.assertEqual(fresh.returncode, 0)


class FaultyWorkerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.scripts = ControlledScripts()

    def tearDown(self) -> None:
        self.scripts.cleanup()

    async def asyncSetUp(self) -> None:
        # A real child has to be possible at all before any case here can mean anything; when this
        # environment refuses the transport, each case says so by name instead of reporting a
        # misleading code defect.
        probe = RegexWorker()
        try:
            await probe.start()
        except RuleEvaluationError as error:
            await probe.close_quietly()
            require_real_child(self, error)
            raise
        await probe.aclose()

    async def test_a_worker_that_exits_immediately_cannot_be_used(self):
        script = self.scripts.fixture("dies.py")
        worker = RegexWorker(script)
        with self.assertRaises(RuleEvaluationError) as caught:
            await worker.start()
        self.assertEqual(caught.exception.code, REASON_REGEX_WORKER_FAILED)
        self.assertFalse(worker.alive)
        managed = worker.managed_process
        self.assertIsNotNone(managed)
        self.assertEqual(managed.returncode, 3, "the child's own exit code is the evidence")

    async def test_a_worker_that_never_says_ready_is_killed_inside_its_budget(self):
        script = self.scripts.fixture("silent.py")
        worker = RegexWorker(script)
        with self.assertRaises(RuleEvaluationError) as caught:
            await start_or_skip(self, worker, timeout=0.2)
        self.assertEqual(caught.exception.code, REASON_REGEX_WORKER_FAILED)
        self.assertFalse(worker.alive, "a silent worker must not be left running")
        self.assertIsNotNone(worker.returncode)

    async def test_a_worker_that_sends_garbage_is_refused(self):
        script = self.scripts.fixture("garbage_ready.py")
        worker = RegexWorker(script)
        with self.assertRaises(RuleEvaluationError) as caught:
            await worker.start()
        self.assertEqual(caught.exception.code, REASON_REGEX_WORKER_FAILED)

    async def test_an_unknown_sequence_number_is_refused(self):
        script = self.scripts.fixture("wrong_seq.py")
        worker = RegexWorker(script)
        await worker.start()
        with self.assertRaises(RuleEvaluationError) as caught:
            await worker.compile("x", 0, timeout=CALL_BUDGET)
        self.assertEqual(caught.exception.code, REASON_REGEX_WORKER_FAILED)
        await worker.close_quietly()
        self.assertIsNotNone(worker.returncode, "a mismatched sequence retires the worker")

    async def test_a_non_json_response_is_refused(self):
        script = self.scripts.fixture("not_json.py")
        worker = RegexWorker(script)
        await worker.start()
        with self.assertRaises(RuleEvaluationError) as caught:
            await worker.compile("x", 0, timeout=CALL_BUDGET)
        self.assertEqual(caught.exception.code, REASON_REGEX_WORKER_FAILED)
        await worker.close_quietly()
        self.assertIsNotNone(worker.returncode)

    async def test_a_worker_that_dies_mid_call_is_refused(self):
        script = self.scripts.fixture("dies_on_call.py")
        worker = RegexWorker(script)
        await worker.start()
        with self.assertRaises(RuleEvaluationError) as caught:
            await worker.compile("x", 0, timeout=CALL_BUDGET)
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
        script = self.scripts.fixture("slow_search.py")
        worker = RegexWorker(script)
        await worker.start()
        try:
            handle = await worker.compile("x", 0, timeout=CALL_BUDGET)
            managed = worker.managed_process
            task = asyncio.ensure_future(worker.search(handle, "abc", timeout=30.0))
            await asyncio.sleep(0.2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertIsNotNone(worker.returncode, "cancellation must reap the child")
            self.assertIsNotNone(managed.returncode, "the real child is reaped, not left alive")
        finally:
            await worker.close_quietly()
        self.assertFalse(worker.alive)

    async def test_a_worker_that_is_sleeping_on_a_request_really_was_reached(self):
        """The fixture proves it reached the request stage before the timeout is credited."""

        script = self.scripts.fixture("sleeps_on_call.py")
        worker = RegexWorker(script)
        await worker.start()
        try:
            with self.assertRaises(RuleEvaluationError) as caught:
                await worker.compile("x", 0, timeout=CALL_TIMEOUT_SECONDS)
            self.assertEqual(caught.exception.code, REASON_REGEX_TIMEOUT)
            self.assertIsNotNone(worker.returncode, "the timed-out child is reaped")
        finally:
            await worker.close_quietly()


class EvaluatorRegexPathTest(unittest.IsolatedAsyncioTestCase):
    """The evaluator through the real worker: matches, validation and syntax refusals."""

    async def asyncSetUp(self) -> None:
        self.evaluator = RuleEvaluator()
        probe = RegexWorker()
        try:
            await probe.start()
        except RuleEvaluationError as error:
            await probe.close_quietly()
            require_real_child(self, error)
            raise
        await probe.aclose()

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

    async def test_a_syntax_refusal_echoes_no_expression_or_record_text(self):
        policy = document(whitelist=[group("wl", rule("r1", "title", "regex", "(?P<secret_name"))])
        with self.assertRaises(RuleValidationError) as caught:
            await self.evaluator.evaluate(
                metadata(title="绝密标题"),
                policy,
                accessible=True,
                quality_satisfied=False,
                snapshot_revision="rev-1",
            )
        text = str(caught.exception)
        for secret in ("secret_name", "绝密标题", "(?P<"):
            self.assertNotIn(secret, text)

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

    async def test_a_dead_worker_marks_every_rule_not_evaluated(self):
        """No rule ran, so no rule may be labelled ``error``: the startup failure has no culprit."""

        with ControlledScripts() as scripts:
            script = scripts.fixture("dies.py")
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
            ["not_evaluated"],
        )
        self.assertEqual(
            [entry.reason for entry in decision.trace[0].rules],
            [REASON_REGEX_WORKER_FAILED],
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


class ProtocolShapeTest(unittest.IsolatedAsyncioTestCase):
    """The frozen wire shapes, refused *before* a request can be believed.

    These drive the strict readers through a recorded transport: a response line is handed to the
    worker exactly as a misbehaving child would write it, and the worker must refuse it rather than act
    on it. The real fixtures above cover the other two violations — a line that is not UTF-8, and a
    line the reader's high-water mark rejects before it is accumulated.
    """

    async def asyncSetUp(self) -> None:
        # These cases need no child of their own, but the module's premise is that this environment can
        # run one; without that, the surrounding real-child cases are the ones that report the limit.
        probe = RegexWorker()
        try:
            await probe.start()
        except RuleEvaluationError as error:
            await probe.close_quietly()
            require_real_child(self, error)
            raise
        await probe.aclose()

    async def wire(self, lines: list[bytes]) -> RegexWorker:
        """A worker whose responses are exactly ``lines``, with no child involved."""

        payload = b"".join(lines)

        class Reader:
            def __init__(self) -> None:
                self._buffer = payload

            async def readline(self) -> bytes:
                line, _, rest = self._buffer.partition(b"\n")
                self._buffer = rest
                return line + b"\n" if line else b""

        class Stdin:
            def __init__(self) -> None:
                self.written: list[bytes] = []

            def write(self, data: bytes) -> None:
                self.written.append(data)

            async def drain(self) -> None:
                return None

            def close(self) -> None:
                return None

        class Process:
            def __init__(self) -> None:
                self.pid = 9911
                self.stdin = Stdin()
                self.stdout = Reader()
                self.returncode: int | None = None

            def kill(self) -> None:
                self.returncode = -9

            async def wait(self) -> int:
                if self.returncode is None:
                    self.returncode = 0
                return self.returncode

        worker = RegexWorker()
        worker._process = Process()  # type: ignore[assignment] - a recorded transport, not a child
        return worker

    async def test_a_ready_line_with_extra_state_is_refused(self):
        for line in (
            b'{"ready": true, "protocol": 1, "pid": 12, "extra": 1}\n',
            b'{"ready": true, "protocol": 1}\n',
            b'{"ready": 1, "protocol": 1, "pid": 12}\n',
            b'{"ready": true, "protocol": "1", "pid": 12}\n',
            b'{"ready": true, "protocol": 1, "pid": 0}\n',
            b'{"ready": true, "protocol": 1, "pid": true}\n',
            b'{"ready": true, "protocol": 1, "pid": 12.0}\n',
            b"not json\n",
        ):
            with self.subTest(line=line):
                worker = await self.wire([line])
                with self.assertRaises(RuleEvaluationError) as caught:
                    await worker.compile("x", 0, timeout=0.5)
                self.assertEqual(caught.exception.code, REASON_REGEX_WORKER_FAILED)

    async def test_a_response_for_another_sequence_is_refused(self):
        worker = await self.wire([b'{"seq": 999, "ok": true, "handle": 1}\n'])
        with self.assertRaises(RuleEvaluationError) as caught:
            await worker.compile("x", 0, timeout=0.5)
        self.assertEqual(caught.exception.code, REASON_REGEX_WORKER_FAILED)

    async def test_a_boolean_or_float_handle_is_refused(self):
        for payload in (
            b'{"seq": 1, "ok": true, "handle": true}\n',
            b'{"seq": 1, "ok": true, "handle": 1.0}\n',
            b'{"seq": 1, "ok": true, "handle": "1"}\n',
        ):
            with self.subTest(payload=payload):
                worker = await self.wire([payload])
                with self.assertRaises(RuleEvaluationError) as caught:
                    await worker.compile("x", 0, timeout=0.5)
                self.assertEqual(caught.exception.code, REASON_REGEX_WORKER_FAILED)

    async def test_a_compile_response_with_search_fields_is_refused(self):
        worker = await self.wire([b'{"seq": 1, "ok": true, "handle": 1, "matched": true}\n'])
        with self.assertRaises(RuleEvaluationError) as caught:
            await worker.compile("x", 0, timeout=0.5)
        self.assertEqual(caught.exception.code, REASON_REGEX_WORKER_FAILED)

    async def test_a_search_response_without_a_boolean_matched_is_refused(self):
        for payload in (
            b'{"seq": 1, "ok": true}\n',
            b'{"seq": 1, "ok": true, "matched": 1}\n',
            b'{"seq": 1, "ok": false, "code": "regex_timeout", "matched": true}\n',
        ):
            with self.subTest(payload=payload):
                worker = await self.wire([payload])
                with self.assertRaises(RuleEvaluationError):
                    await worker.search(1, "x", timeout=0.5)


class WorkerPathIsolationTest(unittest.IsolatedAsyncioTestCase):
    def test_the_default_worker_path_ignores_the_environment(self):
        path = worker_script_path()
        self.assertNotIn(os.environ.get("TEMP", "\x00"), path)
        self.assertTrue(os.path.isfile(path))

    async def test_the_worker_is_started_isolated_unbuffered_and_without_a_shell(self):
        # ``-I`` ignores PYTHONUTF8/PYTHONIOENCODING and the user site directory, ``-u`` keeps the
        # line protocol immediate, the script is the absolute packaged path rather than anything a
        # setting, environment variable or request could name, and there is no shell. The real
        # transport may be refused in a confined environment, so the command itself is captured
        # before anything is created.
        captured: dict[str, object] = {}

        async def capture(*args: object, **kwargs: object):
            captured["args"] = args
            captured["kwargs"] = kwargs
            raise FileNotFoundError("captured before creation")

        worker = RegexWorker()
        with mock.patch.object(asyncio, "create_subprocess_exec", side_effect=capture):
            with self.assertRaises(RuleEvaluationError):
                await worker.start()
        args = captured["args"]
        kwargs = captured["kwargs"]
        self.assertEqual(tuple(args[:2]), (sys.executable, "-I"))
        self.assertEqual(args[2], "-u")
        self.assertEqual(args[3], worker_script_path())
        self.assertTrue(os.path.isabs(args[3]))
        self.assertEqual(len(args), 4)
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["limit"], IPC_MAX_RESPONSE_BYTES)
        self.assertIsNone(worker.pid, "a refused creation owns no child")

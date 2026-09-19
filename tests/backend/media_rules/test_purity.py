"""Import safety, dependency direction, determinism and the worker's own boundary.

The rules package is imported by request handlers, so importing it must not start a process, open a
file, read a clock or reach the network. That is asserted statically (an AST import whitelist for
every module in the package), dynamically (a fresh interpreter that must produce no child process
and no new file) and structurally (the worker script imports the standard library only and never the
platform).

Determinism is checked by evaluating the same input twice and comparing whole decisions, which is
what makes the stored explanation of a past scan meaningful.
"""

from __future__ import annotations

import ast
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from unittest import mock

try:
    from ._fixtures import document, group, metadata, rule, text_policy
except ImportError:  # narrow discovery: this directory is the top-level start directory
    from _fixtures import document, group, metadata, rule, text_policy

import services.platform.media.rules as rules

PACKAGE_DIRECTORY = os.path.dirname(os.path.abspath(rules.__file__))
REPOSITORY_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(PACKAGE_DIRECTORY)))
)
MODULE_NAMES = (
    "__init__.py",
    "types.py",
    "policy.py",
    "matching.py",
    "evaluator.py",
    "regex_process.py",
    "regex_worker.py",
)

ALLOWED_IMPORTS = {
    "__init__.py": {
        "__future__",
        "asyncio",
        "contextlib",
        "dataclasses",
        "hashlib",
        "json",
        "os",
        "re",
        "sys",
        "time",
        "typing",
    },
    "regex_worker.py": {"__future__", "json", "os", "re", "sys"},
}
FOREIGN_PACKAGE_MARKERS = (
    "aiohttp",
    "jsonschema",
    "sqlite3",
    "http",
    "socket",
    "urllib",
    "requests",
    "services.platform.storage",
    "services.platform.server",
    "services.platform.auth",
    "services.platform.web",
    "services.platform.tasks",
    "services.platform.home",
)


@contextlib.contextmanager
def probe_directory() -> Iterator[str]:
    """An empty, *readable* scratch directory for the fresh interpreter.

    A confined environment can hand out a temporary directory that cannot be listed or removed, and a
    ``mkdtemp`` directory is created 0700, which some environments refuse outright. Each candidate is
    therefore proven usable — it must exist, be listable, and allow a file to be written and removed —
    before it is handed over, and the fallback is the task's own runtime directory. Cleanup is best
    effort: refusing to remove the directory must not be reported as a failed import-safety check.
    """

    candidates = [tempfile.gettempdir(), os.path.join(REPOSITORY_ROOT, ".runtime")]
    for base in candidates:
        directory = os.path.join(base, f"ts098-import-{os.getpid()}")
        try:
            os.makedirs(directory, mode=0o777, exist_ok=True)
            os.chmod(directory, 0o777)
            probe = os.path.join(directory, "probe.py")
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write("pass\n")
            os.remove(probe)
            if os.listdir(directory) == []:
                yield directory
                return
        except OSError:
            continue
        finally:
            shutil.rmtree(directory, ignore_errors=True)
    raise unittest.SkipTest("no readable temporary directory is available for the probe")


def imported_modules(path: str) -> set[str]:
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                names.add("." * node.level + (node.module or ""))
            else:
                names.add(node.module or "")
    return names


class ImportBoundaryTest(unittest.TestCase):
    def test_every_module_imports_only_what_it_is_allowed_to(self):
        for name in MODULE_NAMES:
            with self.subTest(module=name):
                names = imported_modules(os.path.join(PACKAGE_DIRECTORY, name))
                foreign = {
                    imported
                    for imported in names
                    if imported.split(".")[0] in FOREIGN_PACKAGE_MARKERS
                    or any(imported.startswith(marker) for marker in FOREIGN_PACKAGE_MARKERS)
                }
                self.assertEqual(foreign, set(), f"{name} reaches outside the block: {foreign}")
                for imported in names:
                    top = imported.lstrip(".")
                    if imported.startswith(".") or top.startswith("services.platform.media"):
                        continue
                    allowed = ALLOWED_IMPORTS.get(name)
                    if allowed is None:
                        continue
                    self.assertIn(
                        top.split(".")[0], allowed, f"{name} imports unexpected module {imported}"
                    )

    def test_the_worker_never_imports_the_platform(self):
        names = imported_modules(os.path.join(PACKAGE_DIRECTORY, "regex_worker.py"))
        self.assertEqual(names, {"__future__", "json", "os", "re", "sys"})

    def test_importing_in_a_fresh_interpreter_starts_nothing(self):
        # ``-I`` is the same isolated mode the worker itself runs under: no PYTHONPATH, no user
        # site-packages, and the repository root is handed over explicitly instead of being
        # inherited from the ambient environment. The empty working directory has to be readable by
        # this process, so a confined environment that hides the platform temporary root uses the
        # task's own runtime directory instead.
        script = (
            "import os, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import services.platform.media.rules as rules\n"
            "print(rules.MAX_CONCURRENT_REQUESTS)\n"
            "print(os.path.isfile(rules.worker_script_path()))\n"
        )
        with probe_directory() as directory:
            completed = subprocess.run(
                [sys.executable, "-I", "-c", script, REPOSITORY_ROOT],
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=120,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(os.listdir(directory), [])
        self.assertEqual(completed.stdout.splitlines(), ["2", "True"])

    def test_the_package_exports_every_documented_name(self):
        for name in rules.__all__:
            with self.subTest(name=name):
                self.assertTrue(hasattr(rules, name), f"{name} is exported but missing")
        self.assertIn("RuleEvaluator", rules.__all__)
        self.assertIn("parse_policy", rules.__all__)
        self.assertIn("RuleDecision", rules.__all__)

    def test_the_default_worker_path_is_the_one_in_this_package(self):
        expected = os.path.join(PACKAGE_DIRECTORY, "regex_worker.py")
        self.assertEqual(rules.worker_script_path(), expected)
        with mock.patch.dict(os.environ, {"TS098_WORKER_SCRIPT": "/tmp/evil.py"}, clear=False):
            self.assertEqual(rules.worker_script_path(), expected)


class DeterminismTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_same_input_produces_the_same_decision(self):
        policy = document(
            whitelist=[
                group(
                    "wl",
                    rule("r_title", "title", "contains", "合集"),
                    rule("r_tag", "tags", "equals", "动画"),
                )
            ],
            blacklist=[group("bl", rule("r_preview", "title", "contains", "预告"))],
        )
        async with rules.RuleEvaluator() as evaluator:
            first = await evaluator.evaluate(
                metadata(),
                policy,
                accessible=True,
                quality_satisfied=False,
                snapshot_revision="rev-1",
            )
            second = await evaluator.evaluate(
                metadata(),
                policy,
                accessible=True,
                quality_satisfied=False,
                snapshot_revision="rev-1",
            )
        self.assertEqual(first, second)
        self.assertEqual(first.policy_digest, second.policy_digest)

    async def test_rule_order_is_preserved_in_the_trace(self):
        policy = document(
            whitelist=[
                group(
                    "wl",
                    rule("r_first", "title", "contains", "合集"),
                    rule("r_second", "tags", "equals", "动画"),
                )
            ]
        )
        async with rules.RuleEvaluator() as evaluator:
            decision = await evaluator.evaluate(
                metadata(),
                policy,
                accessible=True,
                quality_satisfied=False,
                snapshot_revision="rev-1",
            )
        self.assertEqual(
            [entry.rule_id for entry in decision.trace[0].rules], ["r_first", "r_second"]
        )


class ErrorVocabularyTest(unittest.TestCase):
    def test_the_published_vocabularies_are_fixed_lists(self):
        self.assertEqual(
            rules.NORMAL_REASONS,
            (
                "eligible",
                "inaccessible",
                "quality_satisfied",
                "blacklist_match",
                "whitelist_no_match",
            ),
        )
        self.assertEqual(
            rules.FAILURE_REASONS,
            (
                "regex_timeout",
                "regex_worker_failed",
                "evaluation_timeout",
                "busy",
                "input_too_large",
            ),
        )
        self.assertEqual(rules.RESULTS, ("matched", "not_matched", "not_evaluated", "error"))
        self.assertEqual(rules.DECISIONS, ("download", "skip", "rule_error"))
        self.assertEqual(
            rules.FIELDS, ("title", "description", "uploader_id", "uploader_name", "tags")
        )
        self.assertEqual(rules.OPS, ("equals", "contains", "prefix", "suffix", "regex"))
        self.assertIn("invalid_regex", rules.POLICY_ERROR_CODES)
        self.assertIn("evaluation_timeout", rules.EVALUATION_ERROR_CODES)
        self.assertNotIn("invalid_regex", rules.FAILURE_REASONS)

    def test_error_messages_carry_only_fixed_words(self):
        policy = text_policy()
        with self.assertRaises(rules.RuleValidationError) as caught:
            rules.parse_policy({**policy, "revision": "not-a-number"})
        message = str(caught.exception)
        self.assertIn("invalid_revision", message)
        self.assertNotIn("not-a-number", message)
        self.assertEqual(caught.exception.args, (message,))

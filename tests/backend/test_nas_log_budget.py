import os
import tempfile
import time
import unittest
from unittest.mock import patch

from services.platform.__main__ import build_sink
from services.platform.diagnostics_config import ContractProblem, resolve_durability_timeout


class LogBudgetTests(unittest.TestCase):
    def test_budget_is_explicit_bounded_and_default_unchanged(self):
        self.assertEqual(resolve_durability_timeout({}), 0.25)
        self.assertEqual(
            resolve_durability_timeout({"diagnostics": {"durability_timeout_ms": 2000}}), 2
        )
        for value in (True, 0, 249, 5001, "2000", 250.5, None):
            with self.subTest(value=value), self.assertRaises(ContractProblem):
                resolve_durability_timeout({"diagnostics": {"durability_timeout_ms": value}})

    def test_slow_fsync_is_really_confirmed_within_configured_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            sink = build_sink(
                {"diagnostics": {"log_directory": directory, "durability_timeout_ms": 2000}}
            )
            original = os.fsync

            def slow(fd):
                time.sleep(0.35)
                return original(fd)

            try:
                with patch("os.fsync", side_effect=slow):
                    started = time.monotonic()
                    sequence = sink.admit_durable("runtime.starting", "INFO", "started")
                    self.assertIsNotNone(sequence)
                    self.assertGreaterEqual(time.monotonic() - started, 0.30)
                    self.assertEqual(sink.state, "durable")
            finally:
                self.assertTrue(sink.close(5))

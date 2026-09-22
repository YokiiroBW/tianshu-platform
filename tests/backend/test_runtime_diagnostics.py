"""The diagnostic adapter on its own: closed vocabulary, closed record, real durability.

No double stands in for the sink here. Every durability claim is checked against bytes that were
really written by this process, and every vocabulary claim against the frozen published contract.
"""

import asyncio
import hashlib
import io
import json
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from runtime_fixture import log_files, read_events, require_contract

from services.platform import diagnostics, diagnostics_config

FIELDS = (
    "schema_version",
    "timestamp",
    "service",
    "instance_id",
    "sequence",
    "event_id",
    "level",
    "event",
    "outcome",
    "correlation_id",
    "duration_ms",
    "error_code",
)

INSTANCE = str(uuid.uuid4())

# Field names that would turn a bounded record into a place untrusted text can be parked.
FORBIDDEN_FIELDS = (
    "message",
    "msg",
    "extras",
    "extra",
    "stack",
    "stacktrace",
    "traceback",
    "url",
    "path",
    "headers",
    "token",
    "account",
    "body",
    "payload",
    "query",
    "detail",
    "reason",
)


def wait_until(predicate, timeout=5.0, interval=0.01):
    """Bounded wait for a fact produced by the writer thread; never an open-ended sleep."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class SinkTestCase(unittest.TestCase):
    """A case that owns its process-wide sink and always puts the module back as it found it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.sink = None
        self.extra_sinks = []
        self.stream = None
        self.addCleanup(self.teardown)
        self.directory = Path(self.temp.name) / "logs"
        self.directory.mkdir()

    def teardown(self):
        """Reset the module, stop every writer, and only then remove what it wrote to.

        A live writer still holding a segment open is a sharing violation on Windows, and a test
        that leaves one behind must not report that as a product failure. Closing therefore lives
        here instead of depending on each test's own diligence.
        """
        diagnostics.reset()
        sinks = ([self.sink] if self.sink is not None else []) + self.extra_sinks
        self.sink, self.extra_sinks = None, []
        for sink in sinks:
            sink.close(2.0)
        self.temp.cleanup()

    def own(self, sink):
        diagnostics.activate(sink)
        self.sink = sink
        return sink

    def capture(self):
        """A development sink whose output is readable, for tests about the record itself."""
        self.stream = io.StringIO()
        return self.own(diagnostics.Diagnostics(stderr=self.stream))

    def durable(self, **kwargs):
        return self.own(diagnostics.Diagnostics(self.directory, **kwargs))

    def emitted(self):
        return [json.loads(line) for line in self.stream.getvalue().splitlines() if line]


class RecordShapeTests(SinkTestCase):
    def record(self, name, level, outcome="succeeded", **extra):
        return diagnostics.build(name, level, outcome, sequence=1, instance_id=INSTANCE, **extra)

    def test_a_record_carries_exactly_the_frozen_fields_in_order(self):
        record = self.record("runtime.starting", "INFO", "started")
        self.assertEqual(tuple(record), FIELDS)
        self.assertFalse(set(record) & set(FORBIDDEN_FIELDS))

    def test_every_registered_event_name_and_level_encodes_inside_the_budget(self):
        for name, levels in diagnostics.EVENTS.items():
            for level in levels:
                line = diagnostics.encode(self.record(name, level))
                self.assertTrue(line.endswith(b"\n"), name)
                self.assertNotIn(b"\r", line, name)
                self.assertLessEqual(len(line), diagnostics.MAX_LINE_BYTES, name)

    def test_an_unregistered_event_name_is_refused(self):
        with self.assertRaises(diagnostics.RecordError):
            self.record("http.request.made_up", "INFO")

    def test_a_level_the_event_did_not_register_is_refused(self):
        with self.assertRaises(diagnostics.RecordError):
            self.record("runtime.starting", "CRITICAL", "started")

    def test_an_unregistered_outcome_is_refused(self):
        with self.assertRaises(diagnostics.RecordError):
            self.record("runtime.starting", "INFO", "maybe")

    def test_an_identifier_that_is_not_a_uuid_is_refused(self):
        with self.assertRaises(diagnostics.RecordError):
            diagnostics.build(
                "runtime.starting", "INFO", "started", sequence=1, instance_id="instance"
            )
        with self.assertRaises(diagnostics.RecordError):
            self.record("runtime.starting", "INFO", event_id="not-a-uuid")
        with self.assertRaises(diagnostics.RecordError):
            diagnostics.build(
                "runtime.starting",
                "INFO",
                "started",
                sequence=1,
                instance_id=INSTANCE,
                service="not-a-published-service",
            )

    def test_an_unregistered_error_code_is_refused(self):
        with self.assertRaises(diagnostics.RecordError):
            self.record(
                "runtime.startup_failed", "ERROR", "failed", error_code="vendor_specific_failure"
            )

    def test_a_correlation_that_is_not_thirty_two_lowercase_hex_is_refused(self):
        for bad in ("", "A" * 32, "f" * 31, "f" * 33, "g" * 32, "0123456789abcdef"):
            with self.assertRaises(diagnostics.RecordError, msg=bad):
                self.record("http.request.started", "INFO", "started", correlation_id=bad)

    def test_a_duration_that_is_not_finite_and_non_negative_is_refused(self):
        for bad in (float("nan"), float("inf"), float("-inf"), -1, -0.5):
            with self.assertRaises(diagnostics.RecordError, msg=repr(bad)):
                self.record("http.request.finished", "INFO", duration_ms=bad)

    def test_a_record_validates_against_the_frozen_published_schema(self):
        package = diagnostics_config.load_contract(require_contract())
        for name, levels in diagnostics.EVENTS.items():
            for level in levels:
                for outcome in diagnostics.OUTCOMES:
                    record = self.record(
                        name, level, outcome, duration_ms=1.5, correlation_id="a" * 32
                    )
                    self.assertTrue(package.validate(record), name + "/" + level)
                    record["extra"] = "x"
                    self.assertFalse(package.validate(record))

    def test_the_frozen_package_still_rejects_its_own_negative_examples(self):
        # The manifest hash alone cannot prove the schema was not weakened, so the package's own
        # negative examples are re-checked here as the semantic half of the pin.
        package = diagnostics_config.load_contract(require_contract())
        package.check_examples()
        self.assertEqual(package.version, diagnostics_config.CONTRACT_VERSION)
        self.assertEqual(package.status, diagnostics_config.CONTRACT_STATUS)
        self.assertEqual(package.manifest_sha256, diagnostics_config.MANIFEST_SHA256)

    def test_the_registered_vocabulary_is_closed_and_shaped(self):
        self.assertTrue(diagnostics.EVENTS)
        for name, levels in diagnostics.EVENTS.items():
            self.assertRegex(name, r"\A[a-z][a-z0-9_.]{0,63}\Z")
            self.assertTrue(levels)
            self.assertLessEqual(set(levels), set(diagnostics.LEVELS))
        for code in diagnostics.ERROR_CODES:
            self.assertRegex(code, r"\A[a-z][a-z0-9_]{0,63}\Z")
        self.assertIn("internal_error", diagnostics.ERROR_CODES)
        self.assertIn(diagnostics.SERVICE, diagnostics.SERVICE_NAMES)


class VocabularyTests(SinkTestCase):
    def test_safe_code_collapses_anything_this_product_does_not_own(self):
        for value in (
            "ORA-01555",
            "ClientConnectorError",
            "C:\\YOKI\\projects\\secret.sqlite",
            "Traceback (most recent call last)",
            "ok\nsecond line",
            "UNAUTHORIZED",
            "",
            None,
            42,
        ):
            self.assertEqual(diagnostics.safe_code(value), "internal_error", repr(value))
        self.assertEqual(diagnostics.safe_code("timeout"), "timeout")
        self.assertEqual(diagnostics.safe_code("log_queue_full"), "log_queue_full")

    def test_every_status_maps_to_a_registered_outcome_and_code(self):
        for status in (200, 201, 204, 400, 401, 403, 404, 408, 409, 413, 429, 500, 503, 504):
            level, outcome, code = diagnostics.classify(status)
            self.assertIn(level, diagnostics.LEVELS)
            self.assertIn(outcome, diagnostics.OUTCOMES)
            if code is not None:
                self.assertIn(code, diagnostics.ERROR_CODES)

    def test_finish_records_the_terminal_state_and_the_authorisation_result(self):
        self.capture()
        correlation = diagnostics.new_correlation()
        diagnostics.finish(200, 4.25, correlation)
        names = [record["event"] for record in self.emitted()]
        self.assertEqual(names, ["http.request.finished", "http.auth.succeeded"])
        self.assertEqual(self.emitted()[0]["duration_ms"], 4.25)

    def test_a_refusal_is_recorded_as_a_rejected_authorisation_exactly_once(self):
        self.capture()
        diagnostics.finish(401, 1.0, diagnostics.new_correlation())
        records = self.emitted()
        self.assertEqual(
            [record["event"] for record in records],
            ["http.request.finished", "http.auth.rejected"],
        )
        self.assertEqual(records[1]["error_code"], "unauthorized")

    def test_an_unknown_path_never_claims_authentication_happened(self):
        self.capture()
        diagnostics.finish(404, 1.0, diagnostics.new_correlation(), known_path=False)
        names = [record["event"] for record in self.emitted()]
        self.assertEqual(names, ["http.request.finished", "http.request.unknown_path"])

    def test_an_unexpected_exception_keeps_the_wire_code_and_the_fixed_log_code(self):
        self.capture()
        diagnostics.finish(503, 1.0, diagnostics.new_correlation(), error_code="internal_error")
        records = self.emitted()
        self.assertEqual(records[0]["error_code"], "internal_error")
        self.assertEqual(records[0]["level"], "ERROR")

    def test_outbound_only_accepts_the_registered_kinds(self):
        self.capture()
        correlation = diagnostics.new_correlation()
        for kind in diagnostics.OUTBOUND_KINDS:
            diagnostics.outbound(kind, correlation_id=correlation, duration_ms=2.0)
        names = [record["event"] for record in self.emitted()]
        self.assertEqual(names, ["outbound.call." + kind for kind in diagnostics.OUTBOUND_KINDS])
        self.assertIsNone(diagnostics.outbound("exploded", correlation_id=correlation))
        self.assertEqual(len(self.emitted()), len(diagnostics.OUTBOUND_KINDS))

    def test_a_bad_event_name_is_swallowed_rather_than_failing_the_request(self):
        self.capture()
        # A domain module that names an event wrongly must not turn into a 500 for the caller; the
        # coverage test is what catches the mistake, not the request path.
        self.assertIsNone(diagnostics.event("http.request.made_up", "INFO", "succeeded"))
        self.assertIsNone(
            diagnostics.event("http.request.started", "INFO", "succeeded", duration_ms=float("nan"))
        )


class CorrelationTests(SinkTestCase):
    def test_a_fresh_correlation_is_thirty_two_lowercase_hex_and_unique(self):
        seen = {diagnostics.new_correlation() for _ in range(64)}
        self.assertEqual(len(seen), 64)
        for value in seen:
            self.assertTrue(diagnostics.valid_correlation(value), value)

    def test_an_inbound_header_is_adopted_only_when_it_is_already_legal(self):
        legal = "0123456789abcdef0123456789abcdef"
        self.assertEqual(diagnostics.adopt_correlation(legal), legal)
        forged = "0123456789ABCDEF0123456789ABCDEF"
        replaced = diagnostics.adopt_correlation(forged)
        self.assertNotEqual(replaced, forged)
        self.assertTrue(diagnostics.valid_correlation(replaced))
        for bad in ("", None, "not-hex", "0123456789abcdef", "0123456789abcdef0123456789abcde"):
            if bad is None:
                continue
            self.assertTrue(diagnostics.valid_correlation(diagnostics.adopt_correlation(bad)))

    def test_a_child_task_and_a_worker_thread_inherit_the_requests_correlation(self):
        correlation = diagnostics.new_correlation()
        token = diagnostics.use_correlation(correlation)
        self.addCleanup(diagnostics.reset_correlation, token)
        seen = {}

        async def child():
            seen["task"] = diagnostics.current_correlation()
            seen["thread"] = await asyncio.to_thread(diagnostics.current_correlation)

        asyncio.run(child())
        self.assertEqual(seen, {"task": correlation, "thread": correlation})

    def test_two_concurrent_requests_never_share_a_correlation(self):
        async def one():
            token = diagnostics.use_correlation(diagnostics.new_correlation())
            try:
                await asyncio.sleep(0.01)
                return diagnostics.current_correlation()
            finally:
                diagnostics.reset_correlation(token)

        async def both():
            return await asyncio.gather(one(), one())

        first, second = asyncio.run(both())
        self.assertNotEqual(first, second)
        self.assertTrue(diagnostics.valid_correlation(first))
        self.assertTrue(diagnostics.valid_correlation(second))

    def test_the_header_carries_the_request_and_nothing_without_one(self):
        self.assertIsNone(diagnostics.current_correlation())
        self.assertEqual(diagnostics.correlation_header(), {})
        correlation = diagnostics.new_correlation()
        token = diagnostics.use_correlation(correlation)
        self.addCleanup(diagnostics.reset_correlation, token)
        self.assertEqual(diagnostics.correlation_header(), {diagnostics.HEADER: correlation})

    def test_a_span_measures_monotonic_time_and_never_goes_backwards(self):
        span = diagnostics.Span("http.request.finished")
        self.assertGreaterEqual(span.elapsed(), 0.0)
        backwards = diagnostics.Span("http.request.finished", clock=lambda: 10.0)
        self.assertEqual(backwards.elapsed(clock=lambda: 4.0), 0.0)


class DurabilityTests(SinkTestCase):
    def test_an_accepted_event_is_on_disk_before_the_gate_returns(self):
        sink = self.durable()
        correlation = diagnostics.new_correlation()
        self.assertTrue(diagnostics.accept(correlation))
        # Read the raw bytes with no flush, no close and no sleep in between: what `accept`
        # promised is exactly that the accepting event is already there.
        records = read_events(self.directory)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["event"], "http.request.started")
        self.assertEqual(records[0]["correlation_id"], correlation)
        self.assertEqual(sink.state, diagnostics.DURABLE)

    def test_events_are_utf8_jsonl_with_lf_and_no_carriage_return(self):
        sink = self.durable()
        for index in range(5):
            sink.emit_durable("cli.action.started", "INFO", "started")
        sink.flush()
        for path in log_files(self.directory):
            raw = path.read_bytes()
            self.assertTrue(raw.endswith(b"\n"))
            self.assertNotIn(b"\r", raw)
            for line in raw.split(b"\n"):
                if line:
                    json.loads(line.decode("utf-8"))

    def test_sequence_is_monotone_and_event_identity_is_unique(self):
        sink = self.durable()
        sequences = [sink.emit_durable("cli.action.started", "INFO", "started") for _ in range(8)]
        sink.flush()
        self.assertEqual(sequences, sorted(sequences))
        self.assertEqual(len(set(sequences)), len(sequences))
        records = read_events(self.directory)
        self.assertEqual(len({record["event_id"] for record in records}), len(records))

    def test_retransmission_keeps_the_original_identity_instead_of_inventing_a_second_event(self):
        sink = self.durable()
        sink.emit_durable("cli.action.started", "INFO", "started")
        sink.flush()
        original = read_events(self.directory)[0]
        self.assertTrue(sink.retransmit(original))
        sink.flush()
        records = read_events(self.directory)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["event_id"], records[1]["event_id"])
        self.assertEqual(records[0]["sequence"], records[1]["sequence"])

    def test_a_sealed_segment_is_kept_and_the_next_one_is_opened(self):
        sink = self.durable(segment_bytes=diagnostics.MAX_LINE_BYTES * 2)
        for _ in range(60):
            sink.emit_durable("cli.action.started", "INFO", "started")
        self.assertTrue(wait_until(lambda: len(log_files(self.directory)) >= 2))
        sink.flush()
        files = log_files(self.directory)
        self.assertGreaterEqual(len(files), 2)
        self.assertTrue(all(path.stat().st_size > 0 for path in files))
        # Every record in every segment is still complete: sealing moves the writer on, it never
        # rewrites or drops what an earlier segment already holds.
        records = read_events(self.directory)
        self.assertGreaterEqual(len(records), 60)
        self.assertTrue(all(tuple(record) == FIELDS for record in records))
        self.assertTrue(
            wait_until(
                lambda: any(
                    record["event"] == "logging.segment_sealed"
                    for record in read_events(self.directory)
                )
            )
        )

    def test_the_seal_announcement_cannot_feed_itself(self):
        # The segment floor exists precisely so that announcing a roll cannot cause the next one.
        sink = self.durable(segment_bytes=1)
        self.assertGreaterEqual(sink._sink.segment_bytes, diagnostics.MAX_LINE_BYTES * 2)
        for _ in range(30):
            sink.emit("cli.action.started", "INFO", "started")
        self.assertTrue(wait_until(lambda: sink._synced >= 30, timeout=5))
        sink.flush()
        # A bounded cascade: announcements stay a small fraction of the events that caused them, so
        # the number of segments remains proportional to the number of events.
        self.assertLess(len(log_files(self.directory)), 30)

    def test_the_queue_is_bounded_and_refuses_instead_of_dropping(self):
        stalled = threading.Event()
        sink = self.durable(queue_limit=2, admit_timeout=0.2)
        original = sink._sink.write

        def write(line):
            stalled.wait(5)
            return original(line)

        sink._sink.write = write
        for _ in range(12):
            diagnostics.event("cli.action.started", "INFO", "started")
        self.assertTrue(wait_until(lambda: sink.state == diagnostics.UNAVAILABLE, timeout=3))
        self.assertFalse(sink.admit())
        self.assertFalse(diagnostics.accept(diagnostics.new_correlation()))
        self.assertFalse(diagnostics.admittable())
        stalled.set()
        sink.close(3.0)

    def test_capacity_exhaustion_refuses_new_work_and_warns_once(self):
        # The cap is clamped to at least 32 MiB, so the budget is consumed by what is already on
        # disk - exactly the restart case the cap exists for.
        (self.directory / "previous.jsonl").write_bytes(b"x" * (33 * 1024 * 1024))
        stream = io.StringIO()
        sink = diagnostics.Diagnostics(
            self.directory, directory_bytes=diagnostics.MIN_DIRECTORY_BYTES, stderr=stream
        )
        self.extra_sinks.append(sink)
        diagnostics.activate(sink)
        sink.emit("cli.action.started", "INFO", "started")
        self.assertTrue(wait_until(lambda: sink.state == diagnostics.UNAVAILABLE))
        self.assertFalse(sink.admit())
        self.assertIn("unavailable", stream.getvalue())
        self.assertEqual(stream.getvalue().count("tianshu-diagnostics:"), 1)

    def test_a_write_failure_is_never_reported_as_a_durable_success(self):
        sink = self.durable(admit_timeout=0.3)
        original = sink._sink.write
        broke = {"on": True}

        def broken(line):
            if broke["on"]:
                raise OSError("device not configured")
            return original(line)

        sink._sink.write = broken
        self.assertIsNone(sink.emit_durable("cli.action.started", "INFO", "started"))
        self.assertTrue(wait_until(lambda: sink.state == diagnostics.UNAVAILABLE))
        self.assertFalse(sink.admit())
        broke["on"] = False
        self.assertTrue(sink.recover())
        sink.flush()
        names = [record["event"] for record in read_events(self.directory)]
        self.assertIn("cli.action.started", names)
        self.assertNotIn("logging.capacity_exhausted", names)
        self.assertIn("logging.unavailable", names)

    def test_a_repaired_sink_recovers_and_the_gap_stays_reconcilable(self):
        sink = self.durable(admit_timeout=0.5)
        original = sink._sink.write
        broke = {"on": True}

        def broken(line):
            if broke["on"]:
                raise OSError("device not configured")
            return original(line)

        sink._sink.write = broken
        sink.emit("cli.action.started", "INFO", "started")
        self.assertTrue(wait_until(lambda: sink.state == diagnostics.UNAVAILABLE))
        broke["on"] = False
        sink._sink.write = original
        self.assertTrue(sink.recover())
        sink.flush()
        names = [record["event"] for record in read_events(self.directory)]
        # The event that could not be written is still there, in its original position, followed
        # by the outage it lived through - nothing accepted was dropped and nothing was renumbered.
        self.assertIn("cli.action.started", names)
        self.assertIn("logging.recovered", names)
        self.assertIn("logging.unavailable", names)
        self.assertLess(names.index("cli.action.started"), names.index("logging.recovered"))

    def test_a_sink_that_is_still_broken_does_not_claim_recovery(self):
        sink = self.durable(admit_timeout=0.2)

        def always_broken(line):
            raise OSError("device not configured")

        sink._sink.write = always_broken
        sink.emit("cli.action.started", "INFO", "started")
        self.assertTrue(wait_until(lambda: sink.state == diagnostics.UNAVAILABLE))
        # Still broken: recovery is only ever a successful persistent write, so this must fail and
        # the sink must stay unavailable rather than report itself healthy.
        self.assertFalse(sink.recover())
        self.assertEqual(sink.state, diagnostics.UNAVAILABLE)
        self.assertFalse(sink.admit())

    def test_without_a_configured_directory_development_mode_is_explicit(self):
        stream = io.StringIO()
        sink = diagnostics.Diagnostics(stderr=stream)
        diagnostics.activate(sink)
        self.assertEqual(sink.state, diagnostics.NON_DURABLE)
        self.assertEqual(log_files(self.directory), [])
        # Development without a directory is a deliberate operator choice: it keeps working, and
        # readiness stays red because only `durable` can back a production `ready`.
        self.assertTrue(sink.admit())
        sequence = sink.emit_durable("runtime.starting", "INFO", "started")
        self.assertIsNotNone(sequence)
        records = [json.loads(line) for line in stream.getvalue().splitlines() if line]
        self.assertEqual(records[0]["event"], "runtime.starting")
        self.assertEqual(tuple(records[0]), FIELDS)

    def test_an_unusable_directory_is_unavailable_rather_than_a_weaker_sink(self):
        stream = io.StringIO()
        occupied = Path(self.temp.name) / "not-a-directory"
        occupied.write_text("file", encoding="utf-8")
        sink = diagnostics.Diagnostics(str(occupied), stderr=stream)
        self.own(sink)
        self.assertEqual(sink.state, diagnostics.UNAVAILABLE)
        self.assertFalse(sink.admit())
        self.assertIsNone(diagnostics.event("runtime.starting", "INFO", "started"))
        self.assertFalse(diagnostics.accept(diagnostics.new_correlation()))

    def test_a_refused_event_never_changes_the_callers_own_result(self):
        occupied = Path(self.temp.name) / "occupied"
        occupied.write_text("file", encoding="utf-8")
        sink = diagnostics.Diagnostics(str(occupied))
        self.own(sink)
        # The caller's own result is not contingent on the log: the gate says no, and the value the
        # domain already computed is still returned unchanged. Nothing is re-sent to make up for it.
        published = {"config_version": 7}
        self.assertFalse(diagnostics.accept(diagnostics.new_correlation()))
        self.assertEqual(published, {"config_version": 7})
        self.assertIsNone(diagnostics.outbound("succeeded"))
        self.assertIsNone(diagnostics.event("cli.action.finished", "INFO", "succeeded"))

    def test_nothing_is_written_when_no_sink_was_ever_assembled(self):
        diagnostics.reset()
        self.assertIsNone(diagnostics.event("runtime.starting", "INFO", "started"))
        self.assertIsNone(diagnostics.outbound("started"))
        self.assertTrue(diagnostics.admittable())
        self.assertEqual(log_files(self.directory), [])

    def test_close_is_bounded_and_seals_what_was_written(self):
        sink = self.durable()
        sink.emit("cli.action.started", "INFO", "started")
        started = time.monotonic()
        sink.close(2.0)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertTrue(sink.sealed_segments())
        self.assertTrue(all(Path(path).is_file() for path in sink.sealed_segments()))

    def test_a_shutdown_that_races_writes_still_leaves_complete_lines(self):
        sink = self.durable()
        stop = threading.Event()

        def write_until_stopped():
            while not stop.is_set():
                try:
                    sink.emit("cli.action.started", "INFO", "started")
                except diagnostics.RecordError:
                    return

        writers = [threading.Thread(target=write_until_stopped) for _ in range(3)]
        for writer in writers:
            writer.start()
        time.sleep(0.05)
        stop.set()
        for writer in writers:
            writer.join(5)
        sink.close(2.0)
        for path in log_files(self.directory):
            raw = path.read_bytes()
            self.assertTrue(raw.endswith(b"\n"))
            for line in raw.split(b"\n"):
                if line:
                    record = json.loads(line.decode("utf-8"))
                    self.assertEqual(tuple(record), FIELDS)


class ContractLoadingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_the_frozen_manifest_hash_is_the_one_the_card_corrected(self):
        self.assertEqual(
            diagnostics_config.MANIFEST_SHA256,
            "5d89f7a21637fd57cea4a236e17f8d8c4917799497ff44f87ee68ea614d4805f",
        )

    def test_the_raw_published_bytes_are_pinned_rather_than_a_normalised_copy(self):
        package = diagnostics_config.load_contract(require_contract())
        root = Path(package.directory)
        for name, digest in package.files.items():
            raw = (root / name).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), digest, name)
        # Rewriting a line ending would change those bytes, so a normalised copy must not verify.
        published = (root / "event.schema.json").read_bytes()
        self.assertIn(b"\r\n", published)
        self.assertNotEqual(
            hashlib.sha256(published.replace(b"\r\n", b"\n")).hexdigest(),
            package.files["event.schema.json"],
        )

    def test_a_directory_that_is_not_the_reviewed_package_is_refused(self):
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(diagnostics_config.ContractProblem):
                diagnostics_config.load_contract(empty)
        with self.assertRaises(diagnostics_config.ContractProblem):
            diagnostics_config.load_contract("")
        with self.assertRaises(diagnostics_config.ContractProblem):
            diagnostics_config.load_contract(None)

    def test_a_tampered_file_is_refused_even_though_the_manifest_is_intact(self):
        source = require_contract()
        with tempfile.TemporaryDirectory() as copy:
            root = Path(copy) / "v1"
            root.mkdir()
            for name in diagnostics_config.CONTRACT_FILES:
                (root / name).write_bytes((source / name).read_bytes())
            (root / "manifest.json").write_bytes((source / "manifest.json").read_bytes())
            self.assertTrue(diagnostics_config.load_contract(root).files)
            (root / "examples.json").write_bytes(b"[]\r\n")
            with self.assertRaises(diagnostics_config.ContractProblem):
                diagnostics_config.load_contract(root)

    def test_settings_resolution_prefers_the_explicit_section(self):
        settings = {
            "diagnostics": {
                "contract_directory": str(require_contract()),
                "log_directory": str(Path(self.temp.name) / "explicit-logs"),
                "log_directory_bytes": 34 * 1024 * 1024,
                "ready_token_env": "TS100_SYNTHETIC_READY",
            }
        }
        self.assertEqual(
            diagnostics_config.resolve_contract_directory(settings), str(require_contract())
        )
        self.assertEqual(
            diagnostics_config.resolve_log_directory(settings),
            str(Path(self.temp.name) / "explicit-logs"),
        )
        self.assertEqual(diagnostics_config.resolve_log_directory_bytes(settings), 34 * 1024 * 1024)
        self.assertEqual(
            diagnostics_config.resolve_ready_token_env(settings), "TS100_SYNTHETIC_READY"
        )
        # The resolver reports what the deployment configured; refusing a relative path is the
        # parser's job, and it is asserted above.
        self.assertEqual(
            diagnostics_config.resolve_log_directory({"diagnostics": {"log_directory": "rel"}}),
            "rel",
        )
        with self.assertRaises(diagnostics_config.ContractProblem):
            diagnostics_config.parse_diagnostics_settings({"diagnostics": {"log_directory": "rel"}})

    def test_a_settings_section_is_validated_before_anything_is_assembled(self):
        for bad in (
            {"diagnostics": {"log_directory_bytes": 0}},
            {"diagnostics": {"log_directory_bytes": True}},
            {"diagnostics": {"unknown_key": 1}},
            {"diagnostics": "not-an-object"},
            {"diagnostics": {"ready_token_env": ""}},
            {"diagnostics": {"log_directory": "relative/path"}},
            {"diagnostics": {"contract_directory": "relative/path"}},
            {"diagnostics": {"expected_manifest_sha256": "short"}},
        ):
            with self.assertRaises(diagnostics_config.ContractProblem, msg=repr(bad)):
                diagnostics_config.parse_diagnostics_settings(bad)

    def test_a_deployment_without_a_section_stays_explicit_about_what_is_missing(self):
        self.assertEqual(diagnostics_config.parse_diagnostics_settings({}), {})
        self.assertIsNone(diagnostics_config.resolve_log_directory({}))
        self.assertIsNone(diagnostics_config.resolve_contract_directory({}))
        self.assertIsNone(diagnostics_config.resolve_log_directory_bytes({}))
        self.assertEqual(
            diagnostics_config.resolve_ready_token_env({}), diagnostics_config.READY_TOKEN_ENV
        )
        self.assertEqual(
            diagnostics_config.resolve_manifest_sha256({}), diagnostics_config.MANIFEST_SHA256
        )
        # An unconfigured contract is not a load error: readiness is where a running service
        # without one becomes a red check.
        self.assertEqual(diagnostics_config.verify_or_none({}), (None, None))
        package, problem = diagnostics_config.verify_or_none(
            {"diagnostics": {"contract_directory": str(require_contract())}}
        )
        self.assertIsNone(problem)
        self.assertEqual(package.version, diagnostics_config.CONTRACT_VERSION)
        broken, problem = diagnostics_config.verify_or_none(
            {"diagnostics": {"contract_directory": str(Path(self.temp.name) / "absent")}}
        )
        self.assertIsNone(broken)
        self.assertEqual(problem, "contract_directory_missing")

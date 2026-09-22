"""The diagnostic adapter on its own: closed vocabulary, closed record, real durability.

No double stands in for the sink here. Every durability claim is checked against bytes that were
really written by this process, and every vocabulary claim against the frozen published contract.
"""

import asyncio
import hashlib
import io
import json
import os
import ssl
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from aiohttp import web

from runtime_fixture import certificates, log_files, read_events, require_contract, start_server

from services.platform import diagnostics, diagnostics_config, persona_client, transport

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

# A registered credential has to look like one: `secret` accepts 24-4096 printable characters.
CREDENTIAL = "synthetic-outbound-terminal-credential"

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


async def wait_for(predicate, timeout=5.0, interval=0.01):
    """The same bounded wait from a coroutine: it yields, so the loop keeps running."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
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

        `close` is deliberately bounded, so a sink whose owner is stuck returns unconfirmed and
        keeps working in its own time - which is the behaviour under test elsewhere. The directory
        is therefore only removed once the owner has really finished, or not at all.
        """
        diagnostics.reset()
        sinks = ([self.sink] if self.sink is not None else []) + self.extra_sinks
        self.sink, self.extra_sinks = None, []
        for sink in sinks:
            sink.close(2.0)
            # The owner releases its descriptor on its own thread; waiting for that is what makes
            # the cleanup below safe, and it is the only thing this wait is for.
            sink._owner_done.wait(10.0)
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
        """The JSON records on the captured stream.

        The same stream also carries the one fixed operator warning a failed sink writes, so only
        lines that are actually records are parsed: a warning is not an event and must never be
        read as one.
        """
        return [
            json.loads(line) for line in self.stream.getvalue().splitlines() if line.startswith("{")
        ]


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
        diagnostics.finish(200, 4.25, correlation, auth=diagnostics.AUTH_SUCCEEDED)
        names = [record["event"] for record in self.emitted()]
        self.assertEqual(names, ["http.request.finished", "http.auth.succeeded"])
        self.assertEqual(self.emitted()[0]["duration_ms"], 4.25)

    def test_finish_without_a_disposition_never_claims_authentication(self):
        """The absent claim is the honest one: a caller that proves nothing gets nothing written."""
        self.capture()
        diagnostics.finish(200, 1.0, diagnostics.new_correlation())
        names = [record["event"] for record in self.emitted()]
        self.assertEqual(names, ["http.request.finished"])
        self.assertNotIn("http.auth.succeeded", names)

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
        diagnostics.finish(404, 1.0, diagnostics.new_correlation())
        names = [record["event"] for record in self.emitted()]
        self.assertEqual(names, ["http.request.finished", "http.request.unknown_path"])

    def test_an_unexpected_exception_keeps_the_wire_code_and_the_fixed_log_code(self):
        self.capture()
        diagnostics.finish(503, 1.0, diagnostics.new_correlation(), error_code="internal_error")
        records = self.emitted()
        self.assertEqual(records[0]["error_code"], "internal_error")
        self.assertEqual(records[0]["level"], "ERROR")

    async def test_outbound_only_accepts_the_registered_kinds(self):
        self.capture()
        correlation = diagnostics.new_correlation()
        for kind in diagnostics.OUTBOUND_KINDS:
            await diagnostics.outbound(kind, correlation_id=correlation, duration_ms=2.0)
        names = [record["event"] for record in self.emitted()]
        self.assertEqual(names, ["outbound.call." + kind for kind in diagnostics.OUTBOUND_KINDS])
        self.assertIsNone(await diagnostics.outbound("exploded", correlation_id=correlation))
        self.assertEqual(len(self.emitted()), len(diagnostics.OUTBOUND_KINDS))

    def test_an_unregistered_event_name_is_refused_and_never_swallowed(self):
        sink = self.capture()
        before = sink._sequence
        # A domain module that names an event wrongly is a programming error. It is not allowed to
        # become a silent gap: the sink stops admitting new work with one fixed code and the error
        # reaches the caller, so "every registered event is written" stays a claim this process can
        # actually keep.
        with self.assertRaises(diagnostics.RecordError):
            diagnostics.event("http.request.made_up", "INFO", "succeeded")
        self.assertEqual(sink.state, diagnostics.UNAVAILABLE)
        self.assertEqual(sink.error, "log_record_invalid")
        self.assertFalse(sink.admit())
        # The refused record consumed no sequence, so the numbering has no hole in it.
        self.assertEqual(sink._sequence, before)
        self.assertEqual(self.emitted(), [])
        # The only thing on the stream is the one fixed operator warning, which carries a code and
        # never the rejected name, a value or a path.
        self.assertEqual(self.stream.getvalue().count("tianshu-diagnostics:"), 1)
        self.assertNotIn("http.request.made_up", self.stream.getvalue())

    def test_an_unencodable_value_is_refused_without_consuming_a_sequence(self):
        sink = self.capture()
        before = sink._sequence
        with self.assertRaises(diagnostics.RecordError):
            diagnostics.event("http.request.started", "INFO", "succeeded", duration_ms=float("nan"))
        self.assertEqual(sink._sequence, before)
        self.assertEqual(self.emitted(), [])


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

    async def test_a_refused_event_never_changes_the_callers_own_result(self):
        occupied = Path(self.temp.name) / "occupied"
        occupied.write_text("file", encoding="utf-8")
        sink = diagnostics.Diagnostics(str(occupied))
        self.own(sink)
        # The caller's own result is not contingent on the log: the gate says no, and the value the
        # domain already computed is still returned unchanged. Nothing is re-sent to make up for it.
        published = {"config_version": 7}
        self.assertFalse(diagnostics.accept(diagnostics.new_correlation()))
        self.assertEqual(published, {"config_version": 7})
        self.assertIsNone(await diagnostics.outbound("succeeded"))
        self.assertIsNone(diagnostics.event("cli.action.finished", "INFO", "succeeded"))

    async def test_nothing_is_written_when_no_sink_was_ever_assembled(self):
        diagnostics.reset()
        self.assertIsNone(diagnostics.event("runtime.starting", "INFO", "started"))
        self.assertIsNone(await diagnostics.outbound("started"))
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


class OutboundTerminalTests(unittest.IsolatedAsyncioTestCase):
    """A cancelled outbound call gets an end, and the end says whether anything could have left.

    The upstream is a real TLS peer where a socket can exist at all, and the queued case needs no
    socket: a read still waiting for its outbound slot has provably sent nothing, and telling those
    two apart is exactly what the fixed code has to carry.
    """

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.logs = self.directory / "logs"
        self.sink = diagnostics.Diagnostics(str(self.logs))
        diagnostics.activate(self.sink)
        self.addCleanup(diagnostics.reset)
        self.addCleanup(self.sink.close, 5.0)
        self.tls = None
        if os.environ.get("TS013_TLS_PYTHON"):
            self.tls = certificates(self.directory / "tls")

    def events(self):
        self.sink.flush(5.0)
        return read_events(self.logs)

    def named(self, name):
        return [record for record in self.events() if record["event"] == name]

    async def hanging_upstream(self):
        held = asyncio.Event()

        async def hang(request):
            held.set()
            # Long enough that the call is certainly still in flight when it is cancelled, short
            # enough that the peer's own teardown does not hold the suite open.
            await asyncio.sleep(2.0)
            return web.json_response({})

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(
            self.tls["valid"]["certificate_file"], self.tls["valid"]["private_key_file"]
        )
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", hang)
        runner, base = await start_server(app, tls=context)
        self.addAsyncCleanup(runner.cleanup)
        return held, base

    def settings_for(self, base):
        return {
            "base_url": base,
            "token_env": "TS012_ADMIN",
            "ca_file": self.tls["valid"]["certificate_file"],
            "timeout_seconds": 10,
        }

    async def test_a_core_call_cancelled_in_flight_is_not_reported_as_still_running(self):
        held, base = await self.hanging_upstream()
        payload = {"query": {"request_id": "request:synthetic-cancel"}}
        with patch.dict(os.environ, {"TS012_ADMIN": CREDENTIAL}, clear=False):
            task = asyncio.ensure_future(
                transport.core_web_call(
                    self.settings_for(base), "web-snapshot", payload, None, None
                )
            )
            await asyncio.wait_for(held.wait(), 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(self.named("outbound.call.started")), 1)
        cancelled = self.named("outbound.call.cancelled")
        self.assertEqual(len(cancelled), 1)
        # The request was on the wire, so nothing claims it was never sent - and nothing claims it
        # succeeded either.
        self.assertIsNone(cancelled[0]["error_code"])
        self.assertEqual(cancelled[0]["outcome"], "cancelled")
        self.assertEqual(self.named("outbound.call.succeeded"), [])
        self.assertEqual(self.named("outbound.call.failed"), [])

    async def test_a_core_post_cancelled_in_flight_is_not_reported_as_still_running(self):
        held, base = await self.hanging_upstream()
        with patch.dict(os.environ, {"TS012_ADMIN": CREDENTIAL}, clear=False):
            task = asyncio.ensure_future(
                transport.core_post(self.settings_for(base), {"schema_version": 1})
            )
            await asyncio.wait_for(held.wait(), 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(self.named("outbound.call.started")), 1)
        cancelled = self.named("outbound.call.cancelled")
        self.assertEqual(len(cancelled), 1)
        self.assertIsNone(cancelled[0]["error_code"])

    async def test_a_call_cancelled_before_it_could_connect_says_it_never_left(self):
        class Never:
            """A session that cannot be entered: the call ends before any socket exists."""

            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                await asyncio.sleep(30)

            async def __aexit__(self, *exc):
                return False

        settings = {
            "base_url": "https://127.0.0.1:9",
            "token_env": "TS012_ADMIN",
            "timeout_seconds": 10,
        }
        with patch.dict(os.environ, {"TS012_ADMIN": CREDENTIAL}, clear=False):
            with patch.object(transport.aiohttp, "ClientSession", Never):
                task = asyncio.ensure_future(
                    transport.core_web_call(
                        settings, "web-snapshot", {"query": {"request_id": "r"}}, None, None
                    )
                )
                await asyncio.sleep(0.05)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        cancelled = self.named("outbound.call.cancelled")
        self.assertEqual(len(cancelled), 1)
        # Nothing could have reached any peer, and the record says so with a registered code.
        self.assertEqual(cancelled[0]["error_code"], "outbound_not_sent")
        self.assertIn("outbound_not_sent", diagnostics.ERROR_CODES)

    async def test_a_queued_persona_read_cancelled_in_the_queue_never_left(self):
        client = persona_client.PersonaClient.__new__(persona_client.PersonaClient)
        client.connection_id = "synthetic"
        client.base_url = "https://127.0.0.1:9"
        client.token_env = "TS012_ADMIN"
        client.ca_file = None
        client.timeout = 5
        client.candidate = None
        client.reserved = ()
        client.slots = asyncio.Semaphore(1)
        document = {"operation": "get"}
        with patch.object(persona_client, "validate_request", lambda *a: None):
            with patch.object(persona_client, "prove", lambda *a: None):
                # The only slot is taken, so this read is queued and has provably sent nothing.
                await client.slots.acquire()
                with patch.dict(os.environ, {"TS012_ADMIN": CREDENTIAL}, clear=False):
                    task = asyncio.ensure_future(client.call(document))
                    for _ in range(300):
                        if self.named("outbound.call.queued"):
                            break
                        await asyncio.sleep(0.01)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
        self.assertEqual(len(self.named("outbound.call.queued")), 1)
        cancelled = self.named("outbound.call.cancelled")
        self.assertEqual(len(cancelled), 1)
        # A read that never left the queue is not "started", and its end says it was not sent.
        self.assertEqual(self.named("outbound.call.started"), [])
        self.assertEqual(cancelled[0]["error_code"], "outbound_not_sent")


class SettlementTests(SinkTestCase):
    """Shutdown reports what really happened to the events it accepted.

    A clean seal on a file that never received them is not a success, a timely exit is not a durable
    one, and neither can be turned into the other by asking a second time. Every assertion here is
    made on the sink's own state and on the bytes it really wrote; no test flushes on the
    implementation's behalf before checking.
    """

    def failing_sink(self, **kwargs):
        sink = self.durable(**kwargs)

        def failed(line):
            raise OSError("synthetic write failure")

        sink._sink.write = failed
        return sink

    def test_a_write_failure_before_a_stop_is_never_reported_as_settled(self):
        sink = self.failing_sink()
        accepted = sink.emit("runtime.started", "INFO", "succeeded")
        self.assertTrue(wait_until(lambda: sink.state == diagnostics.UNAVAILABLE))
        self.assertFalse(sink.close(0.5))
        self.assertTrue(sink._owner_done.wait(5.0))
        # The owner stopped, and that is all it did: one accepted event was never written.
        self.assertFalse(sink._final_ok)
        self.assertEqual(sink.unconfirmed, 1)
        self.assertEqual(sum(path.stat().st_size for path in self.directory.glob("*.jsonl")), 0)
        # The accepted event is still there to be reconciled, not thrown away to make the count
        # work: the queue is the evidence, and dropping it is not a way to become settled.
        self.assertTrue(any(item[0] == "line" and item[2] == accepted for item in sink._queue))

    def test_a_repeated_close_and_flush_never_overwrite_a_failure(self):
        sink = self.failing_sink()
        sink.emit("runtime.started", "INFO", "succeeded")
        self.assertTrue(wait_until(lambda: sink.state == diagnostics.UNAVAILABLE))
        first = sink.close(0.5)
        sink._owner_done.wait(5.0)
        counted = sink.unconfirmed
        # Asking again is not a way to become settled: the failure is a fact about this process, and
        # a later caller must not be able to read a success into it.
        for _ in range(3):
            self.assertFalse(sink.close(0.5))
            self.assertFalse(sink.flush(0.1))
        self.assertFalse(first)
        self.assertEqual(sink.unconfirmed, counted)
        self.assertEqual(counted, 1)

    def test_a_sync_failure_before_a_stop_is_never_reported_as_settled(self):
        sink = self.durable()
        sink.emit("runtime.started", "INFO", "succeeded")

        def failed():
            raise OSError("synthetic sync failure")

        sink._sink.sync = failed
        self.assertTrue(wait_until(lambda: sink.state == diagnostics.UNAVAILABLE))
        self.assertFalse(sink.close(0.5))
        self.assertTrue(sink._owner_done.wait(5.0))
        # Written is not the same as durable: the bytes exist, the watermark never moved, and the
        # shutdown says so instead of counting the write syscall as a confirmation.
        self.assertEqual(sink._synced, 0)
        self.assertFalse(sink._final_ok)
        self.assertEqual(sink.unconfirmed, 1)
        self.assertFalse(sink.flush(0.1))

    def test_a_seal_that_fails_never_reports_a_settled_shutdown(self):
        sink = self.durable()
        self.assertIsNotNone(sink.emit_durable("runtime.started", "INFO", "succeeded"))

        def failed():
            raise OSError("synthetic seal failure")

        sink._sink.seal = failed
        self.assertFalse(sink.close(0.5))
        self.assertTrue(sink._owner_done.wait(5.0))
        self.assertFalse(sink._final_ok)
        self.assertFalse(sink.flush(0.1))
        # The descriptor is still released exactly once, by the thread that opened it.
        self.assertIsNone(sink._sink.fd)

    def test_a_healthy_shutdown_still_settles_every_accepted_event(self):
        """The control: the stricter verdict must not turn every shutdown into a failure."""
        sink = self.durable()
        accepted = [sink.emit("cli.action.started", "INFO", "started") for _ in range(3)]
        self.assertTrue(sink.close(2.0))
        self.assertTrue(sink._owner_done.wait(5.0))
        self.assertTrue(sink._final_ok)
        self.assertTrue(sink.flush(0.1))
        self.assertEqual(sink.unconfirmed, 0)
        records = read_events(self.directory)
        self.assertEqual([record["sequence"] for record in records], accepted)

    def test_a_close_that_runs_out_of_its_bound_counts_what_was_not_yet_durable(self):
        sink = self.durable()
        sink.emit("runtime.started", "INFO", "succeeded")
        release = threading.Event()
        real = sink._sink.sync

        def held_sync():
            release.wait(5)
            return real()

        sink._sink.sync = held_sync
        try:
            # The bound expires with an accepted event not yet fsynced: that is recorded as
            # unconfirmed rather than reported as a prompt, clean stop.
            self.assertFalse(sink.close(0.05))
            self.assertEqual(sink.unconfirmed, 1)
        finally:
            release.set()
        self.assertTrue(sink._owner_done.wait(5.0))
        # The owner still finished its own work, and the event really is durable now - but the
        # confirmation that expired stays expired instead of being quietly rewritten.
        self.assertTrue(sink._final_ok)
        self.assertEqual(sink.unconfirmed, 1)
        self.assertEqual(len(read_events(self.directory)), 1)


class CancellationSettlementTests(unittest.IsolatedAsyncioTestCase):
    """A cancelled call's terminal record is owned by the sink, not by the cancelled task.

    The upstream is a real TLS peer and the disk is really held open, so the terminal bound really
    expires. Nothing here flushes before asserting: a test that asks the sink to finish the work
    first would be doing the implementation's job, and would pass even if the record had been
    dropped when the task went away.
    """

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.logs = self.directory / "logs"
        self.sink = diagnostics.Diagnostics(str(self.logs), terminal_timeout=0.25)
        diagnostics.activate(self.sink)
        self.addCleanup(diagnostics.reset)
        self.addCleanup(self.stop_sink)
        self.tls = certificates(self.directory / "tls")
        self.release = threading.Event()
        self.held = 0
        self.holding = threading.Event()

    def stop_sink(self):
        self.release.set()
        self.sink.close(2.0)
        self.sink._owner_done.wait(10.0)

    def raw(self):
        """What is really on disk right now, read without asking the sink to do anything."""
        return read_events(self.logs)

    def hold_writes(self):
        """Hold the next write in the owner thread, so the terminal bound really expires."""
        real = self.sink._sink.write

        def held(line):
            self.held += 1
            self.holding.set()
            self.release.wait(10)
            return real(line)

        self.sink._sink.write = held

    async def hanging_upstream(self):
        entered = asyncio.Event()

        async def hang(request):
            entered.set()
            await asyncio.sleep(2.0)
            return web.json_response({})

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(
            self.tls["valid"]["certificate_file"], self.tls["valid"]["private_key_file"]
        )
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", hang)
        runner, base = await start_server(app, tls=context)
        self.addAsyncCleanup(runner.cleanup)
        return entered, base

    def settings_for(self, base):
        return {
            "base_url": base,
            "token_env": "TS012_ADMIN",
            "ca_file": self.tls["valid"]["certificate_file"],
            "timeout_seconds": 10,
        }

    async def cancelled_call(self):
        """One real in-flight call, cancelled: returns once its handler reached the wait."""
        entered, base = await self.hanging_upstream()
        payload = {"query": {"request_id": "request:synthetic-cancel-settlement"}}
        with patch.dict(os.environ, {"TS012_ADMIN": CREDENTIAL}, clear=False):
            task = asyncio.ensure_future(
                transport.core_web_call(
                    self.settings_for(base), "web-snapshot", payload, None, None
                )
            )
            await asyncio.wait_for(entered.wait(), 5)
            # The started event is durable before the hold, so the only record still owed is the
            # cancelled terminal itself.
            self.assertTrue(await wait_for(lambda: self.sink._synced >= 1))
            self.hold_writes()
            task.cancel()
            self.assertTrue(await wait_for(self.holding.is_set, timeout=5))
        return task

    async def test_a_cancelled_terminal_is_counted_when_its_bound_expires(self):
        task = await self.cancelled_call()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        # No flush, no close, no help: the record could not be confirmed inside the terminal bound,
        # and that is what the process says about itself.
        self.assertEqual(self.sink.state, diagnostics.UNAVAILABLE)
        self.assertGreaterEqual(self.sink.unconfirmed, 1)
        self.assertEqual(self.sink.error, "log_flush_timeout")
        # The obligation is still the sink's, and the sink really does write it once the disk moves:
        # a cancelled caller hands the record over instead of taking it away with it.
        self.release.set()
        self.assertTrue(await wait_for(lambda: self.sink._synced >= self.sink._sequence))
        self.assertEqual(
            [
                record["outcome"]
                for record in self.raw()
                if record["event"] == "outbound.call.cancelled"
            ],
            ["cancelled"],
        )
        self.assertEqual(self.sink._owed, set())

    async def test_a_second_cancellation_cannot_remove_the_confirmation_deadline(self):
        """A wait can be cancelled twice; the obligation it carried cannot be cancelled at all.

        The disk is never released while this asserts, and nothing is flushed on the sink's behalf.
        The second cancellation takes the timeout away with it - the code that would have reported
        "not confirmed" never runs - so a sink that only noticed at its deadline would answer "still
        durable, nothing unconfirmed" at every later moment. That is what a promise with no deadline
        looks like, and it is the one thing this must never say. The obligation is settled when the
        wait dies, and the waiter that died with it is deregistered rather than left behind.
        """
        task = await self.cancelled_call()
        accepted = self.sink._sequence
        # The first cancellation is already inside the terminal confirmation; the second one lands
        # on the confirmation wait itself.
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        # Well past the 250ms terminal bound, disk still held.
        await asyncio.sleep(0.4)
        self.assertEqual(self.sink.state, diagnostics.UNAVAILABLE)
        self.assertEqual(self.sink.error, "log_flush_timeout")
        self.assertGreaterEqual(self.sink.unconfirmed, 1)
        # Settled, not left as a memory: nothing is still owed, and no terminated wait is registered.
        self.assertEqual(self.sink._owed, set())
        self.assertEqual(self.sink._waiters, [])
        # The accepted record is neither dropped nor re-sent: it is still queued, and the durable
        # watermark has not moved past it.
        self.assertEqual(self.sink._sequence, accepted)
        self.assertLess(self.sink._synced, accepted)
        # Releasing the disk now is cleanup, not the assertion: the owner still writes what was
        # accepted, exactly once.
        self.release.set()
        self.assertTrue(await wait_for(lambda: self.sink._synced >= self.sink._sequence))
        records = [record for record in self.raw() if record["event"] == "outbound.call.cancelled"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["outcome"], "cancelled")

    async def test_a_cancelled_admission_is_deregistered_without_inventing_a_failure(self):
        """An admission nobody is waiting for any more leaves no waiter and claims nothing.

        Nobody was promised a confirmation here - the caller was torn down before it ever got an
        answer - so the sink is not failed and no event is counted: the accepted record simply stays
        queued for the owner, exactly as it was. What must not survive the cancellation is the
        registered wait, because a terminated waiter for a promise nobody is waiting for is how
        waits accumulate.
        """
        self.hold_writes()
        task = asyncio.ensure_future(diagnostics.accept_async(diagnostics.new_correlation()))
        self.assertTrue(await wait_for(self.holding.is_set, timeout=5))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        self.assertEqual(self.sink._waiters, [])
        self.assertEqual(self.sink.state, diagnostics.DURABLE)
        self.assertEqual(self.sink.unconfirmed, 0)
        # Cleanup only: the owner writes the accepted record once the disk moves, and the sink is
        # still the durable sink it was.
        self.release.set()
        self.assertTrue(await wait_for(lambda: self.sink._synced >= self.sink._sequence))
        self.assertEqual(self.sink.state, diagnostics.DURABLE)

    async def test_a_cancelled_terminal_racing_a_shutdown_is_still_written(self):
        task = await self.cancelled_call()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        # The shutdown bound expires while the disk is held: it reports that it could not confirm
        # settlement rather than reporting a clean, prompt stop.
        self.assertFalse(self.sink.close(0.05))
        self.assertGreaterEqual(self.sink.unconfirmed, 1)
        self.release.set()
        self.assertTrue(self.sink._owner_done.wait(10))
        # The owner still finished its own work: the record is durable, and the segment is sealed.
        self.assertTrue(self.sink._final_ok)
        self.assertTrue(await wait_for(lambda: bool(self.raw())))
        self.assertEqual(
            [
                record["outcome"]
                for record in self.raw()
                if record["event"] == "outbound.call.cancelled"
            ],
            ["cancelled"],
        )


class SlowDiskTests(SinkTestCase):
    """A slow disk costs a caller its bound; it never costs the process its heartbeat."""

    def slow_sink(self, *, write=None, sync=None, **kwargs):
        sink = self.durable(**kwargs)
        if write is not None:
            sink._sink.write = write
        if sync is not None:
            sink._sink.sync = sync
        return sink

    def test_a_slow_fsync_does_not_block_the_event_loop(self):
        sink = self.durable(admit_timeout=1.0)
        real = sink._sink.sync
        release = threading.Event()

        def slow_sync():
            release.wait(5)
            return real()

        sink._sink.sync = slow_sync

        async def measure():
            ticks = []

            async def ticker():
                while True:
                    started = time.monotonic()
                    await asyncio.sleep(0.01)
                    ticks.append((time.monotonic() - started) * 1000.0)

            task = asyncio.ensure_future(ticker())
            admission = asyncio.ensure_future(
                diagnostics.accept_async(diagnostics.new_correlation())
            )
            # The fsync is held for a while, and the loop is free for the whole of it: a heartbeat
            # that had to wait for the disk would show up here as one long tick.
            await asyncio.sleep(0.15)
            release.set()
            try:
                admitted = await asyncio.wait_for(admission, 5)
            finally:
                task.cancel()
            return admitted, ticks

        admitted, ticks = asyncio.run(measure())
        # The admission really happened, and the loop kept its own schedule while it waited for the
        # fsync: the wait yields instead of blocking the thread that serves every other request.
        self.assertIsNotNone(admitted)
        self.assertTrue(ticks)
        # `ticks` are milliseconds: the loop's own 10ms cadence held while the disk was held for
        # 150ms, so no tick is anywhere near the length of the stall.
        self.assertLess(max(ticks), 100.0)

    def test_an_admission_that_cannot_be_confirmed_refuses_the_work(self):
        sink = self.durable(admit_timeout=0.05)
        release = threading.Event()
        real = sink._sink.write

        def held(line):
            release.wait(5)
            return real(line)

        sink._sink.write = held
        try:
            started = time.monotonic()
            admitted = diagnostics.accept(diagnostics.new_correlation())
            elapsed = time.monotonic() - started
        finally:
            release.set()
        self.assertIsNone(admitted)
        # Bounded: it gives up inside its budget instead of waiting for the disk.
        self.assertLess(elapsed, 1.0)
        self.assertGreaterEqual(elapsed, 0.04)
        self.assertEqual(sink.state, diagnostics.UNAVAILABLE)
        self.assertFalse(diagnostics.admittable())
        self.assertEqual(sink.unconfirmed, 1)

    def test_a_terminal_event_is_confirmed_before_the_answer_and_never_claims_it_early(self):
        sink = self.durable()
        sink.emit_durable("runtime.starting", "INFO", "started")
        release = threading.Event()
        entered = threading.Event()
        real = sink._sink.write

        def held(line):
            entered.set()
            release.wait(5)
            return real(line)

        sink._sink.write = held
        try:
            started = time.monotonic()
            confirmed = diagnostics.finish(200, 1.0, diagnostics.new_correlation())
            elapsed = time.monotonic() - started
            # The write was still held when the answer came back, so it cannot have been a real
            # confirmation - and it did not pretend to be one.
            self.assertTrue(entered.is_set())
            self.assertFalse(confirmed)
            self.assertLess(elapsed, 1.0)
            self.assertGreaterEqual(elapsed, 0.2)
            self.assertEqual(sink.state, diagnostics.UNAVAILABLE)
        finally:
            release.set()
        self.assertTrue(sink.flush(5.0) is False or sink.state == diagnostics.UNAVAILABLE)

    def test_a_terminal_event_is_confirmed_when_the_disk_is_healthy(self):
        self.durable()
        self.assertTrue(diagnostics.finish(200, 1.0, diagnostics.new_correlation()))
        self.assertEqual(diagnostics.current_correlation(), None)

    def test_the_queue_saturating_never_drops_an_accepted_event(self):
        release = threading.Event()
        sink = self.durable(queue_limit=4, admit_timeout=0.05)
        real = sink._sink.write

        def held(line):
            release.wait(5)
            return real(line)

        sink._sink.write = held
        accepted = [diagnostics.event("cli.action.started", "INFO", "started") for _ in range(20)]
        self.assertTrue(wait_until(lambda: sink.state == diagnostics.UNAVAILABLE, timeout=3))
        self.assertFalse(sink.admit())
        self.assertIsNone(diagnostics.accept(diagnostics.new_correlation()))
        release.set()
        sink.close(3.0)
        # Everything that was accepted before the refusal is still there: the sink stopped taking
        # new work, it did not throw away what it had already promised to write.
        records = read_events(self.directory)
        self.assertEqual(
            [record["sequence"] for record in records],
            sorted(record["sequence"] for record in records),
        )
        self.assertGreaterEqual(len(records), sum(1 for value in accepted if value is not None))


class OwnerTests(SinkTestCase):
    """One thread owns the file, and a caller that runs out of patience never takes it away."""

    def test_close_is_bounded_and_the_owner_still_finishes_on_its_own(self):
        sink = self.durable()
        sink.emit_durable("runtime.starting", "INFO", "started")
        release = threading.Event()
        real = sink._sink.sync

        def held_sync():
            release.wait(5)
            return real()

        sink._sink.sync = held_sync
        started = time.monotonic()
        confirmed = sink.close(0.05)
        elapsed = time.monotonic() - started
        # Bounded, honest about it, and refusing new work from the instant it was asked to stop.
        self.assertFalse(confirmed)
        self.assertLess(elapsed, 1.0)
        self.assertFalse(sink.admit())
        self.assertIsNone(diagnostics.accept(diagnostics.new_correlation()))
        self.assertFalse(sink._owner_done.is_set())
        release.set()
        # The owner is left alone to finish: it seals its own segment and closes its own descriptor.
        self.assertTrue(sink._owner_done.wait(5.0))
        self.assertIsNone(sink._sink.fd)
        self.assertTrue(sink.sealed_segments())

    def test_flush_never_fsyncs_on_the_callers_thread(self):
        sink = self.durable()
        seen = []
        real = sink._sink.sync

        def sync():
            seen.append(threading.current_thread().name)
            return real()

        sink._sink.sync = sync
        sink.emit("cli.action.started", "INFO", "started")
        self.assertTrue(sink.flush(2.0))
        self.assertTrue(seen)
        self.assertEqual(set(seen), {"tianshu-diagnostics"})

    def test_a_concurrent_shutdown_and_recovery_never_races_the_writer(self):
        sink = self.durable(admit_timeout=0.1)
        real = sink._sink.write
        broke = {"on": True}

        def broken(line):
            if broke["on"]:
                raise OSError("device not configured")
            return real(line)

        sink._sink.write = broken
        sink.emit("cli.action.started", "INFO", "started")
        self.assertTrue(wait_until(lambda: sink.state == diagnostics.UNAVAILABLE))
        errors = []

        def recover():
            try:
                sink.recover()
            except Exception as exc:  # pragma: no cover - a raise here is the failure
                errors.append(exc)

        def close():
            try:
                sink.close(2.0)
            except Exception as exc:  # pragma: no cover - a raise here is the failure
                errors.append(exc)

        broke["on"] = False
        threads = [threading.Thread(target=recover), threading.Thread(target=close)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(errors, [])
        # Whatever the race decided, the owner ended up closing exactly one descriptor and the sink
        # never claimed a durability it had not proved.
        self.assertTrue(sink._owner_done.wait(5.0))
        self.assertIsNone(sink._sink.fd)
        for path in log_files(self.directory):
            raw = path.read_bytes()
            self.assertNotIn(b"\r", raw)
            for line in raw.split(b"\n"):
                if line:
                    self.assertEqual(tuple(json.loads(line.decode("utf-8"))), FIELDS)

    def test_retransmission_obeys_capacity_and_identity(self):
        sink = self.durable(queue_limit=2)
        sink.emit_durable("cli.action.started", "INFO", "started")
        sink.flush()
        original = read_events(self.directory)[0]
        # Another instance's record is not this sink's to re-send, and neither is a sequence this
        # stream never allocated.
        foreign = dict(original, instance_id=str(uuid.uuid4()))
        self.assertFalse(sink.retransmit(foreign))
        ahead = dict(original, sequence=original["sequence"] + 5)
        self.assertFalse(sink.retransmit(ahead))
        self.assertFalse(sink.retransmit(dict(original, service="elsewhere")))
        self.assertTrue(sink.retransmit(original))
        sink.flush()
        records = read_events(self.directory)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["event_id"], records[1]["event_id"])
        self.assertEqual(records[0]["sequence"], records[1]["sequence"])

    def test_a_retransmission_never_confirms_events_that_are_still_waiting(self):
        release = threading.Event()
        sink = self.durable()
        first = sink.emit_durable("cli.action.started", "INFO", "started")
        sink.flush()
        original = read_events(self.directory)[0]
        real = sink._sink.write

        def held(line):
            release.wait(5)
            return real(line)

        sink._sink.write = held
        # A high-sequence retransmission must not be able to vouch for the events queued behind it.
        pending = sink.emit("cli.action.finished", "INFO", "succeeded")
        self.assertGreater(pending, first)
        self.assertTrue(sink.retransmit(original))
        self.assertIsNone(sink.admit_durable("runtime.stopping", "INFO", "started", timeout=0.05))
        release.set()
        sink.close(3.0)

    def test_recovery_stays_refused_until_a_real_write_succeeds(self):
        sink = self.durable(admit_timeout=0.05)
        real = sink._sink.write
        broke = {"on": True}

        def broken(line):
            if broke["on"]:
                raise OSError("device not configured")
            return real(line)

        sink._sink.write = broken
        sink.emit("cli.action.started", "INFO", "started")
        self.assertTrue(wait_until(lambda: sink.state == diagnostics.UNAVAILABLE))
        # While the recovery probe is being attempted the sink is neither durable nor admitting:
        # a state that only a confirmed fsync may leave.
        observed = []

        def watch():
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                observed.append(sink.state)
                if sink.state == diagnostics.DURABLE:
                    return
                time.sleep(0.005)

        watcher = threading.Thread(target=watch)
        watcher.start()
        self.assertFalse(sink.recover())
        watcher.join(5)
        self.assertFalse(sink.admit())
        self.assertEqual(sink.state, diagnostics.UNAVAILABLE)
        self.assertNotIn(diagnostics.DURABLE, observed)
        broke["on"] = False
        self.assertTrue(sink.recover())
        self.assertEqual(sink.state, diagnostics.DURABLE)
        self.assertTrue(sink.admit())
        sink.flush()
        names = [record["event"] for record in read_events(self.directory)]
        self.assertIn("cli.action.started", names)
        self.assertIn("logging.recovered", names)
        self.assertIn("logging.unavailable", names)

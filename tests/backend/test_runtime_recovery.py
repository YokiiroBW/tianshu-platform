"""Runtime recovery on real log files and HTTP; no business request is retried."""

import asyncio
import io
import json
import os
import ssl
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import aiohttp
from aiohttp import web

from runtime_fixture import READY_TOKEN, environment, read_events, runtime_settings

from services.platform import __main__ as entrypoint
from services.platform import diagnostics
from services.platform.diagnostics_recovery import DiagnosticsRecovery
from services.platform.service import Platform


async def until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("runtime fact did not arrive within its bound")
        await asyncio.sleep(0.005)


class RecoveryTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "logs"
        self.sink = diagnostics.Diagnostics(
            self.directory, admit_timeout=0.05, terminal_timeout=0.05, stderr=io.StringIO()
        )
        self.worker = DiagnosticsRecovery(self.sink, interval=0.01, max_interval=0.08)
        self.release = threading.Event()
        self.addAsyncCleanup(self.finish)

    async def finish(self):
        self.release.set()
        await self.worker.stop()
        self.sink.close(1)
        await until(self.sink._owner_done.is_set)
        self.temp.cleanup()

    def hold_sync(self):
        entered = threading.Event()
        original = self.sink._sink.sync

        def held():
            entered.set()
            self.release.wait()
            original()

        self.sink._sink.sync = held
        return entered

    async def test_repeated_start_has_one_worker_and_healthy_logging_needs_no_recovery(self):
        with patch.object(self.sink, "recover", wraps=self.sink.recover) as recover:
            starters = [threading.Thread(target=self.worker.start) for _ in range(4)]
            for thread in starters:
                thread.start()
            for thread in starters:
                thread.join(1)
            owner = self.worker._thread
            self.worker.start()
            await asyncio.sleep(0.05)
            self.assertIs(self.worker._thread, owner)
            recover.assert_not_called()
        self.assertTrue(await self.worker.stop())
        self.assertFalse(owner.is_alive())
        self.worker.start()
        self.assertIs(self.worker._thread, owner)
        self.assertFalse(owner.is_alive())

    async def test_overlapping_recovery_creates_one_probe_and_waits_for_actual_fsync(self):
        entered = self.hold_sync()
        self.sink._fail("log_flush_timeout")
        results = []
        attempt = threading.Thread(target=lambda: results.append(self.sink.recover()))
        attempt.start()
        await until(entered.is_set)
        self.assertEqual(self.sink.state, diagnostics.RECOVERING)
        self.assertFalse(self.sink.admit())
        self.assertFalse(self.sink.recover())
        self.assertEqual(self.sink._sequence, 1)
        self.release.set()
        await until(lambda: not attempt.is_alive())
        self.assertEqual(results, [True])
        self.assertEqual(self.sink.state, diagnostics.DURABLE)
        self.assertTrue(self.sink.flush(1))
        events = read_events(self.directory)
        self.assertEqual(sum(event["event"] == "logging.recovered" for event in events), 1)

    async def test_stop_interrupts_recovery_without_waiting_for_the_blocked_disk(self):
        entered = self.hold_sync()
        self.sink._fail("log_flush_timeout")
        self.worker.start()
        await until(entered.is_set)
        started = time.monotonic()
        self.assertTrue(await self.worker.stop())
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(self.worker._thread.is_alive())
        self.assertFalse(self.sink.admit())
        self.assertFalse(self.sink._owner_done.is_set())
        self.release.set()
        await until(lambda: self.sink._synced == self.sink._sequence)
        self.assertNotEqual(self.sink.state, diagnostics.DURABLE)
        self.assertFalse(self.sink.recover())
        self.assertFalse(self.sink.admit())
        self.assertIsNone(asyncio.get_running_loop()._default_executor)

    async def test_failed_fsync_reuses_probe_bytes_and_really_syncs_on_retry(self):
        failing = threading.Event()
        failing.set()
        original = self.sink._sink.sync

        def sync():
            if failing.is_set():
                raise OSError("synthetic persistent fsync failure")
            original()

        self.sink._sink.sync = sync
        self.sink.emit("runtime.started", "INFO", "succeeded")
        await until(lambda: self.sink.state == diagnostics.UNAVAILABLE)
        self.assertFalse(self.sink.recover())
        self.assertFalse(self.sink.recover())
        events = read_events(self.directory)
        self.assertEqual(len(events), 2)
        self.assertEqual(sum(event["event"] == "logging.recovered" for event in events), 1)
        self.assertFalse(self.sink.admit())
        failing.clear()
        self.assertTrue(self.sink.recover())
        self.assertTrue(self.sink.flush(1))
        events = read_events(self.directory)
        self.assertEqual(len({event["event_id"] for event in events}), len(events))
        self.assertEqual([event["sequence"] for event in events], list(range(1, len(events) + 1)))
        self.assertEqual(sum(event["event"] == "logging.recovered" for event in events), 1)
        self.assertGreater(self.sink.unconfirmed, 0)

    async def test_persistent_failure_backs_off_and_never_claims_durability(self):
        def broken(line):
            raise OSError("synthetic unwritable disk")

        self.sink._sink.write = broken
        self.sink.emit("runtime.started", "INFO", "succeeded")
        await until(lambda: self.sink.state == diagnostics.UNAVAILABLE)
        attempts = []
        original = self.sink.recover

        def recover():
            attempts.append(time.monotonic())
            return original()

        with patch.object(self.sink, "recover", side_effect=recover):
            self.worker.start()
            await until(lambda: len(attempts) >= 4)
            self.assertFalse(self.sink.admit())
            self.assertNotEqual(self.sink.state, diagnostics.DURABLE)
            self.assertEqual(read_events(self.directory), [])
            self.assertTrue(await self.worker.stop())
        gaps = [second - first for first, second in zip(attempts, attempts[1:])]
        self.assertGreaterEqual(gaps[0], 0.019)
        self.assertGreaterEqual(gaps[1], 0.039)
        self.assertGreaterEqual(gaps[2], 0.079)

    async def test_capacity_failure_remains_closed_with_one_pending_probe(self):
        self.sink._sink.directory_bytes = self.sink._sink.used
        self.sink.emit("runtime.started", "INFO", "succeeded")
        await until(lambda: self.sink.state == diagnostics.UNAVAILABLE)
        self.worker.start()
        await asyncio.sleep(0.2)
        self.assertFalse(self.sink.admit())
        self.assertNotEqual(self.sink.state, diagnostics.DURABLE)
        self.assertEqual(read_events(self.directory), [])
        probes = [item for item in self.sink._queue if item[0] == "line"]
        self.assertLessEqual(len(probes), 2)

    async def test_unconfigured_unopened_and_invalid_record_sinks_are_not_repaired(self):
        for sink in (
            diagnostics.Diagnostics(stderr=io.StringIO()),
            diagnostics.Diagnostics(
                self.directory / "missing", directory_bytes=1, stderr=io.StringIO()
            ),
            self.sink,
        ):
            with self.subTest(state=sink.state):
                if sink is self.sink:
                    sink._fail("log_record_invalid")
                worker = DiagnosticsRecovery(sink, interval=0.005)
                with patch.object(sink, "recover", wraps=sink.recover) as recover:
                    worker.start()
                    await asyncio.sleep(0.025)
                    self.assertTrue(await worker.stop())
                    recover.assert_not_called()
                sink.close(1)
                self.assertFalse(sink.recover())


class RuntimeRecoveryHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, environment(), clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(diagnostics.reset)
        self.settings = runtime_settings(self.temp.name)
        self.platform = Platform(self.settings)
        self.sink = diagnostics.Diagnostics(
            self.settings["diagnostics"]["log_directory"],
            admit_timeout=0.05,
            terminal_timeout=0.05,
            stderr=io.StringIO(),
        )
        diagnostics.activate(self.sink)
        self.release = threading.Event()
        self.stop = None
        self.task = None
        self.client = None
        self.worker = DiagnosticsRecovery(self.sink, interval=0.01, max_interval=0.08)
        self.addAsyncCleanup(self.finish)
        started = asyncio.Event()
        original_start = web.TCPSite.start

        async def start(site):
            await original_start(site)
            port = site._server.sockets[0].getsockname()[1]
            self.url = f"https://127.0.0.1:{port}"
            self.platform.settings["web"]["origin"] = self.url

        def install(stop):
            self.stop = stop
            started.set()

        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(
            self.settings["tls"]["certificate_file"], self.settings["tls"]["private_key_file"]
        )
        self.patches = (
            patch.object(web.TCPSite, "start", start),
            patch.object(entrypoint, "DiagnosticsRecovery", return_value=self.worker),
            patch.object(entrypoint, "SHUTDOWN_FLUSH_SECONDS", 0.05),
        )
        for replacement in self.patches:
            replacement.start()
            self.addCleanup(replacement.stop)
        self.task = asyncio.create_task(
            entrypoint.serve_forever(
                self.platform, self.sink, host="127.0.0.1", port=0, tls=tls, install_signals=install
            )
        )
        await asyncio.wait_for(started.wait(), 3)
        await until(lambda: self.sink._synced == self.sink._sequence)
        self.client = aiohttp.ClientSession(
            trust_env=False,
            timeout=aiohttp.ClientTimeout(total=3),
            connector=aiohttp.TCPConnector(
                ssl=ssl.create_default_context(cafile=self.settings["tls"]["certificate_file"])
            ),
        )

    async def finish(self):
        self.release.set()
        if self.client is not None:
            await self.client.close()
        if self.stop is not None:
            self.stop.set()
        if self.task is not None:
            await asyncio.wait_for(self.task, 3)
        self.sink.close(1)
        await until(self.sink._owner_done.is_set)
        self.platform.close()

    async def get(self, path, *, headers=None):
        async with self.client.get(self.url + path, headers=headers) as response:
            return response.status, await response.text()

    async def ready(self):
        return await self.get("/health/ready", headers={"Authorization": "Bearer " + READY_TOKEN})

    async def test_slow_fsync_returns_503_then_background_recovery_restores_http_and_ready(self):
        entered = threading.Event()
        original = self.sink._sink.sync

        def held():
            entered.set()
            self.release.wait()
            original()

        self.sink._sink.sync = held
        correlation = diagnostics.new_correlation()
        self.assertEqual((await self.get("/", headers={diagnostics.HEADER: correlation}))[0], 503)
        self.assertTrue(entered.is_set())
        self.assertEqual((await self.get("/"))[0], 503)
        self.assertEqual((await self.ready())[0], 503)
        self.assertEqual((await self.get("/health/live"))[0], 200)
        await asyncio.sleep(0.12)
        self.assertFalse(self.sink.admit())
        self.release.set()
        await until(lambda: self.sink.state == diagnostics.DURABLE)
        self.assertEqual((await self.get("/"))[0], 200)
        deadline = time.monotonic() + 2
        while (await self.ready())[0] != 200:
            self.assertLess(time.monotonic(), deadline)
            await asyncio.sleep(0.025)
        await until(lambda: self.sink._synced == self.sink._sequence)
        events = read_events(self.settings["diagnostics"]["log_directory"])
        self.assertEqual(len({event["event_id"] for event in events}), len(events))
        self.assertEqual(sum(event["event"] == "logging.recovered" for event in events), 1)
        self.assertEqual(
            sum(
                event["event"] == "http.request.started" and event["correlation_id"] == correlation
                for event in events
            ),
            1,
        )
        sequence = self.sink._sequence
        await self.ready()
        await self.get("/health/live")
        self.assertEqual(self.sink._sequence, sequence)

    async def test_persistent_write_failure_keeps_real_http_and_ready_red(self):
        def broken(line):
            raise OSError("synthetic persistent disk failure")

        self.sink._sink.write = broken
        self.sink.emit("runtime.started", "INFO", "succeeded")
        await until(lambda: self.sink.state == diagnostics.UNAVAILABLE)
        await asyncio.sleep(0.2)
        self.assertEqual((await self.get("/"))[0], 503)
        status, document = await self.ready()
        self.assertEqual(status, 503)
        self.assertIn('"logging":"failed"', document.replace(" ", ""))
        self.assertEqual((await self.get("/health/live"))[0], 200)
        self.assertFalse(self.sink.admit())

    async def test_runtime_stop_finishes_without_unblocking_disk_or_leaving_recovery_worker(self):
        entered = threading.Event()
        original = self.sink._sink.sync

        def held():
            entered.set()
            self.release.wait()
            original()

        self.sink._sink.sync = held
        self.assertEqual((await self.get("/"))[0], 503)
        await until(lambda: self.sink.state == diagnostics.RECOVERING)
        started = time.monotonic()
        self.stop.set()
        await asyncio.wait_for(self.task, 0.75)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(self.worker._thread.is_alive())
        self.assertFalse(self.sink.admit())
        self.assertFalse(self.sink._owner_done.is_set())
        self.release.set()
        await until(self.sink._owner_done.is_set)
        self.assertFalse(self.sink.recover())
        self.assertNotEqual(self.sink.state, diagnostics.DURABLE)

    async def test_asyncio_run_exits_with_the_disk_still_blocked(self):
        # A real process exit catches an executor accidentally holding asyncio.run open. The
        # child's file owner stays blocked forever, while its recovery worker must already stop.
        child = Path(self.temp.name) / "child"
        child.mkdir()
        settings = json.loads(json.dumps(self.settings))
        settings["database_path"] = str(child / "authority.sqlite")
        settings["diagnostics"]["log_directory"] = str(child / "logs")
        path = child / "settings.json"
        path.write_text(json.dumps(settings), encoding="utf-8")
        source = """
import asyncio, json, sys, threading, time
from services.platform import __main__ as entrypoint, diagnostics
from services.platform.diagnostics_recovery import DiagnosticsRecovery
from services.platform.service import Platform
from services.platform.transport import server_tls
settings = json.loads(open(sys.argv[1], encoding='utf-8').read())
platform = Platform(settings)
sink = diagnostics.Diagnostics(settings['diagnostics']['log_directory'], admit_timeout=0.05)
diagnostics.activate(sink)
blocked = threading.Event()
sink._sink.sync = blocked.wait
worker = DiagnosticsRecovery(sink, interval=0.005, max_interval=0.02)
entrypoint.DiagnosticsRecovery = lambda _: worker
entrypoint.SHUTDOWN_FLUSH_SECONDS = 0.05
def install(stop):
    loop = asyncio.get_running_loop()
    loop.call_later(0.01, sink._fail, 'log_flush_timeout')
    loop.call_later(0.15, stop.set)
async def run():
    await entrypoint.serve_forever(platform, sink, host='127.0.0.1', port=0,
        tls=server_tls(settings['tls']), install_signals=install)
    return {'recovery_alive': worker._thread.is_alive(), 'admitting': sink.admit(),
        'owner_blocked': not sink._owner_done.is_set(),
        'executor_unused': asyncio.get_running_loop()._default_executor is None}
print(json.dumps(asyncio.run(run())))
"""
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            source,
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 3)
        finally:
            if process.returncode is None:
                process.kill()
                await process.communicate()
        self.assertEqual(process.returncode, 0, stderr.decode())
        self.assertEqual(
            json.loads(stdout),
            {
                "recovery_alive": False,
                "admitting": False,
                "owner_blocked": True,
                "executor_unused": True,
            },
        )

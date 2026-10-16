"""No-network Linux proof: platform Executor -> fixed node transport -> synthetic sleep.

Stream the test and frozen sources into a read-only container's /tmp. Cleanup addresses only
known synthetic PIDs/groups, including after a failed assertion; no SSH or Docker is used.
"""

import asyncio
import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


def active(pid):
    try:
        status = Path(f"/proc/{pid}/stat").read_text()
        return status.rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def write_ready(path, document):
    temporary = path.with_suffix(".writing")
    temporary.write_text(json.dumps(document))
    temporary.replace(path)


def executor_fixture(directory, node_root, mode):
    sys.path.insert(0, node_root)
    from ops.infrastructure.transport import Deadline, run_bounded

    json.loads(sys.stdin.buffer.read())
    child_file = Path(directory) / "child.json"
    code = (
        "import json,os,pathlib,sys,time; "
        "p=pathlib.Path(sys.argv[1]); t=p.with_suffix('.writing'); "
        "t.write_text(json.dumps({'pid':os.getpid(),'group':os.getpgrp(),"
        "'executor_group':int(sys.argv[2])})); t.replace(p); time.sleep(60)"
    )

    def collect():
        run_bounded(
            [sys.executable, "-B", "-c", code, str(child_file), str(os.getpgrp())],
            b"",
            Deadline(30),
        )

    if mode == "parent_exit":
        threading.Thread(target=collect, daemon=True).start()
        deadline = time.monotonic() + 10
        while not child_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        os._exit(0)
    collect()


def configuration(directory, node_root, contract, mode="cancel"):
    return {
        "executor": {
            "command": [
                sys.executable,
                "-B",
                str(Path(__file__).resolve()),
                "--executor",
                directory,
                str(node_root),
                mode,
            ],
            "directory": str(node_root),
        },
        "timeout_ms": 30000,
        "contract_directory": str(contract),
    }


async def platform_fixture(directory, node_root, contract, platform_root):
    sys.path.insert(0, platform_root)
    from services.platform.infrastructure.client import Executor

    executor = Executor(configuration(directory, node_root, contract))
    worker = asyncio.create_task(executor.invoke("synthetic-node", "node.snapshot"))
    deadline = time.monotonic() + 20
    while not (Path(directory) / "child.json").exists() and time.monotonic() < deadline:
        if worker.done():
            await worker
        await asyncio.sleep(0.01)
    write_ready(
        Path(directory) / "platform.json",
        {"supervisor": next(iter(executor.processes)).pid},
    )
    # Simulate abrupt platform death, bypassing close()/finally. PDEATHSIG must retire the tree.
    os._exit(0)


@unittest.skipUnless(sys.platform == "linux", "Requires real Linux /proc and process groups")
class InfrastructurePosixLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ts182-posix-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.node_root = Path(os.environ["TS182_NODE_TRANSPORT_ROOT"]).resolve()
        self.contract = Path(os.environ["TS177_NODE_CONTRACT_DIR"]).resolve()

    async def child(self, worker):
        ready = self.directory / "child.json"
        deadline = time.monotonic() + 20
        while not ready.exists() and time.monotonic() < deadline:
            if worker.done():
                await worker
            await asyncio.sleep(0.02)
        self.assertTrue(ready.exists(), "Synthetic child did not start")
        document = json.loads(ready.read_text())
        self.assertNotEqual(document["group"], document["executor_group"])
        return document["pid"]

    async def assert_retired(self, child, case, started):
        deadline = time.monotonic() + 2
        while Path(f"/proc/{child}").exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        exists = Path(f"/proc/{child}").exists()
        print(
            json.dumps(
                {
                    "case": case,
                    "separate_groups": True,
                    "descendant_alive_after_retirement": active(child),
                    "descendant_reaped": not exists,
                    "retire_seconds": round(time.monotonic() - started, 3),
                }
            ),
            flush=True,
        )
        self.assertFalse(exists, "Synthetic child survived or remained unreaped after retirement")

    def cleanup_known_child(self):
        ready = self.directory / "child.json"
        if ready.exists():
            child = json.loads(ready.read_text())["pid"]
            if active(child):
                try:
                    os.killpg(child, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    async def executor_case(self, mode):
        from services.platform.contracts import Fault
        from services.platform.infrastructure.client import Executor

        executor = Executor(configuration(str(self.directory), self.node_root, self.contract, mode))
        worker = asyncio.create_task(executor.invoke("synthetic-node", "node.snapshot"))
        try:
            child = await self.child(worker)
            started = time.monotonic()
            if mode == "cancel":
                worker.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(worker, 5)
            else:
                with self.assertRaises(Fault) as failure:
                    await asyncio.wait_for(worker, 5)
                self.assertEqual(failure.exception.code, "infrastructure_response_invalid")
            self.assertFalse(executor.processes)
            await self.assert_retired(child, mode, started)
        finally:
            if not worker.done():
                worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            await executor.close()
            self.cleanup_known_child()

    async def test_outer_cancellation_retires_the_separate_ssh_process_group(self):
        await self.executor_case("cancel")

    async def test_executor_exits_first_retires_and_reaps_the_adopted_child(self):
        await self.executor_case("parent_exit")

    async def test_platform_exits_without_finally_retires_the_separate_process_group(self):
        from services.platform.infrastructure import client

        platform_root = Path(client.__file__).resolve().parents[3]
        outer = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-B",
            str(Path(__file__).resolve()),
            "--platform",
            str(self.directory),
            str(self.node_root),
            str(self.contract),
            str(platform_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            started = time.monotonic()
            stdout, stderr = await asyncio.wait_for(outer.communicate(), 20)
            self.assertEqual((outer.returncode, stdout, stderr), (0, b"", b""))
            child = json.loads((self.directory / "child.json").read_text())["pid"]
            await self.assert_retired(child, "platform_parent_death", started)
        finally:
            if outer.returncode is None:
                outer.kill()
                await outer.wait()
            self.cleanup_known_child()
            ready = self.directory / "platform.json"
            if ready.exists():
                supervisor = json.loads(ready.read_text())["supervisor"]
                if active(supervisor):
                    os.killpg(supervisor, signal.SIGKILL)

    async def test_supervisor_preserves_protocol_bytes_and_actual_exit_code(self):
        from services.platform.infrastructure import supervisor

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-B",
            supervisor.__file__,
            "--parent-pid",
            str(os.getpid()),
            "--",
            sys.executable,
            "-I",
            "-B",
            "-c",
            "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read());"
            "sys.stderr.write('synthetic stderr');sys.exit(17)",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        protocol = '{"protocol":"保留原始 UTF-8 输出"}\n'.encode()
        stdout, stderr = await asyncio.wait_for(process.communicate(protocol), 5)
        self.assertEqual((stdout, stderr, process.returncode), (protocol, b"synthetic stderr", 17))
        print(json.dumps({"case": "protocol_passthrough", "actual_exit_code": 17}), flush=True)

    async def test_supervisor_rejects_parent_mismatch_before_spawning(self):
        from services.platform.infrastructure import supervisor

        started = self.directory / "should-not-start"
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-B",
            supervisor.__file__,
            "--parent-pid",
            str(os.getpid() + 1),
            "--",
            sys.executable,
            "-c",
            "import pathlib,sys;pathlib.Path(sys.argv[1]).touch()",
            str(started),
        )
        self.assertEqual(await asyncio.wait_for(process.wait(), 5), 126)
        self.assertFalse(started.exists())


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--executor":
        executor_fixture(sys.argv[2], sys.argv[3], sys.argv[4])
    elif len(sys.argv) > 1 and sys.argv[1] == "--platform":
        asyncio.run(platform_fixture(*sys.argv[2:]))
    else:
        unittest.main(verbosity=2)

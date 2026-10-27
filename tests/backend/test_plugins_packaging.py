"""Installed plugin execution from the Docker builder's actual source inputs.

This validates Python packaging on the local OS. It does not execute a Linux
image, apt or external dependency installation.
"""

import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

from services.platform.contracts import Fault
from services.platform.plugins.bootstrap import initialize

ROOT = Path(__file__).resolve().parents[2]
STORAGE = "tianshu_storage-0.1.3-py3-none-any.whl"
STORAGE_HASH = "a87947a1260fd24de5ae241f19fcc3bb2c2d3fa2869c9a0328825b5f81789d0f"
SMOKE = r"""
import asyncio
import importlib.metadata
import json
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])
import tianshu_plugin
from services.platform.plugins import PluginManager
from services.platform.plugins.process import WORKER
from services.platform.plugins.views import BRIDGE

assert tianshu_plugin.__version__ == "1.0.0"
assert WORKER.is_file() and BRIDGE.is_file()
assert importlib.metadata.version("tianshu-storage") == "0.1.3"
assert "tianshu-storage==0.1.3" in importlib.metadata.requires("tianshu-platform-backend")

async def finished(read, identifier):
    async with asyncio.timeout(20):
        while True:
            value = read(identifier)
            if value["execution"] not in {"accepted", "running"}:
                assert value["execution"] == "succeeded", value
                return value
            await asyncio.sleep(.02)

async def main():
    host = PluginManager({"database_path": str(Path(sys.argv[2]) / "platform.sqlite")})
    await host.start()
    try:
        call = await host.install({"type": "builtin", "id": "hello"}, "packaged-install")
        package = await finished(lambda key: host.repository.get("operations", key), call["id"])
        instance = await host.create(package["package_id"], "Packaged hello", "packaged-instance")
        instance = await host.state(instance["id"], True, instance["revision"])
        call = await host.executions.invoke(
            instance["id"], "echo", {"text": "installed wheel"}, "packaged-call", "operator"
        )
        result = await finished(host.repository.call, call["id"])
        assert result["result"] == {"text": "installed wheel"}, result
    finally:
        await host.close()

asyncio.run(main())
from pip._internal.cli.main import main as pip
assert pip(["check"]) == 0
print(json.dumps({"sdk": tianshu_plugin.__version__, "storage": "0.1.3", "worker": "passed"}))
"""


def builder_sources(stage):
    definition = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    builder = definition.split(" AS builder\n", 1)[1].split(" AS runtime\n", 1)[0]
    for line in builder.splitlines():
        if not line.startswith("COPY "):
            continue
        *sources, destination = shlex.split(line)[1:]
        for source in sources:
            path = ROOT / source
            target = stage / destination
            if path.is_dir():
                shutil.copytree(path, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                target.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target / path.name)


class PluginPackagingTests(unittest.TestCase):
    def test_maintenance_initialization_rejects_unbound_sqlite_without_creating_files(self):
        with tempfile.TemporaryDirectory(prefix="ts294-unbound-", dir=ROOT / ".runtime") as root:
            with self.assertRaisesRegex(Fault, "plugin_store_unavailable"):
                initialize({"database_path": str(Path(root) / "platform.sqlite")})
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_docker_source_inputs_build_an_installed_runnable_host_and_sdk(self):
        with tempfile.TemporaryDirectory(prefix="ts294-package-", dir=ROOT / ".runtime") as root:
            root = Path(root)
            stage, wheels, installed = root / "stage", root / "wheels", root / "installed"
            stage.mkdir()
            wheels.mkdir()
            builder_sources(stage)
            wheel = stage / "vendor" / STORAGE
            self.assertEqual(hashlib.sha256(wheel.read_bytes()).hexdigest(), STORAGE_HASH)
            built = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--no-cache-dir",
                    "--no-deps",
                    "--no-build-isolation",
                    "--no-index",
                    "--wheel-dir",
                    str(wheels),
                    str(stage),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            platform = next(wheels.glob("tianshu_platform_backend-*.whl"))
            with zipfile.ZipFile(platform) as archive:
                for entry in (
                    "tianshu_plugin/worker.py",
                    "tianshu_plugin/view_bridge.js",
                    "services/platform/plugins/builtin/hello/main.py",
                    "services/platform/plugins/builtin/comic-files/main.py",
                    "services/platform/plugins/builtin/comic-files/contract.json",
                    "services/platform/plugins/builtin/comic-files/plugin.json",
                ):
                    self.assertIn(entry, archive.namelist())
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--no-deps",
                    "--target",
                    str(installed),
                    str(platform),
                    str(wheel),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            active = root / "active"
            active.mkdir()
            smoke = subprocess.run(
                [sys.executable, "-I", "-c", SMOKE, str(installed), str(active)],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(smoke.returncode, 0, smoke.stdout + smoke.stderr)
            self.assertEqual(json.loads(smoke.stdout.splitlines()[-1])["worker"], "passed")

"""Build input boundary tests; these do not claim an image was built."""

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "snapshot_contract", ROOT / "scripts/build/snapshot_contract.py"
)
snapshot_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot_module)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "source"
        self.source.mkdir()
        self.target = Path(self.temp.name) / "target"

    def fixture(self, member="README.md", normalize=False):
        data = b"contract\r\n"
        (self.source / "README.md").write_bytes(data)
        manifest = {
            "files": {
                member: hashlib.sha256(
                    data.replace(b"\r\n", b"\n") if normalize else data
                ).hexdigest()
            }
        }
        if normalize:
            manifest["hash_basis"] = "UTF-8 text bytes with CRLF normalized to LF"
        raw = json.dumps(manifest).encode()
        (self.source / "manifest.json").write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def test_raw_bytes_preserved(self):
        result = snapshot_module.snapshot(self.source, self.target, self.fixture())
        self.assertEqual((self.target / "README.md").read_bytes(), b"contract\r\n")
        self.assertEqual(result["README.md"], hashlib.sha256(b"contract\r\n").hexdigest())

    def test_declared_normalization_only_for_verification(self):
        snapshot_module.snapshot(self.source, self.target, self.fixture(normalize=True))
        self.assertEqual((self.target / "README.md").read_bytes(), b"contract\r\n")

    def test_wrong_manifest_pin_writes_nothing(self):
        self.fixture()
        with self.assertRaises(ValueError):
            snapshot_module.snapshot(self.source, self.target, "0" * 64)
        self.assertFalse(self.target.exists())

    def test_modified_member_writes_nothing(self):
        pin = self.fixture()
        (self.source / "README.md").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            snapshot_module.snapshot(self.source, self.target, pin)
        self.assertFalse(self.target.exists())

    def test_traversal_rejected(self):
        for name in ("../README.md", "/README.md", "C:/README.md", "a\\README.md"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                snapshot_module.snapshot(self.source, self.target, self.fixture(name))
        self.assertFalse(self.target.exists())

    def test_existing_target_is_never_overwritten(self):
        self.target.mkdir()
        with self.assertRaises(ValueError):
            snapshot_module.snapshot(self.source, self.target, self.fixture())


class PinsTests(unittest.TestCase):
    def test_runtime_versions_match_existing_development_closure_without_ruff(self):
        import re

        def pins(path):
            return dict(re.findall(r"^([\w-]+)==([^\s]+)", path.read_text(), re.M))

        expected = pins(ROOT / "requirements-dev.txt")
        expected.pop("ruff")
        self.assertEqual(pins(ROOT / "scripts/build/runtime.lock"), expected)

    def test_build_lock_directory_is_not_excluded(self):
        ignored = (ROOT / ".dockerignore").read_text().splitlines()
        for pattern in ("scripts", "scripts/build", "**/build", "*.lock"):
            self.assertNotIn(pattern, ignored)

    def test_every_from_is_an_evidenced_amd64_manifest(self):
        evidence = json.loads((ROOT / "scripts/build/base-images.json").read_text())
        refs = {
            x["repository"].removeprefix("docker.io/library/")
            + ":"
            + x["tag"]
            + "@"
            + x["manifest_digest"]
            for x in evidence["images"]
        }
        for line in (ROOT / "Dockerfile").read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("FROM "):
                self.assertEqual(line.split()[1], "--platform=linux/amd64")
                self.assertIn(line.split()[2], refs)


if __name__ == "__main__":
    unittest.main()

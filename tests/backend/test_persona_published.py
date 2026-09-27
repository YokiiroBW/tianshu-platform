"""A synthetic publication proves the loader while the real release remains closed."""

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.platform.contracts import Fault
from services.platform.persona_page_config import load_published

SOURCE = Path("C:/YOKI/Codex/tianshu-peiban-bot/contracts/persona-management/v1")


class PublishedPersonaTests(unittest.TestCase):
    def setUp(self):
        if not (SOURCE / "manifest.json").is_file():
            self.skipTest("coordination candidate absent")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "persona"
        shutil.copytree(SOURCE, self.root)

    def _publish_fixture(self):
        manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        manifest.update(
            status="published",
            production_publish_authorized=True,
            joint_runtime_acceptance="passed",
        )
        raw = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        (self.root / "manifest.json").write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def test_pending_release_is_never_accepted(self):
        current = hashlib.sha256((self.root / "manifest.json").read_bytes()).hexdigest()
        with mock.patch("services.platform.persona_page_config.PUBLISHED_MANIFEST_SHA256", current):
            with self.assertRaises(Fault) as result:
                load_published(self.root)
        self.assertEqual(result.exception.code, "dependency_unavailable")

    def test_only_exact_pinned_publication_loads(self):
        digest = self._publish_fixture()
        with mock.patch("services.platform.persona_page_config.PUBLISHED_MANIFEST_SHA256", digest):
            loaded = load_published(self.root)
            self.assertEqual(loaded.manifest["status"], "published")
            self.assertEqual(loaded.manifest_sha256, digest)
            (self.root / "examples.json").write_bytes(b"{}\n")
            with self.assertRaises(Fault):
                load_published(self.root)

    def test_unpinned_publication_stays_closed(self):
        self._publish_fixture()
        with self.assertRaises(Fault):
            load_published(self.root)

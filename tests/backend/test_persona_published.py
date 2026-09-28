"""The pinned publication loads; altered and unpublished packages remain closed."""

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

    def _pending_fixture(self):
        manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        manifest.update(
            status="release_candidate",
            production_publish_authorized=False,
            joint_runtime_acceptance="pending",
        )
        raw = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        (self.root / "manifest.json").write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def test_pending_release_is_never_accepted(self):
        pending = self._pending_fixture()
        with mock.patch("services.platform.persona_page_config.PUBLISHED_MANIFEST_SHA256", pending):
            with self.assertRaises(Fault) as result:
                load_published(self.root)
        self.assertEqual(result.exception.code, "dependency_unavailable")

    def test_only_exact_pinned_publication_loads(self):
        loaded = load_published(self.root)
        self.assertEqual(loaded.manifest["status"], "published")
        self.assertEqual(loaded.manifest_sha256, hashlib.sha256((SOURCE / "manifest.json").read_bytes()).hexdigest())
        (self.root / "examples.json").write_bytes(b"{}\n")
        with self.assertRaises(Fault):
            load_published(self.root)

    def test_unpinned_publication_stays_closed(self):
        with mock.patch("services.platform.persona_page_config.PUBLISHED_MANIFEST_SHA256", None):
            with self.assertRaises(Fault):
                load_published(self.root)

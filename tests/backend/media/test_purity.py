"""Determinism, elapsed-time independence and purity of the TS-090 block.

Three separate claims are checked here. First, the same normalized input and the same request always
produce byte-identical output, with no random identifier and no clock reading. Second, an unchanged
snapshot keeps its stable keys even when the source reorders parts. Third, the production modules
perform no IO at all: an import is inspected statically for forbidden dependencies, and the import
is repeated in a fresh interpreter to prove no file or process appears as a side effect.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Iterable

try:
    from . import _fixtures as fx
    from ._fixtures import (
        CID_ONE,
        CID_THREE,
        CID_TWO,
        multipart_request,
        multipart_snapshot,
        poster,
        snapshot,
        thumb,
    )
except ImportError:  # narrow discovery: this directory is the top-level start directory
    import _fixtures as fx
    from _fixtures import (
        CID_ONE,
        CID_THREE,
        CID_TWO,
        multipart_request,
        multipart_snapshot,
        poster,
        snapshot,
        thumb,
    )

from services.platform.media import normalize_bilibili, render_sidecars

PRODUCTION_DIR = Path(__file__).resolve().parents[3] / "services" / "platform" / "media"
FORBIDDEN_MODULES = frozenset(
    {
        "asyncio",
        "http",
        "io",
        "os",
        "pickle",
        "requests",
        "shutil",
        "socket",
        "sqlite3",
        "subprocess",
        "tempfile",
        "threading",
        "time",
        "urllib",
        "uuid",
    }
)
ALLOWED_MODULES = frozenset(
    {
        "__future__",
        "dataclasses",
        "datetime",
        "json",
        "pathlib",
        "re",
        "types",
        "typing",
        "xml",
    }
)
# ``pathlib`` is allowed for its pure lexical types only: the renderer splits an already validated
# relative path with ``PurePosixPath`` and never touches a filesystem.
ALLOWED_PATHLIB_NAMES = frozenset({"PurePosixPath", "PurePath"})
IMPURE_PATH_CALL = re.compile(r"(?<![A-Za-z0-9_])Path\(")
IO_CALLS = ("open(", "os.", "subprocess", "sqlite3", "urlopen", "read_text", "write_text")


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def production_files() -> list[Path]:
    # TS090 remains the pure public core. The subscription runtime's explicit IO owners now live
    # beside it; their presence must not make a renderer-purity test forbid the requested runtime.
    return [
        PRODUCTION_DIR / name
        for name in ("__init__.py", "identity.py", "metadata.py", "sidecars.py", "types.py")
    ]


def pathlib_imports(path: Path) -> list[str]:
    """Every name a production module pulls out of ``pathlib``."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "pathlib":
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names if alias.name.startswith("pathlib"))
    return names


class StaticPurityTest(unittest.TestCase):
    def test_every_production_module_imports_only_the_allowed_standard_library(self):
        modules = production_files()
        self.assertEqual(
            [path.name for path in modules],
            ["__init__.py", "identity.py", "metadata.py", "sidecars.py", "types.py"],
        )
        for path in modules:
            with self.subTest(module=path.name):
                found = imported_modules(path)
                self.assertEqual(found & FORBIDDEN_MODULES, set())
                self.assertEqual(found - ALLOWED_MODULES, set())

    def test_no_todo_marker_survives_on_a_committed_path(self):
        for path in production_files():
            source = path.read_text(encoding="utf-8")
            with self.subTest(module=path.name):
                self.assertNotIn("TODO", source)
                self.assertNotIn("NotImplementedError", source)
                self.assertNotIn("pass\n", source.replace("pragma: no cover", ""))

    def test_no_file_extension_of_a_media_or_image_is_opened(self):
        source = "\n".join(path.read_text(encoding="utf-8") for path in production_files())
        for call in IO_CALLS:
            with self.subTest(call=call):
                self.assertNotIn(call, source)
        self.assertIsNone(IMPURE_PATH_CALL.search(source))

    def test_pathlib_is_used_only_for_its_pure_lexical_types(self):
        """The renderer may split a path lexically; it may not own a filesystem path object."""

        for path in production_files():
            source = path.read_text(encoding="utf-8")
            with self.subTest(module=path.name):
                self.assertIsNone(IMPURE_PATH_CALL.search(source))
                for imported in pathlib_imports(path):
                    self.assertIn(imported, ALLOWED_PATHLIB_NAMES)
        sidecars = (PRODUCTION_DIR / "sidecars.py").read_text(encoding="utf-8")
        self.assertIn("from pathlib import PurePosixPath", sidecars)


class ImportSideEffectTest(unittest.TestCase):
    def test_import_in_a_fresh_interpreter_creates_no_file_or_directory(self):
        worktree = Path(__file__).resolve().parents[3]
        script = (
            "import sys;"
            f"sys.path.insert(0, {str(worktree)!r});"
            "from services.platform.media import normalize_bilibili, render_sidecars;"
            "print('imported', normalize_bilibili is not None and render_sidecars is not None)"
        )
        before = sorted(entry.name for entry in worktree.iterdir())
        completed = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", script],
            capture_output=True,
            text=True,
            cwd=str(worktree),
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("imported True", completed.stdout)
        after = sorted(entry.name for entry in worktree.iterdir())
        self.assertEqual(before, after)

    def test_import_does_not_read_a_clock_and_the_runtime_directory_is_not_touched(self):
        worktree = Path(__file__).resolve().parents[3]
        runtime = worktree / ".runtime"
        before = sorted(path.name for path in runtime.iterdir()) if runtime.is_dir() else []
        script = (
            "import sys;"
            f"sys.path.insert(0, {str(worktree)!r});"
            "import time;"
            "before = time.time();"
            "from services.platform.media import normalize_bilibili;"
            "after = time.time();"
            "print('imported', after >= before)"
        )
        completed = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", script],
            capture_output=True,
            text=True,
            cwd=str(worktree),
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        after = sorted(path.name for path in runtime.iterdir()) if runtime.is_dir() else []
        self.assertEqual(before, after)


class DeterminismTest(unittest.TestCase):
    def document(self) -> dict:
        bundle = render_sidecars(
            normalize_bilibili(multipart_snapshot()),
            multipart_request(images=(poster("jpg"), thumb(CID_ONE, "png"))),
        )
        return {entry.path: entry.content for entry in bundle.files}

    def test_the_same_input_produces_byte_identical_output(self):
        first = self.document()
        second = self.document()
        self.assertEqual(list(first), list(second))
        for path in first:
            self.assertEqual(first[path], second[path], path)

    def test_no_generated_identifier_or_timestamp_comes_from_the_environment(self):
        rendered = self.document()
        document = json.loads(rendered["source.json"].decode("utf-8"))
        self.assertEqual(document["captured_at"], "2026-09-19T12:00:00Z")
        self.assertEqual(document["generator"], "tianshu-media-metadata/1")
        self.assertNotIn("generated_at", document)
        self.assertNotIn("uuid", json.dumps(document))

    def test_reordering_the_source_parts_keeps_keys_and_episode_numbers(self):
        reordered = normalize_bilibili(
            multipart_snapshot(
                parts=[
                    {"cid": CID_TWO, "index": 2, "title": None, "duration_seconds": None},
                    {"cid": CID_THREE, "index": 3, "title": "第三集", "duration_seconds": 59},
                    {"cid": CID_ONE, "index": 1, "title": "第一集", "duration_seconds": 3601},
                ]
            )
        )
        request = multipart_request(selected_cids=(CID_ONE, CID_TWO))
        original = render_sidecars(normalize_bilibili(multipart_snapshot()), request)
        shuffled = render_sidecars(reordered, request)
        self.assertEqual(original.package_key, shuffled.package_key)
        self.assertEqual(original.relative_directory, shuffled.relative_directory)
        self.assertEqual(
            [entry.path for entry in original.expected_media],
            [entry.path for entry in shuffled.expected_media],
        )
        self.assertEqual(
            [entry.path for entry in original.files],
            [entry.path for entry in shuffled.files],
        )
        for path in ("tvshow.nfo", "Season 01/S01E01-cid-111111111.nfo"):
            self.assertEqual(original.text(path), shuffled.text(path), path)
        original_parts = _parts_of(original)
        shuffled_parts = _parts_of(shuffled)
        self.assertEqual(original_parts[CID_ONE]["episode_number"], 1)
        self.assertEqual(shuffled_parts[CID_ONE]["episode_number"], 1)
        self.assertEqual(original_parts[CID_TWO]["video"], shuffled_parts[CID_TWO]["video"])
        self.assertEqual(original_parts[CID_ONE]["video"], shuffled_parts[CID_ONE]["video"])
        self.assertEqual(original_parts[CID_ONE]["video"], f"Season 01/S01E01-cid-{CID_ONE}.mp4")

    def test_a_normalized_record_can_be_rendered_many_times_without_mutation(self):
        metadata = normalize_bilibili(snapshot())
        before = (metadata.display_title, metadata.parts, metadata.issues)
        for _ in range(5):
            render_sidecars(metadata, fx.single_request(images=(poster(),)))
        self.assertEqual((metadata.display_title, metadata.parts, metadata.issues), before)


class IndependentExpectationTest(unittest.TestCase):
    """The expected bytes are computed here, not produced by calling the renderer again."""

    def test_movie_nfo_matches_a_locally_built_expected_document(self):
        metadata = normalize_bilibili(snapshot())
        bundle = render_sidecars(metadata, fx.single_request(images=(poster("jpg"),)))
        expected = (
            '<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n'
            "<movie>\n"
            f"  <title>{_escape(fx.TITLE)}</title>\n"
            f"  <originaltitle>{_escape(fx.TITLE)}</originaltitle>\n"
            f"  <plot>{_escape(fx.DESCRIPTION)}</plot>\n"
            "  <year>2019</year>\n"
            "  <premiered>2019-08-02</premiered>\n"
            "  <tag>测试</tag>\n"
            "  <tag>动画</tag>\n"
            "  <actor>\n"
            f"    <name>{_escape(fx.UP_NAME)}</name>\n"
            '    <role infoset="true" name="uploader" language="zh-CN">UP主</role>\n'
            "    <order>0</order>\n"
            "  </actor>\n"
            "  <runtime>12</runtime>\n"
            '  <thumb aspect="poster">poster.jpg</thumb>\n'
            '  <uniqueid type="bilibili" default="true">'
            f"bilibili:video:{fx.BV}:cid:{CID_ONE}</uniqueid>\n"
            "</movie>\n"
        )
        self.assertEqual(bundle.text("movie.nfo"), expected)

    def test_expected_media_and_layout_are_derived_from_the_card_rules(self):
        bundle = render_sidecars(
            normalize_bilibili(multipart_snapshot()),
            multipart_request(
                selected_cids=(CID_ONE, CID_THREE),
                episode_numbers={CID_ONE: 1, CID_THREE: 3},
            ),
        )
        expected = [
            f"Season 01/S01E01-cid-{CID_ONE}.mp4",
            f"Season 01/S01E03-cid-{CID_THREE}.mp4",
        ]
        self.assertEqual([entry.path for entry in bundle.expected_media], expected)
        self.assertEqual(bundle.relative_directory, f"bilibili-{fx.BV}")


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _parts_of(bundle) -> dict[str, dict]:
    document = json.loads(bundle.text("source.json"))
    return {part["cid"]: part for part in document["parts"]}


class TestHygieneTest(unittest.TestCase):
    def test_no_test_writes_to_the_repository(self):
        worktree = Path(__file__).resolve().parents[3]
        before = _snapshot(worktree / "services" / "platform" / "media")
        render_sidecars(normalize_bilibili(snapshot()), fx.single_request())
        after = _snapshot(worktree / "services" / "platform" / "media")
        self.assertEqual(before, after)

    def test_the_renderer_needs_no_environment_variable(self):
        """Nothing in the block may read configuration, so the same call works with an empty env."""

        metadata = normalize_bilibili(snapshot())
        expected = render_sidecars(metadata, fx.single_request())
        saved = dict(os.environ)
        try:
            os.environ.clear()
            os.environ["PYTHONHASHSEED"] = "1"
            without_environment = render_sidecars(metadata, fx.single_request())
        finally:
            os.environ.clear()
            os.environ.update(saved)
        self.assertEqual(expected.paths, without_environment.paths)
        self.assertEqual(
            [entry.content for entry in expected.files],
            [entry.content for entry in without_environment.files],
        )


def _snapshot(directory: Path) -> Iterable[tuple[str, int]]:
    return sorted((path.name, path.stat().st_mtime_ns) for path in directory.iterdir())


if __name__ == "__main__":
    unittest.main()

"""Portable source directories share the existing destination and storage identities."""

import unittest

from services.platform.media.config import source
from services.platform.media.destination import source_subdirectory, subdirectory


class SourceDirectoryTests(unittest.TestCase):
    def test_source_categories_and_identity_suffixes(self):
        cases = [
            ("uploader", "123", "UP主/同名 [UID123]"),
            ("favorite", "123", "收藏夹/同名 [FID123]"),
            ("collection", "123:456", "合集/同名 [合集123-456]"),
            ("series", "123:456", "合集/同名 [系列123-456]"),
            ("watch_later", "self", "稍后再看"),
        ]
        for kind, identity, expected in cases:
            with self.subTest(kind=kind):
                value = source_subdirectory(source({"kind": kind, "id": identity}), "同名")
                self.assertEqual(value, expected)
                self.assertEqual(subdirectory(value), value)

    def test_unsafe_names_utf8_bounds_and_namesakes(self):
        source_value = {"kind": "uploader", "id": "123"}
        for name in [' ../A\\B/C:<>"|?* . ', "CON", "aux.mp4", ".", "\x00\ud800", "汉" * 128]:
            with self.subTest(name=repr(name)):
                directory = source_subdirectory(source_value, name)
                self.assertEqual(subdirectory(directory), directory)
                self.assertTrue(directory.endswith(" [UID123]"))
                self.assertLessEqual(len(directory.split("/")[1].encode("utf-8")), 255)
        self.assertNotEqual(
            source_subdirectory(source_value, "同名"),
            source_subdirectory({"kind": "uploader", "id": "124"}, "同名"),
        )
        self.assertNotEqual(
            source_subdirectory({"kind": "collection", "id": "123:456"}, "同名"),
            source_subdirectory({"kind": "series", "id": "123:456"}, "同名"),
        )

"""Field projection and ordinary-text matching, with hand-written expectation tables.

Two things are deliberately not "computed by the implementation":

* the expected match of every table row is written out beside the row, so a change to the casefold
  or prefix/suffix rule fails a named case instead of silently agreeing with itself;
* the projections are asserted as whole tuples, which is what fixes *which* text a rule sees
  (original vs display, uploader vs collaborator, blank vs missing).
"""

from __future__ import annotations

import unittest

try:
    from . import _fixtures as fx
    from ._fixtures import CID_ONE, CID_TWO, MID_JOINT, MID_UP, metadata
except ImportError:  # narrow discovery: this directory is the top-level start directory
    import _fixtures as fx
    from _fixtures import CID_ONE, CID_TWO, MID_JOINT, MID_UP, metadata

from services.platform.media import normalize_bilibili
from services.platform.media.rules import (
    FIELD_PROJECTION_MAX_BYTES,
    any_value_matches,
    compare,
    field_values,
    projection_within_budget,
)


class TextComparisonTest(unittest.TestCase):
    def test_the_four_ordinary_operations_in_both_case_modes(self):
        cases: list[tuple[str, str, str, bool, bool]] = [
            # op, projected value, rule value, case_sensitive, expected
            ("equals", "总集篇 4K 合集", "总集篇 4K 合集", False, True),
            ("equals", "总集篇 4K 合集", "总集篇 4k 合集", False, True),
            ("equals", "总集篇 4K 合集", "总集篇 4k 合集", True, False),
            ("equals", "总集篇 4K 合集", "总集篇 4K", False, False),
            ("contains", "总集篇 4K 合集", "4K", False, True),
            ("contains", "总集篇 4K 合集", "4k", False, True),
            ("contains", "总集篇 4K 合集", "4k", True, False),
            ("contains", "总集篇 4K 合集", "预告", False, False),
            ("prefix", "Re:Zero 第二季", "re:", False, True),
            ("prefix", "Re:Zero 第二季", "re:", True, False),
            ("prefix", "Re:Zero 第二季", "Zero", False, False),
            ("suffix", "第 12 集 [完结]", "[完结]", False, True),
            ("suffix", "第 12 集 [完结]", "[完結]", False, False),
            ("suffix", "第 12 集 [完结]", "[完结]", True, True),
        ]
        for op, value, wanted, case_sensitive, expected in cases:
            with self.subTest(op=op, value=value, rule=wanted, case_sensitive=case_sensitive):
                self.assertEqual(
                    compare(value, wanted, op, case_sensitive=case_sensitive), expected
                )

    def test_casefold_is_unicode_caseless_matching_not_lowercasing(self):
        self.assertTrue(compare("Straße", "STRASSE", "equals", case_sensitive=False))
        self.assertFalse(compare("Straße", "STRASSE", "equals", case_sensitive=True))
        self.assertTrue(compare("STRASSE", "straße", "equals", case_sensitive=False))
        self.assertTrue(compare("ΣΊΣΥΦΟΣ", "σίσυφος", "equals", case_sensitive=False))
        self.assertTrue(compare("İstanbul", "İSTANBUL", "equals", case_sensitive=False))
        self.assertFalse(compare("İstanbul", "istanbul", "equals", case_sensitive=False))

    def test_undefined_operation_and_text_are_refused_rather_than_guessed(self):
        with self.assertRaises(ValueError):
            compare("a", "a", "glob", case_sensitive=False)
        with self.assertRaises(ValueError):
            field_values(metadata(), "duration")


class ProjectionTest(unittest.TestCase):
    def test_title_and_description_project_the_original_value(self):
        record = metadata()
        self.assertEqual(field_values(record, "title"), (fx.TITLE,))
        self.assertEqual(field_values(record, "description"), (fx.DESCRIPTION,))

    def test_a_manual_display_override_does_not_change_what_a_rule_reads(self):
        with_override = normalize_bilibili(
            fx.snapshot(title="来源标题"), overrides={"title": "人工展示标题"}
        )
        self.assertEqual(with_override.display_title, "人工展示标题")
        self.assertEqual(with_override.original_title, "来源标题")
        self.assertEqual(field_values(with_override, "title"), ("来源标题",))
        self.assertEqual(field_values(metadata(title="来源标题"), "title"), ("来源标题",))

    def test_a_blank_source_is_no_value_at_all(self):
        for blank in ("", "   ", "\n\t"):
            with self.subTest(blank=repr(blank)):
                record = metadata(title=blank, description=blank)
                self.assertEqual(field_values(record, "title"), ())
                self.assertEqual(field_values(record, "description"), ())

    def test_uploader_fields_only_read_the_uploader_role(self):
        record = metadata(
            creators=[
                {"mid": MID_JOINT, "name": "联合投稿人", "role": "collaborator"},
                {"mid": MID_UP, "name": "测试UP主", "role": "uploader"},
            ]
        )
        self.assertEqual(field_values(record, "uploader_id"), (MID_UP,))
        self.assertEqual(field_values(record, "uploader_name"), ("测试UP主",))
        self.assertNotIn("联合投稿人", field_values(record, "uploader_name"))

    def test_an_uploader_without_a_nickname_contributes_no_name_but_keeps_the_id(self):
        record = metadata(creators=[{"mid": MID_UP, "name": None, "role": "uploader"}])
        self.assertEqual(field_values(record, "uploader_name"), ())
        self.assertEqual(field_values(record, "uploader_id"), (MID_UP,))

    def test_tags_project_the_normalized_tags(self):
        record = metadata(tags=[" 动画 ", "动画", "合集"])
        self.assertEqual(field_values(record, "tags"), ("动画", "合集"))

    def test_multiple_uploaders_project_in_source_order(self):
        record = metadata(
            creators=[
                {"mid": MID_UP, "name": "第一位", "role": "uploader"},
                {"mid": MID_JOINT, "name": "第二位", "role": "uploader"},
            ]
        )
        self.assertEqual(field_values(record, "uploader_name"), ("第一位", "第二位"))
        self.assertEqual(field_values(record, "uploader_id"), (MID_UP, MID_JOINT))


class MultiValueTest(unittest.TestCase):
    def test_any_single_value_may_satisfy_a_rule(self):
        values = ("第一集", "第二集", "第三集")
        self.assertTrue(any_value_matches(values, "第二集", "equals", case_sensitive=True))
        self.assertTrue(any_value_matches(values, "第二", "contains", case_sensitive=False))
        self.assertFalse(any_value_matches(values, "第四集", "contains", case_sensitive=False))
        self.assertTrue(any_value_matches(values, "第", "prefix", case_sensitive=False))
        self.assertTrue(any_value_matches(values, "集", "suffix", case_sensitive=False))

    def test_an_empty_projection_matches_nothing(self):
        for op in ("equals", "contains", "prefix", "suffix"):
            with self.subTest(op=op):
                self.assertFalse(any_value_matches((), "x", op, case_sensitive=False))

    def test_values_are_never_joined_into_one_string(self):
        # "第一集" + "第二集" must not satisfy a contains rule that only spans the two values.
        values = ("第一集", "第二集")
        self.assertFalse(any_value_matches(values, "集第二", "contains", case_sensitive=False))


class ProjectionBudgetTest(unittest.TestCase):
    def test_budget_is_measured_in_utf8_bytes(self):
        self.assertTrue(projection_within_budget(["a" * FIELD_PROJECTION_MAX_BYTES]))
        self.assertFalse(projection_within_budget(["a" * (FIELD_PROJECTION_MAX_BYTES + 1)]))
        # A CJK character is three bytes, so 21846 of them are over budget while the string is
        # only 21846 characters long.
        cjk = "集" * (FIELD_PROJECTION_MAX_BYTES // 3 + 1)
        self.assertLess(len(cjk), FIELD_PROJECTION_MAX_BYTES)
        self.assertFalse(projection_within_budget([cjk]))
        self.assertTrue(projection_within_budget(["", "a" * 10, "b" * 20]))

    def test_the_budget_spans_all_values_of_one_field(self):
        half = "a" * (FIELD_PROJECTION_MAX_BYTES // 2)
        self.assertTrue(projection_within_budget([half, half]))
        self.assertFalse(projection_within_budget([half, half, "a"]))


class IdentifierConstantsTest(unittest.TestCase):
    def test_fixture_identifiers_are_the_ones_the_metadata_uses(self):
        record = metadata()
        self.assertEqual(record.bvid, fx.BV)
        self.assertEqual(record.parts[0].cid, CID_ONE)
        self.assertEqual(record.creators[0].mid, MID_UP)
        self.assertEqual(record.item_key, f"bilibili:video:{fx.BV}")
        self.assertEqual(metadata(parts=[fx.snapshot()["parts"][0]]).parts[0].cid, CID_ONE)
        self.assertNotEqual(CID_ONE, CID_TWO)

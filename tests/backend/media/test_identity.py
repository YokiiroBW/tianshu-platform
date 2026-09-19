"""BV/CID/MID identity and snapshot-shape validation.

Identity is checked as a shape only: nothing here asserts that an upload exists. The validation half
of this module checks the frozen field table, including the rule that an error names the failing
field path without ever repeating the offending value.
"""

from __future__ import annotations

import datetime
import unittest

try:
    from . import _fixtures as fx
    from ._fixtures import (
        BV,
        CID_ONE,
        CID_TWO,
        MID_UP,
        OTHER_BV,
        snapshot,
    )
except ImportError:  # narrow discovery: this directory is the top-level start directory
    import _fixtures as fx
    from _fixtures import (
        BV,
        CID_ONE,
        CID_TWO,
        MID_UP,
        OTHER_BV,
        snapshot,
    )

from services.platform.media import (
    ISSUE_CODES,
    MetadataValidationError,
    normalize_bilibili,
)
from services.platform.media.identity import (
    canonical_part_url,
    canonical_video_url,
    creator_key,
    episode_video_name,
    is_bvid,
    item_key,
    media_key,
    require_bvid,
    require_cid,
    require_mid,
)
from services.platform.media.metadata import (
    CREATORS_MAX,
    DESCRIPTION_MAX,
    PARTS_MAX,
    TAGS_MAX,
)


def refused(call) -> MetadataValidationError:
    try:
        call()
    except MetadataValidationError as error:
        return error
    raise AssertionError("expected MetadataValidationError")


class BvShapeTest(unittest.TestCase):
    def test_accepts_uppercase_bv_plus_ten_ascii_alphanumerics(self):
        """The prefix is exactly ``BV``; the ten body characters are kept as sent, case included."""

        for candidate in (BV, OTHER_BV, "BV0000000000", "BV1xx411c7mD"):
            with self.subTest(candidate=candidate):
                self.assertTrue(is_bvid(candidate))
                self.assertEqual(require_bvid(candidate), candidate)
                self.assertEqual(require_bvid(candidate)[:2], "BV")

    def test_lower_or_mixed_case_prefix_is_refused_not_normalized(self):
        """Case carries identity: this layer never rewrites a candidate into a different BV."""

        for candidate in ("bv1xx411c7mD", "bv1xx411c7md", "Bv1xx411c7mD", "bV1xx411c7mD"):
            with self.subTest(candidate=candidate):
                self.assertFalse(is_bvid(candidate))
                self.assertEqual(refused(lambda c=candidate: require_bvid(c)).code, "invalid_bvid")

    def test_rejects_wrong_length_prefix_and_non_ascii(self):
        for candidate in (
            "BV1xx411c7m",
            "BV1xx411c7mDD",
            "AV1xx411c7mD",
            "1xx411c7mD",
            "VB1xx411c7mD",
            "BV1xx411c7m-",
            "BV1xx411c7mé",
            "ＢＶ1xx411c7mD",
            "BV1xx411c7m\u0661",
            "",
            None,
            17,
        ):
            with self.subTest(candidate=candidate):
                self.assertFalse(is_bvid(candidate))
                self.assertEqual(refused(lambda c=candidate: require_bvid(c)).code, "invalid_bvid")


class CidMidShapeTest(unittest.TestCase):
    def test_positive_decimal_string_is_the_only_accepted_form(self):
        for value in ("1", "12345678901234567890", MID_UP):
            with self.subTest(value=value):
                self.assertEqual(require_cid(value), value)
        for value in ("0", "01", "007", "-1", "1.0", "1e3", "", None, True, 0, 10**20):
            with self.subTest(value=value):
                self.assertEqual(refused(lambda v=value: require_cid(v)).code, "invalid_cid")

    def test_integer_identifiers_are_refused_because_a_number_is_not_an_identity(self):
        """A JSON integer cannot carry a 64-bit CID through a browser, and the lost precision is
        unrecoverable, so the projection must send the decimal string and this layer refuses int."""

        for value in (7, 946974, 101, 2**53 + 1, 12345678901234567890):
            with self.subTest(value=value):
                self.assertEqual(refused(lambda v=value: require_cid(v)).code, "invalid_cid")
                self.assertEqual(refused(lambda v=value: require_mid(v)).code, "invalid_mid")

    def test_float_and_bool_are_not_identifiers(self):
        for value in (1.0, 7.0, float("inf"), True, False):
            with self.subTest(value=value):
                self.assertEqual(refused(lambda v=value: require_cid(v)).code, "invalid_cid")
                self.assertEqual(refused(lambda v=value: require_mid(v)).code, "invalid_mid")

    def test_mid_uses_the_same_decimal_rule(self):
        self.assertEqual(require_mid(MID_UP), MID_UP)
        self.assertEqual(refused(lambda: require_mid("0" + MID_UP)).code, "invalid_mid")
        self.assertTrue(creator_key(MID_UP).startswith("bilibili:creator:"))

    def test_path_characters_and_whitespace_are_refused(self):
        for value in ("../1", "1/2", "1\\2", " 1", "1 ", "+1", "1_0"):
            with self.subTest(value=value):
                self.assertEqual(refused(lambda v=value: require_cid(v)).code, "invalid_cid")

    def test_boolean_is_not_an_integer_identifier(self):
        self.assertEqual(refused(lambda: require_cid(True)).code, "invalid_cid")
        self.assertEqual(refused(lambda: require_mid(False)).code, "invalid_mid")


class KeyAndLinkTest(unittest.TestCase):
    def test_keys_and_links_are_built_from_identity_only(self):
        self.assertEqual(item_key(BV), f"bilibili:video:{BV}")
        self.assertEqual(media_key(BV, CID_ONE), f"bilibili:video:{BV}:cid:{CID_ONE}")
        self.assertEqual(canonical_video_url(BV), f"https://www.bilibili.com/video/{BV}/")
        self.assertEqual(
            canonical_part_url(BV, 3),
            f"https://www.bilibili.com/video/{BV}/?p=3",
        )

    def test_episode_video_name_pads_to_two_digits_and_keeps_larger_numbers(self):
        self.assertEqual(episode_video_name(1, CID_ONE, "mkv"), f"S01E01-cid-{CID_ONE}.mkv")
        self.assertEqual(episode_video_name(12, CID_ONE, "mp4"), f"S01E12-cid-{CID_ONE}.mp4")
        self.assertEqual(episode_video_name(1234, CID_TWO, "mkv"), f"S01E1234-cid-{CID_TWO}.mkv")

    def test_knowledge_of_other_libraries_is_not_needed(self):
        self.assertEqual(len(ISSUE_CODES), len(set(ISSUE_CODES)))


class SnapshotShapeTest(unittest.TestCase):
    def test_complete_projection_is_accepted_and_normalized(self):
        metadata = normalize_bilibili(snapshot())
        self.assertEqual(metadata.bvid, BV)
        self.assertEqual(metadata.provider, "bilibili")
        self.assertEqual(metadata.item_key, f"bilibili:video:{BV}")
        self.assertEqual(metadata.media_key, f"bilibili:video:{BV}:cid:{CID_ONE}")
        self.assertEqual(metadata.parts[0].cid, CID_ONE)
        self.assertEqual(metadata.captured_at, "2026-09-19T12:00:00Z")

    def test_every_key_is_required_and_the_first_gap_is_named(self):
        for name in sorted(snapshot()):
            with self.subTest(missing=name):
                document = snapshot()
                del document[name]
                error = refused(lambda d=document: normalize_bilibili(d))
                self.assertEqual(error.code, "missing_field")
                self.assertEqual(error.field, name)

    def test_unknown_key_is_refused_and_never_echoed(self):
        document = snapshot()
        document["cookie"] = "SESSDATA=super-secret-value"
        error = refused(lambda: normalize_bilibili(document))
        self.assertEqual(error.code, "unknown_field")
        self.assertNotIn("super-secret-value", str(error))
        self.assertNotIn("SESSDATA", str(error))

    def test_schema_version_must_be_the_integer_one(self):
        for value in (True, False, 0, 2, "1", 1.0, None):
            with self.subTest(value=value):
                error = refused(lambda v=value: normalize_bilibili(snapshot(schema_version=v)))
                self.assertEqual(error.code, "invalid_schema_version")

    def test_cover_available_must_be_a_real_boolean(self):
        for value in (0, 1, "true", None):
            with self.subTest(value=value):
                error = refused(lambda v=value: normalize_bilibili(snapshot(cover_available=v)))
                self.assertEqual(error.code, "invalid_cover_available")

    def test_title_and_description_limits_and_types(self):
        cases = (
            ("title", 5, "invalid_title"),
            ("title", None, None),
            ("title", ["x"], "invalid_title"),
            ("description", DESCRIPTION_MAX + 1, "invalid_description"),
            ("description", 42, "invalid_description"),
        )
        for field, value, code in cases:
            with self.subTest(field=field, value=type(value).__name__):
                document = snapshot(**{field: value})
                if code is None:
                    self.assertEqual(normalize_bilibili(document).original_title, "")
                    continue
                error = refused(lambda d=document: normalize_bilibili(d))
                self.assertEqual(error.code, code)
                self.assertEqual(error.field, field)

    def test_a_snapshot_mapping_is_never_modified_in_place(self):
        document = snapshot()
        before = fx.frozen_copy(document)
        normalize_bilibili(document, overrides={"title": "人工标题"})
        self.assertEqual(document, before)


class TimestampTest(unittest.TestCase):
    def test_offset_is_converted_to_utc_with_independent_arithmetic(self):
        document = snapshot(published_at="2019-08-02T08:30:00+08:00")
        expected = datetime.datetime(
            2019, 8, 2, 8, 30, tzinfo=datetime.timezone(datetime.timedelta(hours=8))
        ).astimezone(datetime.timezone.utc)
        self.assertEqual(
            normalize_bilibili(document).published_at,
            expected.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    def test_negative_offset_crosses_the_date_boundary(self):
        document = snapshot(published_at="2019-08-02T00:30:00-05:00")
        self.assertEqual(normalize_bilibili(document).published_at, "2019-08-02T05:30:00Z")

    def test_local_time_without_an_offset_is_refused(self):
        for value in (
            "2019-08-02T08:30:00",
            "2019-08-02 08:30:00Z",
            "2019-08-02T08:30:00.500Z",
            "2019-08-02T08:30Z",
            "2019-13-02T08:30:00Z",
            "2019-08-02T25:30:00Z",
            "not-a-date",
            "2019-08-02T08:30:00+0800",
        ):
            with self.subTest(value=value):
                error = refused(lambda v=value: normalize_bilibili(snapshot(captured_at=v)))
                self.assertEqual(error.code, "invalid_timestamp")
                self.assertEqual(error.field, "captured_at")

    def test_offset_that_leaves_the_representable_utc_range_is_a_named_error(self):
        """A well-formed local time whose offset pushes it past year 1 or 9999 is a validation
        failure with a fixed code, never an OverflowError escaping the public entry point."""

        for value, field in (
            ("0001-01-01T00:00:00+01:00", "captured_at"),
            ("9999-12-31T23:59:59-01:00", "captured_at"),
            ("0001-01-01T00:00:00+23:59", "captured_at"),
        ):
            with self.subTest(value=value):
                error = refused(lambda v=value: normalize_bilibili(snapshot(captured_at=v)))
                self.assertEqual(error.code, "invalid_timestamp")
                self.assertEqual(error.field, field)
        published = refused(
            lambda: normalize_bilibili(snapshot(published_at="9999-12-31T23:59:59-01:00"))
        )
        self.assertEqual(published.code, "invalid_timestamp")
        self.assertEqual(published.field, "published_at")

    def test_representable_edges_keep_four_digit_years_and_second_precision(self):
        for value, expected in (
            ("0001-01-01T00:00:00Z", "0001-01-01T00:00:00Z"),
            ("9999-12-31T23:59:59Z", "9999-12-31T23:59:59Z"),
            ("0001-01-01T00:00:00+00:00", "0001-01-01T00:00:00Z"),
            ("9999-12-31T23:59:59+00:00", "9999-12-31T23:59:59Z"),
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    normalize_bilibili(snapshot(captured_at=value)).captured_at, expected
                )

    def test_the_error_never_carries_the_offending_value(self):
        error = refused(
            lambda: normalize_bilibili(snapshot(captured_at="9999-12-31T23:59:59-01:00"))
        )
        self.assertNotIn("9999", str(error))
        self.assertEqual((error.code, error.field), ("invalid_timestamp", "captured_at"))

    def test_captured_at_is_required_and_never_taken_from_a_clock(self):
        error = refused(lambda: normalize_bilibili(snapshot(captured_at=None)))
        self.assertEqual(error.code, "invalid_timestamp")
        fresh = normalize_bilibili(snapshot())
        self.assertEqual(fresh.captured_at, "2026-09-19T12:00:00Z")


class ForbiddenCharacterTest(unittest.TestCase):
    def test_control_characters_are_refused_not_silently_stripped(self):
        for value in ("bad\x00value", "bad\x07value", "bad\u001fvalue", "bad\ufdd0value"):
            with self.subTest(value=repr(value)):
                error = refused(lambda v=value: normalize_bilibili(snapshot(title=f"标题{v}")))
                self.assertEqual(error.code, "forbidden_control_character")
                self.assertEqual(error.field, "title")

    def test_lone_surrogates_and_non_characters_are_refused(self):
        for value in ("bad\ud800value", "bad\uffffvalue", "bad\U0010ffffvalue"):
            with self.subTest(value=repr(value)):
                error = refused(lambda v=value: normalize_bilibili(snapshot(title=f"标题{v}")))
                self.assertEqual(error.code, "forbidden_control_character")
                self.assertNotIn(value, str(error))

    def test_ordinary_newlines_quotes_emoji_and_cjk_are_kept(self):
        metadata = normalize_bilibili(snapshot())
        self.assertEqual(metadata.original_title, fx.TITLE)
        self.assertIn("😀", metadata.original_title)
        self.assertIn("\n", metadata.original_title)

    def test_control_characters_are_refused_in_tags_and_creators(self):
        tag_error = refused(lambda: normalize_bilibili(snapshot(tags=["ok", "bad\x01"])))
        self.assertEqual(tag_error.code, "forbidden_control_character")
        self.assertEqual(tag_error.field, "tags[1]")
        creator_error = refused(
            lambda: normalize_bilibili(
                snapshot(creators=[{"mid": MID_UP, "name": "bad\x02", "role": "uploader"}])
            )
        )
        self.assertEqual(creator_error.code, "forbidden_control_character")
        self.assertEqual(creator_error.field, "creators[0].name")


class PartCollectionTest(unittest.TestCase):
    def test_parts_must_be_a_non_empty_list_within_the_bound(self):
        for value in ([], "x", None, {}):
            with self.subTest(value=type(value).__name__):
                error = refused(lambda v=value: normalize_bilibili(snapshot(parts=v)))
                self.assertEqual(error.code, "invalid_parts")
        error = refused(
            lambda: normalize_bilibili(
                snapshot(
                    parts=[
                        {
                            "cid": str(index + 1),
                            "index": index + 1,
                            "title": None,
                            "duration_seconds": None,
                        }
                        for index in range(PARTS_MAX + 1)
                    ]
                )
            )
        )
        self.assertEqual(error.code, "invalid_parts")

    def test_duplicate_cid_and_duplicate_index_are_refused_separately(self):
        duplicate_cid = snapshot(
            parts=[
                {"cid": CID_ONE, "index": 1, "title": None, "duration_seconds": None},
                {"cid": CID_ONE, "index": 2, "title": None, "duration_seconds": None},
            ]
        )
        error = refused(lambda: normalize_bilibili(duplicate_cid))
        self.assertEqual(error.code, "duplicate_cid")
        self.assertEqual(error.field, "parts[1].cid")
        duplicate_index = snapshot(
            parts=[
                {"cid": CID_ONE, "index": 1, "title": None, "duration_seconds": None},
                {"cid": CID_TWO, "index": 1, "title": None, "duration_seconds": None},
            ]
        )
        error = refused(lambda: normalize_bilibili(duplicate_index))
        self.assertEqual(error.code, "duplicate_index")
        self.assertEqual(error.field, "parts[1].index")

    def test_part_fields_are_checked_individually(self):
        cases = (
            ("cid", "0", "invalid_cid"),
            ("cid", None, "invalid_cid"),
            ("index", 0, "invalid_index"),
            ("index", 1001, "invalid_index"),
            ("index", True, "invalid_index"),
            ("title", "x" * 513, "invalid_title"),
            ("duration_seconds", -1, "invalid_duration"),
            ("duration_seconds", 1.5, "invalid_duration"),
            ("duration_seconds", True, "invalid_duration"),
        )
        for field, value, code in cases:
            with self.subTest(field=field, value=value):
                document = snapshot(
                    parts=[{"cid": CID_ONE, "index": 1, "title": None, "duration_seconds": None}]
                )
                document["parts"][0][field] = value  # type: ignore[index]
                error = refused(lambda d=document: normalize_bilibili(d))
                self.assertEqual(error.code, code)
                self.assertEqual(error.field, f"parts[0].{field}")

    def test_unknown_and_missing_part_keys_are_refused(self):
        document = snapshot(
            parts=[
                {
                    "cid": CID_ONE,
                    "index": 1,
                    "title": None,
                    "duration_seconds": None,
                    "extra": 1,
                }
            ]
        )
        self.assertEqual(refused(lambda: normalize_bilibili(document)).code, "unknown_field")
        document = snapshot(parts=[{"cid": CID_ONE, "index": 1, "title": None}])
        self.assertEqual(refused(lambda: normalize_bilibili(document)).code, "invalid_parts")

    def test_thousand_parts_are_accepted_and_bounded(self):
        parts = [
            {
                "cid": str(4000000000 + index),
                "index": index + 1,
                "title": None,
                "duration_seconds": None,
            }
            for index in range(PARTS_MAX)
        ]
        metadata = normalize_bilibili(snapshot(parts=parts))
        self.assertEqual(metadata.part_count, PARTS_MAX)
        self.assertEqual(metadata.parts[-1].index, PARTS_MAX)
        self.assertEqual(len(metadata.canonical_urls), PARTS_MAX)


class TagTest(unittest.TestCase):
    def test_blank_items_are_dropped_and_duplicates_keep_first_order(self):
        metadata = normalize_bilibili(snapshot(tags=[" b ", "a", "b", "  ", "", "a", "c"]))
        self.assertEqual(metadata.tags, ("b", "a", "c"))

    def test_bounds_and_types(self):
        error = refused(lambda: normalize_bilibili(snapshot(tags="a")))
        self.assertEqual(error.code, "invalid_tags")
        error = refused(lambda: normalize_bilibili(snapshot(tags=["a" * 129])))
        self.assertEqual(error.code, "invalid_tags")
        self.assertEqual(error.field, "tags[0]")
        error = refused(
            lambda: normalize_bilibili(snapshot(tags=[str(i) for i in range(TAGS_MAX + 1)]))
        )
        self.assertEqual(error.code, "invalid_tags")
        error = refused(lambda: normalize_bilibili(snapshot(tags=[7])))
        self.assertEqual(error.code, "invalid_tags")


class CreatorCollectionTest(unittest.TestCase):
    def test_no_creators_is_allowed_and_reported(self):
        metadata = normalize_bilibili(snapshot(creators=[]))
        self.assertEqual(metadata.creators, ())
        self.assertIn("creator_missing", metadata.issue_codes())

    def test_creator_count_and_field_bounds(self):
        error = refused(lambda: normalize_bilibili(snapshot(creators={})))
        self.assertEqual(error.code, "invalid_creators")
        many = [
            {"mid": str(1000 + index), "name": None, "role": "collaborator"}
            for index in range(CREATORS_MAX + 1)
        ]
        error = refused(lambda: normalize_bilibili(snapshot(creators=many)))
        self.assertEqual(error.code, "invalid_creators")
        error = refused(
            lambda: normalize_bilibili(
                snapshot(creators=[{"mid": MID_UP, "name": "x" * 129, "role": "uploader"}])
            )
        )
        self.assertEqual(error.code, "invalid_creator_name")
        self.assertEqual(error.field, "creators[0].name")

    def test_unknown_role_and_bad_mid_are_refused(self):
        error = refused(
            lambda: normalize_bilibili(
                snapshot(creators=[{"mid": MID_UP, "name": None, "role": "admin"}])
            )
        )
        self.assertEqual(error.code, "invalid_creator_role")
        self.assertEqual(error.field, "creators[0].role")
        error = refused(
            lambda: normalize_bilibili(
                snapshot(creators=[{"mid": "0946974", "name": None, "role": "uploader"}])
            )
        )
        self.assertEqual(error.code, "invalid_mid")

    def test_one_snapshot_may_not_carry_two_nicknames_for_one_mid(self):
        document = snapshot(
            creators=[
                {"mid": MID_UP, "name": "旧昵称", "role": "uploader"},
                {"mid": MID_UP, "name": "新昵称", "role": "uploader"},
            ]
        )
        error = refused(lambda: normalize_bilibili(document))
        self.assertEqual(error.code, "conflicting_creator")
        self.assertEqual(error.field, "creators[1].name")
        self.assertNotIn("新昵称", str(error))

    def test_duplicate_rows_with_the_same_name_merge_and_uploader_wins(self):
        document = snapshot(
            creators=[
                {"mid": MID_UP, "name": None, "role": "collaborator"},
                {"mid": MID_UP, "name": MID_UP, "role": "uploader"},
            ]
        )
        metadata = normalize_bilibili(document)
        self.assertEqual(len(metadata.creators), 1)
        self.assertEqual(metadata.creators[0].role, "uploader")
        self.assertEqual(metadata.creators[0].name, MID_UP)


if __name__ == "__main__":
    unittest.main()

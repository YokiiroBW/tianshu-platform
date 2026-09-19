"""Normalized metadata shape, creators across snapshots, manual overrides and issue reporting.

The two-snapshot cases matter because a nickname change on Bilibili must not move any identity: the
MID is the person, the nickname is display text, and a package assembled before a rename must still
describe the same person afterwards.
"""

from __future__ import annotations

import dataclasses
import unittest

import _fixtures as fx
from _fixtures import CID_ONE, MID_JOINT, MID_OTHER, MID_UP, multipart_snapshot, snapshot

from services.platform.media import (
    ISSUE_AUTHOR_NAME_MISSING,
    ISSUE_CODES,
    ISSUE_COVER_LOCAL_MISSING,
    ISSUE_COVER_SOURCE_MISSING,
    ISSUE_CREATOR_MISSING,
    ISSUE_DESCRIPTION_MISSING,
    ISSUE_PART_TITLE_MISSING,
    ISSUE_PUBLISHED_AT_MISSING,
    ISSUE_TITLE_MISSING,
    MetadataValidationError,
    normalize_bilibili,
)


def refused(call) -> MetadataValidationError:
    try:
        call()
    except MetadataValidationError as error:
        return error
    raise AssertionError("expected MetadataValidationError")


class MetadataShapeTest(unittest.TestCase):
    def test_output_carries_every_documented_field(self):
        metadata = normalize_bilibili(snapshot())
        self.assertEqual(metadata.schema_version, 1)
        self.assertEqual(metadata.provider, "bilibili")
        self.assertEqual(metadata.bvid, fx.BV)
        self.assertEqual(metadata.item_key, f"bilibili:video:{fx.BV}")
        self.assertEqual(metadata.media_key, f"bilibili:video:{fx.BV}:cid:{CID_ONE}")
        self.assertEqual(metadata.canonical_url, f"https://www.bilibili.com/video/{fx.BV}/")
        self.assertEqual(
            metadata.canonical_urls,
            ((CID_ONE, f"https://www.bilibili.com/video/{fx.BV}/?p=1"),),
        )
        self.assertEqual(metadata.original_title, fx.TITLE.strip())
        self.assertEqual(metadata.display_title, fx.TITLE.strip())
        self.assertEqual(metadata.original_description, fx.DESCRIPTION)
        self.assertEqual(metadata.display_description, fx.DESCRIPTION)
        self.assertEqual(dict(metadata.field_sources), {"title": "source", "description": "source"})
        self.assertEqual(metadata.published_at, "2019-08-02T00:30:00Z")
        self.assertEqual(metadata.captured_at, "2026-09-19T12:00:00Z")
        self.assertEqual(metadata.tags, ("测试", "动画"))
        self.assertTrue(metadata.cover_available)
        self.assertEqual(metadata.issues, ())
        self.assertEqual(metadata.part_count, 1)
        self.assertEqual(metadata.named_creator_count, 1)

    def test_identifiers_stay_strings(self):
        metadata = normalize_bilibili(snapshot())
        self.assertIsInstance(metadata.bvid, str)
        self.assertIsInstance(metadata.parts[0].cid, str)
        self.assertIsInstance(metadata.creators[0].mid, str)
        numeric = normalize_bilibili(
            snapshot(
                parts=[
                    {
                        "cid": 12345678901234567890,
                        "index": 1,
                        "title": None,
                        "duration_seconds": None,
                    }
                ]
            )
        )
        self.assertEqual(numeric.parts[0].cid, "12345678901234567890")

    def test_results_are_immutable_and_share_no_mutable_container(self):
        metadata = normalize_bilibili(snapshot())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            metadata.display_title = "改写"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            metadata.field_sources["title"] = "user"  # type: ignore[index]
        with self.assertRaises(AttributeError):
            metadata.extra = 1  # type: ignore[attr-defined]
        with self.assertRaises(TypeError):
            metadata.tags[0] = "改写"  # type: ignore[index]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            metadata.parts[0].title = "改写"  # type: ignore[misc]

    def test_part_lookup_and_selected_part_helper(self):
        metadata = normalize_bilibili(multipart_snapshot())
        self.assertEqual(metadata.part(CID_ONE).index, 1)  # type: ignore[union-attr]
        self.assertIsNone(metadata.part("999999999"))
        with self.assertRaises(MetadataValidationError):
            metadata.selected_part
        single = normalize_bilibili(snapshot())
        self.assertEqual(single.selected_part.cid, CID_ONE)


class CreatorIdentityTest(unittest.TestCase):
    def test_names_are_used_and_mids_are_never_substituted_for_names(self):
        metadata = normalize_bilibili(snapshot())
        creator = metadata.creators[0]
        self.assertEqual(creator.mid, MID_UP)
        self.assertEqual(creator.name, fx.UP_NAME)
        self.assertTrue(creator.name_present)
        self.assertNotEqual(creator.name, creator.mid)

    def test_blank_or_absent_name_keeps_identity_and_reports_the_gap(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                metadata = normalize_bilibili(
                    snapshot(creators=[{"mid": MID_UP, "name": value, "role": "uploader"}])
                )
                self.assertEqual(metadata.creators[0].mid, MID_UP)
                self.assertEqual(metadata.creators[0].name, "")
                self.assertFalse(metadata.creators[0].name_present)
                self.assertIn(ISSUE_AUTHOR_NAME_MISSING, metadata.issue_codes())
                self.assertEqual(metadata.issues[0].field, "creators[0].name")

    def test_same_nickname_on_different_mids_stays_two_people(self):
        metadata = normalize_bilibili(
            snapshot(
                creators=[
                    {"mid": MID_UP, "name": "同名", "role": "uploader"},
                    {"mid": MID_OTHER, "name": "同名", "role": "collaborator"},
                ]
            )
        )
        self.assertEqual([creator.mid for creator in metadata.creators], [MID_UP, MID_OTHER])
        self.assertEqual([creator.name for creator in metadata.creators], ["同名", "同名"])

    def test_one_mid_with_two_nicknames_in_one_snapshot_is_a_conflict(self):
        error = refused(
            lambda: normalize_bilibili(
                snapshot(
                    creators=[
                        {"mid": MID_UP, "name": "旧昵称", "role": "uploader"},
                        {"mid": MID_UP, "name": "新昵称", "role": "collaborator"},
                    ]
                )
            )
        )
        self.assertEqual(error.code, "conflicting_creator")
        self.assertNotIn("新昵称", str(error))

    def test_a_rename_between_two_snapshots_changes_only_the_display_name(self):
        before = normalize_bilibili(snapshot())
        after = normalize_bilibili(
            snapshot(creators=[{"mid": MID_UP, "name": "改名后的UP主", "role": "uploader"}])
        )
        self.assertEqual(before.creators[0].mid, after.creators[0].mid)
        self.assertEqual(before.item_key, after.item_key)
        self.assertEqual(before.media_key, after.media_key)
        self.assertEqual(before.parts[0].cid, after.parts[0].cid)
        self.assertEqual(before.creators[0].name, fx.UP_NAME)
        self.assertEqual(after.creators[0].name, "改名后的UP主")

    def test_uploader_and_collaborator_roles_survive_normalization(self):
        metadata = normalize_bilibili(multipart_snapshot())
        self.assertEqual(
            [(creator.mid, creator.role) for creator in metadata.creators],
            [(MID_UP, "uploader"), (MID_JOINT, "collaborator")],
        )


class OverrideTest(unittest.TestCase):
    def test_manual_override_never_erases_the_original(self):
        metadata = normalize_bilibili(
            snapshot(), overrides={"title": "人工标题", "description": "人工简介"}
        )
        self.assertEqual(metadata.display_title, "人工标题")
        self.assertEqual(metadata.display_description, "人工简介")
        self.assertEqual(metadata.original_title, fx.TITLE.strip())
        self.assertEqual(metadata.original_description, fx.DESCRIPTION)
        self.assertEqual(dict(metadata.field_sources), {"title": "user", "description": "user"})

    def test_partial_override_leaves_the_other_field_on_its_source(self):
        metadata = normalize_bilibili(snapshot(), overrides={"title": "人工标题"})
        self.assertEqual(metadata.display_title, "人工标题")
        self.assertEqual(metadata.display_description, fx.DESCRIPTION)
        self.assertEqual(dict(metadata.field_sources), {"title": "user", "description": "source"})

    def test_absent_key_keeps_the_source_and_an_explicit_null_is_refused(self):
        metadata = normalize_bilibili(snapshot(), overrides={})
        self.assertEqual(metadata.display_title, fx.TITLE.strip())
        self.assertEqual(dict(metadata.field_sources), {"title": "source", "description": "source"})
        error = refused(lambda: normalize_bilibili(snapshot(), overrides={"title": None}))
        self.assertEqual(error.code, "invalid_title")
        self.assertEqual(error.field, "overrides.title")

    def test_blank_override_is_recorded_as_a_display_gap_not_a_filled_field(self):
        metadata = normalize_bilibili(snapshot(), overrides={"title": "   "})
        self.assertEqual(metadata.display_title, "")
        self.assertEqual(metadata.original_title, fx.TITLE.strip())
        self.assertEqual(dict(metadata.field_sources), {"title": "user", "description": "source"})
        self.assertIn(ISSUE_TITLE_MISSING, metadata.issue_codes())
        self.assertNotEqual(metadata.display_title, metadata.original_title)

    def test_override_keys_are_restricted_and_unknown_keys_are_never_echoed(self):
        for overrides in ({"bvid": "BV0000000000"}, {"path": "C:/secret"}, {"cover": "/tmp/x.jpg"}):
            with self.subTest(overrides=sorted(overrides)):
                error = refused(lambda o=overrides: normalize_bilibili(snapshot(), overrides=o))
                self.assertEqual(error.code, "unknown_field")
                self.assertEqual(error.field, "overrides")
                for value in overrides.values():
                    self.assertNotIn(value, str(error))

    def test_override_values_obey_the_field_limits_and_character_rules(self):
        error = refused(
            lambda: normalize_bilibili(snapshot(), overrides={"description": "x" * 65537})
        )
        self.assertEqual(error.code, "invalid_description")
        error = refused(lambda: normalize_bilibili(snapshot(), overrides={"title": "bad\x00"}))
        self.assertEqual(error.code, "forbidden_control_character")
        error = refused(lambda: normalize_bilibili(snapshot(), overrides={"title": ["x"]}))
        self.assertEqual(error.code, "invalid_title")
        error = refused(lambda: normalize_bilibili(snapshot(), overrides="title"))
        self.assertEqual(error.code, "invalid_overrides")


class IssueTest(unittest.TestCase):
    def test_every_issue_word_comes_from_the_fixed_vocabulary(self):
        metadata = normalize_bilibili(
            multipart_snapshot(
                title=None,
                description="   ",
                cover_available=False,
                creators=[{"mid": MID_UP, "name": None, "role": "uploader"}],
            )
        )
        for code in metadata.issue_codes():
            self.assertIn(code, ISSUE_CODES)

    def test_issues_name_the_object_that_carries_the_gap(self):
        metadata = normalize_bilibili(multipart_snapshot())
        pairs = {(issue.code, issue.field) for issue in metadata.issues}
        self.assertIn((ISSUE_DESCRIPTION_MISSING, "description"), pairs)
        self.assertIn((ISSUE_PART_TITLE_MISSING, "parts[2].title"), pairs)
        self.assertNotIn((ISSUE_PUBLISHED_AT_MISSING, "published_at"), pairs)

    def test_a_missing_publication_date_is_reported_with_its_own_field(self):
        metadata = normalize_bilibili(snapshot(published_at=None))
        self.assertIsNone(metadata.published_at)
        self.assertIn((ISSUE_PUBLISHED_AT_MISSING, "published_at"), set(
            (issue.code, issue.field) for issue in metadata.issues
        ))

    def test_a_complete_projection_reports_nothing(self):
        self.assertEqual(normalize_bilibili(snapshot()).issues, ())

    def test_absent_cover_source_is_reported_and_never_invented(self):
        metadata = normalize_bilibili(snapshot(cover_available=False))
        self.assertFalse(metadata.cover_available)
        self.assertIn(ISSUE_COVER_SOURCE_MISSING, metadata.issue_codes())

    def test_missing_creator_list_is_distinct_from_an_unnamed_creator(self):
        empty = normalize_bilibili(snapshot(creators=[]))
        self.assertIn(ISSUE_CREATOR_MISSING, empty.issue_codes())
        self.assertNotIn(ISSUE_AUTHOR_NAME_MISSING, empty.issue_codes())
        unnamed = normalize_bilibili(
            snapshot(creators=[{"mid": MID_UP, "name": None, "role": "uploader"}])
        )
        self.assertNotIn(ISSUE_CREATOR_MISSING, unnamed.issue_codes())
        self.assertIn(ISSUE_AUTHOR_NAME_MISSING, unnamed.issue_codes())

    def test_missing_title_and_description_are_two_separate_words(self):
        metadata = normalize_bilibili(snapshot(title=None, description=None))
        self.assertEqual(metadata.original_title, "")
        self.assertEqual(metadata.original_description, "")
        self.assertEqual(
            set(metadata.issue_codes()),
            {ISSUE_TITLE_MISSING, ISSUE_DESCRIPTION_MISSING},
        )
        self.assertEqual(
            dict(metadata.field_sources), {"title": "missing", "description": "missing"}
        )

    def test_the_render_bundle_adds_the_local_cover_gap_without_hiding_metadata_gaps(self):
        from services.platform.media import render_sidecars

        metadata = normalize_bilibili(snapshot(title=None))
        bundle = render_sidecars(metadata, fx.single_request())
        codes = [issue.code for issue in bundle.issues]
        self.assertIn(ISSUE_TITLE_MISSING, codes)
        self.assertIn(ISSUE_COVER_LOCAL_MISSING, codes)
        self.assertEqual(bundle.status, "rendered_unverified")


if __name__ == "__main__":
    unittest.main()

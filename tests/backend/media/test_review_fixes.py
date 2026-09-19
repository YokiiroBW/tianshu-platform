"""Bounded shapes and named failures at the public entry points.

This module is the first-review regression suite. Each case here reproduced a real defect in the
fixed head: a shape that leaked a built-in ``TypeError`` or ``OverflowError`` out of a public
function instead of a named refusal with a fixed code and field. The contract under test is that a
caller can always branch on ``MetadataValidationError.code`` / ``RenderRequestError.code`` and never
has to catch an implementation accident, while a genuine bug is still allowed to surface.
"""

from __future__ import annotations

import unittest

try:
    from . import _fixtures as fx
    from ._fixtures import (
        CID_ONE,
        CID_TWO,
        MID_UP,
        multipart_request,
        multipart_snapshot,
        poster,
        single_request,
        snapshot,
        thumb,
    )
except ImportError:  # narrow discovery: this directory is the top-level start directory
    import _fixtures as fx
    from _fixtures import (
        CID_ONE,
        CID_TWO,
        MID_UP,
        multipart_request,
        multipart_snapshot,
        poster,
        single_request,
        snapshot,
        thumb,
    )

from services.platform.media import (
    ImageBinding,
    MetadataValidationError,
    RenderRequest,
    RenderRequestError,
    normalize_bilibili,
    render_sidecars,
)

NON_ITERABLE = (None, 5, 1.5, True, object())


def named(call, error_type) -> object:
    """Return the named failure, or fail loudly if a built-in exception escaped instead."""

    try:
        call()
    except error_type as error:
        return error
    raise AssertionError(f"expected {error_type.__name__}")


class BuildShapeTest(unittest.TestCase):
    """``RenderRequest.build`` refuses malformed collections with a fixed code, never TypeError."""

    def test_non_iterable_selected_cids_is_a_named_refusal(self):
        for value in NON_ITERABLE:
            with self.subTest(value=type(value).__name__):
                error = named(
                    lambda v=value: RenderRequest.build(
                        layout="single", selected_cids=v, media_extension="mkv"
                    ),
                    RenderRequestError,
                )
                self.assertEqual(error.code, "invalid_selected_cids")
                self.assertEqual(error.field, "selected_cids")

    def test_non_iterable_episode_numbers_is_a_named_refusal(self):
        for value in NON_ITERABLE:
            with self.subTest(value=type(value).__name__):
                error = named(
                    lambda v=value: RenderRequest.build(
                        layout="single",
                        selected_cids=[CID_ONE],
                        media_extension="mkv",
                        episode_numbers=v,
                    ),
                    RenderRequestError,
                )
                self.assertEqual(error.code, "invalid_episode_numbers")
                self.assertEqual(error.field, "episode_numbers")

    def test_non_iterable_images_is_a_named_refusal(self):
        for value in NON_ITERABLE:
            with self.subTest(value=type(value).__name__):
                error = named(
                    lambda v=value: RenderRequest.build(
                        layout="single",
                        selected_cids=[CID_ONE],
                        media_extension="mkv",
                        images=v,
                    ),
                    RenderRequestError,
                )
                self.assertEqual(error.code, "invalid_images")
                self.assertEqual(error.field, "images")

    def test_wrong_element_shapes_are_refused_one_by_one(self):
        cases = (
            ({"selected_cids": [CID_ONE, 7]}, "invalid_selected_cids"),
            ({"selected_cids": [CID_ONE, None]}, "invalid_selected_cids"),
            ({"episode_numbers": [(CID_ONE, "1")]}, "invalid_episode_numbers"),
            ({"episode_numbers": [(CID_ONE, True)]}, "invalid_episode_numbers"),
            ({"episode_numbers": [CID_ONE]}, "invalid_episode_numbers"),
            ({"episode_numbers": [(1, 1)]}, "invalid_episode_numbers"),
            ({"images": ["poster.jpg"]}, "invalid_images"),
            ({"images": [("poster", None, "jpg")]}, "invalid_images"),
        )
        for options, code in cases:
            with self.subTest(code=code, options=options):
                merged = {
                    "layout": "single",
                    "selected_cids": [CID_ONE],
                    "media_extension": "mkv",
                    **options,
                }
                error = named(lambda o=merged: RenderRequest.build(**o), RenderRequestError)
                self.assertEqual(error.code, code)

    def test_a_bytes_collection_is_not_a_collection_of_identifiers(self):
        error = named(
            lambda: RenderRequest.build(
                layout="single", selected_cids=b"111111111", media_extension="mkv"
            ),
            RenderRequestError,
        )
        self.assertEqual(error.code, "invalid_selected_cids")


class HandBuiltImageTest(unittest.TestCase):
    """A hand-built request is re-validated, and its cid shape is checked before any hashing."""

    def setUp(self):
        self.metadata = normalize_bilibili(multipart_snapshot())

    def request(self, image: ImageBinding) -> RenderRequest:
        return RenderRequest(
            layout="multipart",
            selected_cids=(CID_ONE,),
            episode_numbers=((CID_ONE, 1),),
            media_extension="mp4",
            images=(image,),
        )

    def test_unhashable_thumb_cid_is_a_named_refusal(self):
        for value in ([], {}, set(), CID_ONE.split()):
            with self.subTest(value=type(value).__name__):
                image = ImageBinding(role="episode_thumb", cid=value, extension="jpg")  # type: ignore[arg-type]
                error = named(
                    lambda i=image: render_sidecars(self.metadata, self.request(i)),
                    RenderRequestError,
                )
                self.assertEqual(error.code, "invalid_thumb_cid")
                self.assertEqual(error.field, "images[0].cid")

    def test_integer_thumb_cid_is_a_named_refusal(self):
        for value in (111111111, 0, True):
            with self.subTest(value=value):
                image = ImageBinding(role="episode_thumb", cid=value, extension="jpg")  # type: ignore[arg-type]
                error = named(
                    lambda i=image: render_sidecars(self.metadata, self.request(i)),
                    RenderRequestError,
                )
                self.assertEqual(error.code, "invalid_thumb_cid")

    def test_non_string_poster_cid_is_a_named_refusal(self):
        for value in ([], 7, {}):
            with self.subTest(value=type(value).__name__):
                image = ImageBinding(role="poster", cid=value, extension="jpg")  # type: ignore[arg-type]
                error = named(
                    lambda i=image: render_sidecars(self.metadata, self.request(i)),
                    RenderRequestError,
                )
                self.assertEqual(error.code, "invalid_poster_cid")
                self.assertEqual(error.field, "images[0].cid")

    def test_non_string_role_is_a_named_refusal(self):
        image = ImageBinding(role=["poster"], cid=None, extension="jpg")  # type: ignore[arg-type]
        error = named(
            lambda: render_sidecars(self.metadata, self.request(image)), RenderRequestError
        )
        self.assertEqual(error.code, "invalid_image_role")

    def test_non_tuple_collections_in_a_hand_built_request_are_refused(self):
        for kwargs, code in (
            ({"images": [poster()]}, "invalid_images"),
            ({"selected_cids": [CID_ONE]}, "invalid_selected_cids"),
            ({"episode_numbers": [[CID_ONE, 1]]}, "invalid_episode_numbers"),
        ):
            with self.subTest(code=code):
                options = {
                    "layout": "multipart",
                    "selected_cids": (CID_ONE,),
                    "episode_numbers": ((CID_ONE, 1),),
                    "media_extension": "mp4",
                    "images": (),
                    **kwargs,
                }
                request = RenderRequest(**options)  # type: ignore[arg-type]
                error = named(
                    lambda r=request: render_sidecars(self.metadata, r), RenderRequestError
                )
                self.assertEqual(error.code, code)

    def test_a_valid_hand_built_request_still_renders(self):
        bundle = render_sidecars(self.metadata, self.request(thumb(CID_ONE, "png")))
        self.assertEqual(bundle.status, "rendered_unverified")


class TimestampRangeTest(unittest.TestCase):
    """An offset that leaves the representable UTC range is a validation failure, not OverflowError."""

    CASES = (
        "0001-01-01T00:00:00+01:00",
        "0001-01-01T00:00:00+23:59",
        "0001-01-01T00:00:01+01:00",
        "9999-12-31T23:59:59-01:00",
        "9999-12-31T23:59:59-23:59",
    )

    def test_captured_at_range_overflow_is_a_named_error(self):
        for value in self.CASES:
            with self.subTest(value=value):
                error = named(
                    lambda v=value: normalize_bilibili(snapshot(captured_at=v)),
                    MetadataValidationError,
                )
                self.assertEqual(error.code, "invalid_timestamp")
                self.assertEqual(error.field, "captured_at")
                self.assertNotIn(value, str(error))

    def test_published_at_range_overflow_is_a_named_error(self):
        for value in self.CASES:
            with self.subTest(value=value):
                error = named(
                    lambda v=value: normalize_bilibili(snapshot(published_at=v)),
                    MetadataValidationError,
                )
                self.assertEqual(error.code, "invalid_timestamp")
                self.assertEqual(error.field, "published_at")

    def test_in_range_edges_still_normalize_to_utc_seconds(self):
        cases = (
            ("0001-01-01T00:00:00Z", "0001-01-01T00:00:00Z"),
            ("9999-12-31T23:59:59Z", "9999-12-31T23:59:59Z"),
            ("0001-01-01T01:00:00+01:00", "0001-01-01T00:00:00Z"),
            ("9999-12-31T22:59:59-01:00", "9999-12-31T23:59:59Z"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                metadata = normalize_bilibili(snapshot(captured_at=value))
                self.assertEqual(metadata.captured_at, expected)
                self.assertEqual(len(metadata.captured_at), 20)

    def test_a_null_published_at_stays_absent_instead_of_becoming_an_error(self):
        metadata = normalize_bilibili(snapshot(published_at=None))
        self.assertIsNone(metadata.published_at)


class IdentityStrictnessTest(unittest.TestCase):
    """The strict identity rules reach the whole snapshot, not just the helper functions."""

    def test_lower_case_prefix_in_a_snapshot_is_refused(self):
        for value in ("bv1xx411c7mD", "Bv1xx411c7mD", "bV1xx411c7mD"):
            with self.subTest(value=value):
                error = named(
                    lambda v=value: normalize_bilibili(snapshot(bvid=v)), MetadataValidationError
                )
                self.assertEqual(error.code, "invalid_bvid")
                self.assertEqual(error.field, "bvid")
                self.assertNotIn(value, str(error))

    def test_integer_identifiers_in_a_snapshot_are_refused(self):
        part = named(
            lambda: normalize_bilibili(snapshot(parts=[fx.part(cid=101)])),
            MetadataValidationError,
        )
        self.assertEqual((part.code, part.field), ("invalid_cid", "parts[0].cid"))
        creator = named(
            lambda: normalize_bilibili(
                snapshot(creators=[{"mid": 946974, "name": "UP主", "role": "uploader"}])
            ),
            MetadataValidationError,
        )
        self.assertEqual((creator.code, creator.field), ("invalid_mid", "creators[0].mid"))

    def test_an_accepted_snapshot_keeps_the_identifiers_it_was_given(self):
        metadata = normalize_bilibili(snapshot())
        self.assertEqual(metadata.bvid, fx.BV)
        self.assertEqual(metadata.parts[0].cid, CID_ONE)
        self.assertEqual(metadata.creators[0].mid, MID_UP)
        wide = normalize_bilibili(snapshot(parts=[fx.part(cid="12345678901234567890")]))
        self.assertEqual(wide.parts[0].cid, "12345678901234567890")
        self.assertEqual(wide.parts[0].cid, str(12345678901234567890))

    def test_a_rejected_value_never_reaches_a_path_or_a_key(self):
        for document in (
            snapshot(bvid="bv1xx411c7mD"),
            snapshot(parts=[fx.part(cid=101)]),
            snapshot(creators=[{"mid": 946974, "name": None, "role": "uploader"}]),
        ):
            with self.subTest(document=sorted(document)):
                error = named(lambda d=document: normalize_bilibili(d), MetadataValidationError)
                self.assertIsInstance(error.code, str)
                self.assertIsInstance(error.field, str)


class ExistingBehaviourKeptTest(unittest.TestCase):
    """The stricter shapes must not remove any capability the first delivery already had."""

    def test_a_normal_render_still_produces_the_full_candidate(self):
        single = render_sidecars(
            normalize_bilibili(snapshot()), single_request(images=(poster("jpg"),))
        )
        self.assertEqual(single.paths, ("movie.nfo", "source.json"))
        multi = render_sidecars(
            normalize_bilibili(multipart_snapshot()),
            multipart_request(images=(poster(), thumb(CID_TWO, "jpg"))),
        )
        self.assertEqual(
            multi.paths,
            (
                "tvshow.nfo",
                "Season 01/S01E01-cid-111111111.nfo",
                "Season 01/S01E02-cid-222222222.nfo",
                "source.json",
            ),
        )
        self.assertEqual(multi.status, "rendered_unverified")


if __name__ == "__main__":
    unittest.main()

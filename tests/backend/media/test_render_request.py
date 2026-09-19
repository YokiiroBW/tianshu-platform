"""RenderRequest validation and hostile display text.

Display text is chosen here to be hostile on purpose: traversal, Windows reserved device names, a
very long nickname, quotes and emoji. None of it may reach a path, and none of it may turn into a
file name through the renderer.
"""

from __future__ import annotations

import dataclasses
import unittest
from xml.etree import ElementTree

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

from services.platform.media import (
    ImageBinding,
    MetadataValidationError,
    RenderRequest,
    RenderRequestError,
    SidecarBundle,
    normalize_bilibili,
    render_sidecars,
)


def refused(call) -> RenderRequestError:
    try:
        call()
    except RenderRequestError as error:
        return error
    raise AssertionError("expected RenderRequestError")


class RenderRequestBuildTest(unittest.TestCase):
    def setUp(self):
        self.single = normalize_bilibili(snapshot())

    def test_build_accepts_ordinary_collections_and_freezes_them(self):
        request = RenderRequest.build(
            layout="multipart",
            selected_cids=[CID_ONE, CID_TWO],
            media_extension="mkv",
            episode_numbers={CID_ONE: 1, CID_TWO: 2},
            images=[ImageBinding(role="poster", cid=None, extension="jpg")],
        )
        self.assertEqual(request.selected_cids, (CID_ONE, CID_TWO))
        self.assertEqual(request.episode_numbers, ((CID_ONE, 1), (CID_TWO, 2)))
        self.assertEqual(dict(request.episode_map), {CID_ONE: 1, CID_TWO: 2})
        self.assertEqual(request.episode_for(CID_TWO), 2)
        self.assertIsNone(request.episode_for(CID_THREE))
        self.assertEqual(request.image_for("poster", None).extension, "jpg")  # type: ignore
        self.assertIsNone(request.image_for("episode_thumb", None))

    def test_request_is_immutable(self):
        request = multipart_request()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            request.layout = "single"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            request.episode_map[CID_ONE] = 9  # type: ignore[index]
        with self.assertRaises(AttributeError):
            request.extra = 1  # type: ignore[attr-defined]

    def test_build_rejects_malformed_shapes_before_any_rendering(self):
        single = {"layout": "single", "media_extension": "mkv"}
        bad_extension = {**single, "selected_cids": [CID_ONE], "media_extension": 5}
        cases = (
            ({"layout": 1, "selected_cids": [CID_ONE], "media_extension": "mkv"}, "invalid_layout"),
            ({**single, "selected_cids": CID_ONE}, "invalid_selected_cids"),
            ({**single, "selected_cids": [1]}, "invalid_selected_cids"),
            (bad_extension, "invalid_media_extension"),
        )
        for options, code in cases:
            with self.subTest(code=code, options=sorted(options)):
                self.assertEqual(refused(lambda o=options: RenderRequest.build(**o)).code, code)
        unsupported = RenderRequest.build(
            layout="single", selected_cids=[CID_ONE], media_extension="avi"
        )
        self.assertEqual(
            refused(lambda: render_sidecars(self.single, unsupported)).code,
            "invalid_media_extension",
        )
        self.assertEqual(
            refused(
                lambda: RenderRequest.build(
                    layout="single",
                    selected_cids=[CID_ONE],
                    media_extension="mkv",
                    episode_numbers=[("x", "1")],
                )
            ).code,
            "invalid_episode_numbers",
        )
        self.assertEqual(
            refused(
                lambda: RenderRequest.build(
                    layout="single",
                    selected_cids=[CID_ONE],
                    media_extension="mkv",
                    images=["poster.jpg"],
                )
            ).code,
            "invalid_images",
        )


class RenderRequestValidationTest(unittest.TestCase):
    def setUp(self):
        self.single = normalize_bilibili(snapshot())
        self.multi = normalize_bilibili(multipart_snapshot())

    def test_empty_selection_is_refused(self):
        request = RenderRequest.build(layout="single", selected_cids=[], media_extension="mkv")
        error = refused(lambda: render_sidecars(self.single, request))
        self.assertEqual(error.code, "empty_selection")

    def test_duplicate_and_unknown_cid_are_refused(self):
        duplicate = RenderRequest.build(
            layout="multipart",
            selected_cids=[CID_ONE, CID_ONE],
            media_extension="mkv",
            episode_numbers={CID_ONE: 1},
        )
        self.assertEqual(
            refused(lambda: render_sidecars(self.multi, duplicate)).code, "duplicate_selected_cid"
        )
        unknown = RenderRequest.build(
            layout="single", selected_cids=["999999999"], media_extension="mkv"
        )
        self.assertEqual(refused(lambda: render_sidecars(self.single, unknown)).code, "unknown_cid")

    def test_multipart_requires_one_unique_positive_episode_number_per_selected_cid(self):
        missing = RenderRequest.build(
            layout="multipart", selected_cids=[CID_ONE, CID_TWO], media_extension="mkv"
        )
        self.assertEqual(
            refused(lambda: render_sidecars(self.multi, missing)).code, "missing_episode_number"
        )
        partial = multipart_request(episode_numbers={CID_ONE: 1})
        self.assertEqual(
            refused(lambda: render_sidecars(self.multi, partial)).code, "missing_episode_number"
        )
        duplicated = multipart_request(episode_numbers={CID_ONE: 4, CID_TWO: 4})
        error = refused(lambda: render_sidecars(self.multi, duplicated))
        self.assertEqual(error.code, "duplicate_episode_number")
        for value in ("1", 1.5, None, True):
            with self.subTest(value=value):
                self.assertEqual(
                    refused(
                        lambda v=value: RenderRequest.build(
                            layout="multipart",
                            selected_cids=[CID_ONE, CID_TWO],
                            media_extension="mkv",
                            episode_numbers={CID_ONE: v, CID_TWO: 2},
                        )
                    ).code,
                    "invalid_episode_numbers",
                )
        for value in (0, -1, 1000000):
            with self.subTest(value=value):
                request = RenderRequest(
                    layout="multipart",
                    selected_cids=(CID_ONE, CID_TWO),
                    episode_numbers=((CID_ONE, value), (CID_TWO, 2)),
                    media_extension="mkv",
                    images=(),
                )
                self.assertEqual(
                    refused(lambda r=request: render_sidecars(self.multi, r)).code,
                    "invalid_episode_number",
                )

    def test_episode_number_for_an_unselected_cid_is_refused(self):
        request = multipart_request(episode_numbers={CID_ONE: 1, CID_TWO: 2, CID_THREE: 3})
        self.assertEqual(
            refused(lambda: render_sidecars(self.multi, request)).code, "episode_cid_not_selected"
        )

    def test_single_layout_rejects_episode_numbers(self):
        request = RenderRequest.build(
            layout="single",
            selected_cids=[CID_ONE],
            media_extension="mkv",
            episode_numbers={CID_ONE: 1},
        )
        self.assertEqual(
            refused(lambda: render_sidecars(self.single, request)).code,
            "unexpected_episode_numbers",
        )

    def test_single_requires_one_part_and_rejects_a_thumbnail(self):
        multipart_on_single = multipart_request()
        error = refused(lambda: render_sidecars(self.single, multipart_on_single))
        self.assertEqual(error.code, "multipart_layout_requires_multiple_parts")
        self.assertEqual(error.field, "layout")
        single_on_multipart = RenderRequest.build(
            layout="single", selected_cids=[CID_ONE], media_extension="mkv"
        )
        self.assertEqual(
            refused(lambda: render_sidecars(self.multi, single_on_multipart)).code,
            "single_layout_requires_one_part",
        )
        thumb_on_single = RenderRequest.build(
            layout="single",
            selected_cids=[CID_ONE],
            media_extension="mkv",
            images=[thumb(CID_ONE)],
        )
        self.assertEqual(
            refused(lambda: render_sidecars(self.single, thumb_on_single)).code,
            "single_layout_rejects_thumb",
        )

    def test_layout_and_extension_are_checked_again_at_render_time(self):
        bad_layout = RenderRequest.build(
            layout="season", selected_cids=[CID_ONE], media_extension="mkv"
        )
        error = refused(lambda: render_sidecars(self.single, bad_layout))
        self.assertEqual(error.code, "invalid_layout")
        bad_extension = RenderRequest.build(
            layout="single", selected_cids=[CID_ONE], media_extension="avi"
        )
        error = refused(lambda: render_sidecars(self.single, bad_extension))
        self.assertEqual(error.code, "invalid_media_extension")

    def test_non_request_and_non_metadata_are_refused(self):
        error = refused(lambda: render_sidecars(self.single, object()))
        self.assertEqual(error.code, "invalid_request")
        error = refused(lambda: render_sidecars(object(), fx.single_request()))
        self.assertEqual(error.code, "invalid_metadata")

    def test_request_built_by_hand_is_still_validated(self):
        handmade = RenderRequest(
            layout="single",
            selected_cids="not-a-tuple",  # type: ignore[arg-type]
            episode_numbers=(),
            media_extension="mkv",
            images=(),
        )
        self.assertEqual(
            refused(lambda: render_sidecars(self.single, handmade)).code, "invalid_selected_cids"
        )
        handmade = RenderRequest(
            layout="single",
            selected_cids=(CID_ONE,),
            episode_numbers=((CID_ONE, 1),),
            media_extension="mkv",
            images=(),
        )
        error = refused(lambda: render_sidecars(self.single, handmade))
        self.assertEqual(error.code, "unexpected_episode_numbers")


class ImageBindingTest(unittest.TestCase):
    def setUp(self):
        self.multi = normalize_bilibili(multipart_snapshot())

    def test_role_extension_cid_and_uniqueness_are_enforced(self):
        unknown_cid = "999999999"
        cases = (
            (ImageBinding(role="fanart", cid=None, extension="jpg"), "invalid_image_role"),
            (ImageBinding(role="poster", cid=CID_ONE, extension="jpg"), "invalid_poster_cid"),
            (ImageBinding(role="poster", cid=None, extension="webp"), "invalid_image_extension"),
            (ImageBinding(role="episode_thumb", cid=None, extension="jpg"), "invalid_thumb_cid"),
            (
                ImageBinding(role="episode_thumb", cid=unknown_cid, extension="jpg"),
                "invalid_thumb_cid",
            ),
        )
        for image, code in cases:
            with self.subTest(code=code):
                request = multipart_request(images=(image,))
                error = refused(lambda r=request: render_sidecars(self.multi, r))
                self.assertEqual(error.code, code)
        duplicated = multipart_request(images=(poster("jpg"), poster("png")))
        error = refused(lambda: render_sidecars(self.multi, duplicated))
        self.assertEqual(error.code, "duplicate_image_binding")

    def test_no_binding_means_no_thumbnail_element_and_no_image_file(self):
        bundle = render_sidecars(self.multi, multipart_request())
        self.assertEqual(bundle.referenced_images, ())
        for path in bundle.paths:
            self.assertFalse(path.endswith((".jpg", ".png")))
            self.assertNotIn("thumb", path)

    def test_bindings_never_claim_the_image_was_verified(self):
        bundle = render_sidecars(self.multi, multipart_request(images=(poster("jpg"),)))
        self.assertEqual(bundle.status, "rendered_unverified")
        self.assertNotIn("poster.jpg", bundle.paths)
        self.assertNotIn("verified", bundle.text("source.json"))


class HostileDisplayTextTest(unittest.TestCase):
    def hostile(self, title: str, name: str = "UP主"):
        return normalize_bilibili(
            snapshot(
                title=title,
                creators=[{"mid": "946974", "name": name, "role": "uploader"}],
            )
        )

    def test_display_text_cannot_change_any_path(self):
        titles = (
            "../../foo",
            "..\\..\\Windows\\System32",
            "CON",
            "NUL.mp4",
            "LPT1",
            "/etc/passwd",
            "C:\\Windows\\explorer.exe",
            "a" * 512,
            "第一集/第二集",
            "name.with.dots.mkv",
            "title\xa0with\xa0nbsp",
        )
        baseline = ("movie.nfo", "source.json")
        for title in titles:
            with self.subTest(title=title[:24]):
                bundle = render_sidecars(self.hostile(title), fx.single_request(images=(poster(),)))
                self.assertEqual(bundle.relative_directory, f"bilibili-{fx.BV}-cid-{CID_ONE}")
                self.assertEqual(bundle.paths, baseline)
                for path in bundle.paths:
                    self.assertNotIn("..", path)
                    self.assertNotIn("\\", path)
                    self.assertFalse(path.startswith("/"))
                    self.assertNotIn(title, path)

    def test_long_nickname_does_not_become_a_file_name(self):
        bundle = render_sidecars(
            self.hostile("普通标题", name="很长的昵称" * 20), fx.single_request()
        )
        self.assertEqual(bundle.paths, ("movie.nfo", "source.json"))
        self.assertEqual(bundle.relative_directory, f"bilibili-{fx.BV}-cid-{CID_ONE}")

    def test_display_text_still_reaches_the_sidecars_as_text(self):
        title = '标题 & <b>"引号"</b> 😀\n换行'
        metadata = self.hostile(title)
        bundle = render_sidecars(metadata, fx.single_request(images=(poster(),)))
        raw = bundle.text("movie.nfo")
        self.assertEqual(metadata.display_title, title)
        self.assertIn("&lt;b&gt;", raw)
        self.assertEqual(ElementTree.fromstring(raw).findtext("title"), title)

    def test_render_helpers_expose_no_mutable_container(self):
        bundle = render_sidecars(self.hostile("标题"), fx.single_request())
        self.assertIsInstance(bundle, SidecarBundle)
        self.assertIsInstance(bundle.files, tuple)
        self.assertIsInstance(bundle.expected_media, tuple)
        self.assertIsInstance(bundle.referenced_images, tuple)
        self.assertIsInstance(bundle.issues, tuple)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            bundle.relative_directory = "elsewhere"  # type: ignore[misc]
        with self.assertRaises(MetadataValidationError):
            bundle.text("missing.nfo")


class BundleLookupTest(unittest.TestCase):
    def test_unknown_path_lookup_is_a_named_failure(self):
        bundle = render_sidecars(normalize_bilibili(snapshot()), fx.single_request())
        self.assertIsNone(bundle.file("nope.nfo"))
        error = None
        try:
            bundle.text("nope.nfo")
        except MetadataValidationError as failure:
            error = failure
        self.assertIsNotNone(error)
        self.assertEqual(error.code, "unknown_sidecar_path")  # type: ignore[union-attr]
        self.assertIsInstance(bundle.package_key, str)
        self.assertIsInstance(bundle.media_extension, str)


if __name__ == "__main__":
    unittest.main()

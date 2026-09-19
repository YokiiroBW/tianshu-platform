"""Package layout, NFO and source.json read back through independent parsers.

Expected XML is never assembled by running the renderer twice. Each assertion parses the produced
bytes with ``xml.etree.ElementTree`` from scratch and reads the element it cares about, and the JSON
is parsed with ``json`` so key order, escaping and the absence of sources/credentials can be checked
as data rather than as text.
"""

from __future__ import annotations

import json
import unittest
from pathlib import PurePosixPath
from xml.etree import ElementTree

try:
    from . import _fixtures as fx
    from ._fixtures import (
        BV,
        CID_ONE,
        CID_THREE,
        CID_TWO,
        MID_JOINT,
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
        BV,
        CID_ONE,
        CID_THREE,
        CID_TWO,
        MID_JOINT,
        MID_UP,
        multipart_request,
        multipart_snapshot,
        poster,
        single_request,
        snapshot,
        thumb,
    )

from services.platform.media import (
    ISSUE_COVER_LOCAL_MISSING,
    ISSUE_PART_TITLE_MISSING,
    ISSUE_TITLE_MISSING,
    NFO_XML_DECLARATION,
    normalize_bilibili,
    render_sidecars,
)


def xml_of(path: str, bundle) -> ElementTree.Element:
    text = bundle.text(path)
    assert text.startswith(NFO_XML_DECLARATION), text[:60]
    return ElementTree.fromstring(text)


def json_of(bundle) -> dict:
    return json.loads(bundle.text("source.json"))


class SingleLayoutTest(unittest.TestCase):
    def setUp(self):
        self.metadata = normalize_bilibili(snapshot())
        self.request = single_request(images=(poster("jpg"),))
        self.bundle = render_sidecars(self.metadata, self.request)

    def test_package_is_named_after_bv_and_cid(self):
        self.assertEqual(self.bundle.relative_directory, f"bilibili-{BV}-cid-{CID_ONE}")
        self.assertEqual(self.bundle.package_key, f"bilibili:package:{BV}:cid:{CID_ONE}")
        self.assertEqual(self.bundle.status, "rendered_unverified")

    def test_files_are_relative_posix_paths_without_traversal(self):
        self.assertEqual(self.bundle.paths, ("movie.nfo", "source.json"))
        for path in self.bundle.paths:
            self.assertNotIn("..", path)
            self.assertNotIn("\\", path)
            self.assertFalse(path.startswith("/"))
        self.assertEqual(self.bundle.relative_directory.count("/"), 0)

    def test_expected_media_and_images_are_declared_not_generated(self):
        self.assertEqual([entry.path for entry in self.bundle.expected_media], ["video.mkv"])
        self.assertEqual(self.bundle.expected_media[0].extension, "mkv")
        self.assertEqual([entry.path for entry in self.bundle.referenced_images], ["poster.jpg"])
        self.assertNotIn("poster.jpg", self.bundle.paths)
        self.assertNotIn("video.mkv", self.bundle.paths)

    def test_movie_nfo_reads_back_with_display_and_original_values(self):
        root = xml_of("movie.nfo", self.bundle)
        self.assertEqual(root.tag, "movie")
        self.assertEqual(root.findtext("title"), fx.TITLE)
        self.assertEqual(root.findtext("originaltitle"), fx.TITLE)
        self.assertEqual(root.findtext("plot"), fx.DESCRIPTION)
        self.assertEqual(root.findtext("premiered"), "2019-08-02")
        self.assertEqual(root.findtext("year"), "2019")
        self.assertEqual([node.text for node in root.findall("tag")], ["测试", "动画"])
        self.assertEqual(root.findtext("runtime"), "12")

    def test_premiere_date_is_the_date_profile_while_source_json_keeps_utc_seconds(self):
        """``premiered``/``aired`` carry ``YYYY-MM-DD``; the full timestamp stays in source.json."""

        root = xml_of("movie.nfo", self.bundle)
        self.assertRegex(root.findtext("premiered") or "", r"\A\d{4}-\d{2}-\d{2}\Z")
        self.assertEqual(root.findtext("premiered"), "2019-08-02")
        self.assertEqual(json_of(self.bundle)["published_at"], "2019-08-02T00:30:00Z")

    def test_poster_uses_the_thumb_aspect_profile_and_no_invented_poster_element(self):
        root = xml_of("movie.nfo", self.bundle)
        thumbs = root.findall("thumb")
        self.assertEqual(len(thumbs), 1)
        self.assertEqual(thumbs[0].get("aspect"), "poster")
        self.assertEqual(thumbs[0].text, "poster.jpg")
        self.assertIsNone(root.find("poster"))
        self.assertEqual(root.findtext("thumb[@aspect='poster']"), "poster.jpg")

    def test_no_image_element_is_written_when_no_poster_is_bound(self):
        plain = render_sidecars(self.metadata, single_request())
        root = xml_of("movie.nfo", plain)
        self.assertIsNone(root.find("thumb"))
        self.assertIsNone(root.find("poster"))

    def test_actor_carries_nickname_and_role_but_never_a_numeric_name(self):
        root = xml_of("movie.nfo", self.bundle)
        actors = root.findall("actor")
        self.assertEqual(len(actors), 1)
        self.assertEqual(actors[0].findtext("name"), fx.UP_NAME)
        self.assertEqual(actors[0].find("role").text, "UP主")  # type: ignore[union-attr]
        self.assertEqual(actors[0].find("role").get("name"), "uploader")  # type: ignore[union-attr]
        self.assertNotEqual(actors[0].findtext("name"), MID_UP)

    def test_unique_id_is_the_stable_media_key_and_no_external_id_is_invented(self):
        root = xml_of("movie.nfo", self.bundle)
        unique = root.findall("uniqueid")
        self.assertEqual(len(unique), 1)
        self.assertEqual(unique[0].get("type"), "bilibili")
        self.assertEqual(unique[0].text, f"bilibili:video:{BV}:cid:{CID_ONE}")
        for tag in ("imdbid", "tmdbid", "tvdbid"):
            self.assertIsNone(root.find(tag))

    def test_manual_override_changes_display_but_not_the_original_element(self):
        metadata = normalize_bilibili(snapshot(), overrides={"title": "人工标题"})
        bundle = render_sidecars(metadata, self.request)
        root = xml_of("movie.nfo", bundle)
        self.assertEqual(root.findtext("title"), "人工标题")
        self.assertEqual(root.findtext("originaltitle"), fx.TITLE)

    def test_xml_is_not_hand_assembled_so_punctuation_survives(self):
        raw = self.bundle.text("movie.nfo")
        self.assertIn("&amp;", raw)
        self.assertIn("&lt;测试&gt;", raw)
        self.assertIn("😀", raw)
        self.assertEqual(xml_of("movie.nfo", self.bundle).findtext("title"), fx.TITLE)


class MultipartLayoutTest(unittest.TestCase):
    def setUp(self):
        self.metadata = normalize_bilibili(multipart_snapshot())
        self.bundle = render_sidecars(self.metadata, multipart_request())

    def test_show_package_lives_next_to_a_season_directory(self):
        self.assertEqual(self.bundle.relative_directory, f"bilibili-{BV}")
        expected = [
            "Season 01/S01E01-cid-111111111.mp4",
            "Season 01/S01E02-cid-222222222.mp4",
        ]
        self.assertEqual([entry.path for entry in self.bundle.expected_media], expected)
        self.assertEqual(
            self.bundle.paths,
            (
                "tvshow.nfo",
                "Season 01/S01E01-cid-111111111.nfo",
                "Season 01/S01E02-cid-222222222.nfo",
                "source.json",
            ),
        )

    def test_tvshow_root_carries_item_key_and_no_season_number(self):
        root = xml_of("tvshow.nfo", self.bundle)
        self.assertEqual(root.tag, "tvshow")
        self.assertEqual(root.findtext("title"), "多 P 投稿")
        unique = root.findall("uniqueid")
        self.assertEqual([node.text for node in unique], [f"bilibili:video:{BV}"])
        self.assertEqual(
            [node.text for node in root.findall("actor/name")],
            [fx.UP_NAME, fx.JOINT_NAME],
        )
        self.assertEqual(
            [node.get("name") for node in root.findall("actor/role")],
            ["uploader", "collaborator"],
        )
        self.assertEqual([node.text for node in root.findall("actor/role")], ["UP主", "联合创作者"])
        self.assertIsNone(root.find("season"))

    def test_a_person_without_a_nickname_gets_no_actor_node_but_stays_in_the_source_record(self):
        """An empty ``actor`` would be a fabricated person candidate. The MID and the gap stay in
        source.json instead, and every named author is still written."""

        metadata = normalize_bilibili(
            multipart_snapshot(
                creators=[
                    {"mid": MID_UP, "name": None, "role": "uploader"},
                    {"mid": MID_JOINT, "name": fx.JOINT_NAME, "role": "collaborator"},
                ]
            )
        )
        bundle = render_sidecars(metadata, multipart_request())
        root = xml_of("tvshow.nfo", bundle)
        self.assertEqual([node.text for node in root.findall("actor/name")], [fx.JOINT_NAME])
        self.assertEqual(
            [node.get("name") for node in root.findall("actor/role")], ["collaborator"]
        )
        self.assertEqual([node.text for node in root.findall("actor/order")], ["0"])
        self.assertNotIn("<name />", self.bundle.text("tvshow.nfo"))
        self.assertNotIn("<name/>", self.bundle.text("tvshow.nfo"))
        document = json_of(bundle)
        self.assertEqual(
            document["creators"],
            [
                {
                    "key": f"bilibili:creator:{MID_UP}",
                    "mid": MID_UP,
                    "name": "",
                    "name_missing": True,
                    "role": "uploader",
                },
                {
                    "key": f"bilibili:creator:{MID_JOINT}",
                    "mid": MID_JOINT,
                    "name": fx.JOINT_NAME,
                    "name_missing": False,
                    "role": "collaborator",
                },
            ],
        )
        self.assertIn(
            {"code": "author_name_missing", "field": "creators[0].name"}, document["issues"]
        )

    def test_a_package_with_no_named_author_writes_no_actor_at_all(self):
        metadata = normalize_bilibili(
            snapshot(creators=[{"mid": MID_UP, "name": "   ", "role": "uploader"}])
        )
        bundle = render_sidecars(metadata, single_request())
        root = xml_of("movie.nfo", bundle)
        self.assertEqual(root.findall("actor"), [])
        self.assertEqual(json_of(bundle)["creators"][0]["name"], "   ")

    def test_episode_roots_use_the_explicit_episode_mapping(self):
        first = xml_of("Season 01/S01E01-cid-111111111.nfo", self.bundle)
        second = xml_of("Season 01/S01E02-cid-222222222.nfo", self.bundle)
        self.assertEqual(first.tag, "episodedetails")
        self.assertEqual(first.findtext("episode"), "1")
        self.assertEqual(second.findtext("episode"), "2")
        self.assertEqual(first.findtext("season"), "1")
        self.assertEqual(first.findtext("showtitle"), "多 P 投稿")
        self.assertEqual(first.findtext("title"), "第一集")
        self.assertEqual(first.findtext("uniqueid"), f"bilibili:video:{BV}:cid:{CID_ONE}")
        self.assertEqual(first.findtext("aired"), "2019-08-02")
        self.assertEqual(first.findtext("runtime"), "60")
        self.assertIsNone(first.find("premiered"))

    def test_external_episode_numbers_are_honoured_and_never_reassigned(self):
        bundle = render_sidecars(
            self.metadata,
            multipart_request(
                selected_cids=(CID_TWO, CID_ONE),
                episode_numbers={CID_TWO: 7, CID_ONE: 12},
            ),
        )
        self.assertEqual(
            [entry.path for entry in bundle.expected_media],
            [
                "Season 01/S01E07-cid-222222222.mp4",
                "Season 01/S01E12-cid-111111111.mp4",
            ],
        )
        self.assertEqual(
            xml_of("Season 01/S01E07-cid-222222222.nfo", bundle).findtext("episode"), "7"
        )

    def test_source_index_reshuffle_does_not_move_an_assigned_episode(self):
        shuffled = normalize_bilibili(
            multipart_snapshot(
                parts=[
                    {"cid": CID_ONE, "index": 1, "title": "第一集", "duration_seconds": 3601},
                    {"cid": CID_TWO, "index": 2, "title": None, "duration_seconds": None},
                    {"cid": CID_THREE, "index": 3, "title": "第三集", "duration_seconds": 59},
                ]
            )
        )
        first = render_sidecars(self.metadata, multipart_request(selected_cids=(CID_ONE, CID_TWO)))
        second = render_sidecars(shuffled, multipart_request(selected_cids=(CID_ONE, CID_TWO)))
        self.assertEqual(
            [entry.path for entry in first.expected_media],
            [entry.path for entry in second.expected_media],
        )
        self.assertEqual(first.package_key, second.package_key)

    def test_partial_selection_renders_only_the_selected_parts(self):
        request = multipart_request(selected_cids=(CID_THREE,), episode_numbers={CID_THREE: 3})
        bundle = render_sidecars(self.metadata, request)
        self.assertEqual(len(bundle.expected_media), 1)
        self.assertEqual(bundle.expected_media[0].path, "Season 01/S01E03-cid-333333333.mp4")
        self.assertEqual(
            bundle.paths,
            ("tvshow.nfo", "Season 01/S01E03-cid-333333333.nfo", "source.json"),
        )

    def test_episode_thumb_is_referenced_only_when_bound(self):
        bundle = render_sidecars(
            self.metadata,
            multipart_request(
                selected_cids=(CID_ONE, CID_TWO),
                images=(poster("png"), thumb(CID_TWO, "jpg")),
            ),
        )
        self.assertEqual(
            [entry.path for entry in bundle.referenced_images],
            ["poster.png", "Season 01/S01E02-cid-222222222-thumb.jpg"],
        )
        self.assertNotIn("Season 01/S01E02-cid-222222222-thumb.jpg", bundle.paths)
        with_thumb = xml_of("Season 01/S01E02-cid-222222222.nfo", bundle)
        without_thumb = xml_of("Season 01/S01E01-cid-111111111.nfo", bundle)
        self.assertEqual(with_thumb.findtext("thumb"), "S01E02-cid-222222222-thumb.jpg")
        self.assertIsNone(without_thumb.find("thumb"))
        self.assertEqual(
            xml_of("tvshow.nfo", bundle).findtext("thumb[@aspect='poster']"), "poster.png"
        )

    def test_episode_thumb_in_the_nfo_resolves_to_the_declared_package_file(self):
        """The NFO names the file inside its own directory; the manifest keeps the package-root
        relative path. Resolving the NFO reference against the NFO directory must land on the
        declared image, otherwise the candidate package contradicts itself."""

        bundle = render_sidecars(
            self.metadata,
            multipart_request(
                selected_cids=(CID_ONE, CID_TWO),
                images=(poster("png"), thumb(CID_TWO, "jpg")),
            ),
        )
        nfo_path = "Season 01/S01E02-cid-222222222.nfo"
        declared = next(
            entry.path for entry in bundle.referenced_images if entry.role == "episode_thumb"
        )
        reference = xml_of(nfo_path, bundle).findtext("thumb")
        self.assertIsNotNone(reference)
        self.assertNotIn("/", reference or "")
        resolved = str(PurePosixPath(nfo_path).parent / (reference or ""))
        self.assertEqual(resolved, declared)
        self.assertEqual(
            [entry.path for entry in bundle.referenced_images if entry.cid == CID_TWO],
            [declared],
        )

    def test_episode_thumb_extension_matches_its_binding_and_no_thumb_is_faked(self):
        bundle = render_sidecars(
            self.metadata,
            multipart_request(selected_cids=(CID_ONE, CID_TWO), images=(thumb(CID_TWO, "png"),)),
        )
        self.assertEqual(
            [entry.path for entry in bundle.referenced_images],
            ["Season 01/S01E02-cid-222222222-thumb.png"],
        )
        self.assertEqual(
            xml_of("Season 01/S01E02-cid-222222222.nfo", bundle).findtext("thumb"),
            "S01E02-cid-222222222-thumb.png",
        )
        self.assertIsNone(xml_of("Season 01/S01E01-cid-111111111.nfo", bundle).find("thumb"))

    def test_episode_title_falls_back_to_display_title_plus_part_ordinal(self):
        bundle = render_sidecars(self.metadata, multipart_request())
        fallback = xml_of("Season 01/S01E02-cid-222222222.nfo", bundle)
        self.assertEqual(fallback.findtext("title"), "多 P 投稿 - P2")
        unnamed = normalize_bilibili(multipart_snapshot(title=None))
        blank_request = multipart_request(selected_cids=(CID_TWO,), episode_numbers={CID_TWO: 2})
        blank = render_sidecars(unnamed, blank_request)
        self.assertIsNone(xml_of("Season 01/S01E02-cid-222222222.nfo", blank).find("title"))
        self.assertIn(ISSUE_TITLE_MISSING, [issue.code for issue in blank.issues])


class PartDurationTest(unittest.TestCase):
    def test_runtime_is_floored_minutes_and_a_short_part_may_be_zero(self):
        metadata = normalize_bilibili(multipart_snapshot())
        bundle = render_sidecars(
            metadata, multipart_request(selected_cids=(CID_THREE,), episode_numbers={CID_THREE: 3})
        )
        self.assertEqual(
            xml_of("Season 01/S01E03-cid-333333333.nfo", bundle).findtext("runtime"), "0"
        )
        self.assertEqual(json_of(bundle)["parts"][0]["duration_seconds"], 59)

    def test_unknown_duration_is_omitted_rather_than_written_as_zero(self):
        bundle = render_sidecars(
            normalize_bilibili(multipart_snapshot()),
            multipart_request(selected_cids=(CID_TWO,), episode_numbers={CID_TWO: 2}),
        )
        self.assertIsNone(xml_of("Season 01/S01E02-cid-222222222.nfo", bundle).find("runtime"))
        self.assertIsNone(xml_of("tvshow.nfo", bundle).find("runtime"))


class SourceJsonTest(unittest.TestCase):
    def setUp(self):
        self.bundle = render_sidecars(
            normalize_bilibili(multipart_snapshot()), multipart_request(images=(poster(),))
        )
        self.document = json_of(self.bundle)

    def test_bytes_are_utf8_without_ascii_escaping_and_end_with_one_newline(self):
        raw = self.bundle.file("source.json").content  # type: ignore[union-attr]
        self.assertIsInstance(raw, bytes)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertFalse(raw.endswith(b"\n\n"))
        self.assertIn("多 P 投稿".encode("utf-8"), raw)
        self.assertNotIn(b"\\u591a", raw)

    def test_document_keys_are_recursively_sorted_while_arrays_keep_business_order(self):
        """The serializer imposes lexicographic key order at every level, so the same projection
        always produces the same bytes. Arrays are data, not a mapping: they keep their defined
        order (``parts`` follows the source snapshot)."""

        raw = self.bundle.text("source.json")
        canonical = (
            json.dumps(json.loads(raw), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        self.assertEqual(raw.encode("utf-8"), canonical)
        self.assertEqual(list(self.document), sorted(self.document))
        for mapping in (self.document["field_sources"], self.document["parts"][0]):
            with self.subTest(mapping=sorted(mapping)):
                self.assertEqual(list(mapping), sorted(mapping))
        self.assertEqual(
            [part["cid"] for part in self.document["parts"]], [CID_THREE, CID_ONE, CID_TWO]
        )
        self.assertEqual(self.document["tags"], ["合集"])
        self.assertEqual(self.document["selected_cids"], [CID_ONE, CID_TWO])

    def test_document_is_rebuildable_metadata_with_the_documented_fields(self):
        expected = {
            "schema_version",
            "generator",
            "provider",
            "bvid",
            "item_key",
            "media_key",
            "canonical_url",
            "layout",
            "media_extension",
            "original_title",
            "original_description",
            "display_title",
            "display_description",
            "field_sources",
            "published_at",
            "captured_at",
            "cover_available",
            "creators",
            "tags",
            "parts",
            "selected_cids",
            "episode_numbers",
            "expected_media",
            "referenced_images",
            "issues",
        }
        self.assertEqual(set(self.document), expected)
        self.assertEqual(self.document["generator"], "tianshu-media-metadata/1")
        self.assertEqual(self.document["provider"], "bilibili")
        self.assertEqual(self.document["item_key"], f"bilibili:video:{BV}")
        self.assertEqual(self.document["layout"], "multipart")
        self.assertEqual(self.document["canonical_url"], f"https://www.bilibili.com/video/{BV}/")
        self.assertEqual(self.document["published_at"], "2019-08-02T00:30:00Z")

    def test_original_and_display_are_both_present_with_their_sources(self):
        document = json_of(
            render_sidecars(
                normalize_bilibili(multipart_snapshot(), overrides={"title": "人工标题"}),
                multipart_request(),
            )
        )
        self.assertEqual(document["original_title"], "多 P 投稿")
        self.assertEqual(document["display_title"], "人工标题")
        self.assertEqual(document["field_sources"], {"title": "user", "description": "missing"})

    def test_people_keys_are_bilibili_creator_keys_and_names_are_not_numeric(self):
        self.assertEqual(
            self.document["creators"],
            [
                {
                    "key": f"bilibili:creator:{MID_UP}",
                    "mid": MID_UP,
                    "name": fx.UP_NAME,
                    "name_missing": False,
                    "role": "uploader",
                },
                {
                    "key": f"bilibili:creator:{MID_JOINT}",
                    "mid": MID_JOINT,
                    "name": fx.JOINT_NAME,
                    "name_missing": False,
                    "role": "collaborator",
                },
            ],
        )

    def test_parts_keep_identity_duration_selection_and_persistent_numbers(self):
        cids = [part["cid"] for part in self.document["parts"]]
        self.assertEqual(cids, [CID_THREE, CID_ONE, CID_TWO])
        by_cid = {part["cid"]: part for part in self.document["parts"]}
        self.assertEqual(by_cid[CID_ONE]["episode_number"], 1)
        self.assertTrue(by_cid[CID_ONE]["selected"])
        self.assertEqual(by_cid[CID_ONE]["video"], "Season 01/S01E01-cid-111111111.mp4")
        self.assertFalse(by_cid[CID_THREE]["selected"])
        self.assertIsNone(by_cid[CID_THREE]["episode_number"])
        self.assertIsNone(by_cid[CID_THREE]["video"])
        self.assertTrue(by_cid[CID_TWO]["title_missing"])
        self.assertIsNone(by_cid[CID_TWO]["duration_seconds"])
        self.assertEqual(self.document["selected_cids"], [CID_ONE, CID_TWO])
        self.assertEqual(self.document["episode_numbers"], [[CID_ONE, 1], [CID_TWO, 2]])

    def test_images_are_relative_paths_only(self):
        self.assertEqual(
            self.document["referenced_images"],
            [{"path": "poster.jpg", "role": "poster", "cid": None}],
        )
        self.assertEqual(
            self.document["expected_media"],
            [
                {"path": "Season 01/S01E01-cid-111111111.mp4", "extension": "mp4"},
                {"path": "Season 01/S01E02-cid-222222222.mp4", "extension": "mp4"},
            ],
        )

    def test_no_credential_no_signature_no_absolute_path_no_raw_passthrough(self):
        raw = self.bundle.text("source.json")
        for forbidden in (
            "cookie",
            "Cookie",
            "SESSDATA",
            "Authorization",
            "token",
            "secret",
            "sign=",
            "deadline=",
            "C:\\",
            "http://",
            "https://api.",
            "raw",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, raw)
        self.assertIn("https://www.bilibili.com/video/", raw)

    def test_an_unknown_snapshot_key_can_never_reach_the_document(self):
        document = json_of(render_sidecars(normalize_bilibili(snapshot()), single_request()))
        self.assertNotIn("cookie", document)
        self.assertNotIn("signature", document)
        wanted = {"code": ISSUE_COVER_LOCAL_MISSING, "field": "images"}
        self.assertEqual(document["issues"], [wanted])
        bound = json_of(
            render_sidecars(normalize_bilibili(snapshot()), single_request(images=(poster(),)))
        )
        self.assertEqual(bound["issues"], [])

    def test_issues_are_written_with_code_and_field(self):
        document = json_of(
            render_sidecars(normalize_bilibili(multipart_snapshot()), multipart_request())
        )
        wanted = {"code": ISSUE_PART_TITLE_MISSING, "field": "parts[2].title"}
        self.assertIn(wanted, document["issues"])
        covered = {"code": ISSUE_COVER_LOCAL_MISSING, "field": "images"}
        self.assertIn(covered, document["issues"])

    def test_a_bound_poster_removes_the_local_cover_gap_from_the_document(self):
        codes = [entry["code"] for entry in self.document["issues"]]
        self.assertNotIn(ISSUE_COVER_LOCAL_MISSING, codes)
        unbound = json_of(
            render_sidecars(normalize_bilibili(multipart_snapshot()), multipart_request())
        )
        self.assertIn(ISSUE_COVER_LOCAL_MISSING, [entry["code"] for entry in unbound["issues"]])

    def test_sidecar_files_are_exactly_the_generated_text_files(self):
        for entry in self.bundle.files:
            self.assertTrue(entry.path.endswith((".nfo", ".json")))
            self.assertTrue(entry.content)
        self.assertEqual([entry.path for entry in self.bundle.files], list(self.bundle.paths))


if __name__ == "__main__":
    unittest.main()

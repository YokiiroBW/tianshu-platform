"""Policy boundaries over pinned extractor output and persistent actual evidence."""

import copy
import unittest

from yt_dlp.extractor.bilibili import BilibiliBaseIE

from services.platform.contracts import Fault
from services.platform.media.config import quality
from services.platform.media.stream_policy import (
    AUDIO_QUALITIES,
    DEFAULT_OPTIONS,
    observed_evidence,
    output_evidence,
    select_formats,
    validate_policy,
)

BEST = {"mode": "best", "quality_id": None, "allow_fallback": False}


def policy(**options):
    return quality({**BEST, "stream_options": options})


def video(qn=80, codec="avc1.640032", identity=None):
    return {
        "quality": qn,
        "format_id": identity or str(qn) + "-" + codec.split(".")[0],
        "vcodec": codec,
        "acodec": "none",
    }


def audio(qn="30280"):
    return {
        "format_id": qn,
        "vcodec": "none",
        "acodec": {"30250": "ec-3", "30251": "flac"}.get(qn, "mp4a.40.2"),
    }


class StreamPolicyTests(unittest.TestCase):
    def setUp(self):
        self.formats = [
            video(64),
            video(),
            video(codec="hev1.1.6"),
            video(codec="av01.0.08"),
            video(125, "hev1.2.4"),
            video(126, "hev1.2.4"),
            *map(audio, AUDIO_QUALITIES),
        ]

    def test_empty_options_preserve_old_dictionary_and_partial_defaults_are_stable(self):
        self.assertEqual(quality(BEST), BEST)
        self.assertEqual(policy(), BEST)
        self.assertEqual(policy(**DEFAULT_OPTIONS), BEST)
        value = policy(codecs=["hevc"])
        self.assertEqual(value["stream_options"], {**DEFAULT_OPTIONS, "codecs": ["hevc"]})
        self.assertEqual(quality(value), value)
        self.assertEqual(BEST, {"mode": "best", "quality_id": None, "allow_fallback": False})

    def test_invalid_ranges_unknown_values_and_shapes_are_rejected(self):
        for fields in (
            {"video_min_quality": "1080"},
            {"audio_min_quality": "30251", "audio_max_quality": "30280"},
            {"video_min_quality": "80", "video_max_quality": "64"},
            {"codecs": ["hevc", "hevc"]},
            {"codecs": ["h264"]},
            {"codecs": [None]},
            {"codecs": "avc"},
            {"allow_hdr": 1},
            {"no_hdr": True},
            {"video_min_quality": 80},
        ):
            with self.subTest(fields=fields), self.assertRaises(Fault) as raised:
                policy(**fields)
            self.assertEqual(raised.exception.code, "invalid_input")

    def test_quality_precedes_codec_order_and_audio_order_is_not_numeric(self):
        request = policy(video_max_quality="80", codecs=["hevc", "av1", "avc"])
        selector, evidence = select_formats(self.formats, request)
        self.assertEqual(selector, "80-hev1+30251")
        self.assertEqual(evidence["audio_quality"], "30251")
        selector, _ = select_formats([video(64, "hev1"), video(80), audio()], request)
        self.assertEqual(selector, "80-avc1+30280")
        for maximum, selected in (("30280", "30280"), ("30250", "30250"), ("30251", "30251")):
            _, evidence = select_formats(self.formats, policy(audio_max_quality=maximum))
            self.assertEqual(evidence["audio_quality"], selected)

    def test_bounds_are_inclusive_and_special_switches_are_independent(self):
        request = policy(
            video_min_quality="80",
            video_max_quality="125",
            audio_min_quality="30232",
            audio_max_quality="30250",
            allow_hdr=False,
            allow_dolby_audio=False,
        )
        _, evidence = select_formats(self.formats, request)
        self.assertEqual((evidence["video_quality"], evidence["audio_quality"]), ("80", "30280"))
        _, evidence = select_formats(self.formats, policy(allow_hdr=False, allow_hires=False))
        self.assertEqual((evidence["video_quality"], evidence["audio_quality"]), ("126", "30250"))
        for fields in (
            {"video_min_quality": "127"},
            {"codecs": ["av1"], "video_min_quality": "125"},
            {"audio_min_quality": "30251", "allow_hires": False},
        ):
            with self.subTest(fields=fields), self.assertRaises(Fault) as raised:
                select_formats(self.formats, policy(**fields))
            self.assertEqual(raised.exception.code, "quality_unavailable")

    def test_exact_fallback_cannot_escape_the_allowed_set(self):
        for fallback in (False, True):
            request = {
                **policy(video_max_quality="64"),
                "mode": "exact",
                "quality_id": "80",
                "allow_fallback": fallback,
            }
            if fallback:
                self.assertEqual(select_formats(self.formats, request)[1]["video_quality"], "64")
            else:
                with self.assertRaises(Fault):
                    select_formats(self.formats, request)

    def test_real_pinned_extractor_exposes_audio_ids_and_hdr_dv(self):
        raw = {
            "dash": {
                "audio": [
                    {"id": int(qn), "baseUrl": "https://fixture.invalid/a", "codecs": "mp4a.40.2"}
                    for qn in ("30216", "30232", "30280")
                ],
                "flac": {
                    "audio": {"id": 30251, "baseUrl": "https://fixture.invalid/f", "codecs": "flac"}
                },
                "dolby": {
                    "audio": [
                        {"id": 30250, "baseUrl": "https://fixture.invalid/d", "codecs": "ec-3"}
                    ]
                },
                "video": [
                    {"id": qn, "baseUrl": "https://fixture.invalid/v", "codecs": "hev1.2.4"}
                    for qn in (80, 125, 126)
                ],
            }
        }
        formats = BilibiliBaseIE().extract_formats(raw)
        self.assertTrue(all("quality" not in f for f in formats if f["vcodec"] == "none"))
        self.assertEqual(
            select_formats(formats, policy(allow_dolby_vision=False))[1]["video_quality"], "125"
        )
        self.assertEqual(
            select_formats(formats, policy(allow_hires=False))[1]["audio_quality"], "30250"
        )

    def test_reuse_needs_actual_evidence_but_legacy_quality_still_works(self):
        old = {"actual_quality": "80"}
        validate_policy(old, BEST)
        for request in (
            policy(codecs=["avc"]),
            policy(allow_hdr=False),
            policy(audio_max_quality="30280"),
            policy(allow_hires=False),
        ):
            with self.subTest(request=request), self.assertRaises(Fault):
                validate_policy(old, request)
        actual = {
            **old,
            "media": {
                "rawffprobe_streams": [
                    {"codec_type": "video", "codec_name": "h264", "color_transfer": "bt709"},
                    {"codec_type": "audio", "codec_name": "aac"},
                ]
            },
        }
        validate_policy(
            actual,
            policy(codecs=["avc"], allow_hdr=False, allow_dolby_audio=False, allow_hires=False),
        )
        with self.assertRaises(Fault):
            validate_policy(actual, policy(codecs=["hevc"]))
        with self.assertRaises(Fault):
            validate_policy(actual, policy(audio_max_quality="30280"))

    def test_probe_overrides_false_claims_and_codec_mismatch_rejects_output(self):
        media = {
            "rawffprobe_streams": [
                {
                    "codec_type": "video",
                    "codec_name": "hevc",
                    "color_transfer": "smpte2084",
                    "side_data_list": [{"side_data_type": "DOVI configuration record"}],
                },
                {"codec_type": "audio", "codec_name": "flac"},
            ]
        }
        evidence = observed_evidence(
            {"media": media, "stream_evidence": {"hdr": False, "hires": False}}
        )
        self.assertTrue(evidence["dolby_vision"])
        self.assertFalse(evidence["hdr"])
        self.assertTrue(evidence["hires"])
        with self.assertRaises(Fault):
            output_evidence([video(), audio()], media, strict=True)
        ordinary = copy.deepcopy(media)
        ordinary["rawffprobe_streams"][0].pop("side_data_list")
        self.assertTrue(observed_evidence({"media": ordinary})["hdr"])

    def test_every_track_obeys_exclusions_and_unrelated_missing_fields_do_not_block(self):
        actual = {
            "actual_quality": "80",
            "media": {
                "rawffprobe_streams": [
                    {"codec_type": "video", "codec_name": "h264", "color_transfer": "bt709"},
                    {"codec_type": "audio", "codec_name": "aac"},
                    {"codec_type": "audio", "codec_name": "eac3"},
                ]
            },
        }
        validate_policy(actual, policy(video_min_quality="64", video_max_quality="80"))
        with self.assertRaises(Fault):
            validate_policy(actual, policy(allow_dolby_audio=False))
        actual["media"]["rawffprobe_streams"].append({"codec_type": "video", "codec_name": "hevc"})
        with self.assertRaises(Fault):
            validate_policy(actual, policy(codecs=["avc"]))
        actual["media"]["rawffprobe_streams"].append({"codec_type": "audio"})
        with self.assertRaises(Fault):
            validate_policy(actual, policy(allow_hires=False))


class ImportedPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_media_imports import MediaImportTests

        for name in ("video", "entry", "reconcile", "record"):
            setattr(self, name, getattr(MediaImportTests, name).__get__(self))
        MediaImportTests.setUp(self)

    async def test_imports_use_persistent_ffprobe_and_cannot_hide_second_special_track(self):
        path = self.video()
        await self.reconcile(self.entry(path, quality=80))
        for options in (
            {"codecs": ["avc"]},
            {"allow_dolby_vision": False},
            {"allow_dolby_audio": False},
            {"audio_max_quality": "30280"},
        ):
            with self.subTest(options=options):
                self.assertIsNone(
                    self.media.retention.source(
                        "BV1xx411c7mD", "123", "80", {"quality": policy(**options)}
                    )
                )
        payload = {
            "bvid": "BV1xx411c7mD",
            "selected_cids": ["123"],
            "target_id": "synthetic",
            "quality": policy(codecs=["hevc"]),
            "layout": "single",
        }
        self.media.repo.enqueue(payload)
        job = self.media.repo.claim("synthetic-owner")
        result = self.media.retention.source("BV1xx411c7mD", "123", "80", job)
        self.assertIsNotNone(result)
        self.assertEqual(result["source"]["kind"], "import")
        self.assertEqual(result["media"]["rawffprobe_streams"][2]["codec_name"], "eac3")
        self.assertTrue(path.samefile(result["file"]))


if __name__ == "__main__":
    unittest.main()

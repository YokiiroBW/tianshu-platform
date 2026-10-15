"""Requested policy and observed audio facts are separate, never inferred from a suffix."""

from ...contracts import require

LEVELS = {"standard": 0, "higher": 1, "exhigh": 2, "lossless": 3, "hires": 4}
DEFAULT = {"level": "lossless", "allow_fallback": True}


def requested_levels(requested):
    highest = "hires" if requested["level"] == "best" else requested["level"]
    if requested["level"] != "best" and not requested["allow_fallback"]:
        return [highest]
    return [level for level in reversed(LEVELS) if LEVELS[level] <= LEVELS[highest]]


def policy(value=None):
    value = DEFAULT if value is None else value
    require(
        isinstance(value, dict)
        and set(value) == {"level", "allow_fallback"}
        and isinstance(value["level"], str)
        and value["level"] in {*LEVELS, "best"}
        and type(value["allow_fallback"]) is bool,
        "invalid_music_quality",
        400,
    )
    return dict(value)


def verify_quality(requested, offered, observed):
    require(offered in LEVELS, "music_quality_unknown", 409)
    lossless = observed["codec"] in {"flac", "alac"}
    require(LEVELS[offered] < 3 or lossless, "music_quality_mismatch", 409)
    if offered == "hires":
        require(
            observed["bits_per_sample"] > 16 or observed["sample_rate"] > 48000,
            "music_quality_mismatch",
            409,
        )
    if requested["level"] != "best" and not requested["allow_fallback"]:
        require(LEVELS[offered] >= LEVELS[requested["level"]], "music_quality_unavailable", 409)
    return {
        **observed,
        "level": offered,
        "lossless": lossless,
        "fallback": requested["level"] != "best" and LEVELS[offered] < LEVELS[requested["level"]],
    }

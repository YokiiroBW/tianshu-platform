"""Requested policy and observed audio facts are separate, never inferred from a suffix."""

from ...contracts import require

LEVELS = {"standard": 0, "higher": 1, "exhigh": 2, "lossless": 3, "hires": 4}
DEFAULT = {"level": "best", "allow_fallback": True}


def requested_levels(requested):
    minimum = minimum_level(requested)
    return [level for level in reversed(LEVELS) if minimum <= LEVELS[level]]


def aggregate(policies):
    highest = max(policies, key=lambda p: LEVELS.get(p["level"], 4))
    minimum = max(
        (LEVELS[p["level"]] for p in policies if p["level"] != "best" and not p["allow_fallback"]),
        default=0,
    )
    return {"level": highest["level"], "allow_fallback": True, "minimum_level": minimum}


def minimum_level(requested):
    minimum = requested.get("minimum_level", 0)
    if not requested["allow_fallback"] and requested["level"] != "best":
        minimum = max(minimum, LEVELS[requested["level"]])
    return minimum


def quality_rank(quality):
    lossless = quality.get("codec") in {"flac", "alac"}
    return (
        LEVELS[quality["level"]],
        quality.get("bits_per_sample", 0) if lossless else 0,
        quality.get("sample_rate", 0) if lossless else 0,
        0 if lossless else quality.get("bit_rate", 0),
    )


def satisfies(requested, artifact):
    actual = LEVELS[artifact["quality"]["level"]]
    minimum = minimum_level(requested)
    if actual < minimum:
        return False
    desired = LEVELS.get(requested["level"], 4)
    attempted = artifact.get("requested_quality") or {}
    return actual >= desired or (
        requested["allow_fallback"]
        and LEVELS.get(attempted.get("level"), 4 if attempted.get("level") == "best" else -1)
        >= desired
    )


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
    lossless = observed["codec"] in {"flac", "alac"}
    actual = (
        (
            "hires"
            if observed["bits_per_sample"] > 16 or observed["sample_rate"] > 48000
            else "lossless"
        )
        if lossless
        else (
            "exhigh"
            if observed["bit_rate"] >= 280000
            else "higher"
            if observed["bit_rate"] >= 160000
            else "standard"
        )
    )
    require(offered == "observed" or offered in LEVELS, "music_quality_unknown", 409)
    require(offered == "observed" or LEVELS[offered] < 3 or lossless, "music_quality_mismatch", 409)
    if offered == "hires":
        require(
            observed["bits_per_sample"] > 16 or observed["sample_rate"] > 48000,
            "music_quality_mismatch",
            409,
        )
    if requested["level"] != "best" and not requested["allow_fallback"]:
        require(LEVELS[actual] >= LEVELS[requested["level"]], "music_quality_unavailable", 409)
    require(LEVELS[actual] >= requested.get("minimum_level", 0), "music_quality_unavailable", 409)
    return {
        **observed,
        "level": actual,
        "offered_level": offered,
        "lossless": lossless,
        "fallback": requested["level"] != "best" and LEVELS[actual] < LEVELS[requested["level"]],
    }

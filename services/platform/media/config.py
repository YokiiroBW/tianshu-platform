"""The media owner's configuration and public command validation."""

import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ..contracts import require

ENGINE_VERSION = "2026.08.19"
SOURCE_KINDS = ("favorite", "collection", "series", "uploader")
EMPTY_RULES = {"schema_version": 1, "revision": 1, "whitelist": [], "blacklist": []}


def text(value, maximum=256):
    require(isinstance(value, str) and 0 < len(value.strip()) <= maximum, "invalid_input", 400)
    require(not any(ord(c) < 32 for c in value), "invalid_input", 400)
    return value.strip()


def quality(value):
    require(isinstance(value, dict), "invalid_input", 400)
    require(set(value) == {"mode", "quality_id", "allow_fallback"}, "invalid_input", 400)
    require(value["mode"] in {"best", "exact"}, "invalid_input", 400)
    require(type(value["allow_fallback"]) is bool, "invalid_input", 400)
    if value["mode"] == "best":
        require(value["quality_id"] is None, "invalid_input", 400)
    else:
        require(
            isinstance(value["quality_id"], str)
            and re.fullmatch(r"[1-9][0-9]{0,3}", value["quality_id"]),
            "invalid_input",
            400,
        )
    return dict(value)


def source(value):
    require(isinstance(value, dict) and set(value) == {"kind", "id"}, "invalid_input", 400)
    require(value["kind"] in SOURCE_KINDS, "invalid_input", 400)
    identity = value["id"]
    require(isinstance(identity, str) and len(identity) <= 2048, "invalid_input", 400)
    if identity.startswith("https://"):
        parsed = urlsplit(identity)
        require(
            parsed.hostname in {"space.bilibili.com", "www.bilibili.com", "bilibili.com"}
            and not parsed.username
            and not parsed.password
            and parsed.port in {None, 443},
            "invalid_source_url",
            400,
        )
        params = parse_qs(parsed.query)
        mid = (
            re.fullmatch(r"/([1-9][0-9]{0,19})(?:/(.*))?/?", parsed.path)
            if parsed.hostname == "space.bilibili.com"
            else None
        )
        if value["kind"] == "uploader":
            require(mid and (mid[2] or "").strip("/") in {"", "video"}, "source_kind_mismatch", 400)
            identity = mid[1]
        elif value["kind"] == "favorite":
            require(
                mid and (mid[2] or "").strip("/") == "favlist" and len(params.get("fid", [])) == 1,
                "source_kind_mismatch",
                400,
            )
            identity = params["fid"][0]
        else:
            sid_name = "sid"
            expected_type = "season" if value["kind"] == "collection" else "series"
            if mid:
                suffix = (mid[2] or "").strip("/")
                list_path = re.fullmatch(r"lists/([1-9][0-9]{0,19})", suffix)
                legacy_path = (
                    suffix == "channel/collectiondetail"
                    if value["kind"] == "collection"
                    else suffix == "channel/seriesdetail"
                )
                require(
                    list_path
                    and params.get("type") == [expected_type]
                    or legacy_path
                    and len(params.get(sid_name, [])) == 1,
                    "source_kind_mismatch",
                    400,
                )
                identity = mid[1] + ":" + (list_path[1] if list_path else params[sid_name][0])
            else:
                require(
                    parsed.path.rstrip("/") == "/medialist/play"
                    and params.get("business") == ["space"]
                    and params.get("business_type")
                    == ["5" if value["kind"] == "collection" else "1"]
                    and len(params.get("business_id", [])) == 1
                    and len(params.get("mid", [])) == 1,
                    "source_kind_mismatch",
                    400,
                )
                identity = params["mid"][0] + ":" + params["business_id"][0]
    pattern = r"[1-9][0-9]{0,19}"
    if value["kind"] in {"collection", "series"}:
        pattern += r":[1-9][0-9]{0,19}"
    require(re.fullmatch(pattern, identity), "invalid_input", 400)
    return {"kind": value["kind"], "id": identity}


def configuration(settings):
    raw = settings.get("media")
    if raw is None:
        return None
    require(isinstance(raw, dict), "invalid_input", 400)
    require(
        set(raw)
        <= {
            "enabled",
            "staging_directory",
            "package_contract_directory",
            "engine",
            "targets",
            "scan_page_limit",
            "scan_item_limit",
            "scan_timeout_seconds",
            "workers",
        },
        "invalid_input",
        400,
    )
    require(type(raw.get("enabled", True)) is bool, "invalid_input", 400)
    if not raw.get("enabled", True):
        return None
    staging = raw.get(
        "staging_directory", str(Path(settings["database_path"]).with_suffix(".media-staging"))
    )
    require(isinstance(staging, str) and Path(staging).is_absolute(), "invalid_input", 400)
    engine = raw.get("engine", {})
    require(
        isinstance(engine, dict)
        and set(engine) <= {"python", "ffmpeg", "ffprobe", "timeout_seconds", "max_bytes"},
        "invalid_input",
        400,
    )
    targets = raw.get("targets", [])
    require(isinstance(targets, list) and len(targets) <= 32, "invalid_input", 400)
    ids = set()
    for target in targets:
        require(isinstance(target, dict), "invalid_input", 400)
        identity = text(target.get("target_id"), 64)
        require(identity not in ids, "invalid_input", 400)
        ids.add(identity)
        text(target.get("label"), 128)
        require(isinstance(target.get("servers", []), list), "invalid_input", 400)
    result = {
        "staging_directory": str(Path(staging).resolve()),
        "package_contract_directory": raw.get(
            "package_contract_directory",
            str(Path(settings["contract_directory"]).resolve().parents[1] / "media-package" / "v1"),
        ),
        "engine": {
            "python": engine.get("python", sys.executable),
            "ffmpeg": engine.get("ffmpeg", "ffmpeg"),
            "ffprobe": engine.get("ffprobe", "ffprobe"),
            "timeout_seconds": engine.get("timeout_seconds", 7200),
            "max_bytes": engine.get("max_bytes", 20 * 1024**3),
        },
        "targets": {target["target_id"]: dict(target) for target in targets},
        "scan_page_limit": raw.get("scan_page_limit", 200),
        "scan_item_limit": raw.get("scan_item_limit", 5000),
        "scan_timeout_seconds": raw.get("scan_timeout_seconds", 900),
        "workers": raw.get("workers", 2),
    }
    for name, lower, upper in (
        ("scan_page_limit", 1, 1000),
        ("scan_item_limit", 1, 50000),
        ("scan_timeout_seconds", 30, 86400),
        ("workers", 1, 4),
    ):
        require(type(result[name]) is int and lower <= result[name] <= upper, "invalid_input", 400)
    for name, lower, upper in (
        ("timeout_seconds", 30, 86400),
        ("max_bytes", 1024**2, 200 * 1024**3),
    ):
        require(
            type(result["engine"][name]) is int and lower <= result["engine"][name] <= upper,
            "invalid_input",
            400,
        )
    for name in ("python", "ffmpeg", "ffprobe"):
        text(result["engine"][name], 4096)
    return result

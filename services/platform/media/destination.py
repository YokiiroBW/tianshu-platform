"""One directory identity for subscription snapshots, queue ownership and publication."""

import hashlib
import os
from pathlib import Path

from ..contracts import require
from .identity import looks_like_windows_device_name, require_relative_path


def subdirectory(value=""):
    require(isinstance(value, str), "invalid_destination_subdirectory", 400)
    value = value.strip().replace("\\", "/")
    require(
        not any(0xD800 <= ord(c) <= 0xDFFF for c in value), "invalid_destination_subdirectory", 400
    )
    require(len(value) <= 1024, "invalid_destination_subdirectory", 400)
    if not value:
        return ""
    parts = value.split("/")
    require(parts[0].casefold() != ".tianshu-transfer", "invalid_destination_subdirectory", 400)
    require(
        all(
            part not in {"", ".", ".."}
            and len(part.encode("utf-8")) <= 255
            and not any(ord(c) < 32 or c in ':<>"|?*' for c in part)
            and not part.endswith((" ", "."))
            and not looks_like_windows_device_name(part)
            for part in parts
        ),
        "invalid_destination_subdirectory",
        400,
    )
    return value


def safe_path(root, relative):
    require_relative_path(relative)
    parts = relative.split("/")
    require(
        all(
            not any(c in part for c in ':\x00<>"|?*')
            and not part.endswith((" ", "."))
            and not looks_like_windows_device_name(part)
            for part in parts
        ),
        "unsafe_library_path",
        409,
    )
    root = Path(root)
    for parent in (root, *root.parents):
        require(not parent.is_symlink() and not parent.is_junction(), "unsafe_library_path", 409)
    path = root
    for part in parts:
        path = path / part
        require(not path.is_symlink() and not path.is_junction(), "unsafe_library_path", 409)
    require(path.resolve().is_relative_to(root.resolve()), "unsafe_library_path", 409)
    return path


def snapshot(target, value=""):
    value = subdirectory(value)
    require(not value or target.get("kind") == "media_server", "destination_not_supported", 409)
    root = target.get("directory")
    path = safe_path(Path(root), value) if root and value else Path(root) if root else None
    if path:
        for parent in (path, *path.parents):
            require(
                not parent.is_symlink() and not parent.is_junction(), "unsafe_library_path", 409
            )
    return {
        "destination_subdirectory": value,
        "destination_directory": str(path) if path else None,
    }


def head_key(job):
    # Retain the existing empty-directory key, including already published jobs.
    base = job["target_id"] + ":" + job["bvid"]
    value = directory_identity(job)
    return base if not value else base + ":dir:" + hashlib.sha256(value.encode()).hexdigest()


def same_destination(first, second):
    return first["target_id"] == second["target_id"] and directory_identity(
        first
    ) == directory_identity(second)


def directory_identity(job):
    return os.path.normcase(job.get("destination_subdirectory") or "").replace("\\", "/")

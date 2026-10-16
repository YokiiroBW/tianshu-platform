"""Deployment settings, independent of AssetLibrary and Home Assistant configuration."""

from pathlib import Path

from ..contracts import require
from ..external_catalog import ExternalCatalog


def configuration(settings):
    value = settings.get("infrastructure")
    if value is None:
        return None
    require(
        isinstance(value, dict)
        and {"directory", "contract_directory", "executor"}
        <= set(value)
        <= {
            "directory",
            "contract_directory",
            "executor",
            "poll_interval_seconds",
            "stale_after_seconds",
            "timeout_ms",
        },
        "invalid_input",
        400,
    )
    for name in ("directory", "contract_directory"):
        require(
            isinstance(value[name], str) and Path(value[name]).is_absolute(), "invalid_input", 400
        )
    external = settings.get("web_external")
    require(
        not external or Path(value["directory"]).resolve() == Path(external["directory"]).resolve(),
        "invalid_input",
        400,
    )
    executor = value["executor"]
    require(
        isinstance(executor, dict) and set(executor) == {"command", "directory"},
        "invalid_input",
        400,
    )
    command = executor["command"]
    require(
        isinstance(command, list)
        and 1 <= len(command) <= 32
        and all(
            isinstance(item, str)
            and 0 < len(item) <= 4096
            and not any(ch in item for ch in "\x00\r\n")
            for item in command
        )
        and Path(command[0]).is_absolute()
        and isinstance(executor["directory"], str)
        and Path(executor["directory"]).is_absolute(),
        "invalid_input",
        400,
    )
    result = {**value, "executor": {**executor, "command": list(command)}}
    for name, default, minimum, maximum in (
        ("poll_interval_seconds", 30, 5, 3600),
        ("stale_after_seconds", 120, 10, 86400),
        ("timeout_ms", 10000, 500, 30000),
    ):
        selected = value.get(name, default)
        require(type(selected) is int and minimum <= selected <= maximum, "invalid_input", 400)
        result[name] = selected
    require(result["stale_after_seconds"] >= result["poll_interval_seconds"], "invalid_input", 400)
    ExternalCatalog.verify_existing(result["directory"])
    from .client import Contract

    Contract(result["contract_directory"])
    return result

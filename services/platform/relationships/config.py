"""Explicit local management configuration; no implicit grants."""

from ..contracts import require
from ..transport import core_settings


def validate(settings):
    config = settings.get("web_relationships")
    if config is None:
        return None
    require(type(config) is dict and type(config.get("enabled")) is bool, "invalid_input", 400)
    require(set(config) <= {"enabled", "memory", "candidate_schema_path"}, "invalid_input", 400)
    if not config["enabled"]:
        return config
    require(set(config) == {"enabled", "memory", "candidate_schema_path"}, "invalid_input", 400)
    core_settings(config["memory"])
    for name in ("web_memory", "web_qq_profiles"):
        reader = settings.get(name)
        require(type(reader) is dict and reader.get("enabled", True), "invalid_input", 400)
        require(
            reader["base_url"].rstrip("/") == config["memory"]["base_url"].rstrip("/"),
            "invalid_input",
            400,
        )
        require(reader["token_env"] != config["memory"]["token_env"], "invalid_input", 400)
    from .contract import Contract

    Contract(config["candidate_schema_path"])
    return config

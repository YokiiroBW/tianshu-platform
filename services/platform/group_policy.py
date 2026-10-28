"""One account's versioned, per-group observation and participation policy."""

import copy
import re

from .contracts import require

MODES = frozenset({"disabled", "observe", "reply", "active"})
GROUP_ID = re.compile(r"[1-9][0-9]*\Z")


def rule(mode="observe", actor_id=None, keywords=()):
    return {"mode": mode, "actor_id": actor_id, "keywords": list(keywords)}


def validate(value, *, saving=False):
    require(
        isinstance(value, dict)
        and set(value) == {"version", "default", "groups"}
        and type(value["version"]) is int
        and value["version"] == 2
        and isinstance(value["groups"], dict)
        and len(value["groups"]) <= 256,
        "invalid_input",
        400,
    )
    for key, item in [(None, value["default"]), *value["groups"].items()]:
        require(
            key is None or isinstance(key, str) and GROUP_ID.fullmatch(key), "invalid_input", 400
        )
        require(
            isinstance(item, dict) and set(item) == {"mode", "actor_id", "keywords"},
            "invalid_input",
            400,
        )
        require(isinstance(item["mode"], str) and item["mode"] in MODES, "invalid_input", 400)
        actor = item["actor_id"]
        require(
            actor is None or isinstance(actor, str) and 1 <= len(actor) <= 128, "invalid_input", 400
        )
        words = item["keywords"]
        require(isinstance(words, list) and len(words) <= 32, "invalid_input", 400)
        require(
            all(isinstance(word, str) and 0 < len(word) <= 64 and word.strip() for word in words),
            "invalid_input",
            400,
        )
        require(len(words) == len(set(words)), "invalid_input", 400)
        if saving and key is not None and item["mode"] != "disabled":
            require(actor is not None, "actor_required", 400)
        if saving and item["mode"] in {"reply", "active"}:
            require(actor is not None, "actor_required", 400)
    return value


def normalize(value):
    """Only explicitly configured groups collect content; retain historical ownership."""
    require(isinstance(value, dict), "invalid_input", 400)
    if value.get("version") == 2:
        result = validate(value)
        if result["default"]["mode"] != "disabled":
            result = copy.deepcopy(result)
            result["default"]["mode"] = "disabled"
        return result
    require(set(value) == {"observe", "mode", "list", "actor_id"}, "invalid_input", 400)
    require(
        type(value["observe"]) is bool
        and value["mode"] in {"observe_only", "whitelist", "blacklist"},
        "invalid_input",
        400,
    )
    require(
        isinstance(value["list"], list)
        and len(value["list"]) <= 256
        and len(set(value["list"])) == len(value["list"]),
        "invalid_input",
        400,
    )
    for group in value["list"]:
        require(isinstance(group, str) and GROUP_ID.fullmatch(group), "invalid_input", 400)
    actor = value["actor_id"]
    if not value["observe"]:
        return {"version": 2, "default": rule("disabled", actor), "groups": {}}
    other = "reply" if value["mode"] == "whitelist" else "observe"
    groups = (
        {}
        if value["mode"] == "observe_only"
        else {key: rule(other, actor) for key in value["list"]}
    )
    return validate({"version": 2, "default": rule("disabled", actor), "groups": groups})


def effective(value, group):
    # Save/load owns closed-shape validation. The event entrance reads only
    # this rule, rather than rescanning every configured group and keyword.
    policy = value if value.get("version") == 2 else normalize(value)
    target = group.removeprefix("group:")
    require(GROUP_ID.fullmatch(target), "invalid_input", 400)
    if target in policy["groups"]:
        return policy["groups"][target]
    return {**policy["default"], "mode": "disabled"}


def decision(value, group, mentioned=False, text=""):
    selected = effective(value, group)
    enabled = selected["mode"] != "disabled"
    permitted = selected["mode"] in {"reply", "active"}
    keyword = bool(permitted and text and any(word in text for word in selected["keywords"]))
    return {
        "observe": enabled,
        "reply_permitted": permitted,
        "reply_triggered": bool(permitted and (mentioned or keyword)),
        "proactive_permitted": selected["mode"] == "active",
        "keyword_triggered": keyword,
        "mode": selected["mode"],
        "actor_id": selected["actor_id"],
    }


def observing(value):
    policy = normalize(value)
    return any(item["mode"] != "disabled" for item in policy["groups"].values())


def default():
    return {"version": 2, "default": rule("disabled"), "groups": {}}


def copy_policy(value):
    return copy.deepcopy(normalize(value))


def advance(row, old, new, known_groups=()):
    """Persist only this owner's delivery generations, independently per group."""
    old, new = normalize(old), normalize(new)
    versions = dict(row.get("group_revisions", {}))
    previous = row.get("group_default_revision", row["host_revision"])
    groups = set(old["groups"]) | set(new["groups"]) | set(versions) | set(known_groups)
    for group in groups:
        version = versions.get(group, previous)
        versions[group] = (
            row["host_revision"] + 1 if effective(old, group) != effective(new, group) else version
        )
    row["group_revisions"] = versions
    row["group_default_revision"] = (
        row["host_revision"] + 1 if old["default"] != new["default"] else previous
    )


def revision(row, group):
    target = group.removeprefix("group:")
    return row.get("group_revisions", {}).get(
        target, row.get("group_default_revision", row["host_revision"])
    )

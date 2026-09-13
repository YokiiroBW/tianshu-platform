"""Deployment-owned identities; payloads cannot manufacture authenticated callers."""

import copy
import hmac
import os
import re

from .contracts import digest, require

ACTIONS = {
    "origin.issue",
    "origin.resolve",
    "origin.revoke",
    "entry.revoke",
    "principal.revoke",
    "mapping.prepare",
    "mapping.confirm",
    "source.observe",
    "source.verify",
    "config.publish",
    "config.revoke",
    "config.snapshot",
    "config.view",
    "capability.read",
    "task.read",
    "task.project",
}
PURPOSES = {"dialogue", "config.snapshot"}


def secret(name):
    value = os.environ.get(name, "")
    if not 24 <= len(value) <= 4096 or not all(33 <= ord(c) <= 126 for c in value):
        return None
    return value


class Auth:
    def __init__(self, settings, contracts):
        self.principals = copy.deepcopy(settings.get("principals", {}))
        self.entries = copy.deepcopy(settings.get("entries", {}))
        self.mode = settings["mode"]
        require(self.mode == "local_rehearsal", "dependency_unavailable", 503)
        envs = []
        for key, item in self.principals.items():
            contracts.check("common#id", key)
            require(
                set(item)
                <= {
                    "kind",
                    "service",
                    "token_env",
                    "actions",
                    "account",
                    "resolver",
                    "config_versions",
                    "task_owners",
                },
                "invalid_input",
                400,
            )
            require(item["kind"] in {"operator", "service"}, "invalid_input", 400)
            contracts.check("common#id", item["service"])
            require(re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", item["token_env"]), "invalid_input", 400)
            envs.append(item["token_env"])
            require(isinstance(item["actions"], list), "invalid_input", 400)
            require(set(item["actions"]) <= ACTIONS, "invalid_input", 400)
            require(isinstance(item.get("task_owners", []), list), "invalid_input", 400)
            for owner in item.get("task_owners", []):
                contracts.check("common#id", owner)
            require(isinstance(item.get("config_versions", []), list), "invalid_input", 400)
            if item["kind"] == "operator":
                contracts.check("common#account", item["account"])
                require(item["account"]["namespace"] == "web", "invalid_input", 400)
            if "resolver" in item:
                require(set(item["resolver"]) == {"caller", "purpose"}, "invalid_input", 400)
                contracts.check("common#id", item["resolver"]["caller"])
                require(item["resolver"]["purpose"] in PURPOSES, "invalid_input", 400)
            require(
                all(type(v) is int and v > 0 for v in item.get("config_versions", [])),
                "invalid_input",
                400,
            )
        require(len(set(envs)) == len(envs), "invalid_input", 400)
        for key, entry in self.entries.items():
            contracts.check("common#id", key)
            require(
                set(entry)
                == {
                    "owner",
                    "kind",
                    "account",
                    "channel",
                    "actor_id",
                    "audience",
                    "routes",
                    "ttl_seconds",
                },
                "invalid_input",
                400,
            )
            owner = self.principals.get(entry["owner"])
            require(owner is not None, "invalid_input", 400)
            require(
                entry["kind"] in {"local_operator", "rehearsal_connector"}, "invalid_input", 400
            )
            if entry["kind"] == "local_operator":
                require(
                    owner["kind"] == "operator" and entry["account"] == owner["account"],
                    "invalid_input",
                    400,
                )
            else:
                require(owner["kind"] == "service", "invalid_input", 400)
            contracts.check("common#account", entry["account"])
            contracts.check("common#channel_key", entry["channel"])
            contracts.check("common#id", entry["actor_id"])
            require(
                entry["account"]["namespace"] == entry["channel"]["namespace"], "invalid_input", 400
            )
            require(entry["audience"] in {"group", "self_private"}, "invalid_input", 400)
            require(
                type(entry["ttl_seconds"]) is int and 1 <= entry["ttl_seconds"] <= 3600,
                "invalid_input",
                400,
            )
            for route in entry["routes"]:
                require(set(route) == {"caller", "receiver", "purpose"}, "invalid_input", 400)
                contracts.check("common#id", route["caller"])
                contracts.check("common#id", route["receiver"])
                require(route["purpose"] in PURPOSES, "invalid_input", 400)
            require(
                len({digest(r) for r in entry["routes"]}) == len(entry["routes"]),
                "invalid_input",
                400,
            )

    def authenticate(self, header, db, action, *, operator=False):
        require(isinstance(header, str) and header.startswith("Bearer "), "unauthorized", 401)
        token = header[7:]
        require(token.isascii(), "unauthorized", 401)
        matches = []
        for key, item in self.principals.items():
            expected = secret(item["token_env"])
            if expected and hmac.compare_digest(token, expected):
                matches.append((key, item))
        require(len(matches) == 1, "unauthorized", 401)
        key, item = matches[0]
        require(
            not db.execute("SELECT 1 FROM revoked_principals WHERE id=?", (key,)).fetchone(),
            "unauthorized",
            401,
        )
        require(action in item["actions"] and (not operator or item["kind"] == "operator"))
        return key, item

    def entry(self, db, entry_id, expected_digest=None):
        entry = self.entries.get(entry_id)
        require(entry is not None)
        require(not db.execute("SELECT 1 FROM revoked_entries WHERE id=?", (entry_id,)).fetchone())
        require(
            not db.execute(
                "SELECT 1 FROM revoked_principals WHERE id=?", (entry["owner"],)
            ).fetchone()
        )
        require(secret(self.principals[entry["owner"]]["token_env"]) is not None)
        require(expected_digest is None or expected_digest == digest(entry))
        return entry

    def route(self, entry, caller, receiver, purpose):
        require({"caller": caller, "receiver": receiver, "purpose": purpose} in entry["routes"])

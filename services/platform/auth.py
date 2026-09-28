"""Deployment-owned identities; payloads cannot manufacture authenticated callers."""

import copy
import hmac
import os
import re

from .contracts import canonical, digest, epoch, loads, require

ACTIONS = {
    "bot.manage",
    "dialogue.send",
    "asset.read",
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
    "config.select",
    "provider.runtime",
    "capability.read",
    "task.read",
    "task.project",
    "source.register",
    "source.input",
    "source.current",
    "observation.verify",
    "source.dispatch",
    "device.control",
    "persona.read",
    "knowledge.read",
    "life.read",
    "memory.read",
    "external.manage",
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
        require(self.mode in {"local_rehearsal", "service_https"}, "dependency_unavailable", 503)
        policy = {k: settings.get(k) for k in ("mode", "principals", "entries", "input_entries")}
        if "asset_connections" in settings:
            policy["asset_connections"] = settings["asset_connections"]
        # Both persona sections are part of the authority a browser session is pinned to, so
        # re-pointing the character service or narrowing the subject allowlist expires the
        # sessions that were opened against the previous deployment.
        for key in (
            "persona_connections",
            "web_personas",
            "web_knowledge",
            "web_life",
            "web_memory",
        ):
            if key in settings:
                policy[key] = settings[key]
        self.policy_digest = digest(policy)
        # The persona page reads through a deployment credential of its own. Its *effective value*
        # is part of the authority a browser session is pinned to, so rotating or removing that
        # credential expires every session and every cursor opened against the previous one - a
        # session is not allowed to outlive the identity it was admitted with. Only the resolved
        # digest is ever kept; the variable's name is configuration and its value never is.
        table = settings.get("persona_connections")
        self.persona_envs = tuple(
            sorted(
                entry["token_env"]
                for entry in (table.values() if isinstance(table, dict) else ())
                if isinstance(entry, dict) and isinstance(entry.get("token_env"), str)
            )
        )
        self.browser_reader_envs = tuple(
            sorted(
                entry["token_env"]
                for entry in (
                    settings.get("web_knowledge"),
                    settings.get("web_life"),
                    settings.get("web_memory"),
                )
                if isinstance(entry, dict) and isinstance(entry.get("token_env"), str)
            )
        )
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
                    "native_config_versions",
                    "task_owners",
                    "asset_connections",
                },
                "invalid_input",
                400,
            )
            require(item["kind"] in {"operator", "service"}, "invalid_input", 400)
            contracts.check("common#id", item["service"])
            require(re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", item["token_env"]), "invalid_input", 400)
            envs.append(item["token_env"])
            require(isinstance(item["actions"], list), "invalid_input", 400)
            require(isinstance(item.get("asset_connections", []), list), "invalid_input", 400)
            for connection in item.get("asset_connections", []):
                contracts.check("common#id", connection)
                require(connection in settings.get("asset_connections", {}), "invalid_input", 400)
            require(set(item["actions"]) <= ACTIONS, "invalid_input", 400)
            require(isinstance(item.get("task_owners", []), list), "invalid_input", 400)
            for owner in item.get("task_owners", []):
                contracts.check("common#id", owner)
            require(isinstance(item.get("config_versions", []), list), "invalid_input", 400)
            require(isinstance(item.get("native_config_versions", []), list), "invalid_input", 400)
            if item["kind"] == "operator":
                contracts.check("common#account", item["account"])
                require(item["account"]["namespace"] == "web", "invalid_input", 400)
            if "resolver" in item:
                require(set(item["resolver"]) == {"caller", "purpose"}, "invalid_input", 400)
                contracts.check("common#id", item["resolver"]["caller"])
                require(item["resolver"]["purpose"] in PURPOSES, "invalid_input", 400)
            for key in ("config_versions", "native_config_versions"):
                require(
                    all(type(v) is int and v > 0 for v in item.get(key, [])),
                    "invalid_input",
                    400,
                )
        require(len(set(envs)) == len(envs), "invalid_input", 400)
        for key, entry in self.entries.items():
            contracts.check("common#id", key)
            require(
                set(entry) - {"expires_at"}
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
                entry["kind"] in {"local_operator", "rehearsal_connector", "trusted_application"},
                "invalid_input",
                400,
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
            if "expires_at" in entry:
                epoch(entry["expires_at"])

    def activate(self, store, clock):
        self.clock = clock
        with store.connect(write=True) as db:
            row = db.execute("SELECT document FROM authority_policy WHERE singleton=1").fetchone()
            previous = loads(row[0]) if row else None
            state = {"policy": self.policy_digest, "effective": self.effective_digest()}
            if previous != state:
                db.execute(
                    "INSERT OR REPLACE INTO authority_policy VALUES(1,?)", (canonical(state),)
                )

    def effective_digest(self):
        # Persist only a combined fingerprint; never tokens or environment values.
        return digest(
            {
                "credentials": {
                    k: digest(secret(p["token_env"])) for k, p in self.principals.items()
                },
                "persona": {name: digest(secret(name)) for name in self.persona_envs},
                "browser_readers": {
                    name: digest(secret(name)) for name in self.browser_reader_envs
                },
                "expired": sorted(
                    k
                    for k, e in self.entries.items()
                    if "expires_at" in e and epoch(e["expires_at"]) <= self.clock()
                ),
            }
        )

    def sync(self, db):
        row = db.execute("SELECT document FROM authority_policy WHERE singleton=1").fetchone()
        require(row is not None, "dependency_unavailable", 503)
        state = loads(row[0])
        require(state["policy"] == self.policy_digest, "dependency_unavailable", 503)
        effective = self.effective_digest()
        if state["effective"] != effective:
            state["effective"] = effective
            db.execute(
                "UPDATE authority_policy SET document=? WHERE singleton=1", (canonical(state),)
            )

    def authenticate(self, header, db, action, *, operator=False):
        self.sync(db)
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
        require("expires_at" not in entry or epoch(entry["expires_at"]) > self.clock())
        require(expected_digest is None or expected_digest == digest(entry))
        return entry

    def route(self, entry, caller, receiver, purpose):
        require({"caller": caller, "receiver": receiver, "purpose": purpose} in entry["routes"])

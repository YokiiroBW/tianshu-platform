"""Explicit coordinator-run check of a deployed public console; never prints response bodies.

Credentials come from a private input's environment/file references. Only an explicitly requested
missing node registration is saved; existing node settings, accounts and permissions are untouched.
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import ssl
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp


class CheckFailed(Exception):
    pass


def check(condition, code):
    if not condition:
        raise CheckFailed(code)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def later(value, previous):
    return bool(
        value
        and previous
        and datetime.fromisoformat(value.replace("Z", "+00:00"))
        > datetime.fromisoformat(previous.replace("Z", "+00:00"))
    )


def credential(config, name):
    choices = [key for key in (name, name + "_env", name + "_file") if key in config]
    check(len(choices) == 1 and (name != "password" or name not in choices), "credential_input")
    key = choices[0]
    if key.endswith("_env"):
        check(re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", config[key]), "credential_input")
        value = os.environ.get(config[key], "")
    elif key.endswith("_file"):
        path = Path(config[key])
        check(path.is_absolute(), "credential_input")
        with path.open("rb") as source:
            raw = source.read(2049)
        check(len(raw) <= 2048, "credential_input")
        value = raw.decode("utf-8").rstrip("\r\n")
    else:
        value = config[key]
    check(isinstance(value, str) and 1 <= len(value) <= 256, "credential_unavailable")
    return value


def validate(config):
    check(isinstance(config, dict), "invalid_input")
    allowed = {
        "origin",
        "username",
        "username_env",
        "username_file",
        "password_env",
        "password_file",
        "node_id",
        "label",
        "register_if_missing",
        "phase",
        "timeout_seconds",
        "log_container_id",
        "ca_file",
    }
    check(set(config) <= allowed and {"origin", "node_id"} <= set(config), "invalid_input")
    parts = urlsplit(config["origin"])
    check(
        parts.scheme in {"http", "https"}
        and parts.hostname
        and not (parts.path or parts.query or parts.fragment or parts.username or parts.password)
        and not any(character.isspace() for character in config["origin"]),
        "invalid_origin",
    )
    check(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", config["node_id"]), "invalid_input")
    check(config.get("phase", "initial") in {"initial", "after_restart"}, "invalid_input")
    check(type(config.get("register_if_missing", False)) is bool, "invalid_input")
    check(
        type(config.get("timeout_seconds", 180)) is int
        and 30 <= config.get("timeout_seconds", 180) <= 600,
        "invalid_input",
    )
    if config.get("log_container_id") is not None:
        check(re.fullmatch(r"[a-f0-9]{64}", config["log_container_id"]), "invalid_input")
    if config.get("ca_file") is not None:
        check(Path(config["ca_file"]).is_absolute(), "invalid_input")
    if config.get("phase") == "after_restart":
        check(not config.get("register_if_missing", False), "invalid_input")


class ConsoleCheck:
    def __init__(self, config, report):
        self.config, self.report = config, report
        self.origin, self.node = config["origin"], config["node_id"]
        self.csrf, self.client = None, None
        self.deadline = time.monotonic() + config.get("timeout_seconds", 180)

    async def request(self, method, path, body=None):
        self.report["stage"] = path.removeprefix("/api/web/")
        headers = {"Origin": self.origin, "X-CSRF-Token": self.csrf} if method == "POST" else {}
        async with self.client.request(
            method, self.origin + path, json=body, headers=headers, allow_redirects=False
        ) as response:
            check(response.status == 200, "http_" + str(response.status))
            raw = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                raw.extend(chunk)
                check(len(raw) <= 4 * 1024**2, "response_limit")
            document = json.loads(raw)
            check(isinstance(document, dict), "response_invalid")
            return document

    async def post(self, operation, body=None):
        return await self.request("POST", "/api/web/" + operation, body or {})

    async def wait(self, read, ready):
        while time.monotonic() < self.deadline:
            value = await read()
            if ready(value):
                return value
            await asyncio.sleep(0.5)
        raise CheckFailed("observation_deadline")

    async def read_node(self):
        return await self.post("infrastructure/node", {"node_id": self.node})

    async def refresh(self):
        receipt = await self.post("infrastructure/refresh", {"node_id": self.node})
        result = await self.wait(
            self.read_node,
            lambda value: (
                value.get("refresh")
                and value["refresh"]["attempt_id"] == receipt["attempt_id"]
                and value["refresh"]["finished_at"] is not None
            ),
        )
        check(result["snapshot"]["freshness"] == "current", "node_not_current")
        return result

    async def inventory(self):
        identities, statistics, stamp, pages, first = set(), [], None, 0, None
        tail = None
        while pages < 50:
            listing = await self.post(
                "infrastructure/containers", {"node_id": self.node, "page": pages, "page_size": 200}
            )
            if first is None:
                first, stamp = listing, listing["inventory"]["observed_at"]
            check(listing["inventory"]["observed_at"] == stamp, "inventory_changed")
            for row in listing["containers"]:
                check(row["id"] not in identities, "inventory_duplicate")
                identities.add(row["id"])
                check(row.get("stats") and row["stats"]["id"] == row["id"], "metrics_incomplete")
                statistics.append(row.get("stats"))
                tail = row
            pages += 1
            if not listing["has_more"]:
                break
        check(not listing["has_more"], "inventory_page_limit")
        completeness = first["inventory"]["completeness"]
        check(
            first["inventory"]["freshness"] == "current"
            and completeness
            and completeness["state"] == "complete"
            and completeness["returned"] == len(identities) == first["inventory_total"],
            "inventory_incomplete",
        )
        metrics = first["metrics"]
        check(
            metrics["freshness"] == "current"
            and metrics["completeness"]
            and metrics["completeness"]["returned"] == len(identities),
            "metrics_incomplete",
        )
        self.report["inventory"] = {
            "count": len(identities),
            "pages": pages,
            "observed_at": stamp,
            "identity_sha256": digest("\n".join(sorted(identities))),
            "stats_available": sum(bool(row and row["availability"] == "ok") for row in statistics),
            "cpu_known": sum(
                bool(row and row.get("cpu") and row["cpu"]["usage_ratio"] is not None)
                for row in statistics
            ),
            "metrics_status": metrics["status"],
            "metrics_reasons": metrics["reason_codes"],
            "metrics_returned": metrics["completeness"]["returned"],
            "tail": {
                "identity_sha256": digest(tail["id"]),
                "availability": tail["stats"]["availability"],
                "sampled_at": tail["stats"]["sampled_at"],
                "cpu_state": (tail["stats"].get("cpu") or {}).get("state"),
            }
            if tail
            else None,
        }
        return identities

    async def logs(self, identities):
        identity = self.config.get("log_container_id")
        self.report["logs"] = {"state": "not_requested"}
        if identity is None or self.config.get("phase") == "after_restart":
            return
        check(identity in identities, "log_target_missing")
        body = {"node_id": self.node, "container_id": identity}
        await self.post("infrastructure/logs/refresh", body)
        result = await self.wait(
            lambda: self.post("infrastructure/logs", body),
            lambda value: value["state"] in {"observed", "failed"},
        )
        check(result["state"] == "observed" and result["data"], "logs_unavailable")
        data = result["data"]
        raw = data["stdout"].encode() + data["stderr"].encode()
        check(len(raw) == data["returned_bytes"] <= 65536 and data["tail"] <= 200, "log_bounds")
        self.report["logs"] = {
            "state": "observed",
            "bytes": len(raw),
            "truncated": data["truncated"],
            "sha256": hashlib.sha256(raw).hexdigest(),
            "observed_at": result["observed_at"],
        }

    async def run(self):
        username, password = (
            credential(self.config, "username"),
            credential(self.config, "password"),
        )
        context = ssl.create_default_context(cafile=self.config.get("ca_file"))
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=context),
            cookie_jar=aiohttp.CookieJar(unsafe=True),
            timeout=aiohttp.ClientTimeout(total=6),
            trust_env=False,
        ) as self.client:
            self.csrf = (await self.request("GET", "/api/web/session"))["csrf"]
            logged = await self.post("login", {"username": username, "password": password})
            check(logged.get("authenticated") is True, "login_unverified")
            self.csrf = logged["csrf"]
            try:
                await self.observe()
            finally:
                stage = self.report["stage"]
                try:
                    await self.post("logout")
                    self.report["logout"] = "confirmed"
                except (CheckFailed, aiohttp.ClientError, TimeoutError):
                    self.report["logout"] = "unverified"
                self.report["stage"] = stage
            check(self.report["logout"] == "confirmed", "logout_unverified")

    async def observe(self):
        view = await self.wait(
            lambda: self.post("infrastructure/view"),
            lambda value: value.get("configured") and value.get("catalog", {}).get("data"),
        )
        registered = next((row for row in view["nodes"] if row["id"] == self.node), None)
        if registered is None:
            check(self.config.get("register_if_missing", False), "node_not_registered")
            source = next(
                (row for row in view["catalog"]["data"]["nodes"] if row["id"] == self.node), None
            )
            check(source is not None, "node_not_in_catalog")
            await self.post(
                "infrastructure/save",
                {
                    "node_id": self.node,
                    "label": self.config.get("label", source["label"]),
                    "enabled": True,
                    "expected_revision": view["revision"],
                    "client_id": str(uuid.uuid4()),
                },
            )
        else:
            check(registered["enabled"], "node_disabled")
        if self.config.get("phase", "initial") == "initial":
            first = await self.refresh()
            second = await self.refresh()
        else:
            first = await self.wait(
                self.read_node, lambda value: value["snapshot"]["freshness"] == "current"
            )
            stamp = first["snapshot"]["observed_at"]
            for _ in range(2):
                second = await self.wait(
                    self.read_node,
                    lambda value: (
                        value["snapshot"]["freshness"] == "current"
                        and later(value["snapshot"]["observed_at"], stamp)
                        and value["refresh"]
                        and value["refresh"]["finished_at"] is not None
                    ),
                )
                stamp = second["snapshot"]["observed_at"]
            self.report["background_advances"] = 2
        check(
            later(second["snapshot"]["observed_at"], first["snapshot"]["observed_at"]),
            "sample_not_advanced",
        )
        snapshot = second["snapshot"]["data"]
        check(
            snapshot["cpu"]["state"] == "ok" and snapshot["cpu"]["usage_ratio"] is not None,
            "cpu_difference_unavailable",
        )
        identities = await self.inventory()
        compose = second["compose"]
        stacks = (compose["data"] or {}).get("stacks", [])
        self.report.update(
            {
                "authenticated": True,
                "phase": self.config.get("phase", "initial"),
                "node_sha256": digest(self.node),
                "origin_sha256": digest(self.origin),
                "first_observed_at": first["snapshot"]["observed_at"],
                "second_observed_at": second["snapshot"]["observed_at"],
                "cpu_usage_ratio": snapshot["cpu"]["usage_ratio"],
                "memory_total_bytes": snapshot["memory"]["total_bytes"],
                "filesystems_count": len(snapshot["filesystems"]),
                "interfaces_count": len(snapshot["interfaces"]),
                "compose": {
                    "status": compose["status"],
                    "stacks": len(stacks),
                    "drifted": sum(row["drift_status"] == "drifted" for row in stacks),
                },
            }
        )
        await self.logs(identities)


async def verify(config):
    report = {"schema_version": 1, "status": "failed", "stage": "input"}
    try:
        validate(config)
        await ConsoleCheck(config, report).run()
        report.update(status="passed", stage="complete")
    except CheckFailed as error:
        report["code"] = str(error)
    except (aiohttp.ClientError, TimeoutError):
        report["code"] = "transport_unavailable"
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        report["code"] = "input_or_response_invalid"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        with arguments.input.open("rb") as source:
            raw = source.read(16385)
        check(len(raw) <= 16384, "input_limit")
        report = asyncio.run(verify(json.loads(raw)))
    except (OSError, ValueError, CheckFailed):
        report = {
            "schema_version": 1,
            "status": "failed",
            "stage": "input",
            "code": "input_unavailable",
        }
    arguments.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    check(not arguments.output.is_symlink(), "output_unavailable")
    descriptor = os.open(arguments.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
        json.dump(report, output, ensure_ascii=True, sort_keys=True)
        output.write("\n")
    print(
        json.dumps(
            {"status": report["status"], "stage": report["stage"], "code": report.get("code")}
        )
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

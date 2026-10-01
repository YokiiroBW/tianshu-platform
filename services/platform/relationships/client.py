"""Bounded TLS service adapter. Management calls are never automatically retried."""

import ssl
from datetime import datetime
import re

import aiohttp

from ..auth import secret
from ..contracts import Fault, loads, require
from .. import diagnostics


def timestamp(value):
    if type(value) is not str or len(value) > 40:
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() is not None
    except ValueError:
        return False


class Client:
    def __init__(self, config, contract, reserved):
        self.config, self.contract, self.reserved = config, contract, reserved

    def credential(self):
        token = secret(self.config["token_env"])
        require(token is not None, "dependency_unavailable", 503)
        require(
            all(
                name == self.config["token_env"] or secret(name) != token for name in self.reserved
            ),
            "forbidden",
            403,
        )
        return token

    async def call(self, operation, payload):
        field = "history" if operation == "history" else "projection"
        token = self.credential()
        tls = ssl.create_default_context(cafile=self.config.get("ca_file"))
        sent = False
        try:
            async with aiohttp.ClientSession(
                trust_env=False,
                timeout=aiohttp.ClientTimeout(total=self.config.get("timeout_seconds", 5)),
            ) as client:
                sent = True
                async with client.post(
                    self.config["base_url"].rstrip("/") + "/internal/v1/relationships/" + operation,
                    json=payload,
                    headers={
                        "Authorization": "Bearer " + token,
                        "Accept-Encoding": "identity",
                        **diagnostics.correlation_header(),
                    },
                    ssl=tls,
                    allow_redirects=False,
                ) as response:
                    require(
                        response.content_type == "application/json"
                        and response.headers.get("Content-Encoding", "identity").lower()
                        == "identity",
                        "invalid_upstream",
                        502,
                    )
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(4096):
                        raw.extend(chunk)
                        require(len(raw) <= 32768, "invalid_upstream", 502)
                    try:
                        answer = loads(bytes(raw))
                    except Fault:
                        raise Fault("invalid_upstream", 502) from None
                    require(
                        type(answer) is dict and answer.get("request_id") == payload["request_id"],
                        "invalid_upstream",
                        502,
                    )
                    if response.status != 200:
                        code = answer.get("code")
                        allowed = {
                            "version_conflict",
                            "idempotency_conflict",
                            "migration_pending",
                            "scope_changed",
                            "forbidden",
                            "unauthorized",
                            "invalid_input",
                        }
                        if (
                            type(code) is str
                            and code in allowed
                            and response.status in {400, 401, 403, 409}
                        ):
                            raise Fault(code, response.status)
                        raise Fault(
                            "result_unknown" if operation == "manage" else "dependency_unavailable",
                            503,
                        )
                    require(
                        set(answer) == {"schema_version", "request_id", field}
                        and type(answer["schema_version"]) is int
                        and answer["schema_version"] == 1,
                        "invalid_upstream",
                        502,
                    )
                    value = answer[field]
                    projection = (
                        value.get("projection")
                        if operation == "history" and type(value) is dict
                        else value
                    )
                    self.contract.check("PrivateProjection", projection, upstream=True)
                    require(
                        projection["pair"]
                        == payload.get("pair", payload.get("command", {}).get("pair")),
                        "invalid_upstream",
                        502,
                    )
                    require(
                        timestamp(projection["checked_at"])
                        and timestamp(projection["decay_cursor"])
                        and (
                            projection["frozen_since"] is None
                            or timestamp(projection["frozen_since"])
                        )
                        and projection["frozen"] == (projection["frozen_since"] is not None),
                        "invalid_upstream",
                        502,
                    )
                    if operation == "history":
                        require(
                            set(value) == {"projection", "items", "has_more"}
                            and type(value["items"]) is list
                            and len(value["items"]) <= 20
                            and type(value["has_more"]) is bool,
                            "invalid_upstream",
                            502,
                        )
                        for item in value["items"]:
                            require(
                                type(item) is dict
                                and set(item)
                                == {
                                    "id",
                                    "kind",
                                    "delta",
                                    "outcome",
                                    "at",
                                    "valid",
                                    "operation",
                                    "reason",
                                }
                                and type(item["id"]) is str
                                and re.fullmatch(r"[0-9a-f]{64}", item["id"]) is not None
                                and type(item["kind"]) is str
                                and item["kind"]
                                in {
                                    "automatic",
                                    "management",
                                    "manual_adjustment",
                                    "natural_decay",
                                    "source_correction",
                                    "legacy_import",
                                }
                                and type(item["delta"]) is int
                                and -2000000 <= item["delta"] <= 2000000
                                and type(item["outcome"]) is str
                                and item["outcome"]
                                in {
                                    "accepted",
                                    "no_change",
                                    "rejected_frozen",
                                    "rejected_budget",
                                    "rejected_source",
                                    "rejected_authorization",
                                    "conflict",
                                }
                                and timestamp(item["at"])
                                and type(item["valid"]) is bool
                                and (
                                    item["operation"] is None
                                    or type(item["operation"]) is str
                                    and item["operation"]
                                    in {
                                        "set_binding",
                                        "set_freeze",
                                        "adjust_affinity",
                                    }
                                )
                                and (
                                    item["reason"] is None
                                    or type(item["reason"]) is str
                                    and len(item["reason"]) <= 200
                                ),
                                "invalid_upstream",
                                502,
                            )
                    return value
        except Fault as error:
            if operation == "manage" and sent and error.code == "invalid_upstream":
                raise Fault("result_unknown", 503) from None
            raise
        except (TimeoutError, aiohttp.ClientError, OSError):
            raise Fault(
                "result_unknown" if operation == "manage" and sent else "dependency_unavailable",
                503,
            ) from None

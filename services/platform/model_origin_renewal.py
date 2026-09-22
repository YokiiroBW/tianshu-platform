"""Candidate model-only renewal wire; no issuance authority or delegated origin chain."""

import re
import uuid
from datetime import datetime

from .contracts import digest, epoch, require, utc

PATH = "/internal/v1/model-config/origin/renew"
ROUTE = {"caller": "gateway", "receiver": "platform", "purpose": "config.snapshot"}
FIELDS = {"schema_version", "request_id", "assertion_ref"}


def enabled(settings):
    value = settings.get("model_origin_renewal_http", False)
    require(type(value) is bool, "invalid_input", 400)
    require(not value or settings.get("mode") == "service_https", "invalid_input", 400)
    return value


def validate_request(body):
    require(isinstance(body, dict) and set(body) == FIELDS, "invalid_input", 400)
    require(
        type(body["schema_version"]) is int and body["schema_version"] == 1, "invalid_input", 400
    )
    request_id = body["request_id"]
    try:
        valid_id = isinstance(request_id, str) and str(uuid.UUID(request_id)) == request_id
    except ValueError:
        valid_id = False
    require(valid_id, "invalid_input", 400)
    require(
        isinstance(body["assertion_ref"], str)
        and re.fullmatch(r"origin:[0-9a-f]{32}", body["assertion_ref"]) is not None,
        "invalid_input",
        400,
    )


def renew(origins, header, request):
    validate_request(request)
    # The write transaction serializes renewal with all existing revocation operations.
    # Same-ref updates cannot keep a revoked parent alive through a previously minted child.
    with origins.store.connect(write=True, timeout=2) as db:
        identity, caller = origins.auth.authenticate(header, db, "config.snapshot")
        require(caller["kind"] == "service" and caller["service"] == ROUTE["caller"])
        ref = request["assertion_ref"]
        row = db.execute("SELECT * FROM origins WHERE ref=?", (ref,)).fetchone()
        now = origins.clock()
        require(row is not None and not row["revoked"] and row["expires_at"] > now)
        entry = origins.auth.entry(db, row["entry_id"], row["entry_digest"])
        origins.auth.route(entry, **ROUTE)
        # Renewing a mixed-purpose entry would also extend unrelated authorization.
        require(entry["routes"] == [ROUTE])
        now = origins.clock()
        require(row["expires_at"] > now)
        expires = min(
            now + entry["ttl_seconds"],
            epoch(entry["expires_at"]) if "expires_at" in entry else now + 3600,
        )
        require(expires > now)
        db.execute("UPDATE origins SET expires_at=? WHERE ref=?", (expires, ref))
        db.execute(
            "INSERT INTO audit(principal,operation,object_id,observed_at) VALUES(?,?,?,?)",
            (identity, "origin.renew", digest(ref), now),
        )
        response = {**request, "expires_at": utc(expires)}
        validate_response(response)
        return response


def validate_response(body):
    require(isinstance(body, dict) and set(body) == FIELDS | {"expires_at"}, "invalid_input", 400)
    validate_request({k: body[k] for k in FIELDS})
    value = body["expires_at"]
    require(
        isinstance(value, str)
        and re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z", value
        )
        is not None,
        "invalid_input",
        400,
    )
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        require(False, "invalid_input", 400)

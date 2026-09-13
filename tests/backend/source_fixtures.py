"""Published synthetic fixtures; responses here are explicit Core stand-ins."""

import copy
import json

from fixtures import CONTRACT, settings
from services.platform.contracts import digest, utc

SOURCE_DIR = CONTRACT.parents[1] / "source-sync/v1"
SOURCE_DOCS = {
    d["id"]: d["document"]
    for d in json.loads((SOURCE_DIR / "examples/documents.json").read_text(encoding="utf-8"))
}


def source_settings(directory, audience="group"):
    result = settings(directory)
    data = SOURCE_DOCS[audience + "/request"]["input"]
    result["principals"]["connector"]["service"] = "platform"
    result["principals"]["connector"]["actions"] += ["source.register", "source.dispatch"]
    result["principals"]["companion"]["actions"] += ["source.input"]
    result["principals"]["memory"]["actions"] += ["source.current"]
    result["input_entries"] = {
        "input-entry": {
            "owner": "connector",
            "account": data["author"],
            "channel": data["message_key"]["channel"],
            "audience": audience,
            "ttl_seconds": 60,
            "actor_entries": ["actor-a", "actor-b"],
            "default_actor_ids": ["actor:a"],
            "routing_version": 1,
        }
    }
    for actor in ("a", "b"):
        result["entries"]["actor-" + actor] = {
            "kind": "trusted_application",
            "owner": "connector",
            "account": data["author"],
            "channel": data["message_key"]["channel"],
            "actor_id": "actor:" + actor,
            "audience": audience,
            "ttl_seconds": 60,
            "routes": [
                {"caller": "platform", "receiver": "companion", "purpose": "dialogue"},
                {"caller": "companion", "receiver": "memory", "purpose": "dialogue"},
            ],
        }
    return result


def input_request(ingest):
    return {
        "schema_version": 1,
        "request_id": "access:synthetic",
        "operation": "input",
        "ingest": ingest,
    }


def current_request(response=None, viewer=None):
    return {
        "schema_version": 1,
        "request_id": "current:synthetic",
        "operation": "current",
        "admissions": [o["admission"] for o in response["outcomes"] if o["admission"]]
        if response
        else [],
        "viewer": viewer,
    }


def register(platform, now, audience="group", targets=None, data=None):
    from fixtures import bearer

    ingest = copy.deepcopy(SOURCE_DOCS[audience + "/request"])
    if data is not None:
        ingest["input"] = copy.deepcopy(data)
    if targets is not None:
        ingest["target_actor_ids"] = targets
    ref = platform.sources.register_input(bearer("CONNECTOR"), "input-entry", ingest["input"])
    ingest["command"]["origin"] = {"assertion_ref": ref["assertion_ref"]}
    ingest["command"]["deadline_at"] = utc(now + 30)
    return ingest


def core_fixture(ingest, authority, now, audience="group", previous=None):
    """Not Core execution: synthesize published-shaped responses for Platform tests."""
    response = copy.deepcopy(SOURCE_DOCS[audience + "/response"])
    response.update(
        request_id=ingest["command"]["request_id"],
        request_digest=digest(ingest),
        routing_version=authority["routing_version"],
    )
    effective = (
        []
        if ingest["input"]["kind"] == "retract"
        else sorted(ingest["target_actor_ids"] or authority["default_actor_ids"])
    )
    contexts = {c["allowed_scope"]["actor_id"]: c for c in authority["actor_contexts"]}
    response.update(
        effective_actor_ids=effective, routing_state="routed" if effective else "unrouted"
    )
    originals = {o["actor_id"]: o for o in response["outcomes"]}
    old = {o["actor_id"]: o for o in previous["outcomes"]} if previous else {}
    response["outcomes"] = []
    for actor in effective:
        if actor not in contexts:
            response["outcomes"].append(
                {"actor_id": actor, "state": "forbidden", "receipt": None, "admission": None}
            )
            continue
        outcome = copy.deepcopy(old.get(actor, originals[actor]))
        receipt, admission = outcome["receipt"], outcome["admission"]
        receipt["request_id"] = response["request_id"]
        if actor in old:
            outcome["state"] = "duplicate"
            receipt["deduplicated"] = True
        else:
            receipt["accepted_at"] = admission["accepted_at"] = utc(now)
            admission["accepted_origin"] = {"assertion_ref": contexts[actor]["assertion_ref"]}
            admission["source"]["message_key"] = copy.deepcopy(ingest["input"]["message_key"])
            admission["selector"]["key"] = {
                k: v for k, v in ingest["input"]["message_key"].items() if k != "revision"
            }
        response["outcomes"].append(outcome)
    if not any(o["receipt"] for o in response["outcomes"]):
        response["person_id"] = None
    return response

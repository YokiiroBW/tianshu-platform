"""Synthetic identities are deployment inputs; all origins go through the real issuer."""

import copy
import json
import os
import time
from pathlib import Path

from services.platform.contracts import utc

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = Path(os.environ["TS012_CONTRACT_DIR"])
DOCUMENTS = {
    item["id"]: item["document"]
    for item in json.loads((CONTRACT / "examples/documents.json").read_text(encoding="utf-8"))
}
NATIVE_CONTRACT = CONTRACT.parents[1] / "model-protocol/v1"
NATIVE_DOCUMENTS = {
    item["id"]: item["document"]
    for item in json.loads(
        (NATIVE_CONTRACT / "examples/documents.json").read_text(encoding="utf-8")
    )
}
# Native registrations are protocol-pinned, so the native provider keeps its own entry.
NATIVE_PROVIDER = "provider-native"
TOKENS = {
    name: "synthetic-ts012-" + name.lower() + "-credential-for-local-test"
    for name in (
        "ADMIN",
        "CONNECTOR",
        "COMPANION",
        "MEMORY",
        "MEMORY_RESOLVER",
        "COMPANION_RESOLVER",
        "WRONG_RESOLVER",
        "GATEWAY",
        "NATIVE",
        "READER",
        "UPSTREAM",
    )
}
ENV = {"TS012_" + key: value for key, value in TOKENS.items()}


def bearer(name):
    return "Bearer " + TOKENS[name]


def settings(directory, upstream="https://upstream.example.invalid/v1"):
    principals = {}

    def principal(name, service, actions, **extra):
        principals[name.lower()] = {
            "kind": "service",
            "service": service,
            "token_env": "TS012_" + name,
            "actions": actions,
            **extra,
        }

    principal(
        "ADMIN",
        "platform",
        [
            "origin.issue",
            "origin.revoke",
            "entry.revoke",
            "principal.revoke",
            "config.publish",
            "config.revoke",
            "config.view",
            "capability.read",
            "task.read",
        ],
        kind="operator",
        account={"namespace": "web", "immutable_account_id": "local-operator"},
        task_owners=["companion"],
    )
    principal("CONNECTOR", "nonebot", ["origin.issue", "mapping.prepare", "source.observe"])
    principal(
        "COMPANION",
        "companion",
        ["mapping.prepare", "mapping.confirm", "source.verify", "task.project"],
    )
    principal("MEMORY", "memory", ["mapping.confirm"])
    principal(
        "MEMORY_RESOLVER",
        "memory",
        ["origin.resolve"],
        resolver={"caller": "companion", "purpose": "dialogue"},
    )
    principal(
        "COMPANION_RESOLVER",
        "companion",
        ["origin.resolve"],
        resolver={"caller": "nonebot", "purpose": "dialogue"},
    )
    principal(
        "WRONG_RESOLVER",
        "memory",
        ["origin.resolve"],
        resolver={"caller": "nonebot", "purpose": "dialogue"},
    )
    principal("GATEWAY", "gateway", ["config.snapshot"], config_versions=[6, 7, 8, 9])
    principal("NATIVE", "gateway", ["config.snapshot"], native_config_versions=[7, 8])
    principal("READER", "reader", ["capability.read", "task.read"])
    channel = copy.deepcopy(DOCUMENTS["ingest"]["message_key"]["channel"])
    account = copy.deepcopy(DOCUMENTS["ingest"]["author"])
    entries = {
        "chat-entry": {
            "kind": "rehearsal_connector",
            "owner": "connector",
            "account": account,
            "channel": channel,
            "actor_id": "actor-fixture",
            "audience": "group",
            "ttl_seconds": 60,
            "routes": [
                {"caller": "nonebot", "receiver": "companion", "purpose": "dialogue"},
                {"caller": "companion", "receiver": "memory", "purpose": "dialogue"},
            ],
        },
        "config-entry": {
            "kind": "local_operator",
            "owner": "admin",
            "account": principals["admin"]["account"],
            "channel": {
                "namespace": "web",
                "binding_id": "local-management",
                "channel_conversation_id": "config-console",
                "thread_id": None,
            },
            "actor_id": "actor-config",
            "audience": "self_private",
            "ttl_seconds": 60,
            "routes": [{"caller": "gateway", "receiver": "platform", "purpose": "config.snapshot"}],
        },
    }
    provider = copy.deepcopy(DOCUMENTS["config"]["providers"][0])
    registration = {
        k: provider[k]
        for k in (
            "credential_ref",
            "credential_namespace",
            "capability_verification",
            "verified_capabilities",
        )
    }
    registration.update(
        base_url=upstream,
        model_ids=[provider["model_id"]],
        reviewed_addresses=["127.0.0.1"] if upstream.startswith("http://") else ["192.0.2.10"],
        allow_private_http=upstream.startswith("http://"),
    )
    native = native_document()
    native_registration = {
        k: native["providers"][0][k]
        for k in (
            "protocol",
            "credential_ref",
            "credential_namespace",
            "capability_verification",
            "verified_capabilities",
        )
    }
    native_registration.update(
        base_url=upstream,
        model_ids=[native["providers"][0]["model_id"]],
        reviewed_addresses=["127.0.0.1"] if upstream.startswith("http://") else ["192.0.2.10"],
        allow_private_http=upstream.startswith("http://"),
    )
    return {
        "mode": "local_rehearsal",
        "storage": "sqlite_local",
        "database_path": str(Path(directory) / "platform.sqlite"),
        "contract_directory": str(CONTRACT),
        "principals": principals,
        "entries": entries,
        "providers": {
            provider["provider_id"]: registration,
            NATIVE_PROVIDER: native_registration,
        },
    }


def native_document():
    """The published model-protocol/v1 config_response example, with a test provider id."""
    result = copy.deepcopy(NATIVE_DOCUMENTS["config_response"])
    result["providers"][0]["provider_id"] = NATIVE_PROVIDER
    result["bindings"][0]["provider_id"] = NATIVE_PROVIDER
    return result


def config(now=None, upstream=None, version=7):
    now = time.time() if now is None else now
    result = copy.deepcopy(DOCUMENTS["config"])
    result.update(config_version=version, published_at=utc(now - 1), usable_until=utc(now + 300))
    if upstream:
        result["providers"][0]["base_url"] = upstream
    return result


def native_config(now=None, upstream=None, version=7, request_id="native-publish-test"):
    now = time.time() if now is None else now
    result = native_document()
    result.update(
        request_id=request_id,
        native_config_version=version,
        published_at=utc(now - 1),
        usable_until=utc(now + 300),
    )
    if upstream:
        result["providers"][0]["base_url"] = upstream
    return result


def query(ref, version=7):
    return {
        "query": {
            "schema_version": 1,
            "request_id": "snapshot-test",
            "origin": {"assertion_ref": ref},
        },
        "config_version": version,
    }


def native_query(ref, version=7):
    return {
        "query": {
            "schema_version": 1,
            "request_id": "native-snapshot-test",
            "origin": {"assertion_ref": ref},
        },
        "native_config_version": version,
        "contract": "model-protocol/v1",
    }


def resolve(ref):
    return {"schema_version": 1, "request_id": "resolve-test", "assertion_ref": ref}


def mapping_request(kind, ref, now):
    result = copy.deepcopy(DOCUMENTS["register" if kind == "identity" else "ingest"])
    result["command"]["origin"]["assertion_ref"] = ref
    result["command"]["deadline_at"] = utc(now + 30)
    return result


def complete_mapping(platform, ref, now):
    for kind, requester, responder, sample in (
        ("identity", "COMPANION", "MEMORY", "registered"),
        ("ingest", "CONNECTOR", "COMPANION", "ingest_receipt"),
    ):
        ticket = platform.origins.prepare_mapping(
            bearer(requester), kind, mapping_request(kind, ref, now)
        )
        platform.origins.confirm_mapping(
            bearer(responder), ticket, copy.deepcopy(DOCUMENTS[sample])
        )


async def start_http(app):
    from aiohttp import web

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, "http://127.0.0.1:" + str(runner.addresses[0][1])
